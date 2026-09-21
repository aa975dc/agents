"""流式普查 → SQLite 索引：分批落库与 generation 管理（stdlib only, Py3.9+）。

generation 语义（03_CAPACITY_AND_INDEXING.md §4/§6）：
- files 表是"已发布快照"：path UNIQUE，每行 generation = 写入它的世代；只在
  世代完成时的单事务里换装，因此**扫描进行中/中断时旧代始终完整可读**。
- 新扫描 = 世代 max+1：开始时先把 manifest 里仍 active 的半代标记 abandoned
  （含其 staging 残留，作废重建），随后逐批把观察写入 files_staging
  （PK (generation, path)，批批提交——内存有界、进度可查、崩了不污染旧代）。
- 完成事务（原子）：staging → files upsert；DELETE 旧世代残留行（即源树已删除
  的路径的最后一行）；清本代 staging；manifest 置 complete + file_count。
- 变更候选检测：同 path 的 size+mtime_ns 与已发布行一致 → 原样沿用旧 sha256
  （不重哈希）；否则按 hash_hook 计算，hook 缺省为 None → sha256 留 NULL（待哈希，
  内容哈希增量归 P3-02）。mtime/size 只是候选优化非真实性证明（同 ns 同大小改内容
  需 P3-02 校验，03 §6）。
- 写路径复用 storage/db.py：打开方式、<db>.writer 锁与 epoch 单写者——每批事务
  require_epoch 重校验，旧写者无法覆盖新写者。
"""
from pathlib import Path
from typing import NamedTuple

from agents_kernel.filesystem import walk
from agents_kernel.storage.db import require_epoch, utcnow
from agents_kernel.validation import CompanionError

DEFAULT_BATCH_SIZE = 5000

_INDEX_DDL = (
    """CREATE TABLE IF NOT EXISTS files (
        path TEXT NOT NULL UNIQUE,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        kind TEXT NOT NULL,
        sha256 TEXT,
        generation INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS files_staging (
        generation INTEGER NOT NULL,
        path TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        kind TEXT NOT NULL,
        sha256 TEXT,
        PRIMARY KEY (generation, path))""",
    """CREATE TABLE IF NOT EXISTS files_excluded (
        generation INTEGER NOT NULL,
        path TEXT NOT NULL,
        reason TEXT NOT NULL,
        PRIMARY KEY (generation, path))""",
    """CREATE TABLE IF NOT EXISTS scan_generations (
        generation INTEGER PRIMARY KEY,
        root TEXT NOT NULL,
        mode TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        file_count INTEGER)""",
)

_STAGING_UPSERT = """INSERT OR IGNORE INTO files_staging
    (generation, path, size, mtime_ns, kind, sha256) VALUES (?, ?, ?, ?, ?, ?)"""
_EXCLUDED_UPSERT = """INSERT OR IGNORE INTO files_excluded
    (generation, path, reason) VALUES (?, ?, ?)"""


class ScanResult(NamedTuple):
    generation: int
    mode: str                 # "git" | "plain"
    file_count: int           # 完成后 files 表中本代行数
    excluded_count: int       # 本代排除账条数
    batch_count: int          # 已提交的批次数
    hash_reused: int          # size+mtime 未变而沿用旧 sha256 的条数
    hash_computed: int        # 实际调用 hash_hook 的条数（缺省 hook 恒为 0）


class IndexScanner:
    """把 walk 枚举流分批写入索引；一个 scanner 可服务多次 scan（每次一个新代）。"""

    def __init__(self, store, batch_size=DEFAULT_BATCH_SIZE):
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise CompanionError("batch_size 必须为正整数")
        self.store = store
        self.batch_size = batch_size

    def scan(self, root, writer, *, extra_excluded=(), hash_hook=None, entries=None):
        """执行一轮完整普查。

        writer：db.acquire_writer 颁发的会话，每批事务携带其 epoch 重校验。
        extra_excluded：额外排除的路径段名集合（与 kernel 策略并集）。
        hash_hook：callable(绝对路径) -> sha256hex | None；缺省 None 不做任何
        内容读取（零 open 保证）。
        entries：测试注入口——显式提供枚举流时替代 walk_tree（中断注入用）；
        生产调用方不要传。
        """
        root = Path(root).absolute()
        mode = walk.detect_mode(root)
        counts = {"files": 0, "excluded": 0, "batches": 0, "reused": 0, "hashed": 0}
        generation, buffers = self._open_generation(root, mode, writer)
        stream = entries if entries is not None else walk.walk_tree(
            root, extra_excluded=extra_excluded, mode=mode)
        for item in stream:
            if isinstance(item, walk.FileEntry):
                buffers["files"].append(
                    self._observe(root, generation, item, hash_hook, counts))
            elif isinstance(item, walk.ExcludedEntry):
                buffers["excluded"].append((generation, item.path, item.reason))
                counts["excluded"] += 1
            else:
                raise CompanionError("未知枚举记录类型：%r" % (item,))
            if (len(buffers["files"]) >= self.batch_size
                    or len(buffers["excluded"]) >= self.batch_size):
                self._flush(buffers, writer, counts)
        self._flush(buffers, writer, counts)
        # 异常中断时半代留在 staging 与 manifest（status=active），files 未动；
        # 不在此处标记作废——硬杀进程同样走不进 finally，作废统一由下次扫描的
        # manifest 驱动（崩溃安全）。
        return self._publish(generation, writer, counts, mode)

    # ---- 内部：世代登记与批写 ----

    def _open_generation(self, root, mode, writer):
        """登记新代：作废遗留 active 半代并清其 staging，返回 (generation, 空缓冲)。"""
        with self.store.transaction() as conn:
            require_epoch(conn, writer.epoch)
            for statement in _INDEX_DDL:
                conn.execute(statement)
            row = conn.execute("SELECT MAX(generation) AS g FROM scan_generations").fetchone()
            generation = (row["g"] or 0) + 1
            conn.execute(
                """UPDATE scan_generations SET status = 'abandoned', completed_at = ?
                   WHERE status = 'active'""", (utcnow(),))
            conn.execute(
                """DELETE FROM files_staging WHERE generation IN (
                       SELECT generation FROM scan_generations WHERE status = 'abandoned')""")
            conn.execute(
                """INSERT INTO scan_generations
                   (generation, root, mode, status, started_at) VALUES (?, ?, ?, 'active', ?)""",
                (generation, str(root), mode, utcnow()))
        return generation, {"files": [], "excluded": []}

    def _observe(self, root, generation, item, hash_hook, counts):
        """单文件观察 → staging 行；size+mtime 未变沿用旧 sha256（不重哈希）。"""
        sha256 = None
        previous = self.store.query_one(
            "SELECT size, mtime_ns, sha256 FROM files WHERE path = ?", (item.path,))
        if (previous is not None and previous["size"] == item.size
                and previous["mtime_ns"] == item.mtime_ns):
            sha256 = previous["sha256"]
            counts["reused"] += 1
        elif hash_hook is not None:
            sha256 = hash_hook(str(root / item.path))
            counts["hashed"] += 1
        return (generation, item.path, item.size, item.mtime_ns, "file", sha256)

    def _flush(self, buffers, writer, counts):
        if not buffers["files"] and not buffers["excluded"]:
            return
        with self.store.transaction() as conn:
            require_epoch(conn, writer.epoch)
            conn.executemany(_STAGING_UPSERT, buffers["files"])
            conn.executemany(_EXCLUDED_UPSERT, buffers["excluded"])
        buffers["files"].clear()
        buffers["excluded"].clear()
        counts["batches"] += 1

    def _publish(self, generation, writer, counts, mode):
        """完成事务：staging→files 换装 + 旧代残行清除 + manifest 收口（原子）。"""
        with self.store.transaction() as conn:
            require_epoch(conn, writer.epoch)
            conn.execute(
                """INSERT INTO files (path, size, mtime_ns, kind, sha256, generation)
                   SELECT path, size, mtime_ns, kind, sha256, generation
                   FROM files_staging WHERE generation = ?
                   ON CONFLICT(path) DO UPDATE SET
                       size = excluded.size, mtime_ns = excluded.mtime_ns,
                       kind = excluded.kind, sha256 = excluded.sha256,
                       generation = excluded.generation""", (generation,))
            conn.execute("DELETE FROM files WHERE generation < ?", (generation,))
            conn.execute("DELETE FROM files_staging WHERE generation = ?", (generation,))
            file_count = conn.execute(
                "SELECT COUNT(*) AS c FROM files WHERE generation = ?",
                (generation,)).fetchone()["c"]
            conn.execute(
                """UPDATE scan_generations SET status = 'complete', completed_at = ?,
                   file_count = ? WHERE generation = ?""",
                (utcnow(), file_count, generation))
        return ScanResult(generation=generation, mode=mode, file_count=file_count,
                          excluded_count=counts["excluded"], batch_count=counts["batches"],
                          hash_reused=counts["reused"], hash_computed=counts["hashed"])

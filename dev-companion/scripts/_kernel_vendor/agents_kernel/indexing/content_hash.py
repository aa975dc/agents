"""内容哈希：消费 P3-01 预留的 hash_hook 接口（stdlib only, Py3.9+）。

03_CAPACITY_AND_INDEXING.md §5：大文件内容 hash 用固定缓冲区分块流式读取，
严禁 read_bytes 一次载入——本模块复用 kernel.digest 的 1MiB 口径（READ_SIZE）。

两个入口：
- ContentHasher：可直接作为 IndexScanner.scan(hash_hook=...) 传入的 callable，
  对"待哈希"条目逐个分块计算 sha256；文件消失/不可读返回 None（保持待哈希，
  不让单文件 IO 错误中断整轮普查）。
- backfill_hashes：对已发布快照中 sha256 IS NULL 的"待哈希"条目批量补算并
  回写 files 表（每批一个 epoch 校验事务，内存有界）。回写前用行内
  size+mtime_ns 与当前 lstat 核对——快照已过期（扫描后文件又变了）则跳过，
  留给下一轮扫描重哈希，不用新内容冒充旧快照的哈希（03 §6：mtime/size 是
  候选优化而非真实性证明）。
"""
from pathlib import Path
from typing import NamedTuple

from agents_kernel import digest
from agents_kernel.storage.db import require_epoch
from agents_kernel.validation import CompanionError

DEFAULT_BATCH_SIZE = 5000

_PENDING_PAGE = """SELECT path, size, mtime_ns FROM files
    WHERE generation = ? AND sha256 IS NULL AND path > ? ORDER BY path LIMIT ?"""


class BackfillResult(NamedTuple):
    generation: int
    hashed: int        # 已回写哈希的条数
    skipped: int       # 快照过期（size/mtime 变化）或文件不可读而跳过的条数
    batches: int       # 已提交的回写批次数


class ContentHasher:
    """分块流式 sha256 的 hash_hook 实现；不整读文件（内存有界）。"""

    def __init__(self, read_size=digest.READ_SIZE):
        if not isinstance(read_size, int) or isinstance(read_size, bool) or read_size < 1:
            raise CompanionError("read_size 必须为正整数")
        self.read_size = read_size

    def __call__(self, absolute_path):
        """返回 (absolute_path) 的 sha256 hex；文件消失/不可读 → None（待哈希）。"""
        try:
            sha256, _ = digest.sha256_file(absolute_path, self.read_size)
        except OSError:
            return None
        return sha256


def backfill_hashes(store, writer, generation=None, hasher=None,
                    batch_size=DEFAULT_BATCH_SIZE):
    """对已发布快照中 sha256 IS NULL 的条目补算哈希并批量回写 files 表。

    generation 缺省取最近 complete 世代（files 表只保留该世代的行）。
    """
    if hasher is None:
        hasher = ContentHasher()
    if generation is None:
        generation = _latest_complete(store)
    elif generation != _latest_complete(store):
        raise CompanionError(
            "files 表只保留最近完整世代，不能回填旧代 %r" % (generation,))
    root = _generation_root(store, generation)
    counts = {"hashed": 0, "skipped": 0, "batches": 0}
    while True:
        # keyset 游标推进：UPDATE 不改 path，游标安全。
        after = counts.get("_after", "")
        rows = store.query_all(_PENDING_PAGE, (generation, after, batch_size))
        if not rows:
            break
        updates = []
        for row in rows:
            sha256 = _hash_if_fresh(root, row, hasher)
            if sha256 is None:
                counts["skipped"] += 1
            else:
                updates.append((sha256, row["path"]))
                counts["hashed"] += 1
        if updates:
            with store.transaction() as conn:
                require_epoch(conn, writer.epoch)
                conn.executemany(
                    "UPDATE files SET sha256 = ? WHERE path = ?", updates)
        counts["batches"] += 1
        counts["_after"] = rows[-1]["path"]
        if len(rows) < batch_size:
            break
    counts.pop("_after", None)
    return BackfillResult(generation=generation, hashed=counts["hashed"],
                          skipped=counts["skipped"], batches=counts["batches"])


def _hash_if_fresh(root, row, hasher):
    """快照仍新鲜才计算哈希；过期/不可读一律跳过（返回 None）。"""
    absolute = Path(root) / row["path"]
    try:
        stat = absolute.stat()
    except OSError:
        return None
    if stat.st_size != row["size"] or stat.st_mtime_ns != row["mtime_ns"]:
        return None  # 扫描后文件已变：此行描述的是旧快照，留给下轮扫描
    try:
        sha256, _ = digest.sha256_file(str(absolute), hasher.read_size)
    except OSError:
        return None
    return sha256


def _latest_complete(store):
    row = store.query_one(
        "SELECT MAX(generation) AS g FROM scan_generations WHERE status = 'complete'")
    if row is None or row["g"] is None:
        raise CompanionError("索引中尚无完整世代，无法回填内容哈希")
    return int(row["g"])


def _generation_root(store, generation):
    row = store.query_one(
        "SELECT root FROM scan_generations WHERE generation = ?", (generation,))
    if row is None:
        raise CompanionError("世代不存在：%r" % (generation,))
    return row["root"]

"""XXL 跨片协调与分页查询（C15 后半/SC03 实现半/CV05 尾/SEC03 前半；stdlib only, Py3.9+）。

在 sharding（P6-01 分片写/跨片图/generation vector）之上的只读协调层。
03_CAPACITY_AND_INDEXING.md 对 XXL（千万索引行）的核心要求是"任何查询有界"：

- ShardRootManifest：分片根清单（shard_id/库文件/行数/generation/sha256 指纹）。
  capture() 在 P6-01 完整性校验（漏片/清单外/行数核对）之上补齐各片库文件指纹；
  load() 读时单边复核：库文件缺失（漏片）、sha256 不符（坏 sha）、物化行数与
  清单不符、清单外分片库，任一不符即拒绝——绝不带病返回部分结果。
- MergedPageReader：跨片 keyset 分页。全局排序键 = (shard_id, path)；全局游标
  绑定 generation vector（§10"游标绑定 generation 与排序键"）——任一分片在
  翻页期间世代推进即拒绝续页，不同 generation 不拼"完整当前结果"（§6）。
  每片各自 keyset 推进，单页 ≤ page_size；内存 O(page_size + 分片数)：缓冲区
  只在补足"本页缺口"时才取（peak_live_rows 计数器可观测），因此查询成本随
  分片数而非总行数增长——每页每片至多一次 keyset 查询 + 一次世代核对，千万行
  下翻一页不触碰未涉及分片的任何行。
- 聚合查询：count_by_shard/count_total 读各片物化 file_count（manifest 口径，
  不做全表 COUNT 扫描，§10）；count_by_module 用 SQL GROUP BY 仅在各片聚合、
  不拉行到进程（模块口径 = 缺省规则：首段目录/_root；自定义 mapper rules 不
  支持——如实边界，需要时由各片 modules 表另行聚合）。

SEC03 前半（远端边界）：SHARD_LOCATION 仅支持本地文件路径。含 remote:// 或
http(s):// 前缀的位置一律结构化拒绝（RemoteShardLocation，code=remote_disabled）
——远端协议未实现且默认禁用（§8），本模块不提供占位 stub 冒充支持。

已知边界：XXL 千万行物理实测归 P6-05（NOT_RUN），本层交付的是小样本正确性与
资源有界的实现及证明；sha256 指纹覆盖主库文件字节（ShardWriter 收尾已关闭连接、
WAL 已并回主库；外部进程持有连接遗留的未并回页不在指纹内——已知边界）。
"""
import hashlib
import json
from pathlib import Path
from typing import NamedTuple

from agents_kernel.indexing.modules import DEFAULT_ROOT_MODULE
from agents_kernel.indexing.reader import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from agents_kernel.indexing.sharding import SHARD_PREFIX, CrossShardGraph, ShardRegistry
from agents_kernel.storage import db
from agents_kernel.validation import CompanionError

MANIFEST_VERSION = 1
HASH_CHUNK = 1 << 20            # §5：文件指纹按固定缓冲区分块读，严禁整读

# files 是"已发布单快照"（只留最新 complete 世代行），无需世代过滤；
# 世代由 MergedPageReader 显式读取并绑定进游标。
_FILES_PAGE = ("SELECT path, size, mtime_ns, kind, sha256, generation"
               " FROM files WHERE path > ? ORDER BY path LIMIT ?")
_COMPLETE_GEN = ("SELECT MAX(generation) AS g FROM scan_generations"
                 " WHERE status = 'complete'")
# 模块归属的 SQL 口径（缺省规则）：首段目录；不含 / 的顶层散文件归 _root。
_MODULE_GROUP = ("SELECT CASE WHEN instr(path, '/') = 0 THEN ?"
                 " ELSE substr(path, 1, instr(path, '/') - 1) END AS module_id,"
                 " COUNT(*) AS c FROM files GROUP BY module_id")


class RemoteShardLocation(CompanionError):
    """SEC03：远端分片位置被结构化拒绝（远端协议未实现且默认禁用）。"""

    def __init__(self, location):
        super().__init__(
            "[remote_disabled] 分片位置 %r 为远端协议：远端协议未实现且默认禁用，"
            "SHARD_LOCATION 仅支持本地文件路径" % (location,))
        self.location = location
        self.code = "remote_disabled"


def require_local_shard_location(location):
    """校验分片位置为本地路径；remote:// 或 http(s):// 前缀结构化拒绝（SEC03）。"""
    if not isinstance(location, (str, Path)):
        raise CompanionError("分片位置必须是本地路径字符串或 Path：%r" % (location,))
    text = str(location)
    lowered = text.lower()
    if "remote://" in lowered or lowered.startswith(("http://", "https://")):
        raise RemoteShardLocation(text)
    return Path(text)


def _file_sha256(path):
    """库文件字节指纹：固定缓冲区流式读取（大库也不整读入内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _latest_complete_generation(store, shard_id):
    row = store.query_one(_COMPLETE_GEN)
    if row is None or row["g"] is None:
        raise CompanionError("分片 %s 无完整世代" % shard_id)
    return int(row["g"])


def _materialized_file_count(store, shard_id):
    """物化行数（scan_generations.file_count，manifest 口径），非 COUNT 扫描。"""
    store_row = store.query_one(
        """SELECT file_count FROM scan_generations
           WHERE generation = (SELECT MAX(generation) FROM scan_generations
                               WHERE status = 'complete')""")
    if store_row is None or store_row["file_count"] is None:
        raise CompanionError("分片 %s 无完整世代" % shard_id)
    return int(store_row["file_count"])


class ShardEntry(NamedTuple):
    """根清单中的一个分片项：标识、物化行数、世代与库文件 sha256 指纹。"""
    shard_id: str
    db: str                 # 库文件名（相对分片目录）
    file_count: int
    generation: int
    sha256: str


class ShardRootManifest:
    """分片根清单：capture（建账）与 load（读时单边完整性复核）。

    直接构造 = 纯数据持有（verified=False，overview 如实标 unverified）；
    capture()/load() 才做完整性校验并置 verified=True。
    """

    def __init__(self, shard_dir, entries, verified=False):
        self.shard_dir = Path(shard_dir)
        checked, seen = [], set()
        for entry in entries:
            entry = self._check_entry(entry)
            if entry.shard_id in seen:
                raise CompanionError("分片 %s 在清单中重复（重片拒绝）" % entry.shard_id)
            seen.add(entry.shard_id)
            checked.append(entry)
        self._entries = tuple(sorted(checked, key=lambda entry: entry.shard_id))
        self.verified = bool(verified)

    @staticmethod
    def _check_entry(entry):
        if not isinstance(entry, ShardEntry):
            raise CompanionError("清单项必须是 ShardEntry：%r" % (entry,))
        digest = entry.sha256
        if (not isinstance(digest, str) or len(digest) != 64
                or any(letter not in "0123456789abcdef" for letter in digest)):
            raise CompanionError("分片 %s 的 sha256 指纹非法" % entry.shard_id)
        if not isinstance(entry.db, str) or not entry.db.endswith(".sqlite"):
            raise CompanionError("分片 %s 的库文件名非法" % entry.shard_id)
        for name, value in (("file_count", entry.file_count),
                            ("generation", entry.generation)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise CompanionError("分片 %s 的 %s 必须为非负整数" % (entry.shard_id, name))
        return entry

    # ---- 建账与读时校验 ----

    @classmethod
    def capture(cls, shard_dir):
        """建账：P6-01 完整性校验（漏片/清单外/行数）+ 各片世代/物化行数/指纹。"""
        location = require_local_shard_location(shard_dir)
        registry = ShardRegistry.load(location)
        graph = CrossShardGraph(registry)
        try:
            graph.verify_integrity()        # 漏片/清单外/行数核对（P6-01 拒绝语义）
            rows = [(shard_id,
                     _latest_complete_generation(graph.open_shard(shard_id), shard_id),
                     _materialized_file_count(graph.open_shard(shard_id), shard_id))
                    for shard_id in sorted(registry.shard_ids())]
        finally:
            graph.close()                   # 先并回 WAL 再取指纹，保证指纹覆盖全部页
        entries = [ShardEntry(shard_id=shard_id, db=shard_id + ".sqlite",
                              file_count=file_count, generation=generation,
                              sha256=_file_sha256(registry.db_path(shard_id)))
                   for shard_id, generation, file_count in rows]
        return cls(location, entries, verified=True)

    @classmethod
    def load(cls, path, shard_dir=None):
        """读时单边完整性复核：漏片/坏 sha/行数/清单外任一不符即拒绝。"""
        location = require_local_shard_location(path)
        if not location.is_file():
            raise CompanionError("根清单快照不存在：%s" % location)
        try:
            document = json.loads(location.read_text(encoding="utf-8"))
        except ValueError as error:
            raise CompanionError("根清单快照损坏：%s" % error)
        if not isinstance(document, dict) or document.get("version") != MANIFEST_VERSION:
            raise CompanionError("根清单快照版本不支持：%r" % (document.get("version"),))
        base = require_local_shard_location(shard_dir if shard_dir is not None
                                            else document.get("dir"))
        entries = [cls._check_entry(ShardEntry(*row)) if isinstance(row, (list, tuple))
                   else cls._check_entry(ShardEntry(**row))
                   for row in document.get("entries") or []]
        manifest = cls(base, entries, verified=True)
        manifest._verify_against_disk()
        return manifest

    def _verify_against_disk(self):
        """逐片核对：清单外库 → 缺失（漏片）→ 坏 sha → 物化行数不符。"""
        present = {path.name for path in self.shard_dir.glob(SHARD_PREFIX + "*.sqlite")}
        extra = sorted(present - {entry.db for entry in self._entries})
        if extra:
            raise CompanionError("存在清单外分片库（重片/孤儿拒绝）：%s" % ", ".join(extra))
        stores = []
        try:
            for entry in self._entries:
                db_path = self.shard_dir / entry.db
                if not db_path.is_file():
                    raise CompanionError("分片库缺失（漏片拒绝）：%s" % db_path)
                if _file_sha256(db_path) != entry.sha256:
                    raise CompanionError(
                        "分片 %s 库文件 sha256 与清单不符（坏 sha 拒绝）" % entry.shard_id)
                store = db.Store(db_path)
                store.open()
                stores.append(store)
                actual = _materialized_file_count(store, entry.shard_id)
                if actual != entry.file_count:
                    raise CompanionError(
                        "分片 %s 行数核对失败：实际 %d != 清单 %d"
                        % (entry.shard_id, actual, entry.file_count))
        finally:
            for store in stores:
                store.close()

    def save(self, path):
        """落盘快照（JSON，确定性序列化）；目标位置同样仅支持本地路径。"""
        location = require_local_shard_location(path)
        location.write_text(
            json.dumps(self.document(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        return location

    # ---- 只读面（services.summary 等下游按此接口消费） ----

    def shard_entries(self):
        return self._entries

    def total_files(self):
        return sum(entry.file_count for entry in self._entries)

    def generation_vector(self):
        return {entry.shard_id: entry.generation for entry in self._entries}

    def document(self):
        return {"version": MANIFEST_VERSION, "dir": str(self.shard_dir),
                "entries": [entry._asdict() for entry in self._entries],
                "generation_vector": self.generation_vector(),
                "total_files": self.total_files()}


class MergedPage(NamedTuple):
    """跨片有序页：全局排序键 (shard_id, path)；cursor=None 即末页。"""
    rows: list
    cursor: object          # {shard_id, path, generations} 或 None
    generation_vector: dict  # 本页数据绑定的分片世代向量


class _ShardState:
    """一个分片的翻页运行态：连接、世代、本地 keyset 与待消费缓冲。"""

    __slots__ = ("shard_id", "store", "generation", "keyset", "buffer", "offset",
                 "exhausted")

    def __init__(self, shard_id, store, generation):
        self.shard_id = shard_id
        self.store = store
        self.generation = generation
        self.keyset = ""        # 本地 keyset（片内 path 游标）
        self.buffer = []        # 最近一次取回、尚未交给调用方的行
        self.offset = 0
        self.exhausted = False


class MergedPageReader:
    """跨片 keyset 分页与聚合的协调查询面（任何查询有界：O(page_size + 分片数)）。

    成本模型（XXL 的关键）：每页对每个分片至多一次 keyset 查询 + 一次世代核对，
    与总行数无关；连接数为分片数（懒开、常驻，close() 释放）。
    """

    def __init__(self, registry, page_size=DEFAULT_PAGE_SIZE):
        self.registry = registry
        self.page_size = _check_page_size(page_size)
        self._order = sorted(registry.shard_ids())
        self._states = {}
        self._bound = None      # 本次翻页绑定的世代向量
        self._started = False
        self._last_cursor = None
        self._cursor_used = False
        self._peak = 0

    @classmethod
    def load(cls, shard_dir, page_size=DEFAULT_PAGE_SIZE):
        return cls(ShardRegistry.load(require_local_shard_location(shard_dir)),
                   page_size)

    def close(self):
        for state in self._states.values():
            state.store.close()
        self._states = {}
        self._bound = None

    # ---- 分页 ----

    def page(self, after=None):
        """取下一页：after=上次返回的 cursor（或 None 起始）；每页 ≤ page_size。"""
        vector = self._bind(after)
        if not self._started:
            if after is not None:
                self._apply_resume(after)
            self._started = True
        self._cursor_used = True
        page = []
        for shard_id in self._order:
            if len(page) >= self.page_size:
                break
            state = self._states[shard_id]
            while len(page) < self.page_size:
                while (state.offset < len(state.buffer)
                       and len(page) < self.page_size):
                    row = state.buffer[state.offset]
                    state.offset += 1
                    state.keyset = row["path"]
                    page.append(_merged_row(state.shard_id, row))
                if len(page) >= self.page_size or state.exhausted:
                    break
                remaining = self.page_size - len(page)
                rows = state.store.query_all(_FILES_PAGE, (state.keyset, remaining))
                if len(rows) < remaining:
                    state.exhausted = True
                if rows:
                    state.buffer, state.offset = rows, 0
                    self._note_peak(len(page))
                else:
                    break
        more = any(state.offset < len(state.buffer) or not state.exhausted
                   for state in self._states.values())
        cursor = None
        if page and more:
            cursor = {"shard_id": page[-1]["shard_id"], "path": page[-1]["path"],
                      "generations": dict(vector)}
        self._last_cursor = cursor if cursor is not None else after
        self._cursor_used = False
        return MergedPage(rows=page, cursor=cursor, generation_vector=dict(vector))

    def iter_rows(self):
        """按页流式产出全部行；逐页消费，无全量物化。"""
        after = None
        while True:
            page = self.page(after)
            for row in page.rows:
                yield row
            if page.cursor is None:
                return
            after = page.cursor

    def peak_live_rows(self):
        """已取回未交付行数的峰值（内存有界的可观测计数器）。"""
        return self._peak

    # ---- 聚合（仅各片聚合，不拉行） ----

    def count_by_shard(self):
        """{shard_id: 行数}：读各片物化 file_count（manifest 口径，非 COUNT 扫描）。"""
        return {shard_id: _materialized_file_count(self._open(shard_id)[0].store,
                                                   shard_id)
                for shard_id in self._order}

    def count_total(self):
        return sum(self.count_by_shard().values())

    def count_by_module(self):
        """{module_id: 行数}：各片 SQL GROUP BY 聚合（缺省模块规则口径）。"""
        counts = {}
        for shard_id in self._order:
            store = self._open(shard_id)[0].store
            for row in store.query_all(_MODULE_GROUP, (DEFAULT_ROOT_MODULE,)):
                counts[row["module_id"]] = counts.get(row["module_id"], 0) + int(row["c"])
        return counts

    # ---- 内部 ----

    def _open(self, shard_id):
        """懒开分片库；缺失拒绝（不新建空库掩盖漏片）。返回 (state, generation)。"""
        state = self._states.get(shard_id)
        if state is not None:
            return state, state.generation
        path = self.registry.db_path(shard_id)
        if not path.is_file():
            raise CompanionError("分片库缺失：%s" % path)
        store = db.Store(path)
        store.open()
        generation = _latest_complete_generation(store, shard_id)
        state = _ShardState(shard_id, store, generation)
        self._states[shard_id] = state
        return state, generation

    def _bind(self, after):
        """绑定世代向量（§10 游标绑定 generation）：任一片世代推进即拒绝续页。"""
        if after is None:
            if self._started:
                raise CompanionError("读取器已开始翻页，请继续传上次返回的游标")
            if self._bound is None:
                self._bound = {shard_id: self._open(shard_id)[1]
                               for shard_id in self._order}
            return self._bound
        if not isinstance(after, dict) or set(after) != {"shard_id", "path",
                                                         "generations"}:
            raise CompanionError("分页游标结构非法（需要 shard_id/path/generations）")
        if after["shard_id"] not in self._order or not isinstance(after["path"], str):
            raise CompanionError("分页游标的排序键非法：%r" % (after["shard_id"],))
        vector = after["generations"]
        if not isinstance(vector, dict) or set(vector) != set(self._order):
            raise CompanionError("游标世代向量与分片清单不一致（清单已变更，拒绝续页）")
        if self._started and (after != self._last_cursor or self._cursor_used):
            raise CompanionError("分页游标与读取器进度不一致：游标只能按返回顺序使用一次")
        self._cursor_used = True
        for shard_id in self._order:
            generation = self._open(shard_id)[1]
            if generation != vector[shard_id]:
                raise CompanionError(
                    "分页游标已失效：分片 %s 世代 %d != 游标绑定 %r（分页期间分片已重扫，"
                    "不同 generation 不拼完整当前结果）" % (shard_id, generation,
                                                     vector[shard_id]))
        self._bound = dict(vector)
        return self._bound

    def _apply_resume(self, cursor):
        """从序列化游标恢复：cursor 之前的片视为已耗尽，cursor 片从其 path 之后继续。"""
        for shard_id in self._order:
            state = self._open(shard_id)[0]
            if shard_id < cursor["shard_id"]:
                state.exhausted = True
            elif shard_id == cursor["shard_id"]:
                state.keyset = cursor["path"]

    def _note_peak(self, page_len):
        buffered = sum(len(state.buffer) - state.offset
                       for state in self._states.values())
        self._peak = max(self._peak, buffered + page_len)


def _merged_row(shard_id, row):
    """分片行 → 带分片归属的普通字典（JSON 可序列化，页内有界）。"""
    return {"shard_id": shard_id, "path": row["path"], "size": int(row["size"]),
            "mtime_ns": int(row["mtime_ns"]), "kind": row["kind"],
            "sha256": row["sha256"], "generation": int(row["generation"])}


def _check_page_size(page_size):
    if (not isinstance(page_size, int) or isinstance(page_size, bool)
            or not 1 <= page_size <= MAX_PAGE_SIZE):
        raise CompanionError("page_size 必须在 1..%d" % MAX_PAGE_SIZE)
    return page_size

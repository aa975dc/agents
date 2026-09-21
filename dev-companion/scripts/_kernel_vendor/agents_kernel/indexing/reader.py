"""索引只读查询：keyset 分页，只读库、永不触碰源码树（stdlib only, Py3.9+）。

03_CAPACITY_AND_INDEXING.md §6/§10：
- 默认只呈现最近一个 complete 世代——不同 generation 不拼"完整当前结果"；
  扫描进行中/半代对查询方不可见（半代只在 staging，从未进 files）。
- 稳定 keyset/cursor 分页（WHERE path > ? ORDER BY path），不用巨量 OFFSET；
  游标绑定世代与排序键（世代由 page/iter 调用固定）。
- 计数来自 manifest 物化聚合（file_count），不做全表 COUNT 扫描。
- 本模块只执行 SELECT（走 storage.db 的 query_all/query_one），无文件 IO。
"""
from typing import NamedTuple

from agents_kernel.validation import CompanionError

DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 5000

_FILES_PAGE = """SELECT path, size, mtime_ns, kind, sha256, generation
    FROM files WHERE generation = ? AND path > ? ORDER BY path LIMIT ?"""
_EXCLUDED_PAGE = """SELECT path, reason, generation
    FROM files_excluded WHERE generation = ? AND path > ? ORDER BY path LIMIT ?"""


class Page(NamedTuple):
    rows: list
    cursor: object          # 下一页起点（最后一条的 path）；None = 已到末页
    generation: object      # 本页数据所属世代（None = 库中尚无 complete 世代）


class IndexReader:
    """files/files_excluded/scan_generations 的分页只读面。"""

    def __init__(self, store):
        self.store = store

    # ---- 世代与计数（manifest 物化） ----

    def _tables_ready(self):
        """从未扫描过的库还没有索引表；一律按"空索引"如实回答，不报错。"""
        return self.store.query_one(
            "SELECT 1 AS ok FROM sqlite_master WHERE type = 'table' AND name = 'scan_generations'"
        ) is not None

    def generations(self):
        """全部世代 manifest（含 active/abandoned），按世代号升序。"""
        if not self._tables_ready():
            return []
        return self.store.query_all(
            """SELECT generation, root, mode, status, started_at, completed_at, file_count
               FROM scan_generations ORDER BY generation""")

    def latest_complete(self):
        """最近一个 complete 世代的代号；库中尚无时返回 None。"""
        if not self._tables_ready():
            return None
        row = self.store.query_one(
            "SELECT MAX(generation) AS g FROM scan_generations WHERE status = 'complete'")
        return row["g"] if row is not None else None

    def count_files(self, generation=None):
        """已发布文件数；来自 manifest 物化 file_count，未指定世代取最近 complete。"""
        if not self._tables_ready():
            return 0
        gen = generation if generation is not None else self.latest_complete()
        if gen is None:
            return 0
        row = self.store.query_one(
            "SELECT file_count FROM scan_generations WHERE generation = ?", (gen,))
        return int(row["file_count"]) if row is not None and row["file_count"] is not None else 0

    # ---- files 分页 ----

    def page_files(self, after=None, limit=DEFAULT_PAGE_SIZE, generation=None):
        """files 表 keyset 翻页：after=上一页末行 path；返回 Page。

        generation 缺省锁定最近 complete 世代；显式传入即查该世代（历史快照）。
        """
        gen = self._resolve_generation(generation)
        if not self._tables_ready():
            return Page(rows=[], cursor=None, generation=gen)
        rows = self.store.query_all(_FILES_PAGE, (gen, after or "", self._limit(limit)))
        cursor = rows[-1]["path"] if len(rows) == self._limit(limit) else None
        return Page(rows=rows, cursor=cursor, generation=gen)

    def iter_files(self, limit=DEFAULT_PAGE_SIZE, generation=None):
        """按 keyset 翻页流式产出全部行；调用方逐条消费，无全量物化。"""
        gen = self._resolve_generation(generation)
        if not self._tables_ready():
            return
        after = ""
        while True:
            rows = self.store.query_all(_FILES_PAGE, (gen, after, self._limit(limit)))
            if not rows:
                return
            for row in rows:
                yield row
            if len(rows) < self._limit(limit):
                return
            after = rows[-1]["path"]

    # ---- 排除账分页 ----

    def page_excluded(self, after=None, limit=DEFAULT_PAGE_SIZE, generation=None):
        """排除账（{path, reason}）keyset 翻页；世代语义同 page_files。"""
        gen = self._resolve_generation(generation)
        if not self._tables_ready():
            return Page(rows=[], cursor=None, generation=gen)
        rows = self.store.query_all(_EXCLUDED_PAGE, (gen, after or "", self._limit(limit)))
        cursor = rows[-1]["path"] if len(rows) == self._limit(limit) else None
        return Page(rows=rows, cursor=cursor, generation=gen)

    # ---- 内部 ----

    def _resolve_generation(self, generation):
        if generation is not None:
            return generation
        gen = self.latest_complete()
        if gen is None:
            raise CompanionError("索引中尚无完整世代，无法查询")
        return gen

    @staticmethod
    def _limit(limit):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE:
            raise CompanionError("limit 必须在 1..%d" % MAX_PAGE_SIZE)
        return limit

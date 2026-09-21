"""分页汇总报告（C15 后半/SC03 实现半/CV06 尾；stdlib only, Py3.9+）。

ReportPaginator：总览页 + 每请求一页明细，**永不一次性返回全量列表**
（03_CAPACITY_AND_INDEXING.md §7"主协调者仅加载有限摘要"；§10 稳定 keyset/
cursor 分页）。响应大小只由 page_size 决定，与分片行数无关：

- overview()：固定键总览（分片数/总行数估计/generation vector/完整性状态），
  不含任何随行数增长的列表。"总行数估计"来自根清单物化计数（manifest 口径），
  如实以 estimate 命名；integrity 为 verified（manifest 经 capture/load 完整性
  校验）或 unverified（直接构造的清单对象）——不谎报。
- page_shards(after)：分片清单明细 keyset 翻页（游标 = shard_id），单页 ≤
  page_size 项；总返回项数恒等于清单真实条数。
- page_files(after, reader)：文件行明细，直接委托 MergedPageReader.page
  （跨片全局有序页，游标绑定 generation vector，见 coordination 模块文档）。

清单对象接口（鸭子类型，规范实现为 indexing.coordination.ShardRootManifest）：
shard_entries() / total_files() / generation_vector() / verified。
边界：本模块只组装与分页，不落盘、不做完整性校验（那是 manifest 的职责）；
XXL 千万行物理实测归 P6-05（NOT_RUN）。
"""
from agents_kernel.indexing.coordination import MAX_PAGE_SIZE
from agents_kernel.validation import CompanionError

DEFAULT_PAGE_SIZE = 50


class ReportPaginator:
    """总览 + 每请求一页明细的分页汇总报告（只组装，不做 IO 与校验）。"""

    def __init__(self, manifest, page_size=DEFAULT_PAGE_SIZE):
        if (not isinstance(page_size, int) or isinstance(page_size, bool)
                or not 1 <= page_size <= MAX_PAGE_SIZE):
            raise CompanionError("page_size 必须在 1..%d" % MAX_PAGE_SIZE)
        self.manifest = manifest
        self.page_size = page_size

    def overview(self):
        """总览页：固定键，大小 O(分片数)（generation vector 为汇总报告固有）。"""
        return {
            "shard_count": len(self.manifest.shard_entries()),
            "total_files_estimate": int(self.manifest.total_files()),
            "generation_vector": self.manifest.generation_vector(),
            "integrity": "verified" if self.manifest.verified else "unverified",
        }

    def page_shards(self, after=None):
        """分片明细页：after=上页末 shard_id；返回 {"items", "cursor"}。"""
        if after is not None and (not isinstance(after, str) or not after):
            raise CompanionError("page_shards 游标必须为非空 shard_id 字符串")
        items, cursor = [], None
        for entry in self.manifest.shard_entries():     # 已按 shard_id 排序
            if after is not None and entry.shard_id <= after:
                continue
            if len(items) >= self.page_size:
                cursor = items[-1]["shard_id"]
                break
            items.append(entry._asdict())
        return {"items": items, "cursor": cursor}

    def page_files(self, after=None, reader=None):
        """文件行明细页：委托跨片读取器；reader 缺省拒绝（不给隐式全量路径）。"""
        if reader is None:
            raise CompanionError(
                "page_files 需要 MergedPageReader（indexing.coordination）")
        return reader.page(after)

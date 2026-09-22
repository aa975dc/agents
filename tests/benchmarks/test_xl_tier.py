# -*- coding: utf-8 -*-
"""XL 档（目标 100 万当前代 distinct 条目）可执行基准入口（P6-05/R07）。

L_TIER=1 环境门——未设置时全部 skip（NOT_RUN，打印原因，skip 不算 PASS），
`python3 -m unittest discover -s tests` 只看到 skip，不执行任何基准步骤。

规模参数化（同一执行路径经 fixture_gen.py CLI，bench_runner 共享 harness）：
- 缺省 = 小样本保守档（800 行 / 64MiB 写入上限 / 4×4 目录），本轮真实执行并通过；
  小样本只证明入口可运行与口径正确，不冒充 XL 容量认证。
- XL 目标规模 1,000,000 行（09 §2）保留在常量 XL_TARGET_ROWS 与下列命令中，
  未获资源授权不执行（结果账本如实记 NOT_RUN_AUTH）。授权执行方式：
    L_TIER=1 XL_TIER_ROWS=1000000 XL_TIER_AUTH=1 XL_TIER_MAX_BYTES=<字节> \
        python3 -m unittest tests.benchmarks.test_xl_tier -v
  （可用 XL_TIER_GENERATIONS 覆盖 history profile 代数；设 1 时 history 组
  如实 skip——K=1 时累计口径与 distinct 重合，无可演示差异。）

三个 profile 分列（登记文档口径修正）：
- current-generation distinct：--rows N --generations 1 单代快照，
  count_distinct_current_entries 精确 == N；
- physical files：盘上内容文件数/字节与 manifest 逐项核对；
- history retention：--generations K 受控变更逐代扫描，count_total_rows
  （多代累计口径）与 count_distinct_current_entries 分列，generation_count 记录。

跨页/片无漏重：ShardWriter 分片后 MergedPageReader 翻页全集对比独立 oracle；
09 §2 字段 JSON 输出（tasks/events 未采集如实 NOT_MEASURED，edges 实测）。
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_runner import (
    L_TIER, L_TIER_SKIP, capacity_document, emit, gated, percentile,
    resolve_scale, shard_oracle)
from agents_kernel.indexing.coordination import MergedPageReader, ShardRootManifest
from agents_kernel.indexing.reader import IndexReader
from agents_kernel.services.summary import ReportPaginator
from agents_kernel.indexing.sharding import CrossShardGraph

XL_TARGET_ROWS = 1000000        # 09 §2 XL 档目标（未授权不执行，NOT_RUN_AUTH）
XL_TARGET_GENERATIONS = 1       # XL 目标形态：单代 1M distinct（current 口径认证）
XL_HISTORY_GENERATIONS_DEFAULT = 3  # 小样本 history 演示代数（K≥2 才有累计≠distinct）
QUERY_REPS = 30                 # 09 §3：至少重复 30 次
PAGE_SIZE = 100
XL_QUERY_SLO_SECONDS = 3.0      # 09 §3 XL 档温热首页 p95 目标（小样本仅记录不认证）

_SCALE = resolve_scale("XL_TIER", default_rows=800,
                       default_generations=XL_HISTORY_GENERATIONS_DEFAULT,
                       target_rows=XL_TARGET_ROWS,
                       target_generations=XL_TARGET_GENERATIONS)
if not L_TIER:
    print(L_TIER_SKIP, file=sys.stderr)
elif _SCALE["skip_reason"]:
    print(_SCALE["skip_reason"], file=sys.stderr)

_RUNNER = None


def runner():
    global _RUNNER
    if _RUNNER is None:
        from bench_runner import TierRunner
        _RUNNER = TierRunner("xl", _SCALE)
    return _RUNNER


class TestStep1_CurrentDistinctAndPhysical(unittest.TestCase):
    """profile current + physical：--rows N --generations 1 单代快照。"""

    @classmethod
    def setUpClass(cls):
        gated(_SCALE)
        cls.info = runner().ensure_current()
        cls.manifest = cls.info["manifest"]
        cls.rows = _SCALE["rows"]

    def test_dry_run_estimate_recorded(self):
        for token in ("DRY-RUN OK", "files=%d" % self.rows, "bytes=",
                      "inodes=", "rows_total=%d" % self.rows):
            self.assertIn(token, self.info["dry_run"])
        emit("XL", "step1_dry_run", {"dry_run": self.info["dry_run"],
                                     "generate": self.info["generate"]},
             runner().records)

    def test_count_distinct_current_entries_exact(self):
        """current-generation distinct：一轮普查的当前代 distinct 行数精确 == N。"""
        store = runner().open_store(self.info)
        try:
            counted = store.query_one(
                "SELECT COUNT(*) AS c, COUNT(DISTINCT path) AS d,"
                " COUNT(DISTINCT generation) AS g FROM files")
            self.assertEqual(counted["c"], self.rows)
            self.assertEqual(counted["d"], self.rows)   # 逐行路径唯一
            self.assertEqual(counted["g"], 1)           # 单代快照
        finally:
            store.close()
        bench = self.manifest["bench"]
        self.assertEqual(bench["count_distinct_current_entries"], self.rows)
        self.assertEqual(bench["count_total_rows"], self.rows)  # 单代：累计==distinct
        self.assertEqual(bench["generation_count"], 1)

    def test_physical_files_and_bytes_match_manifest(self):
        """physical files profile：普通文件数/内容字节与 manifest 逐项一致。

        rows 口径下 manifest.total_files 含 marker+manifest 各 1（与索引行数同一
        分母）；total_bytes 只计内容负载（不含脚手架与 index 库）。
        """
        regular = runner().regular_files(self.info)
        content = runner().content_files(self.info)
        self.assertEqual(len(regular), self.manifest["total_files"])
        self.assertEqual(len(content) + 2, self.manifest["total_files"])
        self.assertEqual(sum(path.stat().st_size for path in content),
                         self.manifest["total_bytes"])
        emit("XL", "step1_profiles", {
            "rows_requested": self.rows,
            "count_distinct_current_entries":
                self.manifest["bench"]["count_distinct_current_entries"],
            "regular_files": len(regular),
            "content_files": len(content),
            "included_bytes": self.manifest["total_bytes"],
            "index_db_bytes": self.info["store_path"].stat().st_size,
            "generation_seconds": self.manifest["generation_seconds"]},
            runner().records)


class TestStep2_HistoryRetentionProfile(unittest.TestCase):
    """profile history：--generations K 受控变更逐代扫描（累计行数口径）。"""

    @classmethod
    def setUpClass(cls):
        gated(_SCALE)
        if _SCALE["generations"] < 2:
            raise unittest.SkipTest(
                "NOT_RUN_ENV：history profile 需要 --generations ≥2（当前"
                " XL_TIER_GENERATIONS=%d，K=1 时累计口径与 distinct 重合）"
                % _SCALE["generations"])
        cls.info = runner().ensure_history()
        cls.manifest = cls.info["manifest"]
        cls.bench = cls.manifest["bench"]
        cls.per_gen = cls.bench["per_generation"]

    def test_generation_count_and_split_accounting(self):
        """count_total_rows（累计）与 count_distinct_current_entries 分列且不混同。"""
        self.assertEqual(self.bench["generation_count"], _SCALE["generations"])
        self.assertEqual(self.bench["count_total_rows"],
                         sum(row["file_count"] for row in self.per_gen))
        self.assertEqual(self.bench["count_distinct_current_entries"],
                         self.per_gen[-1]["file_count"])
        # 多代累计 ≠ 当前代 distinct（口径修正的核心演示；K>1 且 adds>deletes 时恒成立）
        self.assertGreater(self.bench["count_total_rows"],
                           self.bench["count_distinct_current_entries"])

    def test_per_generation_rows_match_controlled_changes(self):
        """逐代行数 = 上一代 + 新增 - 删除（受控变更账目逐代可核对）。"""
        for previous, current in zip(self.per_gen, self.per_gen[1:]):
            self.assertEqual(current["file_count"],
                             previous["file_count"] + current["added"]
                             - current["deleted"])
            # modified_indices(轮号) = range(轮号-1, small_total, 100)，轮号 = 代-1
            self.assertEqual(current["modified"],
                             len(range(current["generation"] - 2,
                                       self.manifest["small_files"], 100)))

    def test_scan_generations_table_matches_bench(self):
        store = runner().open_store(self.info)
        try:
            generations = store.query_all(
                """SELECT generation, file_count, status FROM scan_generations
                   ORDER BY generation""")
            self.assertEqual(len(generations), _SCALE["generations"])
            for row, record in zip(generations, self.per_gen):
                self.assertEqual(row["generation"], record["generation"])
                self.assertEqual(row["file_count"], record["file_count"])
                self.assertEqual(row["status"], "complete")
            live = IndexReader(store)
            self.assertEqual(live.count_files(),
                             self.bench["count_distinct_current_entries"])
        finally:
            store.close()
        emit("XL", "step2_history", {
            "generations": self.bench["generation_count"],
            "count_total_rows": self.bench["count_total_rows"],
            "count_distinct_current_entries":
                self.bench["count_distinct_current_entries"],
            "per_generation": [{key: row[key] for key in
                                ("generation", "file_count", "added", "deleted",
                                 "modified", "scan_seconds")}
                               for row in self.per_gen]},
            runner().records)


class TestStep3_ShardedPaginationNoLossNoDup(unittest.TestCase):
    """跨页/片无漏重：MergedPageReader 全集对比独立 oracle；跨片图与边实测。"""

    @classmethod
    def setUpClass(cls):
        gated(_SCALE)
        cls.shards = runner().ensure_shards()
        cls.total = cls.shards["result"].file_count
        cls.oracle = shard_oracle(cls.shards["dir"])

    def test_merged_pagination_full_iteration_matches_oracle(self):
        """翻页全集 == 逐片直读排序合并：无漏、无重、全局有序。"""
        reader = MergedPageReader.load(self.shards["dir"], page_size=37)
        try:
            collected = [(row["shard_id"], row["path"])
                         for row in reader.iter_rows()]
            self.assertEqual(collected, self.oracle)
            self.assertEqual(len(collected), self.total)
        finally:
            reader.close()

    def test_cross_shard_integrity_and_edges_measured(self):
        graph = CrossShardGraph.load(self.shards["dir"])
        try:
            report = graph.verify_integrity()
            self.assertEqual(report.file_count, self.total)
            self.assertEqual(report.expected_files, self.total)
            edges = list(graph.iter_edges())
            self.assertEqual(len(edges),
                             self.shards["edges"]["internal"]
                             + self.shards["edges"]["unknown"])
            self.assertGreater(self.shards["edges"]["internal"], 0)  # 真实 internal 边
            outcome = graph.find_shard_cycles()
            self.assertTrue(outcome.complete)
            self.assertEqual(outcome.cycles, ())     # 递减链构造：分片级 DAG
        finally:
            graph.close()
        emit("XL", "step3_shards", {
            "shard_count": len(self.shards["result"].generations),
            "file_count": self.total,
            "edges_internal": self.shards["edges"]["internal"],
            "edges_unknown": self.shards["edges"]["unknown"],
            "parsed_files": self.shards["edges"]["parsed"]},
            runner().records)

    def test_report_paginator_pages_bounded(self):
        manifest = ShardRootManifest.capture(self.shards["dir"])
        paginator = ReportPaginator(manifest, page_size=3)
        overview = paginator.overview()
        self.assertEqual(overview["integrity"], "verified")
        self.assertEqual(overview["total_files_estimate"], self.total)
        seen, cursor = [], None
        while True:
            page = paginator.page_shards(cursor)
            self.assertLessEqual(len(page["items"]), 3)
            seen.extend(item["shard_id"] for item in page["items"])
            if page["cursor"] is None:
                break
            cursor = page["cursor"]
        self.assertEqual(len(seen), len(manifest.shard_entries()))  # 总项数=真实条数


class TestStep4_PageLatencyAndResultLedger(unittest.TestCase):
    """分页时延记录（09 §3 口径）与 09 §2 字段 JSON 账本（含 NOT_MEASURED）。"""

    @classmethod
    def setUpClass(cls):
        gated(_SCALE)
        cls.shards = runner().ensure_shards()
        cls.info = runner().ensure_current()
        cls.manifest = cls.info["manifest"]
        cls.bench = cls.manifest["bench"]

    def test_page100_latency_sampled(self):
        """首页 100 条 30 次温热 p50/p95：小样本仅记录，不外推为 XL 达标。"""
        samples = []
        for _ in range(3):  # 预热（温热口径）
            reader = MergedPageReader.load(self.shards["dir"], page_size=PAGE_SIZE)
            reader.page()
            reader.close()
        for _ in range(QUERY_REPS):
            reader = MergedPageReader.load(self.shards["dir"], page_size=PAGE_SIZE)
            started = time.perf_counter()
            page = reader.page()
            samples.append(time.perf_counter() - started)
            reader.close()
            self.assertEqual(len(page.rows), PAGE_SIZE)
            self.assertEqual(len(page.rows), PAGE_SIZE)
        p50, p95 = percentile(samples, 0.50), percentile(samples, 0.95)
        emit("XL", "step4_page100", {"reps": QUERY_REPS, "page_size": PAGE_SIZE,
                                     "p50_s": round(p50, 6), "p95_s": round(p95, 6),
                                     "note": "小样本时延不外推为 XL 档 SLO 达标"},
             runner().records)
        self.assertLessEqual(p95, XL_QUERY_SLO_SECONDS)

    def test_capacity_result_ledger_emitted(self):
        """09 §2 字段 JSON：分项分列；未采集字段 NOT_MEASURED；大档 NOT_RUN_AUTH。"""
        regular = runner().regular_files(self.info)
        content = runner().content_files(self.info)
        document = capacity_document(
            runner(),
            entries=self.bench["count_distinct_current_entries"],
            regular_files=len(regular),
            included_bytes=sum(path.stat().st_size for path in content),
            edges=self.shards["edges"]["internal"]
            + self.shards["edges"]["unknown"],
            count_total_rows=self.bench["count_total_rows"],
            count_distinct_current_entries=self.bench["count_distinct_current_entries"],
            generation_count=self.bench["generation_count"],
            elapsed_seconds=self.manifest["generation_seconds"],
            extra={"index_db_bytes": self.info["store_path"].stat().st_size,
                   "content_files": len(content),
                   "rows_per_generation": [row["file_count"]
                                           for row in self.bench["per_generation"]]})
        self.assertEqual(document["tasks"], "NOT_MEASURED")
        self.assertEqual(document["events"], "NOT_MEASURED")
        self.assertEqual(document["targets_not_executed"]["status"], "NOT_RUN_AUTH")
        self.assertEqual(document["targets_not_executed"]["rows"], XL_TARGET_ROWS)
        emit("XL", "capacity_09s2", document, runner().records)


if __name__ == "__main__":
    unittest.main()

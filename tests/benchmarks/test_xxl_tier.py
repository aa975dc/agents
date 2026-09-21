# -*- coding: utf-8 -*-
"""XXL 档（目标 1000 万索引行）可执行基准入口（P6-05/R07）。

L_TIER=1 环境门——未设置时全部 skip（NOT_RUN，打印原因，skip 不算 PASS），
`python3 -m unittest discover -s tests` 只看到 skip，不执行任何基准步骤。

规模参数化（同一执行路径经 fixture_gen.py CLI，bench_runner 共享 harness）：
- 缺省 = 小样本保守档（800 行 × 3 代 / 64MiB 写入上限），本轮真实执行并通过；
  小样本只证明入口可运行与口径正确，不冒充 XXL 容量认证。
- XXL 目标规模 10,000,000 索引行 × 10 代（09 §2"索引数据库 XXL"）保留在常量
  XXL_TARGET_ROWS/XXL_TARGET_GENERATIONS 与下列命令中，未获资源授权不执行
  （结果账本如实记 NOT_RUN_AUTH）。授权执行方式：
    L_TIER=1 XXL_TIER_ROWS=10000000 XXL_TIER_GENERATIONS=10 XXL_TIER_AUTH=1 \
        XXL_TIER_MAX_BYTES=<字节> python3 -m unittest tests.benchmarks.test_xxl_tier -v

口径修正（R07 核心）：count_total_rows（多代累计）与
count_distinct_current_entries（当前代单快照）分列——**多代累计 1000 万行 ≠
当前代 1000 万不同条目**。当前实现 files 表为单快照（旧代行在完成事务清除，
历史只留 scan_generations 逐代账），累计口径证明的是吞吐，不是同时可查的
当前规模。

本文件覆盖：history profile 账目、分片根清单（capture/load/拒改）、跨片协调
分页（序列化游标续页、世代绑定拒绝、聚合一致）、汇总报告有界、09 §2 字段
JSON 账本（含 NOT_MEASURED 与外推输入）。
"""
import shutil
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_runner import (
    L_TIER, L_TIER_SKIP, capacity_document, emit, gated, percentile,
    resolve_scale, shard_oracle)
from agents_kernel.indexing.coordination import (
    MergedPageReader, ShardEntry, ShardRootManifest)
from agents_kernel.indexing.reader import IndexReader
from agents_kernel.indexing.sharding import CrossShardGraph, ShardPlanner, ShardWriter
from agents_kernel.services.summary import ReportPaginator
from agents_kernel.validation import CompanionError

XXL_TARGET_ROWS = 10000000        # 09 §2"索引数据库 XXL"目标（未授权不执行）
XXL_TARGET_GENERATIONS = 10
QUERY_REPS = 30
PAGE_SIZE = 100
XXL_QUERY_SLO_SECONDS = 5.0       # 09 §3 XXL 档温热首页 p95 目标（小样本仅记录）

_SCALE = resolve_scale("XXL_TIER", default_rows=800, default_generations=3,
                       target_rows=XXL_TARGET_ROWS,
                       target_generations=XXL_TARGET_GENERATIONS)
if not L_TIER:
    print(L_TIER_SKIP, file=sys.stderr)
elif _SCALE["skip_reason"]:
    print(_SCALE["skip_reason"], file=sys.stderr)

_RUNNER = None


def runner():
    global _RUNNER
    if _RUNNER is None:
        from bench_runner import TierRunner
        _RUNNER = TierRunner("xxl", _SCALE)
    return _RUNNER


class TestStep1_HistoryCumulativeVsDistinct(unittest.TestCase):
    """history retention profile：累计行数与当前 distinct 严格分列。"""

    @classmethod
    def setUpClass(cls):
        gated(_SCALE)
        cls.info = runner().ensure_history()
        cls.bench = cls.info["manifest"]["bench"]
        cls.per_gen = cls.bench["per_generation"]
        cls.manifest = cls.info["manifest"]

    def test_cumulative_and_distinct_reported_separately(self):
        self.assertEqual(self.bench["generation_count"], _SCALE["generations"])
        self.assertEqual(self.bench["count_total_rows"],
                         sum(row["file_count"] for row in self.per_gen))
        self.assertEqual(self.bench["count_distinct_current_entries"],
                         self.per_gen[-1]["file_count"])
        # 口径修正演示：多代累计 > 当前代 distinct，二者不得互相冒充
        self.assertGreater(self.bench["count_total_rows"],
                           self.bench["count_distinct_current_entries"])
        self.assertIn("多代累计", self.bench["count_total_rows_caliber"])

    def test_current_generation_queryable_and_consistent(self):
        """当前代单快照可查：reader 计数 == distinct 口径；全集翻页无重无漏。"""
        store = runner().open_store(self.info)
        try:
            live = IndexReader(store)
            self.assertEqual(live.count_files(),
                             self.bench["count_distinct_current_entries"])
            counted = sum(1 for _ in live.iter_files(limit=500))
            self.assertEqual(counted, self.bench["count_distinct_current_entries"])
        finally:
            store.close()
        emit("XXL", "step1_history", {
            "generations": self.bench["generation_count"],
            "count_total_rows": self.bench["count_total_rows"],
            "count_distinct_current_entries":
                self.bench["count_distinct_current_entries"],
            "per_generation": [{key: row[key] for key in
                                ("generation", "file_count", "added", "deleted",
                                 "modified", "scan_seconds")}
                               for row in self.per_gen]},
            runner().records)


class TestStep2_RootManifestAndMergedPagination(unittest.TestCase):
    """分片根清单完整性与跨片协调分页（游标续页/世代绑定/聚合一致）。"""

    @classmethod
    def setUpClass(cls):
        gated(_SCALE)
        cls.shards = runner().ensure_shards(runner().ensure_history())
        cls.total = cls.shards["result"].file_count
        cls.oracle = shard_oracle(cls.shards["dir"])

    def test_root_manifest_capture_load_roundtrip(self):
        manifest = ShardRootManifest.capture(self.shards["dir"])
        self.assertTrue(manifest.verified)
        self.assertEqual(manifest.total_files(), self.total)
        snapshot = runner().scratch / "xxl-manifest.json"
        manifest.save(snapshot)
        loaded = ShardRootManifest.load(snapshot)
        self.assertEqual([entry._asdict() for entry in loaded.shard_entries()],
                         [entry._asdict() for entry in manifest.shard_entries()])

    def test_root_manifest_row_count_mismatch_rejected(self):
        snapshot = runner().scratch / "xxl-manifest-bad.json"
        ShardRootManifest.capture(self.shards["dir"]).save(snapshot)
        import json as json_module
        document = json_module.loads(snapshot.read_text(encoding="utf-8"))
        document["entries"][0]["file_count"] += 1
        snapshot.write_text(json_module.dumps(document), encoding="utf-8")
        with self.assertRaises(CompanionError) as caught:
            ShardRootManifest.load(snapshot)
        self.assertIn("行数核对失败", str(caught.exception))

    def test_merged_pagination_matches_oracle_and_resumes(self):
        """翻页全集 == oracle；序列化游标跨读取器续页不重不漏。"""
        reader = MergedPageReader.load(self.shards["dir"], page_size=37)
        try:
            self.assertEqual([(row["shard_id"], row["path"])
                              for row in reader.iter_rows()], self.oracle)
        finally:
            reader.close()
        first = MergedPageReader.load(self.shards["dir"], page_size=37)
        page_one = first.page()
        first.close()
        second = MergedPageReader.load(self.shards["dir"], page_size=37)
        try:
            rest, cursor = [], page_one.cursor
            while cursor is not None:
                page = second.page(cursor)
                rest.extend((row["shard_id"], row["path"]) for row in page.rows)
                cursor = page.cursor
            self.assertEqual(rest, self.oracle[len(page_one.rows):])
        finally:
            second.close()

    def test_cursor_rejected_after_shard_rescan(self):
        """游标绑定 generation vector：某片重扫后续页拒绝（不拼两代结果）。"""
        reader = MergedPageReader.load(self.shards["dir"], page_size=37)
        cursor = reader.page().cursor
        reader.close()
        rescan_dir = runner().scratch / "shards-rescan"
        shutil.copytree(self.shards["dir"], rescan_dir)
        plan = ShardPlanner(max_modules=1).plan(self.shards["counts"])
        ShardWriter().write_shards(runner().ensure_history()["dir"], plan,
                                   rescan_dir, only={plan.shards[0].shard_id},
                                   extra_excluded=("index",))
        fresh = MergedPageReader.load(rescan_dir, page_size=37)
        try:
            with self.assertRaises(CompanionError) as caught:
                fresh.page(cursor)
            self.assertIn("失效", str(caught.exception))
        finally:
            fresh.close()

    def test_aggregates_match_real_rows(self):
        reader = MergedPageReader.load(self.shards["dir"])
        try:
            self.assertEqual(reader.count_total(), self.total)
            by_shard = reader.count_by_shard()
            self.assertEqual(sum(by_shard.values()), self.total)
            by_module = reader.count_by_module()
            self.assertEqual(by_module, self.shards["counts"])  # 缺省模块规则口径
        finally:
            reader.close()


class TestStep3_SummaryBoundedAndResultLedger(unittest.TestCase):
    """汇总报告有界（不随行数增长）+ 分页时延记录 + 09 §2 字段 JSON 账本。"""

    @classmethod
    def setUpClass(cls):
        gated(_SCALE)
        cls.shards = runner().ensure_shards()
        cls.info = runner().ensure_history()
        cls.bench = cls.info["manifest"]["bench"]
        cls.total = cls.shards["result"].file_count

    def test_summary_overview_fixed_keys_and_bounded_pages(self):
        manifest = ShardRootManifest.capture(self.shards["dir"])
        paginator = ReportPaginator(manifest, page_size=3)
        overview = paginator.overview()
        self.assertEqual(set(overview), {"shard_count", "total_files_estimate",
                                         "generation_vector", "integrity"})
        self.assertEqual(overview["total_files_estimate"], self.total)
        total, cursor = 0, None
        while True:
            page = paginator.page_shards(cursor)
            self.assertLessEqual(len(page["items"]), 3)
            total += len(page["items"])
            if page["cursor"] is None:
                break
            cursor = page["cursor"]
        self.assertEqual(total, len(manifest.shard_entries()))  # 总项数=真实条数
        # 纯元数据构造的 unverified 清单：总览如实标注，返回项数仍有界
        fabricated = ShardRootManifest(
            runner().scratch,
            [ShardEntry(shard_id="shard-%04d" % index,
                        db="shard-%04d.sqlite" % index, file_count=250000,
                        generation=1, sha256="a" * 64) for index in range(50)])
        unverified = ReportPaginator(fabricated, page_size=7).overview()
        self.assertEqual(unverified["integrity"], "unverified")
        self.assertEqual(unverified["total_files_estimate"], 50 * 250000)

    def test_page100_latency_sampled(self):
        """首页 100 条 30 次温热 p50/p95：小样本仅记录，不外推为 XXL 达标。"""
        for _ in range(3):  # 预热（温热口径）
            reader = MergedPageReader.load(self.shards["dir"], page_size=PAGE_SIZE)
            reader.page()
            reader.close()
        samples = []
        for _ in range(QUERY_REPS):
            reader = MergedPageReader.load(self.shards["dir"], page_size=PAGE_SIZE)
            started = time.perf_counter()
            page = reader.page()
            samples.append(time.perf_counter() - started)
            reader.close()
            self.assertEqual(len(page.rows), PAGE_SIZE)
        p50, p95 = percentile(samples, 0.50), percentile(samples, 0.95)
        emit("XXL", "step3_page100", {"reps": QUERY_REPS, "page_size": PAGE_SIZE,
                                      "p50_s": round(p50, 6), "p95_s": round(p95, 6),
                                      "note": "小样本时延不外推为 XXL 档 SLO 达标"},
             runner().records)
        self.assertLessEqual(p95, XXL_QUERY_SLO_SECONDS)

    def test_capacity_result_ledger_emitted(self):
        """09 §2 字段 JSON：分项分列；未采集字段 NOT_MEASURED；外推输入如实附上。"""
        regular = runner().regular_files(self.info)
        content = runner().content_files(self.info)
        graph = CrossShardGraph.load(self.shards["dir"])
        try:
            report = graph.verify_integrity()
            self.assertEqual(report.file_count, self.total)
        finally:
            graph.close()
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
            elapsed_seconds=sum(row["scan_seconds"]
                                for row in self.bench["per_generation"]),
            extra={"rows_per_generation": [row["file_count"]
                                           for row in self.bench["per_generation"]],
                   "scan_seconds_per_generation":
                       [row["scan_seconds"] for row in self.bench["per_generation"]],
                   "content_files": len(content),
                   "index_db_bytes":
                       runner().ensure_history()["store_path"].stat().st_size,
                   "extrapolation_inputs": {
                       "scan_rows_per_second": round(
                           self.bench["per_generation"][-1]["file_count"]
                           / max(self.bench["per_generation"][-1]["scan_seconds"],
                                 1e-9), 1),
                       "index_bytes_per_row": round(
                           runner().ensure_history()["store_path"].stat().st_size
                           / self.bench["count_distinct_current_entries"], 2),
                       "note": "小样本实测，仅作登记文档外推公式的输入"}},
            )
        self.assertEqual(document["tasks"], "NOT_MEASURED")
        self.assertEqual(document["events"], "NOT_MEASURED")
        self.assertEqual(document["targets_not_executed"]["status"], "NOT_RUN_AUTH")
        self.assertEqual(document["targets_not_executed"]["rows"], XXL_TARGET_ROWS)
        self.assertGreater(document["count_total_rows"],
                           document["count_distinct_current_entries"])
        emit("XXL", "capacity_09s2", document, runner().records)


if __name__ == "__main__":
    unittest.main()

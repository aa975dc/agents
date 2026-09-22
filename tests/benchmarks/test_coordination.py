# -*- coding: utf-8 -*-
"""XXL 协调与分页汇总小样本测试（C15 后半/SC03 实现半/CV05 尾/CV06/SEC03 前半）。

小 fixture（8 分片 × 100 行真实 tempfile 树，共 800 行），**不设容量门、常规
discover 可跑**（与 test_l_tier 的长跑基准区分；XXL 千万行物理实测归 P6-05，
NOT_RUN——本文件只证明实现正确性与资源有界，不冒充物理认证）。覆盖：

- SEC03 前半：remote:// 与 http(s):// 分片位置结构化拒绝（远端协议未实现且
  默认禁用），本地路径放行。
- MergedPageReader：全局 keyset (shard_id, path) 翻页与排序全集对比无重无漏、
  序列化游标跨读取器续页、游标绑定 generation vector（重扫一片后续页拒绝）、
  内存有界（peak_live_rows == page_size，总行数翻倍峰值不变）、游标滥用拒绝。
- 聚合查询：count_by_shard/count_by_module/count_total 与各片真实行数一致。
- ShardRootManifest：capture/load 往返、漏片/坏 sha/行数不符/清单外分片库拒绝。
- ReportPaginator：总览固定键、分片/文件明细逐页、总返回项数=真实条数且单页
  有界；50 分片 × 大行数纯元数据下 summary 返回项数仍有界（CV06）。
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))

from agents_kernel.indexing.coordination import (
    MergedPageReader, RemoteShardLocation, ShardEntry, ShardRootManifest,
    require_local_shard_location)
from agents_kernel.indexing.sharding import ShardPlanner, ShardWriter
from agents_kernel.services.summary import ReportPaginator
from agents_kernel.storage import db
from agents_kernel.validation import CompanionError

MODULES = 7                # m0..m6 各一片
ROOT_FILES = 100           # _root 一片
FILES_PER_MODULE = 100
SHARD_COUNT = MODULES + 1  # 8 分片
TOTAL_ROWS = SHARD_COUNT * FILES_PER_MODULE  # 800 行


def _build_tree(directory, files_per_module=FILES_PER_MODULE):
    """确定性小树：7 个模块目录 + 顶层散文件（→ _root），共 8 个模块。"""
    for index in range(MODULES):
        module_dir = directory / ("m%d" % index)
        module_dir.mkdir(parents=True, exist_ok=True)
        for file_index in range(files_per_module):
            (module_dir / ("mod_%03d.py" % file_index)).write_text(
                "def value_%03d():\n    return %d\n" % (file_index, file_index),
                encoding="utf-8")
    for index in range(ROOT_FILES):
        (directory / ("note_%03d.txt" % index)).write_text(
            "root file %d\n" % index, encoding="utf-8")


def _counts(files_per_module=FILES_PER_MODULE):
    counts = {"m%d" % index: files_per_module for index in range(MODULES)}
    counts["_root"] = ROOT_FILES
    return counts


def _write(tree, shard_dir, files_per_module=FILES_PER_MODULE):
    plan = ShardPlanner(max_modules=1).plan(_counts(files_per_module))
    return plan, ShardWriter().write_shards(tree, plan, shard_dir)


def _oracle(shard_dir):
    """独立真值：逐片直读 files 的 (shard_id, path) 排序全集（无重无漏的对照）。"""
    expected = []
    for shard_id in sorted(row["shard_id"] for row in
                           json.loads((shard_dir / "shards.json").read_text(
                               encoding="utf-8"))["shards"]):
        store = db.Store(shard_dir / (shard_id + ".sqlite"))
        store.open()
        try:
            expected.extend((shard_id, row["path"]) for row in
                            store.query_all("SELECT path FROM files ORDER BY path"))
        finally:
            store.close()
    return sorted(expected)


class _ShardsCase(unittest.TestCase):
    """共享 fixture：一套 8 分片 × 100 行的分片库（类结束清理）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="coordination-test-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.tree = cls.tmp / "tree"
        _build_tree(cls.tree)
        cls.shard_dir = cls.tmp / "shards"
        cls.plan, cls.result = _write(cls.tree, cls.shard_dir)
        cls.shard_ids = sorted(cls.result.generations)


class LocationBoundaryTest(unittest.TestCase):
    """SEC03 前半：SHARD_LOCATION 仅支持本地路径，远端结构化拒绝且默认禁用。"""

    def test_remote_and_http_locations_rejected(self):
        for location in ("remote://host/shards", "http://host/shards",
                         "https://host/shards", "/tmp/mixed-remote://x"):
            with self.assertRaises(RemoteShardLocation) as caught:
                require_local_shard_location(location)
            self.assertEqual(caught.exception.code, "remote_disabled")
            self.assertIsInstance(caught.exception, CompanionError)

    def test_coordinator_entry_points_reject_remote(self):
        for call in (lambda: ShardRootManifest.capture("remote://host/shards"),
                     lambda: MergedPageReader.load("http://host/shards"),
                     lambda: ShardRootManifest.load("remote://host/m.json")):
            with self.assertRaises(RemoteShardLocation):
                call()

    def test_local_path_passes_through(self):
        resolved = require_local_shard_location("/tmp/shards")
        self.assertEqual(resolved, Path("/tmp/shards"))


class MergedPaginationTest(_ShardsCase):
    """全局 keyset 翻页正确性、世代绑定、内存有界（成本随分片数而非总行数）。"""

    def test_full_iteration_matches_sorted_universe(self):
        reader = MergedPageReader.load(self.shard_dir, page_size=37)
        try:
            collected = [(row["shard_id"], row["path"])
                         for row in reader.iter_rows()]
            self.assertEqual(collected, _oracle(self.shard_dir))  # 无重无漏且全局有序
        finally:
            reader.close()

    def test_page_sizes_bounded_and_cursor_terminates(self):
        reader = MergedPageReader.load(self.shard_dir, page_size=37)
        try:
            total, pages, cursor = 0, 0, None
            while True:
                page = reader.page(cursor)
                self.assertLessEqual(len(page.rows), 37)
                total += len(page.rows)
                pages += 1
                cursor = page.cursor
                if cursor is None:
                    break
            self.assertEqual(total, TOTAL_ROWS)
            self.assertGreater(pages, TOTAL_ROWS // 37)   # 确实翻了多页
        finally:
            reader.close()

    def test_serialized_cursor_resumes_on_fresh_reader(self):
        first = MergedPageReader.load(self.shard_dir, page_size=37)
        page_one = first.page()
        first.close()
        expected = _oracle(self.shard_dir)
        self.assertEqual([(row["shard_id"], row["path"]) for row in page_one.rows],
                         expected[:37])
        second = MergedPageReader.load(self.shard_dir, page_size=37)
        try:
            rest, cursor = [], page_one.cursor
            while cursor is not None:
                page = second.page(cursor)
                rest.extend((row["shard_id"], row["path"]) for row in page.rows)
                cursor = page.cursor
            self.assertEqual(rest, expected[37:])
        finally:
            second.close()

    def test_cursor_bound_to_generation_vector(self):
        """翻页中途某片重扫（世代推进）→ 续页拒绝，不拼两个世代的结果。"""
        reader = MergedPageReader.load(self.shard_dir, page_size=37)
        cursor = reader.page().cursor
        reader.close()
        scratch = self.tmp / "shards-rescan"
        shutil.copytree(self.shard_dir, scratch)
        _write(self.tree, scratch)  # 整树重写：各片世代推进
        fresh = MergedPageReader.load(scratch, page_size=37)
        try:
            with self.assertRaises(CompanionError) as caught:
                fresh.page(cursor)
            self.assertIn("失效", str(caught.exception))
        finally:
            fresh.close()

    def test_misused_cursors_rejected(self):
        reader = MergedPageReader.load(self.shard_dir, page_size=37)
        self.addClassCleanup(reader.close)
        with self.assertRaises(CompanionError):
            reader.page({"shard_id": "shard-0000"})           # 结构非法
        page_one = reader.page()
        with self.assertRaises(CompanionError):
            reader.page(None)                                  # 已开始翻页
        page_two = reader.page(page_one.cursor)
        with self.assertRaises(CompanionError):
            reader.page(page_one.cursor)                       # 非最新游标
        bad = dict(page_two.cursor, generations={"shard-0000": 1})
        with self.assertRaises(CompanionError):
            reader.page(bad)                                   # 世代向量与清单不符

    def test_memory_bounded_regardless_of_total_rows(self):
        """peak_live_rows == page_size；总行数翻倍后峰值不变（内存 O(page_size+片数)）。"""
        peaks = []
        for files_per_module in (FILES_PER_MODULE, FILES_PER_MODULE * 2):
            tree = self.tmp / ("tree-x%d" % files_per_module)
            shard_dir = self.tmp / ("shards-x%d" % files_per_module)
            _build_tree(tree, files_per_module)
            _write(tree, shard_dir, files_per_module)
            reader = MergedPageReader.load(shard_dir, page_size=50)
            try:
                total = sum(1 for _ in reader.iter_rows())
                self.assertEqual(total, MODULES * files_per_module + ROOT_FILES)
                peaks.append(reader.peak_live_rows())
            finally:
                reader.close()
        self.assertEqual(peaks, [50, 50])   # 与总行数无关（计数器近似断言）


class AggregateTest(_ShardsCase):
    """聚合查询与各片真实行数一致；仅各片聚合、不拉行。"""

    def test_count_by_shard_matches_real_rows(self):
        reader = MergedPageReader.load(self.shard_dir)
        self.addClassCleanup(reader.close)
        counted = reader.count_by_shard()
        self.assertEqual(len(counted), SHARD_COUNT)
        for shard_id, count in counted.items():
            store = db.Store(self.shard_dir / (shard_id + ".sqlite"))
            store.open()
            try:
                real = store.query_one("SELECT COUNT(*) AS c FROM files")["c"]
            finally:
                store.close()
            self.assertEqual(count, real)

    def test_count_total_matches(self):
        reader = MergedPageReader.load(self.shard_dir)
        self.addClassCleanup(reader.close)
        self.assertEqual(reader.count_total(), TOTAL_ROWS)

    def test_count_by_module_matches_default_rule(self):
        reader = MergedPageReader.load(self.shard_dir)
        self.addClassCleanup(reader.close)
        counted = reader.count_by_module()
        expected = dict(_counts())
        self.assertEqual(counted, expected)
        self.assertEqual(sum(counted.values()), TOTAL_ROWS)


class RootManifestTest(_ShardsCase):
    """根清单：capture/load 往返与漏片/坏 sha/行数/清单外拒绝。"""

    def test_capture_entries_and_fingerprints(self):
        import hashlib
        manifest = ShardRootManifest.capture(self.shard_dir)
        self.assertTrue(manifest.verified)
        self.assertEqual(manifest.total_files(), TOTAL_ROWS)
        self.assertEqual(manifest.generation_vector(),
                         {shard_id: 1 for shard_id in self.shard_ids})
        for entry in manifest.shard_entries():
            self.assertEqual(entry.file_count, FILES_PER_MODULE)
            digest = hashlib.sha256(
                (self.shard_dir / entry.db).read_bytes()).hexdigest()
            self.assertEqual(entry.sha256, digest)

    def test_save_load_roundtrip(self):
        snapshot = self.tmp / "manifest.json"
        ShardRootManifest.capture(self.shard_dir).save(snapshot)
        loaded = ShardRootManifest.load(snapshot)
        self.assertTrue(loaded.verified)
        self.assertEqual([entry._asdict() for entry in loaded.shard_entries()],
                         [entry._asdict() for entry in
                          ShardRootManifest.capture(self.shard_dir).shard_entries()])

    def test_missing_shard_db_rejected(self):
        snapshot = self.tmp / "manifest-missing.json"
        ShardRootManifest.capture(self.shard_dir).save(snapshot)
        victim = self.shard_dir / "shard-0003.sqlite"
        backup = self.tmp / "shard-0003.sqlite.bak"
        shutil.move(str(victim), str(backup))
        try:
            with self.assertRaises(CompanionError) as caught:
                ShardRootManifest.capture(self.shard_dir)
            self.assertIn("漏片", str(caught.exception))
            with self.assertRaises(CompanionError) as caught:
                ShardRootManifest.load(snapshot)
            self.assertIn("漏片", str(caught.exception))
        finally:
            shutil.move(str(backup), str(victim))

    def test_corrupted_sha_rejected(self):
        snapshot = self.tmp / "manifest-sha.json"
        ShardRootManifest.capture(self.shard_dir).save(snapshot)
        victim = self.shard_dir / "shard-0002.sqlite"
        data = bytearray(victim.read_bytes())
        original = bytes(data)
        data[-1] ^= 0xFF
        victim.write_bytes(bytes(data))
        try:
            with self.assertRaises(CompanionError) as caught:
                ShardRootManifest.load(snapshot)
            self.assertIn("sha256", str(caught.exception))
        finally:
            victim.write_bytes(original)

    def test_row_count_mismatch_rejected(self):
        snapshot = self.tmp / "manifest-count.json"
        ShardRootManifest.capture(self.shard_dir).save(snapshot)
        document = json.loads(snapshot.read_text(encoding="utf-8"))
        document["entries"][0]["file_count"] += 1
        snapshot.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(CompanionError) as caught:
            ShardRootManifest.load(snapshot)
        self.assertIn("行数核对失败", str(caught.exception))

    def test_extra_shard_db_rejected(self):
        snapshot = self.tmp / "manifest-extra.json"
        ShardRootManifest.capture(self.shard_dir).save(snapshot)
        extra = self.shard_dir / "shard-0042.sqlite"
        extra.write_bytes(b"")
        try:
            with self.assertRaises(CompanionError) as caught:
                ShardRootManifest.load(snapshot)
            self.assertIn("清单外", str(caught.exception))
        finally:
            extra.unlink()


class SummaryReportTest(_ShardsCase):
    """分页汇总报告：总览固定键、逐页明细、总项数=真实条数、单页有界（CV06）。"""

    def test_overview_is_fixed_size_and_honest(self):
        paginator = ReportPaginator(ShardRootManifest.capture(self.shard_dir))
        overview = paginator.overview()
        self.assertEqual(set(overview),
                         {"shard_count", "total_files_estimate",
                          "generation_vector", "integrity"})
        self.assertEqual(overview["shard_count"], SHARD_COUNT)
        self.assertEqual(overview["total_files_estimate"], TOTAL_ROWS)
        self.assertEqual(len(overview["generation_vector"]), SHARD_COUNT)
        self.assertEqual(overview["integrity"], "verified")

    def test_page_shards_bounded_and_total_matches(self):
        paginator = ReportPaginator(ShardRootManifest.capture(self.shard_dir),
                                    page_size=3)
        entries = {entry.shard_id: entry._asdict()
                   for entry in paginator.manifest.shard_entries()}
        total, seen, cursor = 0, [], None
        while True:
            page = paginator.page_shards(cursor)
            self.assertLessEqual(len(page["items"]), 3)
            for item in page["items"]:
                self.assertEqual(item, entries[item["shard_id"]])
                seen.append(item["shard_id"])
            total += len(page["items"])
            if page["cursor"] is None:
                break
            cursor = page["cursor"]
        self.assertEqual(total, SHARD_COUNT)          # 总返回项数 = 真实条数
        self.assertEqual(seen, sorted(entries))       # keyset 全序无重
        tail = paginator.page_shards("shard-0006")["items"]
        self.assertEqual([item["shard_id"] for item in tail],
                         sorted(entries)[-1:])        # 只返回游标之后的分片

    def test_page_files_bounded_and_total_matches(self):
        paginator = ReportPaginator(ShardRootManifest.capture(self.shard_dir))
        reader = MergedPageReader.load(self.shard_dir, page_size=100)
        self.addClassCleanup(reader.close)
        total, cursor = 0, None
        while True:
            page = paginator.page_files(cursor, reader)
            self.assertLessEqual(len(page.rows), 100)
            total += len(page.rows)
            if page.cursor is None:
                break
            cursor = page.cursor
        self.assertEqual(total, TOTAL_ROWS)
        with self.assertRaises(CompanionError):
            paginator.page_files()

    def test_summary_stays_bounded_for_fifty_large_shards(self):
        """50 分片 × 大行数纯元数据：summary 返回项数仍有界，不随行数涨。"""
        entries = [ShardEntry(shard_id="shard-%04d" % index,
                              db="shard-%04d.sqlite" % index, file_count=250000,
                              generation=1, sha256="a" * 64)
                   for index in range(50)]
        manifest = ShardRootManifest(self.tmp / "fabricated", entries)
        self.assertFalse(manifest.verified)
        paginator = ReportPaginator(manifest, page_size=7)
        overview = paginator.overview()
        self.assertEqual(overview["total_files_estimate"], 50 * 250000)
        self.assertEqual(overview["integrity"], "unverified")
        total, pages, cursor = 0, 0, None
        while True:
            page = paginator.page_shards(cursor)
            self.assertLessEqual(len(page["items"]), 7)
            total += len(page["items"])
            pages += 1
            if page["cursor"] is None:
                break
            cursor = page["cursor"]
        self.assertEqual((total, pages), (50, 8))     # 7×7+1：单页有界且项数精确


if __name__ == "__main__":
    unittest.main()

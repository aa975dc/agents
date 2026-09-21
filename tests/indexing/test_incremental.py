"""增量索引单测：内容哈希流式/回填/跨代幂等（C08）、模块归属稳定推导与
modules 单快照换装（C18/Z12 增量半）、1% 变更只哈希变更文件 + 闭包恰好含
受影响模块（IX04-08 增量半 / ST07 前置）。

真实文件系统（tempfile）+ 真实 SQLite；分块断言用记录型 open 代理统计
read 块边界，不整读 5MiB 内容。
"""
import builtins
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel import digest
from agents_kernel.indexing import content_hash, edges, modules, scanner
from agents_kernel.services import closure
from agents_kernel.storage import db
from agents_kernel.validation import CompanionError

MIB = 1024 * 1024


class _RecordingHandle:
    """二进制句柄代理：记录每次 read 的块大小；with 语义保持代理身份。"""

    def __init__(self, handle, sizes):
        self._handle = handle
        self._sizes = sizes

    def read(self, *args):
        data = self._handle.read(*args)
        self._sizes.append(len(data))
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._handle, name)


class _ReadRecorder:
    """替换 builtins.open：二进制模式包一层块大小记录，其余原样转发。"""

    def __init__(self):
        self.sizes = []
        self._real = builtins.open

    def __call__(self, file, *args, **kwargs):
        handle = self._real(file, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if "b" in mode:
            return _RecordingHandle(handle, self.sizes)
        return handle


class IncrementalBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name) / "tree"
        self.tree.mkdir()
        self.store = db.Store(Path(self.temp.name) / "facts.sqlite")
        self.store.open()
        self.addCleanup(self.store.close)

    def write(self, rel, data):
        path = self.tree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def scan(self, **kwargs):
        writer = db.acquire_writer(self.store)
        try:
            return scanner.IndexScanner(self.store).scan(self.tree, writer, **kwargs)
        finally:
            writer.close()

    def build(self, generation, mapper=None):
        """扫描后同会话内完成建模块表与建边（C08/C18/Z12 的完整增量步）。"""
        writer = db.acquire_writer(self.store)
        try:
            modules_result = modules.build_modules(self.store, writer, generation, mapper)
            edges_result = edges.build_edges(self.store, writer, generation, mapper)
        finally:
            writer.close()
        return modules_result, edges_result

    def module_rows(self):
        return {row["module_id"]: row["file_count"] for row in self.store.query_all(
            "SELECT module_id, file_count FROM modules")}

    def edge_rows(self, sql="SELECT from_module, to_module_or_unknown, kind,"
                            " source_file, line, generation FROM edges"):
        return self.store.query_all(sql)


class ContentHashTests(IncrementalBase):
    def test_streaming_hash_chunk_boundaries_no_whole_read(self):
        payload = b"\xa5" * (5 * MIB + 1)  # 5MiB 零一字节：块边界 + 尾块都覆盖
        target = self.write("big.bin", payload)
        recorder = _ReadRecorder()
        original_open = builtins.open
        builtins.open = recorder
        try:
            hasher = content_hash.ContentHasher(read_size=MIB)
            sha256 = hasher(str(target))
        finally:
            builtins.open = original_open
        self.assertEqual(sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(recorder.sizes, [MIB] * 5 + [1, 0])  # 分块流式，绝不整读

    def test_hasher_defaults_and_validation_and_missing_file(self):
        self.assertEqual(content_hash.ContentHasher().read_size, digest.READ_SIZE)
        with self.assertRaises(CompanionError):
            content_hash.ContentHasher(read_size=0)
        with self.assertRaises(CompanionError):
            content_hash.ContentHasher(read_size=True)
        self.assertIsNone(content_hash.ContentHasher()(str(self.tree / "nope.bin")))

    def test_hasher_serves_as_scanner_hook(self):
        self.write("a.py", b"print(1)\n")
        result = self.scan(hash_hook=content_hash.ContentHasher())
        self.assertEqual(result.hash_computed, 1)
        row = self.store.query_one("SELECT sha256 FROM files WHERE path = 'a.py'")
        self.assertEqual(row["sha256"],
                         digest.sha256_file(str(self.tree / "a.py"))[0])

    def test_backfill_fills_pending_in_batches(self):
        for index in range(5):
            self.write("f%d.txt" % index, b"payload-%d" % index)
        self.scan()
        self.assertTrue(all(row["sha256"] is None for row in
                            self.store.query_all("SELECT sha256 FROM files")))
        writer = db.acquire_writer(self.store)
        try:
            result = content_hash.backfill_hashes(self.store, writer, batch_size=2)
        finally:
            writer.close()
        self.assertEqual((result.hashed, result.skipped, result.batches), (5, 0, 3))
        rows = {row["path"]: row["sha256"] for row in
                self.store.query_all("SELECT path, sha256 FROM files")}
        for path, sha256 in rows.items():
            self.assertEqual(sha256,
                             digest.sha256_file(str(self.tree / path))[0])

    def test_backfill_idempotent_and_expired_snapshot_skipped(self):
        self.write("solo.txt", b"v1")
        self.scan()                                 # 快照：sha 待哈希
        self.write("solo.txt", b"v2-much-longer")   # 扫描后文件又变：快照过期
        writer = db.acquire_writer(self.store)
        try:
            first = content_hash.backfill_hashes(self.store, writer)
            second = content_hash.backfill_hashes(self.store, writer)
        finally:
            writer.close()
        self.assertEqual((first.hashed, first.skipped), (0, 1))  # 过期不冒充旧快照
        self.assertEqual((second.hashed, second.skipped), (0, 1))  # 幂等：不损坏
        self.assertIsNone(self.store.query_one(
            "SELECT sha256 FROM files")["sha256"])
        third = self.scan(hash_hook=content_hash.ContentHasher())
        self.assertEqual(third.hash_computed, 1)  # 下轮扫描补上
        self.assertIsNotNone(self.store.query_one("SELECT sha256 FROM files")["sha256"])

    def test_backfill_cross_generation_and_stale_generation_guard(self):
        self.write("a.txt", b"a")
        self.scan()  # generation 1
        writer = db.acquire_writer(self.store)
        try:
            self.assertEqual(content_hash.backfill_hashes(
                self.store, writer, generation=1).hashed, 1)
        finally:
            writer.close()
        self.scan()  # generation 2：无变化，sha 沿用旧代
        self.assertTrue(all(row["sha256"] is not None for row in
                            self.store.query_all("SELECT sha256 FROM files")))
        writer = db.acquire_writer(self.store)
        try:
            result = content_hash.backfill_hashes(self.store, writer)  # 缺省最新代
            self.assertEqual(result.generation, 2)
            self.assertEqual(result.hashed, 0)  # 跨代幂等：无待哈希条目
            with self.assertRaises(CompanionError):  # files 只保留最新代
                content_hash.backfill_hashes(self.store, writer, generation=1)
        finally:
            writer.close()


class ModuleTests(IncrementalBase):
    def test_module_of_rules_longest_prefix_and_validation(self):
        mapper = modules.ModuleMapper()
        self.assertEqual(mapper.module_of("src/a.py"), "src")
        self.assertEqual(mapper.module_of("src/deep/b.py"), "src")
        self.assertEqual(mapper.module_of("README.md"), modules.DEFAULT_ROOT_MODULE)
        self.assertEqual(mapper.module_of("a.py"), modules.DEFAULT_ROOT_MODULE)
        ruled = modules.ModuleMapper(rules=(("src", "app-src"),
                                            ("src/core", "core")))
        self.assertEqual(ruled.module_of("src/core/x.py"), "core")   # 最长前缀优先
        self.assertEqual(ruled.module_of("src/core2/x.py"), "app-src")  # 段边界匹配
        self.assertEqual(ruled.module_of("other/x.py"), "other")
        for bad in ("../escape.py", "/abs.py", "a//b.py", ""):
            with self.assertRaises(CompanionError):
                mapper.module_of(bad)
        with self.assertRaises(CompanionError):
            modules.ModuleMapper(rules=(("x", "a/b"),))

    def test_build_modules_counts_and_single_snapshot_swap(self):
        self.write("src/a.py", b"a")
        self.write("src/b.py", b"b")
        self.write("docs/d.md", b"d")
        self.write("README.md", b"r")
        first = self.scan()
        writer = db.acquire_writer(self.store)
        try:
            result = modules.build_modules(self.store, writer, first.generation)
        finally:
            writer.close()
        self.assertEqual((result.module_count, result.file_count), (3, 4))
        self.assertEqual(self.module_rows(), {"src": 2, "docs": 1, "_root": 1})

        (self.tree / "src" / "a.py").unlink()  # gen2：src 剩 1，docs 删空
        (self.tree / "docs" / "d.md").unlink()
        second = self.scan()
        writer = db.acquire_writer(self.store)
        try:
            modules.build_modules(self.store, writer, second.generation)
        finally:
            writer.close()
        self.assertEqual(self.module_rows(), {"src": 1, "_root": 1})  # docs 空则除名
        self.assertEqual(self.store.query_one(
            "SELECT COUNT(*) AS c FROM modules WHERE generation < ?",
            (second.generation,))["c"], 0)  # 旧行不残留

    def test_build_modules_defensive_checks(self):
        self.write("solo.py", b"x")
        result = self.scan()
        writer = db.acquire_writer(self.store)
        try:
            with self.assertRaises(CompanionError):
                modules.build_modules(self.store, writer, 99)  # 世代不存在
            with self.store.transaction() as conn:  # 篡改 manifest 计数
                conn.execute("UPDATE scan_generations SET file_count = 999")
            with self.assertRaises(CompanionError):
                modules.build_modules(self.store, writer, result.generation)
        finally:
            writer.close()
        self.assertEqual(self.store.query_one(
            "SELECT COUNT(*) AS c FROM sqlite_master WHERE name = 'modules'")["c"], 0)

    def test_module_ids_stable_across_generations(self):
        self.write("app/a.py", b"a")
        self.write("lib/b.py", b"b")
        first = self.scan()
        writer = db.acquire_writer(self.store)
        try:
            modules.build_modules(self.store, writer, first.generation)
        finally:
            writer.close()
        before = self.module_rows()
        self.write("app/new.py", b"n")  # 未变模块（lib、app 归属规则）不得换 id
        second = self.scan()
        writer = db.acquire_writer(self.store)
        try:
            modules.build_modules(self.store, writer, second.generation)
        finally:
            writer.close()
        after = self.module_rows()
        for module_id, count in before.items():
            self.assertIn(module_id, after)
            self.assertEqual(after[module_id],
                             count + (1 if module_id == "app" else 0))


class IncrementalClosureTests(IncrementalBase):
    def _build_three_module_tree(self):
        """200 文件三模块：app→lib 一条内边；1% 变更 = 2 个文件。"""
        for index in range(65):
            self.write("app/a%03d.py" % index, b"# app %d\n" % index)
        for index in range(65):
            self.write("lib/b%03d.py" % index, b"# lib %d\n" % index)
        for index in range(70):
            self.write("core/c%03d.py" % index, b"# core %d\n" % index)
        self.write("app/a000.py", b"import lib.b000\n")

    def test_one_percent_change_hashes_only_changed_and_closure_exact(self):
        self._build_three_module_tree()
        hashed_paths = []

        def hook(absolute_path):
            hashed_paths.append(absolute_path)
            return digest.sha256_file(absolute_path)[0]

        first = self.scan(hash_hook=hook)
        self.assertEqual(first.hash_computed, 200)
        generation = first.generation
        self.build(generation)

        (self.tree / "lib" / "b000.py").write_bytes(b"# lib 0 changed\n")
        (self.tree / "core" / "c000.py").write_bytes(b"# core 0 changed\n")
        hashed_paths.clear()
        second = self.scan(hash_hook=hook)
        self.assertEqual(second.hash_computed, 2)   # 1% 变更：只算这两个
        self.assertEqual(second.hash_reused, 198)
        self.assertEqual(sorted(hashed_paths), sorted([
            str(self.tree / "lib" / "b000.py"),
            str(self.tree / "core" / "c000.py")]))
        self.build(second.generation)  # 增量流程：每轮扫描后重建模块/边

        result = closure.affected_closure(
            self.store, ["lib/b000.py", "core/c000.py"])
        self.assertEqual(result.changed_modules, ("core", "lib"))
        self.assertEqual(result.closure, ("app", "core", "lib"))  # app 依赖 lib
        self.assertFalse(result.conservative)  # 全部边可解析，无需保守扩展

    def test_add_delete_rename_mix_leaves_no_orphans(self):
        self.write("old/x.py", b"import keep.k1\n")
        self.write("keep/k1.py", b"k1\n")
        self.write("keep/k2.py", b"k2\n")
        first = self.scan()
        self.build(first.generation)

        (self.tree / "old" / "x.py").unlink()          # 删除：模块 old 清空
        (self.tree / "moved").mkdir()                  # 重命名：跨目录移动
        (self.tree / "keep" / "k2.py").rename(
            self.tree / "moved" / "m2.py")
        self.write("fresh/y.py", b"import moved.m2\n")  # 新增：新模块 fresh
        second = self.scan()
        self.build(second.generation)

        self.assertEqual(self.module_rows(), {"keep": 1, "moved": 1, "fresh": 1})
        manifest_count = self.store.query_one(
            "SELECT file_count FROM scan_generations WHERE generation = ?",
            (second.generation,))["file_count"]
        self.assertEqual(sum(self.module_rows().values()), manifest_count)
        edges_now = [tuple(row)[:5] for row in self.edge_rows(
            "SELECT from_module, to_module_or_unknown, kind, source_file, line"
            " FROM edges")]
        self.assertIn(("fresh", "moved", "internal", "fresh/y.py", 1), edges_now)
        sources = {row[3] for row in edges_now}
        self.assertNotIn("keep/k2.py", sources)  # 重命名前文件的边不残留
        live_paths = {row["path"] for row in self.store.query_all(
            "SELECT path FROM files")}
        self.assertTrue(sources <= live_paths)  # 边的 source_file 全部在册
        module_ids = set(self.module_rows())
        for row in self.edge_rows("SELECT from_module, to_module_or_unknown, kind"
                                  " FROM edges WHERE kind = 'internal'"):
            self.assertIn(row["from_module"], module_ids)   # 无孤儿端点
            self.assertIn(row["to_module_or_unknown"], module_ids)

    def test_edges_rebuild_swaps_generation_and_keeps_stable_ids(self):
        self.write("app/a.py", b"import lib.b\n")
        self.write("lib/b.py", b"b\n")
        first = self.scan()
        self.build(first.generation)
        before = [tuple(row)[:5] for row in self.edge_rows()]
        self.assertEqual(before, [("app", "lib", "internal", "app/a.py", 1)])

        (self.tree / "lib" / "b.py").write_bytes(b"# only content changed\n")
        second = self.scan()
        self.build(second.generation)
        after = [tuple(row)[:5] for row in self.edge_rows()]
        self.assertEqual(after, before)  # 未变模块的边逐行一致（id 稳定）
        self.assertEqual(self.store.query_one(
            "SELECT COUNT(*) AS c FROM edges WHERE generation != ?",
            (second.generation,))["c"], 0)  # 旧行整体换装，不残留
        self.assertEqual(self.store.query_one(
            "SELECT COUNT(*) AS c FROM edges_staging")["c"], 0)


if __name__ == "__main__":
    unittest.main()

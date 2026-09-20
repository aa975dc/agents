"""流式普查索引单测：git/plain 双模式枚举、排除账、分批落库、generation 中断恢复、
keyset 分页只读（Z04 扫描半 / IX01-03 / FS04-05）。

真实文件系统（tempfile）+ 真实 git 仓库；数据库一律建在 tempfile（ST06）。
内容不读断言：扫描/查询期间 monkeypatch builtins.open 计数为 0（缺省无 hash_hook）。
"""
import builtins
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel import digest
from agents_kernel.filesystem import walk
from agents_kernel.indexing import reader, scanner
from agents_kernel.storage import db
from agents_kernel.validation import CompanionError

MIB = 1024 * 1024
EXTRA_EXCLUDED = {"scratch"}


class _OpenCounter:
    """替换 builtins.open 的计数代理：统计而不阻断真实打开。"""

    def __init__(self):
        self.calls = []
        self._real = builtins.open

    def __call__(self, file, *args, **kwargs):
        self.calls.append(os.fspath(file))
        return self._real(file, *args, **kwargs)


class ScanTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name) / "tree"
        self.tree.mkdir()
        self.db_path = Path(self.temp.name) / "facts.sqlite"
        self.store = db.Store(self.db_path)
        self.store.open()
        self.addCleanup(self.store.close)
        # 隔离全局 git 配置（global gitignore/属性可能吞掉 .env 等），测试树自决。
        env_patch = mock.patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(Path(self.temp.name) / "gitconfig"),
            "HOME": self.temp.name,
            "XDG_CONFIG_HOME": self.temp.name,
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def write(self, rel, data):
        path = self.tree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def scan(self, root=None, **kwargs):
        writer = db.acquire_writer(self.store)
        try:
            return scanner.IndexScanner(self.store, batch_size=kwargs.pop(
                "batch_size", scanner.DEFAULT_BATCH_SIZE)).scan(
                root or self.tree, writer, **kwargs)
        finally:
            writer.close()

    def zero_open(self):
        counter = _OpenCounter()
        patcher = mock.patch("builtins.open", new=counter)
        patcher.start()
        self.addCleanup(patcher.stop)
        return counter

    def file_rows(self, sql="SELECT path, size, mtime_ns, kind, sha256, generation FROM files"):
        return self.store.query_all(sql)

    def excluded_rows(self):
        return self.store.query_all(
            "SELECT path, reason FROM files_excluded ORDER BY path")

    def manifest(self):
        return {row["generation"]: row["status"]
                for row in self.store.query_all("SELECT generation, status FROM scan_generations")}

    # ---- 树构造 ----

    def build_census_tree(self):
        """混合树：普通文件/子目录/换行文件名/大文件/排除目录/敏感项/special/symlink。"""
        self.write("src/a.py", b"print(1)\n")
        self.write("src/deep/b.txt", b"data")
        self.write("we\nird.txt", b"newline-name")
        self.write("big.bin", b"\0" * (5 * MIB + 1))  # 超 5MiB，内容绝不读
        self.write("node_modules/pkg/index.js", b"junk")
        self.write(".env", b"SECRET=1")
        self.write(".ssh/config", b"Host *")
        self.write("scratch/tmp.log", b"log")
        expected_files = {"src/a.py", "src/deep/b.txt", "we\nird.txt", "big.bin"}
        expected_excluded = {".env": "sensitive", ".ssh": "sensitive",
                             "node_modules": "excluded_dir", "scratch": "extra"}
        if os.name == "posix":
            os.mkfifo(self.tree / "pipe.fifo")
            expected_excluded["pipe.fifo"] = "special"
            os.symlink("src/a.py", self.tree / "link.txt")
            expected_excluded["link.txt"] = "symlink"
        return expected_files, expected_excluded

    def build_git_tree(self):
        """git 树：staged 常规项 + 未跟踪文件 + 工作树删除；换行文件名覆盖 -z 解析。"""
        self._git("init", "-q")
        self.write("a.py", b"a")
        self.write("dir/b.txt", b"b")
        self.write("we\nird.txt", b"newline-name")
        self.write("node_modules/c.js", b"junk")
        self.write(".env", b"SECRET=1")
        if os.name == "posix":
            os.symlink("a.py", self.tree / "link")
        self._git("add", "-A")
        expected_files = {"a.py", "we\nird.txt", "untracked.txt"}
        # git 模式只见文件：目录排除按文件粒度记账；plain 模式在下降前拦截目录。
        expected_excluded = {".env": "sensitive", "node_modules/c.js": "excluded_dir",
                             "dir/b.txt": "missing"}
        if os.name == "posix":
            expected_excluded["link"] = "symlink"
        self.write("untracked.txt", b"fresh")
        (self.tree / "dir" / "b.txt").unlink()  # tracked 但工作树已删
        return expected_files, expected_excluded

    def _git(self, *argv):
        subprocess.run(["git", "-C", str(self.tree), *argv], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class PlainScanTests(ScanTestBase):
    def test_plain_scan_mixed_tree_zero_open_and_full_account(self):
        expected_files, expected_excluded = self.build_census_tree()
        counter = self.zero_open()
        result = self.scan(extra_excluded=EXTRA_EXCLUDED)
        self.assertEqual(counter.calls, [])  # 全程零 open（含 5MiB 大文件）
        self.assertEqual(result.mode, "plain")
        self.assertEqual(result.file_count, len(expected_files))
        self.assertEqual(result.excluded_count, len(expected_excluded))
        rows = {row["path"]: row for row in self.file_rows()}
        self.assertEqual(set(rows), expected_files)
        self.assertTrue(all(row["sha256"] is None for row in rows.values()))  # 待哈希
        self.assertTrue(all(row["kind"] == "file" for row in rows.values()))
        self.assertEqual(rows["big.bin"]["size"], 5 * MIB + 1)
        account = {row["path"]: row["reason"] for row in self.excluded_rows()}
        self.assertEqual(account, expected_excluded)
        self.assertEqual(self.store.query_one(
            "SELECT MAX(version) AS v FROM schema_version")["v"], db.SCHEMA_VERSION)

    def test_batch_count_matches_ceiling_of_file_count(self):
        for index in range(5):
            self.write("f%d.txt" % index, b"x" * index)
        result = self.scan(batch_size=2)
        self.assertEqual(result.file_count, 5)
        self.assertEqual(result.batch_count, 3)  # ceil(5/2)：批间提交可续
        self.assertEqual(self.store.query_one(
            "SELECT COUNT(*) AS c FROM files_staging")["c"], 0)  # 完成后 staging 清空


class GitScanTests(ScanTestBase):
    @unittest.skipUnless(walk.detect_mode(REPO_ROOT) == "git", "需要 git 二进制")
    def test_git_scan_tracked_untracked_missing_and_names(self):
        expected_files, expected_excluded = self.build_git_tree()
        counter = self.zero_open()
        result = self.scan()
        self.assertEqual(counter.calls, [])
        self.assertEqual(result.mode, "git")
        rows = {row["path"] for row in self.file_rows()}
        self.assertEqual(rows, expected_files)  # tracked∪untracked，换行名无损
        account = {row["path"]: row["reason"] for row in self.excluded_rows()}
        self.assertEqual(account, expected_excluded)  # 含 tracked 已删 → missing

    def test_plain_directory_falls_back_to_scandir(self):
        self.write("only.txt", b"1")
        self.assertEqual(walk.detect_mode(self.tree), "plain")
        result = self.scan()
        self.assertEqual(result.mode, "plain")
        self.assertEqual(result.file_count, 1)


class GenerationTests(ScanTestBase):
    def test_unchanged_paths_skip_rehash_and_changed_marked_pending(self):
        for name in ("f1.txt", "f2.txt", "f3.txt"):
            self.write(name, b"payload-of-" + name.encode())
        hashed_paths = []

        def hook(absolute_path):
            hashed_paths.append(absolute_path)
            return digest.sha256_file(absolute_path)[0]

        first = self.scan(hash_hook=hook)
        self.assertEqual(first.hash_computed, 3)
        self.assertEqual(first.hash_reused, 0)
        sha_before = {row["path"]: row["sha256"] for row in self.file_rows()}
        self.assertTrue(all(sha_before.values()))

        second = self.scan(hash_hook=hook)
        self.assertEqual(second.hash_computed, 0)   # size+mtime 未变 → 零重哈希
        self.assertEqual(second.hash_reused, 3)
        self.assertEqual(sorted(hashed_paths), sorted(
            str(self.tree / name) for name in ("f1.txt", "f2.txt", "f3.txt")))
        sha_after = {row["path"]: row["sha256"] for row in self.file_rows()}
        self.assertEqual(sha_after, sha_before)     # 沿用旧 sha256

        self.write("f2.txt", b"two-changed-longer")
        third = self.scan(hash_hook=hook)
        self.assertEqual(third.hash_computed, 1)
        self.assertEqual(third.hash_reused, 2)
        self.assertEqual(hashed_paths[-1], str(self.tree / "f2.txt"))
        rows = {row["path"]: row["sha256"] for row in self.file_rows()}
        self.assertNotEqual(rows["f2.txt"], sha_before["f2.txt"])
        self.assertEqual(rows["f2.txt"],
                         digest.sha256_file(str(self.tree / "f2.txt"))[0])

    def test_scan_without_hook_leaves_sha256_null(self):
        self.write("solo.txt", b"x")
        self.scan()
        self.assertTrue(all(row["sha256"] is None for row in self.file_rows()))

    def test_interrupt_after_second_batch_keeps_old_generation_readable(self):
        for index in range(10):
            self.write("f%02d.txt" % index, b"payload-%d" % index)
        first = self.scan(batch_size=3)
        self.assertEqual(first.generation, 1)

        class Boom(Exception):
            pass

        def interrupted_stream(root, fail_after=6):
            seen_files = 0
            for item in walk.walk_tree(root):
                if isinstance(item, walk.FileEntry):
                    seen_files += 1
                    if seen_files > fail_after:
                        raise Boom("注入的扫描中断")
                yield item

        with self.assertRaises(Boom):
            self.scan(batch_size=3, entries=interrupted_stream(self.tree))

        self.assertEqual(self.manifest(), {1: "complete", 2: "active"})  # 半代被登记
        self.assertEqual(self.store.query_one(
            "SELECT COUNT(*) AS c FROM files")["c"], 10)
        self.assertTrue(all(row["generation"] == 1 for row in self.file_rows()))
        self.assertEqual(self.store.query_one(
            "SELECT COUNT(*) AS c FROM files_staging")["c"], 6)  # 两批残留 staging

        view = reader.IndexReader(self.store)       # 旧代仍完整可读
        self.assertEqual(view.latest_complete(), 1)
        self.assertEqual(view.count_files(), 10)
        self.assertEqual(len(list(view.iter_files(limit=4))), 10)
        self.assertEqual(view.page_files(generation=2).rows, [])  # 半代不进已发布快照

        third = self.scan(batch_size=3)             # 重扫接管
        self.assertEqual(third.generation, 3)
        self.assertEqual(self.manifest(), {1: "complete", 2: "abandoned", 3: "complete"})
        self.assertTrue(all(row["generation"] == 3 for row in self.file_rows()))
        self.assertEqual(view.count_files(), 10)
        self.assertEqual(self.store.query_one(
            "SELECT COUNT(*) AS c FROM files_staging")["c"], 0)


class ReaderTests(ScanTestBase):
    def test_keyset_pagination_and_zero_open(self):
        for index in range(7):
            self.write("f%d.txt" % index, b"x")
        self.scan()
        view = reader.IndexReader(self.store)
        counter = self.zero_open()
        pages, paths = [], []
        page = view.page_files(limit=3)
        while page.rows:
            pages.append(page)
            paths.extend(row["path"] for row in page.rows)
            self.assertIsNotNone(page.generation)
            if page.cursor is None:
                break
            page = view.page_files(after=page.cursor, limit=3)
        self.assertEqual(len(pages), 3)
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), 7)
        self.assertEqual(len(set(paths)), 7)
        self.assertEqual(view.count_files(), 7)     # manifest 物化计数
        self.assertEqual(len(list(view.iter_files(limit=5))), 7)
        self.assertEqual(counter.calls, [])         # reader 全程零 open

    def test_empty_index_has_no_complete_generation(self):
        view = reader.IndexReader(self.store)
        self.assertIsNone(view.latest_complete())
        self.assertEqual(view.count_files(), 0)
        self.assertEqual(view.generations(), [])
        with self.assertRaises(CompanionError):
            view.page_files()


class WriterEpochTests(ScanTestBase):
    def test_stale_writer_epoch_rejected_by_batch_transactions(self):
        self.write("a.txt", b"a")
        writer = db.acquire_writer(self.store)
        try:
            with self.store.transaction() as conn:   # 模拟另一协调者接管（epoch 前进）
                db.set_meta(conn, db.WRITER_EPOCH_KEY, writer.epoch + 1)
            with self.assertRaises(CompanionError):
                scanner.IndexScanner(self.store).scan(self.tree, writer)
        finally:
            writer.close()
        # 首个事务（含建表）整体回滚：没有任何索引写入落地。
        self.assertIsNone(self.store.query_one(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'files'"))


if __name__ == "__main__":
    unittest.main()

"""依赖闭包单测：import 抽取（绝对/相对/越根/解析失败）、unknown 边如实记录、
语法错误文件 skipped 不崩（IX04-08）、反向闭包 BFS 与 unknown 保守扩展
（C18/Z12/ST07）。
"""
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel.indexing import edges, modules, scanner
from agents_kernel.services import closure
from agents_kernel.storage import db
from agents_kernel.validation import CompanionError


class ClosureBase(unittest.TestCase):
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
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def scan(self):
        writer = db.acquire_writer(self.store)
        try:
            return scanner.IndexScanner(self.store).scan(self.tree, writer)
        finally:
            writer.close()

    def scan_and_build(self):
        writer = db.acquire_writer(self.store)
        try:
            result = scanner.IndexScanner(self.store).scan(self.tree, writer)
            edges.build_edges(self.store, writer, result.generation)
            modules.build_modules(self.store, writer, result.generation)
        finally:
            writer.close()
        return result.generation

    def edge_set(self):
        return {(row["from_module"], row["to_module_or_unknown"], row["kind"],
                 row["source_file"], row["line"])
                for row in self.store.query_all(
                    "SELECT * FROM edges")}


class EdgeExtractionTests(ClosureBase):
    def setUp(self):
        super().setUp()
        self.write("app/main.py",
                   "import lib.util\nimport os\nimport requests\n")
        self.write("lib/__init__.py", "")
        self.write("lib/util.py", "from lib import helper\n")
        self.write("lib/helper.py", "from .deep.deep import thing\n")
        self.write("lib/deep/deep.py",
                   "from ..util import fn\nfrom ...far import x\n"
                   "from ....absent import y\n")
        self.write("run.py", "import app.main\n")
        self.write("broken.py", "def oops(:\n")   # 语法错误：跳过不崩
        self.write("notes.txt", "not python\n")   # 非 .py：不参与
        self.generation = self.scan_and_build()

    def test_internal_unknown_and_relative_edges(self):
        self.assertEqual(self.edge_set(), {
            ("app", "lib", "internal", "app/main.py", 1),
            ("lib", "lib", "internal", "lib/util.py", 1),        # from lib import
            ("lib", "lib", "internal", "lib/helper.py", 1),      # from .deep.deep
            ("lib", "lib", "internal", "lib/deep/deep.py", 1),   # from ..util
            ("_root", "app", "internal", "run.py", 1),
            ("app", "os", "unknown", "app/main.py", 2),
            ("app", "requests", "unknown", "app/main.py", 3),
            ("lib", "...far.x", "unknown", "lib/deep/deep.py", 2),
            ("lib", "....absent.y", "unknown", "lib/deep/deep.py", 3),
        })

    def test_counts_and_skipped_account(self):
        row = self.store.query_one(
            """SELECT internal_edges, unknown_edges FROM (
                   SELECT COUNT(*) AS internal_edges FROM edges
                   WHERE kind = 'internal') a,
               (SELECT COUNT(*) AS unknown_edges FROM edges
                   WHERE kind = 'unknown') b""")
        self.assertEqual((row["internal_edges"], row["unknown_edges"]), (5, 4))
        self.assertEqual(self.store.query_one(
            "SELECT COUNT(*) AS c FROM edges_staging")["c"], 0)

    def test_build_result_reports_parsed_and_skipped(self):
        writer = db.acquire_writer(self.store)
        try:
            result = edges.build_edges(self.store, writer, self.generation)
        finally:
            writer.close()
        self.assertEqual(result.parsed_files, 7)  # 含 broken.py（解析失败也计入）
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0][0], "broken.py")
        self.assertIn("语法错误", result.skipped[0][1])


class ClosureTests(ClosureBase):
    def _build_chain_tree(self, with_dynamic=False):
        """全内边链：app → lib → core；可选 dyn 持有 unknown 出边。"""
        self.write("app/main.py", "import lib.util\n")
        self.write("lib/util.py", "import core.base\n")
        self.write("core/base.py", "VALUE = 1\n")
        if with_dynamic:
            self.write("dyn/d1.py", "import someext.thing\n")

    def test_reverse_closure_exact_without_unknown(self):
        self._build_chain_tree()
        self.scan_and_build()
        result = closure.affected_closure(self.store, ["core/base.py"])
        self.assertEqual(result.generation, 1)
        self.assertEqual(result.changed_modules, ("core",))
        self.assertEqual(result.closure, ("app", "core", "lib"))  # 含自身 + 传递
        self.assertFalse(result.conservative)
        mid = closure.affected_closure(self.store, ["lib/util.py"])
        self.assertEqual(mid.closure, ("app", "lib"))
        head = closure.affected_closure(self.store, ["app/main.py"])
        self.assertEqual(head.closure, ("app",))

    def test_unknown_edges_force_conservative_expansion(self):
        self._build_chain_tree(with_dynamic=True)
        self.scan_and_build()
        result = closure.affected_closure(self.store, ["core/base.py"])
        self.assertTrue(result.conservative)
        self.assertIn("dyn", result.closure)  # unknown 出边模块保守并入
        self.assertEqual(result.closure, ("app", "core", "dyn", "lib"))
        unknown_sources = {row["from_module"] for row in self.store.query_all(
            "SELECT from_module FROM edges WHERE kind = 'unknown'")}
        self.assertEqual(unknown_sources, {"dyn"})

    def test_changed_file_outside_index_still_gets_module(self):
        self._build_chain_tree()
        self.scan_and_build()
        result = closure.affected_closure(self.store, ["brandnew/f.py"])
        self.assertEqual(result.changed_modules, ("brandnew",))  # 纯函数推导
        self.assertEqual(result.closure, ("brandnew",))

    def test_input_guards_and_missing_dependency_data(self):
        self._build_chain_tree()
        self.scan()  # 只扫描：edges 表尚不存在
        result = closure.affected_closure(self.store, ["core/base.py"])
        self.assertEqual(result.closure, ("core",))   # 如实：无依赖数据
        self.assertFalse(result.conservative)
        with self.assertRaises(CompanionError):
            closure.affected_closure(self.store, [])
        with self.assertRaises(CompanionError):
            closure.affected_closure(self.store, "core/base.py")

    def test_closure_binds_explicit_generation(self):
        self._build_chain_tree()
        writer = db.acquire_writer(self.store)
        try:
            first = scanner.IndexScanner(self.store).scan(self.tree, writer)
            edges.build_edges(self.store, writer, first.generation)
            (self.tree / "lib" / "util.py").write_bytes(b"VALUE = 2\n")  # 边已失真
            second = scanner.IndexScanner(self.store).scan(self.tree, writer)
        finally:
            writer.close()
        old = closure.affected_closure(self.store, ["core/base.py"],
                                       generation=first.generation)
        self.assertEqual(old.closure, ("app", "core", "lib"))  # 世代 1 的快照
        current = closure.affected_closure(self.store, ["core/base.py"],
                                           generation=second.generation)
        self.assertEqual(current.closure, ("core",))  # 世代 2 尚未建边


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""P3-05：功能级证据失效与有限报告视图（C08 后半/C14/ST07/CV06 尾）。

覆盖：闭包内变更→stale 且原因列出具体文件（含真实 affected_closure 管线）、
闭包外变更→current、映射缺失→unknown 保守、显式映射覆盖、unknown 保守闭包
传导、release/集成级证据不参与功能级失效（恒全局回归）、status_page 集成
（evidence_status 标注且底层视图与存储态未动）、summarize_for_report 不含证据原文。
"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel.indexing import edges, scanner
from agents_kernel.presentation import status_view
from agents_kernel.services import closure, evidence_freshness
from agents_kernel.services.closure import ClosureResult
from agents_kernel.services.evidence_freshness import EvidenceFreshness
from agents_kernel.storage import db
from agents_kernel.validation import CompanionError

SCRIPTS = REPO_ROOT / "dev-companion" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from core import Project

# 链式闭包：app → lib → core（与 affected_closure 的 BFS 语义对齐）
CHAIN = ClosureResult(generation=1, changed_modules=("core",),
                      closure=("app", "core", "lib"), conservative=False)


def feature(identifier, allowed_paths=None):
    item = {"id": identifier}
    if allowed_paths is not None:
        item["allowed_paths"] = allowed_paths
    return item


class AssessTests(unittest.TestCase):
    def setUp(self):
        self.fresh = EvidenceFreshness()

    def test_change_inside_closure_marks_stale_with_reason_files(self):
        features = [feature("ui", ["app/main.py"]), feature("lib", ["lib/util.py"]),
                    feature("far", ["other/thing.py"])]
        result = self.fresh.assess(["core/base.py"], CHAIN, features)
        self.assertEqual(result["ui"].status, "stale")      # app 依赖 core：传导命中
        self.assertTrue(result["ui"].transitive)
        self.assertEqual(result["ui"].stale_files, ("core/base.py",))
        self.assertEqual(result["ui"].hit_modules, ("app",))
        self.assertIn("core/base.py", result["ui"].reason())
        self.assertIn("传导", result["ui"].reason())
        self.assertEqual(result["lib"].status, "stale")
        self.assertEqual(result["far"].status, "current")   # 闭包外变更不抹掉验收
        self.assertEqual(result["far"].reason(), "")

    def test_direct_hit_lists_changed_file_inside_feature_modules(self):
        mixed = ClosureResult(1, ("app", "core"), ("app", "core", "lib"), False)
        result = self.fresh.assess(["core/base.py", "app/main.py"], mixed,
                                   [feature("ui", ["app/main.py", "app/extra.py"])])
        record = result["ui"]
        self.assertEqual(record.status, "stale")
        self.assertFalse(record.transitive)
        self.assertEqual(record.stale_files, ("app/main.py",))  # 直接落入功能模块
        self.assertEqual(record.hit_modules, ("app",))
        self.assertIn("app/main.py", record.reason())

    def test_conservative_closure_marks_reason(self):
        conservative = ClosureResult(1, ("core",), ("app", "core", "dyn", "lib"), True)
        result = self.fresh.assess(["core/base.py"], conservative,
                                   [feature("ui", ["app/main.py"])])
        self.assertEqual(result["ui"].status, "stale")
        self.assertTrue(result["ui"].conservative)
        self.assertIn("保守", result["ui"].reason())

    def test_missing_mapping_is_unknown_conservative(self):
        features = [{"id": "no_paths"}, feature("empty", []), feature("ok", ["z/ok.py"])]
        result = self.fresh.assess(["a/x.py"],
                                   ClosureResult(0, ("a",), ("a",), False), features)
        for unknown in ("no_paths", "empty"):
            self.assertEqual(result[unknown].status, "unknown", "映射缺失必须保守标未知")
            self.assertIn("保守", result[unknown].reason())
        self.assertEqual(result["ok"].status, "current")

    def test_explicit_mapping_overrides_and_partial_falls_back(self):
        features = [feature("x", ["a/x.py"]), feature("y", ["b/y.py"])]
        result = self.fresh.assess(["m/m.py"],
                                   ClosureResult(0, ("m",), ("m", "b"), False),
                                   features, feature_modules={"x": ["m"]})
        self.assertEqual(result["x"].status, "stale")       # 显式映射 x→m，传导命中
        self.assertEqual(result["x"].hit_modules, ("m",))
        self.assertEqual(result["y"].status, "stale")       # 未覆盖者回落 allowed_paths→b
        self.assertEqual(result["y"].hit_modules, ("b",))

    def test_explicit_empty_mapping_stays_unknown(self):
        result = self.fresh.assess(["a/x.py"], ClosureResult(0, ("a",), ("a",), False),
                                   [feature("x", ["a/x.py"])],
                                   feature_modules={"x": []})
        self.assertEqual(result["x"].status, "unknown", "显式空映射同样是映射缺失")

    def test_inputs_are_never_mutated(self):
        features = [dict(feature("ui", ["app/main.py"]), verification={"evidence": "全文"})]
        chain = copy.deepcopy(CHAIN)
        self.fresh.assess(["core/base.py"], chain, features)
        self.assertEqual(features, [{"id": "ui", "allowed_paths": ["app/main.py"],
                                     "verification": {"evidence": "全文"}}])
        self.assertEqual(chain, CHAIN)

    def test_release_and_integration_evidence_out_of_scope(self):
        # 集成/版本级证据不参与功能级失效：输出只含功能 id，不含任何 release 条目，
        # 功能项上的集成记录原样保留（恒由全局回归重新核验）。
        features = [dict(feature("ui", ["app/main.py"]),
                         integration={"passed": True}, release=None)]
        result = self.fresh.assess(["core/base.py"], CHAIN, features)
        self.assertEqual(set(result), {"ui"})
        self.assertEqual(features[0]["integration"], {"passed": True})
        self.assertIsNone(features[0]["release"])

    def test_input_guards(self):
        features = [feature("ui", ["app/main.py"])]
        with self.assertRaises(CompanionError):
            self.fresh.assess([], CHAIN, features)
        with self.assertRaises(CompanionError):
            self.fresh.assess("core/base.py", CHAIN, features)
        with self.assertRaises(CompanionError):
            self.fresh.assess(["core/base.py"], {"closure": ("app",)}, features)
        with self.assertRaises(CompanionError):
            self.fresh.assess(["zzz/f.py"], CHAIN, features)  # 变更集与闭包不匹配
        with self.assertRaises(CompanionError):
            self.fresh.assess(["core/base.py"], CHAIN, [{"title": "无 id"}])
        with self.assertRaises(CompanionError):
            self.fresh.assess(["core/base.py"], CHAIN, features,
                              feature_modules={"ui": "app"})  # 映射必须是集合


class ClosurePipelineTests(unittest.TestCase):
    """接真实 affected_closure 管线：扫描→建边→闭包→新鲜度。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name) / "tree"
        self.tree.mkdir()
        (self.tree / "app").mkdir()
        (self.tree / "lib").mkdir()
        (self.tree / "core").mkdir()
        (self.tree / "app" / "main.py").write_text("import lib.util\n", encoding="utf-8")
        (self.tree / "lib" / "util.py").write_text("import core.base\n", encoding="utf-8")
        (self.tree / "core" / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.store = db.Store(Path(self.temp.name) / "facts.sqlite")
        self.store.open()
        self.addCleanup(self.store.close)
        writer = db.acquire_writer(self.store)
        try:
            result = scanner.IndexScanner(self.store).scan(self.tree, writer)
            edges.build_edges(self.store, writer, result.generation)
        finally:
            writer.close()
        self.closure = closure.affected_closure(self.store, ["core/base.py"])

    def test_real_pipeline_direct_and_transitive(self):
        result = EvidenceFreshness().assess(
            ["core/base.py"], self.closure,
            [feature("底层", ["core/base.py"]), feature("界面", ["app/main.py"]),
             feature("文档", ["docs/guide.md"])])
        self.assertEqual(result["底层"].status, "stale")
        self.assertFalse(result["底层"].transitive)
        self.assertEqual(result["底层"].stale_files, ("core/base.py",))
        self.assertEqual(result["界面"].status, "stale")
        self.assertTrue(result["界面"].transitive)
        self.assertIn("core/base.py", result["界面"].reason())
        self.assertEqual(result["文档"].status, "current")


def scope_feature(identifier, allowed_paths):
    return {"id": identifier, "title": "功能" + identifier,
            "acceptance_criteria": ["可用"], "allowed_paths": allowed_paths,
            "requires_user_acceptance": False,
            "check_commands": [[sys.executable, "-c", "assert True"]]}


class StatusPageEvidenceTests(unittest.TestCase):
    """status_page 集成：标注出现，底层视图与存储中的验收状态原样未动（ST07）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = Project(self.root)
        self.scope = {"title": "失效项目", "goal": "算总金额", "audience": "自己", "scenario": "记账",
                      "out_of_scope": [], "assumptions": [],
                      "features": [scope_feature("sum", ["app/ledger.py"]),
                                   scope_feature("misc", ["lib/misc.py"])]}
        self.project.init(self.scope)
        self.project.confirm(1)
        packet = self.project.packet("sum")
        (self.root / "app").mkdir()
        (self.root / "app" / "ledger.py").write_text("def total(v): return sum(v)\n",
                                                     encoding="utf-8")
        self.project.receipt({"feature_id": "sum", "run_id": packet["run_id"],
                              "scope_version": packet["scope_version"], "status": "implemented",
                              "summary": "合计可用", "changed_files": ["app/ledger.py"],
                              "evidence_files": []})
        self.project.check("sum")
        self.project.accept("sum", "真实检查通过")
        self.view_before = self.project.status()
        self.release_before = copy.deepcopy(self.view_before["release"])
        self.state_before = (self.root / ".dev-companion" / "state.json").read_text()

    def assess(self):
        return EvidenceFreshness().assess(
            ["app/ledger.py"],
            ClosureResult(1, ("app",), ("app",), False),
            self.view_before["features"])

    def test_stale_annotated_without_touching_stored_state(self):
        page = status_view.build_status_page(self.project, evidence_freshness=self.assess())
        by_id = {item["id"]: item for item in page["items"]}
        self.assertEqual(by_id["sum"]["evidence_status"], "stale")
        self.assertIn("app/ledger.py", by_id["sum"]["evidence_stale_reason"])
        self.assertEqual(by_id["misc"]["evidence_status"], "current")
        self.assertNotIn("evidence_stale_reason", by_id["misc"])
        # 标注不改任务状态面孔的来源——底层视图功能项未被改写
        for item in self.view_before["features"]:
            self.assertNotIn("evidence_status", item)
        # 存储态未动：state.json 原文与验收状态保持 accepted
        self.assertEqual((self.root / ".dev-companion" / "state.json").read_text(),
                         self.state_before)
        view_after = self.project.status()
        self.assertEqual({f["id"]: f["status"] for f in view_after["features"]},
                         {"sum": "accepted", "misc": "pending"})
        self.assertEqual(view_after["release"], self.release_before)

    def test_without_param_fields_stay_absent(self):
        page = status_view.build_status_page(self.project)
        for item in page["items"]:
            self.assertNotIn("evidence_status", item)
            self.assertNotIn("evidence_stale_reason", item)

    def test_release_semantics_independent_of_feature_invalidation(self):
        # release/集成证据恒全局回归：功能级失效结论翻不翻都不改变 release 视图。
        features = [dict(f, allowed_paths=[]) for f in self.view_before["features"]]
        all_unknown = EvidenceFreshness().assess(["app/ledger.py"],
                                                 ClosureResult(1, ("app",), ("app",), False),
                                                 features)
        page_unknown = status_view.build_status_page(self.project,
                                                     evidence_freshness=all_unknown)
        page_stale = status_view.build_status_page(self.project,
                                                   evidence_freshness=self.assess())
        self.assertTrue(all(item["evidence_status"] == "unknown"
                            for item in page_unknown["items"]))
        view_after = self.project.status()
        self.assertEqual(view_after["release"], self.release_before)
        self.assertEqual(view_after["counts"], self.view_before["counts"])


class SummarizeForReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Project(Path(self.temp.name))
        self.scope = {"title": "报告项目", "goal": "算总金额", "audience": "自己", "scenario": "记账",
                      "out_of_scope": [], "assumptions": [],
                      "features": [scope_feature("sum", ["app/ledger.py"]),
                                   scope_feature("misc", ["lib/misc.py"])]}
        self.project.init(self.scope)
        self.project.confirm(1)
        self.page = status_view.build_status_page(
            self.project, evidence_freshness=EvidenceFreshness().assess(
                ["app/ledger.py"], ClosureResult(1, ("app",), ("app",), False),
                self.project.status()["features"]))

    def test_rows_are_minimal_and_carry_no_evidence_text(self):
        summary = status_view.summarize_for_report(self.page)
        self.assertEqual(summary["counts"], self.page["counts"])
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["rows"], [
            {"id": "sum", "status": "pending", "evidence_status": "stale"},
            {"id": "misc", "status": "pending", "evidence_status": "current"}])
        text = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("20+30=50", text)          # 验收条件原文不进报告
        self.assertNotIn("allowed_paths", text)
        self.assertNotIn("check_commands", text)
        self.assertLess(len(text), len(json.dumps(self.page, ensure_ascii=False)))
        self.assertIn("revision=", summary["evidence_pointer"])
        self.assertIn(str(self.page["revision"]), summary["evidence_pointer"])

    def test_evidence_pointer_points_at_ledger_not_report(self):
        summary = status_view.summarize_for_report(self.page)
        self.assertIn("原文不在本报告中", summary["evidence_pointer"])
        self.assertIn("台账", summary["evidence_pointer"])


if __name__ == "__main__":
    unittest.main()

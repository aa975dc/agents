import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dev-companion" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from core import CompanionError, Project
from journey import Journey


class JourneyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = Project(self.root)
        self.journey = Journey(self.project)
        (self.root / "prototype.md").write_text("输入页：金额20,30 → 计算 → 合计50；失败时可修改重试")
        self.product = {"title": "记账工具", "goal": "知道支出合计", "audience": "自己", "scenario": "每天记账",
                        "features": [{"id": "sum", "title": "合计", "acceptance_criteria": ["20+30=50"]}]}
        self.scope = copy.deepcopy(self.product)
        self.scope["features"][0].update(allowed_paths=["ledger.py"], check_commands=[[sys.executable, "test_ledger.py"]])
        self.payloads = {
            "concept": {"summary": "记录日常支出", "details": {"audience": "自己", "problem": "不清楚花了多少钱", "scenario": "每天记账", "outcome": "看到合计"}},
            "requirements": {"summary": "明确首版需要", "details": {"constraints": "本地使用", "priorities": "先做合计"}},
            "product": {"summary": "首版方案", "details": {"positioning": "个人记账"}, "scope": self.product},
            "flow": {"summary": "核心流程", "details": {"main_path": "输入金额后查看合计", "alternatives": "无效输入提示修改", "data_changes": "保存金额"}, "feature_ids": ["sum"]},
            "prototype": {"summary": "页面走查", "details": {"screens": "输入页", "states": "输入和结果", "walkthrough": "代理按20和30走查得到50；用户尚未试用"}, "feature_ids": ["sum"], "artifacts": ["prototype.md"]},
            "technical": {"summary": "本地实现", "details": {"architecture": "Python 本地程序", "data_model": "金额列表", "release_target": "本地安装包"}, "scope": self.scope,
                          "interfaces": [{"feature_id": "sum", "kind": "local", "contract": "total(list[number])->number", "check_commands": [[sys.executable, "test_ledger.py"]]}]},
        }

    def tearDown(self):
        self.temp.cleanup()

    def save(self, stage, complete=True, **kwargs):
        status = self.journey.status()
        return self.journey.save(stage, self.payloads[stage], status["revision"] if status else 0,
                                 complete=complete, user_confirmed=stage == "product", **kwargs)

    def ready(self):
        for stage in self.payloads:
            self.save(stage)

    def test_empty_status_is_read_only_and_legacy_scope_is_allowed(self):
        self.assertIsNone(self.journey.status())
        self.assertIsNone(self.journey.context())
        self.journey.require_ready(self.scope)
        self.assertFalse(self.project.data.exists())
        self.project.init(self.scope)
        before = self.project.state_path.read_bytes()
        self.journey.require_ready(self.scope)
        self.assertEqual(before, self.project.state_path.read_bytes())
        self.assertFalse((self.project.data / "journey.json").exists())

    def test_early_draft_does_not_require_code_paths_and_survives_restart(self):
        raw = copy.deepcopy(self.payloads["concept"])
        raw["open_questions"] = ["需要手机使用吗？"]
        raw["decisions"] = [{"question": "先做什么？", "answer": "推荐本地记账", "source": "recommendation"}]
        self.journey.save("concept", raw, 0)
        status = Journey(Project(self.root)).status()
        self.assertEqual(status["revision"], 1)
        self.assertEqual(status["current_stage"], "concept")
        self.assertEqual(status["records"]["concept"]["decisions"], raw["decisions"])
        self.assertFalse(self.project.state_path.exists())

    def test_partial_drafts_preserve_unknowns_without_fabricating_values(self):
        self.journey.save("concept", {"summary": "想做记账", "details": {}}, 0)
        self.assertEqual(self.journey.status()["records"]["concept"]["details"], {})
        self.journey.save("product", {"summary": "暂定记账工具", "details": {}, "scope": {"title": "记账"}}, 1)
        self.journey.save("technical", {"summary": "方案还在选择", "details": {}, "interfaces": []}, 2)
        records = Journey(Project(self.root)).context()["records"]
        self.assertEqual(records["product"]["scope"], {"title": "记账"})
        self.assertNotIn("scope", records["technical"])
        self.assertEqual(records["technical"]["interfaces"], [])
        with self.assertRaises(CompanionError):
            self.journey.require_ready(self.scope)
        with self.assertRaises(CompanionError):
            self.journey.save("concept", {"summary": "想做记账", "details": {}}, 3, complete=True)

    def test_partial_technical_scope_keeps_only_known_implementation_fields(self):
        raw = {"summary": "确定一个功能", "details": {"architecture": "Python"},
               "scope": {"features": [{"id": "sum", "allowed_paths": [], "check_commands": []}]},
               "interfaces": [{"kind": "local", "feature_id": "sum"}]}
        self.journey.save("technical", raw, 0)
        record = self.journey.status()["records"]["technical"]
        self.assertEqual(record["scope"], raw["scope"])
        self.assertEqual(record["interfaces"], raw["interfaces"])
        with self.assertRaises(CompanionError):
            self.journey.save("technical", raw, 1, complete=True)

    def test_complete_requires_previous_stages_and_resolved_questions(self):
        with self.assertRaises(CompanionError):
            self.save("requirements")
        raw = copy.deepcopy(self.payloads["concept"])
        raw["open_questions"] = ["发布哪里？"]
        with self.assertRaises(CompanionError):
            self.journey.save("concept", raw, 0, complete=True)
        self.save("concept")
        self.save("requirements")
        with self.assertRaises(CompanionError):
            self.journey.save("product", self.payloads["product"], 2, complete=True)
        self.save("product")
        self.assertEqual(self.journey.status()["current_stage"], "flow")

    def test_all_stages_complete_with_scope_and_context(self):
        self.ready()
        self.journey.require_ready(self.scope)
        status = self.journey.status()
        self.assertTrue(status["complete"])
        self.assertEqual(status["current_stage"], "implementation")
        self.assertEqual(status["stale_stages"], [])
        context = self.journey.context()
        self.assertEqual(context["revision"], 6)
        self.assertEqual(len(context["fingerprint"]), 64)
        self.assertEqual(context, Journey(Project(self.root)).context())
        self.assertFalse(status["records"]["prototype"]["user_confirmed"])

    def test_ready_rejects_scope_mismatch_and_missing_technical(self):
        self.save("concept")
        with self.assertRaises(CompanionError):
            self.journey.require_ready(self.scope)
        for stage in list(self.payloads)[1:]:
            self.save(stage)
        changed = copy.deepcopy(self.scope)
        changed["features"][0]["allowed_paths"].append("other.py")
        with self.assertRaises(CompanionError):
            self.journey.require_ready(changed)

    def test_technical_semantics_and_interface_coverage_must_match_product(self):
        for stage in list(self.payloads)[:-1]:
            self.save(stage)
        bad = copy.deepcopy(self.payloads["technical"])
        bad["scope"]["features"][0]["acceptance_criteria"] = ["20+30=40"]
        with self.assertRaises(CompanionError):
            self.journey.save("technical", bad, 5, complete=True)
        bad = copy.deepcopy(self.payloads["technical"])
        bad["interfaces"][0]["feature_id"] = "unknown"
        with self.assertRaises(CompanionError):
            self.journey.save("technical", bad, 5, complete=True)
        bad = copy.deepcopy(self.payloads["technical"])
        bad["scope"]["features"][0]["check_commands"] = []
        with self.assertRaises(CompanionError):
            self.journey.save("technical", bad, 5, complete=True)
        bad = copy.deepcopy(self.payloads["technical"])
        bad["interfaces"][0]["check_commands"] = []
        with self.assertRaises(CompanionError):
            self.journey.save("technical", bad, 5, complete=True)

    def test_flow_and_prototype_cover_product_features_exactly(self):
        for stage in ("concept", "requirements", "product"):
            self.save(stage)
        for stage in ("flow", "prototype"):
            for feature_ids in ([], ["unknown"], ["sum", "sum"]):
                raw = copy.deepcopy(self.payloads[stage])
                raw["feature_ids"] = feature_ids
                with self.assertRaises(CompanionError):
                    self.journey.save(stage, raw, 3, complete=True)

    def test_prototype_completion_requires_a_real_artifact(self):
        for stage in ("concept", "requirements", "product", "flow"):
            self.save(stage)
        raw = {**self.payloads["prototype"], "artifacts": []}
        self.journey.save("prototype", raw, 4)
        with self.assertRaises(CompanionError):
            self.journey.save("prototype", raw, 5, complete=True)
        raw["artifacts"] = ["missing-prototype.md"]
        with self.assertRaises(CompanionError):
            self.journey.save("prototype", raw, 5, complete=True)

    def test_revision_conflict_preserves_file_and_history(self):
        self.save("concept")
        path = self.project.data / "journey.json"
        before = path.read_bytes()
        with self.assertRaises(CompanionError):
            self.journey.save("concept", self.payloads["concept"], 0)
        self.assertEqual(before, path.read_bytes())
        for revision in (True, -1, "1"):
            with self.assertRaises(CompanionError):
                self.journey.save("concept", self.payloads["concept"], revision)
        self.save("concept", complete=False)
        stored = json.loads(path.read_text())
        self.assertEqual(stored["revision"], 2)
        self.assertTrue(stored["history"][-1]["previous_records"]["concept"]["complete"])

    def test_upstream_rewrite_invalidates_downstream_until_resaved(self):
        self.ready()
        fingerprint = self.journey.context()["fingerprint"]
        self.save("concept")
        status = self.journey.status()
        self.assertEqual(status["current_stage"], "requirements")
        self.assertEqual(status["stale_stages"], list(self.payloads)[1:])
        self.assertNotEqual(fingerprint, self.journey.context()["fingerprint"])
        with self.assertRaises(CompanionError):
            self.journey.require_ready(self.scope)
        with self.assertRaises(CompanionError):
            self.save("technical")
        for stage in list(self.payloads)[1:]:
            self.save(stage)
        self.journey.require_ready(self.scope)

    def test_artifact_change_invalidates_stage_and_downstream_without_writing(self):
        artifact = self.root / "concept.md"
        artifact.write_text("初版概念")
        self.payloads["concept"]["artifacts"] = ["concept.md"]
        self.ready()
        before = (self.project.data / "journey.json").read_bytes()
        context = self.journey.context()
        artifact.write_text("产品概念已调整")
        status = self.journey.status()
        self.assertEqual(status["stale_stages"], list(self.payloads))
        self.assertNotEqual(context["fingerprint"], self.journey.context()["fingerprint"])
        self.assertEqual(before, (self.project.data / "journey.json").read_bytes())
        with self.assertRaises(CompanionError):
            self.journey.require_ready(self.scope)
        artifact.unlink()
        self.assertIn("concept", self.journey.status()["stale_stages"])

    def test_invalid_input_does_not_replace_good_record(self):
        self.save("concept")
        path = self.project.data / "journey.json"
        before = path.read_bytes()
        for stage, raw in (("unknown", self.payloads["concept"]), ("concept", []),
                           ("concept", {"summary": "有说明", "details": {"audience": 0}}),
                           ("concept", {**self.payloads["concept"], "decisions": [{"question": "x", "answer": "y", "source": "invented"}]})):
            with self.assertRaises(CompanionError):
                self.journey.save(stage, raw, 1)
        self.assertEqual(before, path.read_bytes())

    def test_product_and_interface_validation_rejects_malformed_shapes(self):
        invalid_product = (
            None,
            {"title": "记账", "goal": "合计", "audience": "自己", "scenario": "每天", "features": []},
            {**self.product, "features": ["sum"]},
            {**self.product, "features": [{**self.product["features"][0], "id": "bad id"}]},
            {**self.product, "features": [{**self.product["features"][0], "requires_user_acceptance": "yes"}]},
        )
        for product in invalid_product:
            raw = {**self.payloads["product"], "scope": product}
            with self.subTest(product=product), self.assertRaises(CompanionError):
                self.journey.save("product", raw, 0, complete=True, user_confirmed=True)
        for interfaces in (None, ["local"], [{"feature_id": "sum", "kind": "queue", "contract": "x", "check_commands": [["ok"]]}]):
            raw = {**self.payloads["technical"], "interfaces": interfaces}
            with self.subTest(interfaces=interfaces), self.assertRaises(CompanionError):
                self.journey.save("technical", raw, 0)

    def test_stage_metadata_and_completion_flags_reject_wrong_shapes(self):
        cases = [
            {**self.payloads["concept"], "artifacts": ["prototype.md", "prototype.md"]},
            {**self.payloads["concept"], "decisions": {}},
            {**self.payloads["technical"], "scope": {"features": [{"id": "sum", "check_commands": "python test.py"}]}},
        ]
        for raw in cases:
            stage = "technical" if "scope" in raw else "concept"
            with self.subTest(stage=stage), self.assertRaises(CompanionError):
                self.journey.save(stage, raw, 0)
        for complete, confirmed in (("yes", False), (False, "yes")):
            with self.subTest(complete=complete, confirmed=confirmed), self.assertRaises(CompanionError):
                self.journey.save("concept", self.payloads["concept"], 0,
                                  complete=complete, user_confirmed=confirmed)

    def test_complete_downstream_stage_requires_completed_product_scope(self):
        self.save("concept")
        self.save("requirements")
        with self.assertRaisesRegex(CompanionError, "产品方案"):
            self.journey.save("flow", self.payloads["flow"], 2, complete=True)

    def test_corrupt_stage_content_and_hash_metadata_are_rejected(self):
        self.save("concept")
        path = self.project.data / "journey.json"
        original = json.loads(path.read_text())
        corruptions = []
        changed = copy.deepcopy(original)
        changed["records"]["concept"]["summary"] = " "
        corruptions.append(changed)
        changed = copy.deepcopy(original)
        changed["records"]["concept"]["artifact_hashes"] = {"ghost.md": "0" * 64}
        corruptions.append(changed)
        changed = copy.deepcopy(original)
        changed["records"]["concept"]["complete"] = True
        changed["records"]["concept"]["open_questions"] = ["仍未决定"]
        corruptions.append(changed)
        changed = copy.deepcopy(original)
        changed["records"]["concept"]["stale"] = "no"
        corruptions.append(changed)
        changed = copy.deepcopy(original)
        changed["records"]["concept"]["details"]["unexpected"] = "not canonical"
        corruptions.append(changed)
        for changed in corruptions:
            path.write_text(json.dumps(changed))
            with self.subTest(changed=changed), self.assertRaises(CompanionError):
                self.journey.status()

    def test_artifact_read_error_and_concurrent_change_fail_closed(self):
        artifact = self.root / "artifact.md"
        artifact.write_text("before")
        with mock.patch.object(Path, "read_bytes", side_effect=OSError("read failed")):
            with self.assertRaisesRegex(CompanionError, "无法读取"):
                self.journey._artifact_hashes(["artifact.md"], exists=True)
        original_read = Path.read_bytes

        def mutate(path):
            content = original_read(path)
            path.write_text("after")
            return content

        artifact.write_text("before")
        with mock.patch.object(Path, "read_bytes", autospec=True, side_effect=mutate):
            with self.assertRaisesRegex(CompanionError, "正在变化"):
                self.journey._artifact_hashes(["artifact.md"], exists=True)

    def test_shared_lock_and_running_task_reject_write(self):
        with self.project.locked():
            with self.assertRaises(CompanionError):
                self.save("concept")
        self.project.init(self.scope)
        self.project.confirm(1)
        self.project.packet("sum")
        with self.assertRaises(CompanionError):
            self.save("concept")
        self.assertIsNone(self.journey.status())

    def test_symlink_and_internal_artifacts_are_rejected(self):
        target = self.root / "real.md"
        target.write_text("说明")
        (self.root / "link.md").symlink_to(target)
        self.project.data.mkdir()
        (self.project.data / "inputs").mkdir()
        (self.project.data / "inputs" / "raw.json").write_text("{}")
        for name in ("link.md", ".dev-companion/inputs/raw.json", "../outside.md"):
            raw = {**self.payloads["concept"], "artifacts": [name]}
            with self.assertRaises(CompanionError):
                self.journey.save("concept", raw, 0)
        path = self.project.data / "journey.json"
        path.symlink_to(target)
        with self.assertRaises(CompanionError):
            self.journey.status()
        with self.assertRaises(CompanionError):
            self.save("concept")
        self.assertEqual(target.read_text(), "说明")

    def test_cross_project_and_unknown_schema_rejected(self):
        self.save("concept")
        path = self.project.data / "journey.json"
        original = json.loads(path.read_text())
        for key, value in (("project", "/another/project"), ("schema_version", 99), ("revision", True)):
            changed = {**original, key: value}
            path.write_text(json.dumps(changed))
            with self.assertRaises(CompanionError):
                self.journey.status()

    def test_atomic_failure_preserves_previous_record(self):
        self.save("concept")
        path = self.project.data / "journey.json"
        before = path.read_bytes()
        with mock.patch("journey.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(CompanionError):
                self.save("requirements")
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(list(self.project.data.glob("journey-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()

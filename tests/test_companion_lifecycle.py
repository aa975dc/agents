import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dev-companion" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from core import CompanionError, Project, render_html, render_markdown
from journey import Journey


def plan(project, scope):
    (project.root / "prototype.md").write_text("命令行原型：输入 [20, 30]，输出 50；负数返回错误。\n")
    journey = Journey(project)
    product = copy.deepcopy(scope)
    for feature in product["features"]:
        feature.pop("allowed_paths")
        feature.pop("check_commands")
    ids = [f["id"] for f in product["features"]]
    stages = [
        ("concept", {"details": {"audience": "自己", "problem": "需要汇总", "scenario": "录入开支", "outcome": "看到合计"}}),
        ("requirements", {"details": {"constraints": "本地使用", "priorities": "先实现合计"}}),
        ("product", {"details": {"positioning": "本地记账"}, "scope": product}),
        ("flow", {"details": {"main_path": "输入20和30得到50", "alternatives": "拒绝负数", "data_changes": "本次不持久化"}, "feature_ids": ids}),
        ("prototype", {"details": {"screens": "命令行输入", "states": "成功与无效输入", "walkthrough": "命令行样例20+30=50"}, "feature_ids": ids, "artifacts": ["prototype.md"]}),
        ("technical", {"details": {"architecture": "Python本地模块", "data_model": "金额列表", "release_target": "本地演示"}, "scope": scope,
                       "interfaces": [{"feature_id": f["id"], "kind": "local", "contract": "金额列表返回总和",
                                       "check_commands": f["check_commands"]} for f in scope["features"]]})]
    for stage, raw in stages:
        raw.update(summary=stage + "成果", open_questions=[], decisions=[])
        raw.setdefault("artifacts", [])
        revision = (journey.status() or {}).get("revision", 0)
        journey.save(stage, raw, revision, complete=True, user_confirmed=True)
    return journey


class LifecycleIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = Project(self.root)
        self.scope = {"title": "记账", "goal": "汇总", "audience": "自己", "scenario": "录入开支",
                      "out_of_scope": [], "assumptions": [], "features": [{
                          "id": "sum", "title": "合计", "acceptance_criteria": ["20+30=50"],
                          "allowed_paths": ["ledger.py"], "requires_user_acceptance": False,
                          "check_commands": [[sys.executable, "-c", "from ledger import total; assert total([20,30]) == 50"]]}]}

    def implement(self):
        self.journey = plan(self.project, self.scope)
        self.project.init(self.scope)
        self.project.confirm(1)
        packet = self.project.packet("sum")
        (self.root / "ledger.py").write_text("def total(values): return sum(values)\n")
        self.project.receipt({"feature_id": "sum", "scope_version": 1, "run_id": packet["run_id"],
                              "status": "implemented", "summary": "实现合计", "changed_files": ["ledger.py"], "evidence_files": []})
        return packet

    def test_planning_only_status_has_no_fabricated_progress(self):
        journey = Journey(self.project)
        journey.save("concept", {"summary": "想做记账", "details": {}}, 0)
        view = self.project.status()
        self.assertIsNone(view["overall_percent"])
        self.assertEqual(view["current_stage"], "concept")
        self.assertFalse(self.project.state_path.exists())
        self.assertIn("概念", render_markdown(view))
        self.assertIn("概念", render_html(view))

    def test_task_handoff_and_both_checks_required(self):
        packet = self.implement()
        self.assertTrue(packet["planning_context"]["fingerprint"])
        self.assertEqual(packet["interfaces"][0]["feature_id"], "sum")
        self.project.check("sum")
        with self.assertRaises(CompanionError):
            self.project.accept("sum", "功能已测")
        self.assertTrue(self.project.check("sum", kind="integration")["passed"])
        self.project.accept("sum", "功能与模块联调通过")
        view = Project(self.root).status(include_release=False)
        self.assertEqual(view["overall_percent"], 100)
        self.assertEqual(view["current_stage"], "release_preparation")
        self.assertEqual(view["publication"], "未核验发布状态")

    def test_planning_change_invalidates_accepted_evidence(self):
        self.implement()
        self.project.check("sum")
        self.project.check("sum", kind="integration")
        self.project.accept("sum", "检查通过")
        record = copy.deepcopy(self.journey.context()["records"]["concept"])
        record["summary"] = "改为跨设备记账"
        self.journey.save("concept", record, self.journey.status()["revision"])
        view = self.project.status()
        self.assertEqual(view["overall_percent"], 0)
        self.assertTrue(view["features"][0]["evidence_stale"])
        with self.assertRaises(CompanionError):
            self.project.packet("sum")

    def test_feedback_cannot_leave_old_acceptance_usable(self):
        self.implement()
        self.project.check("sum")
        self.project.check("sum", kind="integration")
        self.project.accept("sum", "检查通过")
        self.project.feedback("sum", "defect", "负数未拒绝")
        self.assertEqual(self.project.status()["overall_percent"], 0)
        with self.assertRaises(CompanionError):
            self.project.accept("sum", "继续沿用旧检查")
        self.assertEqual(self.project.status()["features"][0]["feedback"][-1]["kind"], "defect")

    def test_requirement_feedback_requires_a_new_scope(self):
        self.implement()
        self.project.feedback("sum", "requirement", "还需要云同步")
        self.project.feedback("sum", "defect", "另有计算错误")
        with self.assertRaises(CompanionError):
            self.project.packet("sum")

    def test_noop_or_title_change_cannot_resolve_requirement_feedback(self):
        self.implement()
        self.project.feedback("sum", "requirement", "还需要云同步")
        self.project.revise(self.scope, self.project.load()["revision"])
        self.project.confirm(self.project.load()["revision"])
        with self.assertRaisesRegex(CompanionError, "需求反馈"):
            self.project.packet("sum")

    def test_meaningful_scope_change_resolves_feedback_and_keeps_history(self):
        self.project.init(self.scope)
        self.project.confirm(1)
        self.project.feedback("sum", "requirement", "要求增加云同步")
        changed = copy.deepcopy(self.scope)
        changed["out_of_scope"] = ["用户确认首版暂不加入云同步"]
        self.project.revise(changed, self.project.load()["revision"])
        self.project.confirm(self.project.load()["revision"])
        self.assertEqual(self.project.load()["tasks"]["sum"]["feedback"][0]["resolved_in_scope"], 2)
        self.assertTrue(self.project.packet("sum")["run_id"])

    def test_reverted_draft_does_not_resolve_requirement_feedback(self):
        self.project.init(self.scope)
        self.project.confirm(1)
        self.project.feedback("sum", "requirement", "增加云同步")
        changed = copy.deepcopy(self.scope)
        changed["features"][0]["acceptance_criteria"].append("支持云同步")
        self.project.revise(changed, self.project.load()["revision"])
        self.project.revise(self.scope, self.project.load()["revision"])
        self.project.confirm(self.project.load()["revision"])
        with self.assertRaisesRegex(CompanionError, "需求反馈"):
            self.project.packet("sum")

    def test_stale_integration_is_routed_back_to_integration(self):
        self.implement()
        self.project.check("sum")
        self.project.check("sum", kind="integration")
        self.project.accept("sum", "自动检查通过")
        with (self.root / "ledger.py").open("a") as handle:
            handle.write("# changed\n")
        self.project.check("sum")
        self.assertEqual(self.project.status()["current_stage"], "integration")
        with self.assertRaisesRegex(CompanionError, "联调"):
            self.project.accept("sum", "旧联调不能沿用")

    def test_status_between_implementation_and_integration_is_readable(self):
        self.implement()
        self.assertEqual(self.project.status()["current_stage"], "integration")
        self.project.check("sum")
        self.assertEqual(self.project.status()["current_stage"], "integration")

    def test_recover_lock_requires_authorization_and_dead_owner(self):
        self.project.data.mkdir()
        lock = self.project.data / "write.lock"
        import os
        lock.write_text(str(os.getpid()))
        with self.assertRaises(CompanionError):
            self.project.recover_lock(True)
        stopped = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
        lock.write_text(stopped.stdout.strip())
        with self.assertRaises(CompanionError):
            self.project.recover_lock(False)
        self.assertTrue(lock.exists())
        self.assertTrue(self.project.recover_lock(True)["cleared"])
        self.assertFalse(lock.exists())

    def test_recover_lock_rejects_links_and_invalid_owners(self):
        self.project.data.mkdir()
        lock = self.project.data / "write.lock"
        lock.write_text("-1")
        with self.assertRaises(CompanionError):
            self.project.recover_lock(True)
        lock.unlink()
        outside = self.root / "other.txt"
        outside.write_text("123")
        lock.symlink_to(outside)
        with self.assertRaises(CompanionError):
            self.project.recover_lock(True)
        self.assertEqual(outside.read_text(), "123")

    def test_recover_lock_handles_absent_unsupported_and_changed_locks(self):
        self.project.data.mkdir()
        self.assertFalse(self.project.recover_lock(True)["cleared"])
        with patch("core.os.name", "nt"), self.assertRaisesRegex(CompanionError, "平台"):
            self.project.recover_lock(True)
        lock = self.project.data / "write.lock"
        lock.write_text("9" * 33)
        with self.assertRaisesRegex(CompanionError, "内容异常"):
            self.project.recover_lock(True)
        lock.write_text("99999999")
        before = lock.stat()
        changed = SimpleNamespace(st_dev=before.st_dev, st_ino=before.st_ino,
                                  st_mtime_ns=before.st_mtime_ns + 1, st_ctime_ns=before.st_ctime_ns,
                                  st_mode=before.st_mode)
        with patch("core.os.kill", side_effect=ProcessLookupError), patch.object(Path, "lstat", return_value=changed):
            with self.assertRaisesRegex(CompanionError, "发生变化"):
                self.project.recover_lock(True)
        self.assertTrue(lock.exists())

    def test_recover_lock_preserves_lock_when_process_check_is_denied(self):
        self.project.data.mkdir()
        lock = self.project.data / "write.lock"
        lock.write_text("99999999")
        with patch("core.os.kill", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(CompanionError, "无法确认"):
                self.project.recover_lock(True)
        self.assertTrue(lock.exists())

    def test_invalid_feedback_check_kind_and_missing_integration_contract_fail_closed(self):
        self.project.init(self.scope)
        self.project.confirm(1)
        packet = self.project.packet("sum")
        (self.root / "ledger.py").write_text("def total(values): return sum(values)\n")
        self.project.receipt({"feature_id": "sum", "scope_version": 1, "run_id": packet["run_id"],
                              "status": "implemented", "summary": "实现合计", "changed_files": ["ledger.py"],
                              "evidence_files": []})
        with self.assertRaisesRegex(CompanionError, "检查类型"):
            self.project.check("sum", "unknown")
        with self.assertRaisesRegex(CompanionError, "检查"):
            self.project.check("sum", "integration")
        with self.assertRaisesRegex(CompanionError, "反馈类型"):
            self.project.feedback("sum", "unknown", "无法分类")

    def test_timed_out_feature_check_records_failure(self):
        self.implement()
        timed_out = {"argv": ["check"], "exit_code": None, "output": "检查超时（120秒）", "executed": True,
                     "truncated": False, "timed_out": True, "termination_confirmed": False,
                     "started_at": "a", "finished_at": "b", "output_sha256": "0" * 64, "output_redacted": False}
        with patch("core.run_argv", return_value=timed_out):
            result = self.project.check("sum")
        self.assertFalse(result["passed"])
        self.assertIsNone(result["results"][0]["exit_code"])
        self.assertIn("超时", result["results"][0]["output"])

    def test_existing_implementation_requires_explicit_mode_and_evidence(self):
        self.project.init(self.scope)
        self.project.confirm(1)
        (self.root / "ledger.py").write_text("def total(values): return sum(values)\n")
        packet = self.project.packet("sum")
        raw = {"feature_id": "sum", "scope_version": 1, "run_id": packet["run_id"],
               "status": "implemented", "summary": "复核既有实现", "changed_files": [], "evidence_files": []}
        with self.assertRaisesRegex(CompanionError, "verified_existing"):
            self.project.receipt(raw)
        raw["status"] = "verified_existing"
        with self.assertRaisesRegex(CompanionError, "证据文件"):
            self.project.receipt(raw)
        raw["evidence_files"] = ["ledger.py"]
        self.assertEqual(self.project.receipt(raw)["status"], "awaiting_review")

    def test_check_output_redacts_secret_shaped_values(self):
        self.implement()
        output = b"api_token=FAKE_SECRET_REVIEW_123\nAssertionError: expected 50\n"
        with patch("core.subprocess.Popen") as popen:
            process = popen.return_value
            process.wait.return_value = 2
            with patch("tempfile.TemporaryFile") as temporary:
                handle = temporary.return_value.__enter__.return_value
                handle.read.return_value = output
                result = self.project.check("sum")
        evidence = result["results"][0]
        self.assertNotIn("FAKE_SECRET_REVIEW_123", evidence["output"])
        self.assertIn("[REDACTED]", evidence["output"])
        self.assertTrue(evidence["output_redacted"])
        self.assertEqual(len(evidence["output_sha256"]), 64)

    def test_cli_planning_and_release_status_are_read_only_when_empty(self):
        for name in ("planning-status", "release-status"):
            result = subprocess.run([sys.executable, str(SCRIPTS / "companion.py"), "--project", str(self.root), name], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNone(json.loads(result.stdout)["record"])
        self.assertFalse(self.project.data.exists())

    def test_plugin_manifest_marketplace_and_release_entrypoint_are_consistent(self):
        plugin = SCRIPTS.parent
        manifest = json.loads((plugin / ".zcode-plugin" / "plugin.json").read_text())
        marketplace = json.loads((plugin.parent / "marketplace.json").read_text())
        listing = next(item for item in marketplace["plugins"] if item["name"] == "dev-companion")
        self.assertEqual(manifest["version"], "0.2.0")
        self.assertEqual(listing["version"], manifest["version"])
        self.assertEqual(listing["source"], "./dev-companion")
        self.assertEqual(manifest["commands"], "commands")
        self.assertEqual(manifest["skills"], "skills")
        self.assertEqual(manifest["agents"], "agents")
        self.assertEqual({path.name for path in (plugin / "commands").glob("*.md")}, {
            "companion-archive.md", "companion-check.md", "companion-progress.md", "companion-release.md",
            "companion-resume.md", "companion-start.md", "companion-work.md"})
        release = (plugin / "commands" / "companion-release.md").read_text()
        self.assertIn("skills: dev-companion", release)
        self.assertIn("release-prepare", release)
        self.assertIn("release-run", release)

    def test_status_export_cannot_create_or_overwrite_fact_records(self):
        self.project.init(self.scope)
        for name in ("journey.json", "release.json", "write.lock", "archives/index.json"):
            destination = self.project.data / name
            destination.parent.mkdir(exist_ok=True)
            result = subprocess.run([sys.executable, str(SCRIPTS / "companion.py"), "--project", str(self.root),
                                     "status", "--format", "json", "--out", str(destination)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertFalse(destination.exists())
        self.assertIsNone(self.project.status()["overall_percent"])


if __name__ == "__main__":
    unittest.main()

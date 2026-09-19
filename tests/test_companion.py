import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "dev-companion" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from core import CompanionError, Project, render_html, render_markdown


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = Project(self.root)
        self.scope = {"title": "记账工具", "goal": "算出总金额", "audience": "自己", "scenario": "记账",
                      "out_of_scope": ["云同步"], "assumptions": [], "features": [
                          {"id": "sum", "title": "计算合计", "acceptance_criteria": ["20+30=50"],
                           "allowed_paths": ["ledger.py"], "requires_user_acceptance": True,
                           "check_commands": [[sys.executable, "-c", "from ledger import total; assert total([20,30]) == 50"]]}]}

    def tearDown(self):
        self.temp.cleanup()

    def start(self):
        self.project.init(self.scope)
        self.project.confirm(1)
        return self.project.packet("sum")

    def implement(self, content="def total(values): return sum(values)\n"):
        packet = self.start()
        (self.root / "ledger.py").write_text(content)
        receipt = {"feature_id": "sum", "scope_version": packet["scope_version"], "run_id": packet["run_id"],
                   "status": "implemented", "summary": "完成合计", "changed_files": ["ledger.py"], "evidence_files": []}
        self.project.receipt(receipt)
        return receipt

    def test_unknown_scope_is_not_zero_progress(self):
        with self.assertRaises(CompanionError):
            self.project.status()
        self.project.init(self.scope)
        self.assertIsNone(self.project.status()["overall_percent"])
        with self.assertRaises(CompanionError):
            self.project.packet("sum")

    def test_full_evidence_and_user_acceptance_lifecycle(self):
        self.implement()
        self.assertEqual(self.project.status()["overall_percent"], 0)
        with self.assertRaises(CompanionError):
            self.project.accept("sum", "用户说可用", True)
        result = self.project.check("sum")
        self.assertTrue(result["passed"])
        self.assertTrue(result["results"][0]["executed"])
        with self.assertRaises(CompanionError):
            self.project.accept("sum", "缺少用户试用")
        self.project.accept("sum", "用户输入20和30，看到50", True)
        self.assertEqual(Project(self.root).status()["overall_percent"], 100)
        self.assertEqual(self.project.status()["publication"], "未核验发布状态")

    def test_failed_check_never_counts_as_done(self):
        self.implement("def total(values): return 0\n")
        self.assertFalse(self.project.check("sum")["passed"])
        with self.assertRaises(CompanionError):
            self.project.accept("sum", "看起来完成", True)
        self.assertEqual(self.project.status()["counts"]["accepted"], 0)

    def test_post_verification_change_invalidates_acceptance(self):
        self.implement()
        self.project.check("sum")
        self.project.accept("sum", "用户试用通过", True)
        (self.root / "ledger.py").write_text("def total(values): return -1\n")
        view = self.project.status()
        self.assertEqual(view["overall_percent"], 0)
        self.assertTrue(view["features"][0]["evidence_stale"])
        self.assertIn("内容已变化", render_markdown(view))

    def test_stale_and_duplicate_worker_receipts_rejected(self):
        packet = self.start()
        (self.root / "ledger.py").write_text("def total(values): return sum(values)\n")
        raw = {"feature_id": "sum", "scope_version": 99, "run_id": packet["run_id"], "status": "implemented",
               "summary": "结果", "changed_files": ["ledger.py"], "evidence_files": []}
        with self.assertRaises(CompanionError):
            self.project.receipt(raw)
        raw["scope_version"] = 1
        self.project.receipt(raw)
        with self.assertRaises(CompanionError):
            self.project.receipt(raw)

    def test_unreported_or_out_of_scope_edits_are_not_accepted(self):
        packet = self.start()
        (self.root / "other.txt").write_text("outside allowed files")
        raw = {"feature_id": "sum", "scope_version": 1, "run_id": packet["run_id"], "status": "implemented",
               "summary": "结果", "changed_files": [], "evidence_files": []}
        with self.assertRaises(CompanionError):
            self.project.receipt(raw)
        raw["changed_files"] = ["other.txt"]
        with self.assertRaises(CompanionError):
            self.project.receipt(raw)

    def test_scope_revision_and_history_survive_restart(self):
        self.project.init(self.scope)
        with self.assertRaises(CompanionError):
            self.project.confirm(999)
        self.project.confirm(1)
        new = copy.deepcopy(self.scope)
        second = copy.deepcopy(new["features"][0])
        second.update(id="export", title="导出", allowed_paths=["export.py"])
        new["features"].append(second)
        self.project.revise(new, 2)
        view = Project(self.root).status()
        self.assertIsNone(view["overall_percent"])
        self.assertEqual(view["total"], 2)
        self.assertEqual(view["scope_version"], 2)
        self.assertEqual(view["history"][-1]["previous_scope"], self.scope)

    def test_changed_requirement_drops_old_acceptance(self):
        self.implement()
        self.project.check("sum")
        self.project.accept("sum", "可用", True)
        changed = copy.deepcopy(self.scope)
        changed["features"][0]["acceptance_criteria"].append("支持退款负数")
        self.project.revise(changed, self.project.load()["revision"])
        self.project.confirm(self.project.load()["revision"])
        self.assertEqual(self.project.status()["counts"]["accepted"], 0)

    def test_changed_product_context_requires_new_acceptance(self):
        self.implement()
        self.project.check("sum")
        self.project.accept("sum", "用户试用", True)
        changed = copy.deepcopy(self.scope)
        changed["goal"] = "给企业生成财务报表"
        self.project.revise(changed, self.project.load()["revision"])
        self.project.confirm(self.project.load()["revision"])
        self.assertEqual(self.project.status()["counts"]["accepted"], 0)

    def test_chmod_invalidates_prior_verification(self):
        self.implement()
        self.project.check("sum")
        self.project.accept("sum", "用户试用", True)
        (self.root / "ledger.py").chmod(0o700)
        self.assertEqual(self.project.status()["counts"]["accepted"], 0)

    def test_fabricated_accepted_state_fails_closed(self):
        self.implement()
        self.project.check("sum")
        state = self.project.load()
        state["tasks"]["sum"]["status"] = "accepted"
        self.project.state_path.write_text(json.dumps(state))
        with self.assertRaises(CompanionError):
            self.project.status()

    def test_incomplete_restore_blocks_new_work_and_acceptance(self):
        self.implement()
        self.project.check("sum")
        directory = self.project.data / "archives"
        directory.mkdir()
        (directory / "restore-pending.json").write_text(json.dumps({"safety_archive_id": "protected-state"}))
        for operation in (lambda: self.project.packet("sum"), lambda: self.project.check("sum"),
                          lambda: self.project.accept("sum", "不能接受", True)):
            with self.assertRaises(CompanionError) as result:
                operation()
            self.assertEqual(result.exception.safety_archive_id, "protected-state")
        view = self.project.status()
        self.assertIsNone(view["overall_percent"])
        self.assertTrue(view["recovery"]["required"])

    def test_concurrent_task_and_lock_fail_closed(self):
        self.start()
        with self.assertRaises(CompanionError):
            self.project.packet("sum")
        with self.project.locked():
            with self.assertRaises(CompanionError):
                self.project.block("sum", "停止")
        self.project.block("sum", "已停止执行")
        self.assertEqual(self.project.status()["counts"]["blocked"], 1)

    def test_paths_and_symlinks_rejected(self):
        for name in ("../escape", "/tmp/escape", ".git/config", ".env", "C:\\secret"):
            bad = copy.deepcopy(self.scope)
            bad["features"][0]["allowed_paths"] = [name]
            with self.assertRaises(CompanionError):
                self.project.init(bad)
        self.project.init(self.scope)
        self.project.confirm(1)
        (self.root / "ledger.py").symlink_to(self.root / "elsewhere")
        with self.assertRaises(CompanionError):
            self.project.packet("sum")

    def test_checker_modifying_sources_cannot_pass(self):
        self.scope["features"][0]["check_commands"] = [[sys.executable, "-c", "from pathlib import Path; Path('ledger.py').write_text('changed')"]]
        self.implement()
        result = self.project.check("sum")
        self.assertFalse(result["passed"])
        self.assertTrue(result["content_changed"])

    def test_missing_checker_is_unavailable_not_passed(self):
        self.scope["features"][0]["check_commands"] = [["dev-companion-nonexistent-executable"]]
        self.implement()
        result = self.project.check("sum")
        self.assertFalse(result["passed"])
        self.assertFalse(result["results"][0]["executed"])

    def test_html_escapes_user_content_and_has_no_fake_actions(self):
        self.scope["title"] = '<script>alert("x")</script>'
        self.project.init(self.scope)
        output = render_html(self.project.status())
        self.assertNotIn("<script>", output)
        self.assertIn("&lt;script&gt;", output)
        self.assertNotIn("<button", output)

    def test_corrupt_or_cross_project_state_is_unknown(self):
        self.project.init(self.scope)
        state = self.project.load()
        state["project"] = "/another-project"
        self.project.state_path.write_text(json.dumps(state))
        with self.assertRaises(CompanionError):
            self.project.status()
        self.project.state_path.write_text("{")
        with self.assertRaises(CompanionError):
            self.project.status()

    def test_cli_failure_is_nonzero_and_readable(self):
        result = subprocess.run([sys.executable, str(SCRIPTS / "companion.py"), "--project", str(self.root), "status", "--format", "json"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stderr)["success"])

    def test_cli_failed_check_exit_code(self):
        self.implement("def total(values): return -100\n")
        result = subprocess.run([sys.executable, str(SCRIPTS / "companion.py"), "--project", str(self.root), "check", "--feature", "sum"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 3)
        self.assertFalse(json.loads(result.stdout)["passed"])


if __name__ == "__main__":
    unittest.main()

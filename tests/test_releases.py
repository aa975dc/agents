"""Release execution uses disposable local projects and local subprocesses only."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dev-companion" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from core import CompanionError, Project
from releases import ReleaseStore


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = Project(self.root)
        self.store = ReleaseStore(self.project)
        self.scope = {"title": "本地工具", "goal": "验证合计", "audience": "自己", "scenario": "本机使用",
                      "features": [{"id": "sum", "title": "合计", "acceptance_criteria": ["20+30=50"],
                                    "allowed_paths": ["ledger.py"], "requires_user_acceptance": False,
                                    "check_commands": [[sys.executable, "-c", "from ledger import total; assert total([20,30]) == 50"]]}]}
        self.config = {"version": "1.0.0", "target": "临时本地文件", "environment": "local",
                       "summary": "发布合计工具", "rollback_plan": "恢复临时目标到旧版本",
                       "verification_notes": "读取实际部署版本 1.0.0，并运行 20+30=50 的核心流程",
                       "deploy_commands": [[sys.executable, "-c", "from pathlib import Path; Path('.dev-companion/deployed.txt').write_text('1.0.0:50')"]],
                       "verify_commands": [[sys.executable, "-c", "from pathlib import Path; assert Path('.dev-companion/deployed.txt').read_text() == '1.0.0:50'"]],
                       "rollback_commands": [[sys.executable, "-c", "from pathlib import Path; Path('.dev-companion/deployed.txt').write_text('0.9.0:50')"]]}

    def ready(self):
        self.project.init(self.scope)
        self.project.confirm(self.project.load()["revision"])
        packet = self.project.packet("sum")
        (self.root / "ledger.py").write_text("def total(values): return sum(values)\n")
        self.project.receipt({"feature_id": "sum", "run_id": packet["run_id"], "scope_version": packet["scope_version"],
                              "status": "implemented", "summary": "合计可用", "changed_files": ["ledger.py"], "evidence_files": []})
        self.project.check("sum")
        self.project.accept("sum", "真实合计检查通过")

    def prepare(self, config=None):
        self.ready()
        return self.store.prepare(config or self.config, 0)

    def execute(self, action):
        return self.store.run(action, self.store.status()["revision"], authorized=True)

    def test_full_local_deployment_and_verification_records_real_results(self):
        self.assertIsNone(self.store.status())
        self.ready()
        self.assertEqual(self.project.status()["overall_percent"], 100)
        self.assertIsNone(self.store.status())
        prepared = self.store.prepare(self.config, 0)
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(prepared["revision"], 1)
        self.assertFalse(prepared["source_stale"])
        deployed = self.execute("deploy")
        self.assertEqual(deployed["status"], "deployed_unverified")
        self.assertEqual((self.project.data / "deployed.txt").read_text(), "1.0.0:50")
        verified = self.execute("verify")
        self.assertEqual(verified["status"], "local_verified")
        self.assertTrue(verified["evidence"]["verify"]["results"][0]["executed"])
        self.assertEqual(verified["evidence"]["verify"]["results"][0]["exit_code"], 0)
        self.assertEqual(ReleaseStore(Project(self.root)).status()["status"], "local_verified")

    def test_environment_only_changes_verified_status_after_real_verification(self):
        self.ready()
        for environment, expected in (("staging", "staging_verified"), ("production", "published")):
            config = {**self.config, "environment": environment}
            current = self.store.status()
            self.store.prepare(config, current["revision"] if current else 0)
            self.assertEqual(self.execute("deploy")["status"], "deployed_unverified")
            self.assertEqual(self.execute("verify")["status"], expected)

    def test_all_execution_actions_require_explicit_authorization(self):
        self.prepare()
        for action in ("deploy", "verify", "rollback"):
            with self.subTest(action=action), self.assertRaisesRegex(CompanionError, "授权"):
                self.store.run(action, self.store.status()["revision"])
        self.assertFalse((self.project.data / "deployed.txt").exists())

    def test_configuration_rejects_fabricated_results_and_invalid_commands(self):
        self.ready()
        for changes in ({"passed": True}, {"status": "published"}, {"deploy_commands": []},
                        {"verify_commands": []}, {"deploy_commands": ["echo pass"]},
                        {"verify_commands": [[]]}, {"environment": "unknown"}, {"rollback_plan": " "},
                        {"deploy_commands": [[sys.executable, "bad\0argument"]]}):
            with self.subTest(changes=changes), self.assertRaises(CompanionError):
                self.store.prepare({**self.config, **changes}, 0)
        self.assertIsNone(self.store.status())

    def test_prepare_rejects_unaccepted_or_running_work(self):
        self.project.init(self.scope)
        self.project.confirm(self.project.load()["revision"])
        with self.assertRaises(CompanionError):
            self.store.prepare(self.config, 0)
        self.project.packet("sum")
        with self.assertRaisesRegex(CompanionError, "制作任务"):
            self.store.prepare(self.config, 0)

    def test_revision_and_shared_lock_prevent_conflicting_writes(self):
        self.prepare()
        with self.assertRaises(CompanionError):
            self.store.prepare(self.config, 0)
        with self.assertRaises(CompanionError):
            self.store.run("deploy", 0, True)
        with self.project.locked(), self.assertRaises(CompanionError):
            self.store.run("deploy", 1, True)
        self.assertFalse((self.project.data / "deployed.txt").exists())

    def test_failed_command_stops_pipeline_and_missing_executable_is_unexecuted(self):
        config = copy.deepcopy(self.config)
        config["deploy_commands"].insert(0, [sys.executable, "-c", "print('deployment failed'); raise SystemExit(7)"])
        self.prepare(config)
        failed = self.execute("deploy")
        self.assertEqual(failed["status"], "deploy_failed")
        self.assertEqual(len(failed["evidence"]["deploy"]["results"]), 1)
        self.assertEqual(failed["evidence"]["deploy"]["results"][0]["exit_code"], 7)
        self.assertFalse((self.project.data / "deployed.txt").exists())
        config["deploy_commands"] = [[str(self.root / "missing-command")]]
        self.store.prepare(config, failed["revision"])
        missing = self.execute("deploy")
        self.assertFalse(missing["evidence"]["deploy"]["results"][0]["executed"])
        with self.assertRaises(CompanionError):
            self.execute("verify")

    def test_timeout_is_failed_evidence_with_bounded_output(self):
        config = {**self.config, "deploy_commands": [[sys.executable, "-c", "import time; print('started', flush=True); time.sleep(5)"]]}
        self.prepare(config)
        with patch("releases.TIMEOUT_SECONDS", 0.05):
            result = self.execute("deploy")
        evidence = result["evidence"]["deploy"]["results"][0]
        self.assertEqual(result["status"], "deploying")
        self.assertTrue(evidence["executed"])
        self.assertIsNone(evidence["exit_code"])
        self.assertIn("超时", evidence["output"])
        self.assertFalse(result["passed"])
        with self.assertRaisesRegex(CompanionError, "核对"):
            self.execute("deploy")

    def test_output_is_limited_and_started_state_is_durable_before_command(self):
        config = copy.deepcopy(self.config)
        config["deploy_commands"] = [[sys.executable, "-c", "import json; from pathlib import Path; assert json.loads(Path('.dev-companion/release.json').read_text())['status'] == 'deploying'; print('x'*70000)"]]
        self.prepare(config)
        result = self.execute("deploy")
        evidence = result["evidence"]["deploy"]["results"][0]
        self.assertEqual(result["status"], "deployed_unverified")
        self.assertTrue(result["passed"])
        self.assertLessEqual(len(evidence["output"].encode()), 65536)
        self.assertTrue(evidence["truncated"])
        self.assertTrue(evidence["started_at"])
        self.assertTrue(evidence["finished_at"])

    def test_source_change_blocks_new_deploy_or_verify_but_not_rollback(self):
        self.prepare()
        self.execute("deploy")
        (self.root / "ledger.py").write_text("def total(values): return -1\n")
        status = self.store.status()
        self.assertTrue(status["source_stale"])
        self.assertEqual(status["status"], "deployed_unverified")
        for action in ("deploy", "verify"):
            with self.subTest(action=action), self.assertRaises(CompanionError):
                self.execute(action)
        result = self.execute("rollback")
        self.assertEqual(result["status"], "rolled_back_unverified")
        self.assertEqual((self.project.data / "deployed.txt").read_text(), "0.9.0:50")
        with self.assertRaises(CompanionError):
            self.execute("verify")

    def test_verify_failures_do_not_publish_and_allow_recheck(self):
        self.prepare()
        self.execute("deploy")
        (self.project.data / "deployed.txt").write_text("wrong version")
        self.assertEqual(self.execute("verify")["status"], "verify_failed")
        (self.project.data / "deployed.txt").write_text("1.0.0:50")
        self.assertEqual(self.execute("verify")["status"], "local_verified")

    def test_verification_that_changes_source_cannot_publish(self):
        config = copy.deepcopy(self.config)
        config["verify_commands"].append([sys.executable, "-c", "from pathlib import Path; Path('ledger.py').write_text('changed')"])
        self.prepare(config)
        self.execute("deploy")
        self.assertEqual(self.execute("verify")["status"], "verify_failed")

    def test_reprepare_preserves_history_and_clears_old_evidence(self):
        self.prepare()
        self.execute("deploy")
        self.execute("verify")
        result = self.store.prepare({**self.config, "version": "1.1.0"}, self.store.status()["revision"])
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["evidence"], {})
        previous = result["history"][-1]["previous"]
        self.assertEqual(previous["config"]["version"], "1.0.0")
        self.assertEqual(previous["status"], "local_verified")
        self.assertTrue(previous["evidence"]["verify"])

    def test_missing_rollback_command_and_failed_rollback_are_explicit(self):
        self.prepare({**self.config, "rollback_commands": []})
        self.execute("deploy")
        with self.assertRaisesRegex(CompanionError, "回退命令"):
            self.execute("rollback")
        config = {**self.config, "rollback_commands": [[sys.executable, "-c", "raise SystemExit(4)"]]}
        self.store.prepare(config, self.store.status()["revision"])
        self.execute("deploy")
        self.assertEqual(self.execute("rollback")["status"], "rollback_failed")

    def test_interrupted_action_requires_inspection_and_cannot_reprepare(self):
        self.prepare()
        with patch.object(self.store, "_execute", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.execute("deploy")
        current = self.store.status()
        self.assertEqual(current["status"], "deploying")
        self.assertIn("核对", current["message"])
        with self.assertRaisesRegex(CompanionError, "核对"):
            self.store.prepare(self.config, current["revision"])
        with self.assertRaisesRegex(CompanionError, "核对"):
            self.execute("deploy")
        with self.assertRaises(CompanionError):
            self.store.reconcile(current["revision"], "已核对进程退出")
        with self.assertRaises(CompanionError):
            self.store.reconcile(current["revision"] - 1, "已核对进程退出", True)
        reconciled = self.store.reconcile(current["revision"], "已核对本地目标，旧进程已停止", True)
        self.assertEqual(reconciled["status"], "interrupted")
        self.assertEqual(reconciled["evidence"], {})
        self.assertEqual(reconciled["history"][-1]["note"], "已核对本地目标，旧进程已停止")
        with self.assertRaises(CompanionError):
            self.execute("deploy")
        prepared = self.store.prepare(self.config, reconciled["revision"])
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(self.execute("deploy")["status"], "deployed_unverified")

    def test_output_is_redacted_before_release_record_is_written(self):
        config = {**self.config, "deploy_commands": [[sys.executable, "-c", "import os; print('api_token='+os.environ['DEMO_API_TOKEN'])"]]}
        self.prepare(config)
        with patch.dict(os.environ, {"DEMO_API_TOKEN": "FAKE_SECRET_REVIEW_123"}):
            result = self.execute("deploy")
        evidence = result["evidence"]["deploy"]["results"][0]
        self.assertTrue(evidence["output_redacted"])
        self.assertNotIn("FAKE_SECRET_REVIEW_123", evidence["output"])
        self.assertNotIn("FAKE_SECRET_REVIEW_123", (self.project.data / "release.json").read_text())

    def test_timeout_kills_process_group_and_requires_reconcile(self):
        child = "import time; from pathlib import Path; time.sleep(.3); Path('.dev-companion/late-side-effect.txt').write_text('late')"
        parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',%r]); time.sleep(5)" % child
        config = {**self.config, "deploy_commands": [[sys.executable, "-c", parent]]}
        self.prepare(config)
        with patch("releases.TIMEOUT_SECONDS", 0.05):
            result = self.execute("deploy")
        self.assertEqual(result["status"], "deploying")
        self.assertTrue(result["evidence"]["deploy"]["results"][0]["timed_out"])
        time.sleep(.5)
        self.assertFalse((self.project.data / "late-side-effect.txt").exists())
        reconciled = self.store.reconcile(result["revision"], "已核对超时命令和目标环境，进程均停止", True)
        self.assertEqual(reconciled["status"], "interrupted")

    def test_release_status_reuses_project_view_snapshot(self):
        self.prepare()
        with patch.object(self.project, "snapshot", wraps=self.project.snapshot) as snapshot:
            self.project.status()
        self.assertEqual(snapshot.call_count, 1)

    def test_invalid_schema_and_symlinks_are_rejected_without_replacing_files(self):
        self.prepare()
        path = self.project.data / "release.json"
        original = json.loads(path.read_text())
        for changed in ({"schema_version": 99}, {"schema_version": True}, {"status": []}, {"status": "published"}):
            path.write_text(json.dumps({**original, **changed}))
            with self.subTest(changed=changed), self.assertRaises(CompanionError):
                self.store.status()
        path.unlink()
        target = self.project.data / "outside.json"
        target.write_text(json.dumps(original))
        path.symlink_to(target)
        with self.assertRaises(CompanionError):
            self.store.prepare(self.config, 1)
        self.assertTrue(path.is_symlink())
        self.assertEqual(json.loads(target.read_text()), original)

    def test_invalid_action_missing_record_and_nonactive_reconcile_fail_closed(self):
        with self.assertRaisesRegex(CompanionError, "发布操作"):
            self.store.run("publish", 0, True)
        with self.assertRaisesRegex(CompanionError, "尚未准备"):
            self.store.run("deploy", 0, True)
        self.prepare()
        with self.assertRaisesRegex(CompanionError, "结果未知"):
            self.store.reconcile(self.store.status()["revision"], "已核对", True)

    def test_release_record_path_must_be_a_regular_file(self):
        self.project.data.mkdir()
        self.store.path.mkdir()
        with self.assertRaisesRegex(CompanionError, "普通文件"):
            ReleaseStore(self.project)

    def test_release_record_rejects_invalid_binding_and_evidence_relationships(self):
        self.prepare()
        self.execute("deploy")
        path = self.project.data / "release.json"
        original = json.loads(path.read_text())
        corruptions = []
        changed = copy.deepcopy(original)
        changed["binding"].pop("journey")
        corruptions.append(changed)
        changed = copy.deepcopy(original)
        changed["evidence"]["deploy"]["results"] = []
        corruptions.append(changed)
        changed = copy.deepcopy(original)
        changed["evidence"]["deploy"]["passed"] = True
        changed["evidence"]["deploy"]["results"][0]["exit_code"] = 3
        corruptions.append(changed)
        changed = copy.deepcopy(original)
        changed["status"] = "deploy_failed"
        changed["evidence"]["deploy"]["passed"] = True
        corruptions.append(changed)
        changed = copy.deepcopy(original)
        changed["status"] = "local_verified"
        changed["evidence"].pop("deploy")
        changed["evidence"]["verify"] = {"id": "verify", "at": "now", "passed": True,
                                                "source_changed": False, "results": [{
                                                    "argv": changed["config"]["verify_commands"][0], "executed": True,
                                                    "exit_code": 0, "truncated": False, "output": "",
                                                    "started_at": "a", "finished_at": "b"}]}
        corruptions.append(changed)
        for changed in corruptions:
            path.write_text(json.dumps(changed))
            with self.subTest(status=changed.get("status")), self.assertRaises(CompanionError):
                self.store.status()

    def test_source_snapshot_race_and_verify_inspection_error_fail_closed(self):
        self.prepare()
        self.execute("deploy")
        current = self.store._current()
        with patch.object(self.store, "_current", side_effect=[current, current, CompanionError("inspection failed"), current]):
            result = self.execute("verify")
        self.assertEqual(result["status"], "verify_failed")
        self.assertTrue(result["evidence"]["verify"]["source_changed"])

    def test_changed_check_or_scope_binding_requires_new_release_preparation(self):
        self.prepare()
        self.project.check("sum")
        self.project.accept("sum", "同样文件的重新检查")
        self.assertEqual(self.project.status()["overall_percent"], 100)
        self.assertTrue(self.store.status()["source_stale"])
        with self.assertRaises(CompanionError):
            self.execute("deploy")
        self.store.prepare(self.config, self.store.status()["revision"])
        changed = {**self.scope, "title": "改名的本地工具"}
        self.project.revise(changed, self.project.load()["revision"])
        self.project.confirm(self.project.load()["revision"])
        self.assertTrue(self.store.status()["source_stale"])

    def test_linked_storage_directory_and_incomplete_success_evidence_are_rejected(self):
        self.prepare()
        self.execute("deploy")
        path = self.project.data / "release.json"
        record = json.loads(path.read_text())
        record["evidence"]["deploy"]["results"][0].pop("exit_code")
        path.write_text(json.dumps(record))
        with self.assertRaises(CompanionError):
            self.store.status()
        real = self.root / "storage-real"
        self.project.data.rename(real)
        self.project.data.symlink_to(real, target_is_directory=True)
        with self.assertRaises(CompanionError):
            self.store.status()
        with self.assertRaises(CompanionError):
            self.store.prepare(self.config, 3)

    def test_recovery_failure_blocks_release_execution(self):
        self.prepare()
        directory = self.project.data / "archives"
        directory.mkdir()
        (directory / "restore-pending.json").write_text(json.dumps({"safety_archive_id": "protected"}))
        with self.assertRaises(CompanionError):
            self.execute("deploy")
        self.assertFalse((self.project.data / "deployed.txt").exists())


if __name__ == "__main__":
    unittest.main()

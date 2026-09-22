# -*- coding: utf-8 -*-
"""Step-8 验收路由：team-only 项目的 accept 入口（tests/closure_store）。

复核 §4（accept 仍走 legacy）：team-only 项目 accept 此前误入 legacy
Project.accept（exit 2 "尚未建立需求记录"）。本组测试经真实 subprocess CLI 锁定：
- 集成未完成 / 无用户真实确认 → 拒绝（五前置门的负向对照）；
- 合法链路 + 显式 --user-confirmed → acceptance 证据 + feature 状态 accepted；
- 验收后产物漂移 → 再次验收拒绝并指出文件与新旧哈希；
- legacy 项目 accept 行为逐字节不回归（含 team.db 与 state.json 并存的迁移项目）；
- team-only 全程不产生 legacy state.json。

授权声明：测试中的 --user-confirmed 是显式模拟授权夹具，仅用于验证机器验收入口
的确认门语义，不冒充、不代表任何真实宿主用户的试用验收记录。
"""
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "dev-companion" / "scripts"
COMPANION = SCRIPTS / "companion.py"

APP_CONTENT = b"VALUE = 1\n"
APP_SHA = hashlib.sha256(APP_CONTENT).hexdigest()
CHECK_CMD = "%s -B -c %s" % (__import__("shlex").quote(sys.executable),
                             __import__("shlex").quote("import app; assert app.VALUE == 1"))
LEGACY_ACCEPT_ERROR = ('{"success": false, "error": "缺少当前内容的成功检查，请先运行 check",'
                       ' "state": "unknown_or_unchanged"}')
SCOPE = {"title": "记账工具", "goal": "算出总金额", "audience": "自己", "scenario": "记账",
         "out_of_scope": ["云同步"], "assumptions": [],
         "features": [
             {"id": "sum", "title": "计算合计", "acceptance_criteria": ["20+30=50"],
              "allowed_paths": ["ledger.py"], "check_commands": [["python3", "-c", "pass"]]}]}


def cli(*argv):
    """跑一次真实 companion.py 进程，返回 CompletedProcess（不抛）。"""
    return subprocess.run([sys.executable, str(COMPANION), *argv],
                          capture_output=True, text=True)


def cli_json(*argv):
    result = cli(*argv)
    assert result.returncode == 0, "CLI 失败(%d)：%s%s" % (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


class TeamAcceptTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # 与 core/legacy 相同的 realpath 口径（macOS /var → /private/var）
        self.project = (Path(self.temp.name) / "proj").resolve()
        self.project.mkdir(parents=True)
        self.data = self.project / ".dev-companion"

    def assert_no_state_json(self):
        self.assertFalse((self.data / "state.json").exists(),
                         "team-only 项目不得产生 legacy state.json")

    def write_app(self, content=APP_CONTENT):
        (self.project / "app.py").write_bytes(content)
        return hashlib.sha256(content).hexdigest()

    def build_chain(self, feature="todo", task="t1", integrate=True):
        """真实 CLI 搭完整团队链路（review_required 走独立审查门，同宿主复核探针）。"""
        cli_json("--project", str(self.project), "team-init", "--feature", feature,
                 "--review-required", "脚本化测试夹具")
        cli_json("--project", str(self.project), "team-task-add", "--set", task,
                 "--feature", feature, "--kind", "impl", "--allowed-paths", "app.py")
        self.write_app()
        cli_json("--project", str(self.project), "team-task", "--set", task,
                 "--feature", feature, "--status", "ready")
        cli_json("--project", str(self.project), "team-task", "--set", task,
                 "--feature", feature, "--status", "running")
        cli_json("--project", str(self.project), "team-report", "--task", task,
                 "--outcome", "succeeded", "--summary", "脚本化夹具；非真实交付",
                 "--changed-files", "app.py", "--artifact-sha256", APP_SHA)
        cli_json("--project", str(self.project), "team-approve", "--task", task,
                 "--reviewer", "fixture-checker", "--verdict", "approved")
        cli_json("--project", str(self.project), "team-task", "--set", task,
                 "--feature", feature, "--status", "done")
        if integrate:
            done = cli_json("--project", str(self.project), "team-integrate",
                            "--candidate", "route-only", "--tasks", task,
                            "--check-cmd", CHECK_CMD)
            self.assertTrue(done["passed"])
            self.assertEqual(done["status"], "completed")

    def accept(self, *extra):
        return cli("--project", str(self.project), "accept", "--feature", "todo",
                   "--note", "夹具试用反馈；非真实用户确认", *extra)

    def evidence_fact(self, evidence_id):
        """只读打开 team.db 取验收证据（evidence_view 可查 + 事件 payload 绑定事实）。"""
        conn = sqlite3.connect("file:%s?mode=ro" % (self.data / "team.db"), uri=True)
        try:
            row = conn.execute("SELECT kind, subject_id, result FROM evidence_view "
                               "WHERE evidence_id = ?", (evidence_id,)).fetchone()
            payload = conn.execute("SELECT payload FROM events WHERE entity_id = ?",
                                   (evidence_id,)).fetchone()
        finally:
            conn.close()
        return row, (json.loads(payload[0]) if payload else None)


class GateRejectionTests(TeamAcceptTestBase):
    def test_a_reject_when_integration_incomplete(self):
        """(a) 集成未完成（链路止于 done）→ 拒绝并指名缺 completed 集成版本。"""
        self.build_chain(integrate=False)
        failed = self.accept("--user-confirmed")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("没有已完成的集成版本", failed.stderr)
        self.assertIn("team-integrate", failed.stderr)  # 指出补齐动作
        self.assert_no_state_json()

    def test_b_reject_without_user_confirmation(self):
        """(b) 链路完整但无 --user-confirmed → 拒绝：需要用户真实试用反馈。"""
        self.build_chain()
        failed = cli("--project", str(self.project), "accept", "--feature", "todo",
                     "--note", "没有确认来源的验收请求")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("需要用户真实试用反馈后验收", failed.stderr)
        self.assert_no_state_json()


class AcceptRouteTests(TeamAcceptTestBase):
    def test_c_accept_binds_integration_and_writes_evidence(self):
        """(c) 合法夹具（显式 --user-confirmed 模拟授权，非真实用户确认）→
        accepted 事件 + evidence 可查 + feature 状态 accepted + 绑定集成信息。"""
        self.build_chain()
        ok = self.accept("--user-confirmed")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        report = json.loads(ok.stdout)
        self.assertEqual(report["status"], "accepted")
        self.assertTrue(report["user_confirmed"])
        binding = report["integration"]
        self.assertEqual(binding["integration_version"], 1)
        self.assertEqual(len(binding["manifest_sha256"]), 64)
        self.assertEqual(binding["candidate_tasks"], {"t1": APP_SHA})
        self.assertEqual(binding["candidates"], ["route-only"])
        # feature 状态经正常 status 入口可查为 accepted
        view = cli_json("--project", str(self.project), "team-status")
        self.assertEqual([f["status"] for f in view["features"]["items"]], ["accepted"])
        # 验收证据 evidence_view 可查，事件绑定候选摘要/反馈/时间/真实确认来源
        row, payload = self.evidence_fact("feature-acceptance:todo")
        self.assertEqual(row, ("acceptance", "todo", "accepted"))
        fact = payload["fact"]
        self.assertEqual(fact["feature_id"], "todo")
        self.assertTrue(fact["user_confirmed"])
        self.assertTrue(fact["note"])
        self.assertTrue(fact["accepted_at"])
        self.assertEqual(fact["integration"]["integration_version"], 1)
        self.assertEqual(fact["integration"]["manifest_sha256"],
                         binding["manifest_sha256"])
        self.assertEqual(fact["integration"]["candidate_tasks"], {"t1": APP_SHA})
        self.assert_no_state_json()

    def test_d_reject_after_content_drift(self):
        """(d) 验收后产物漂移 → 再次验收拒绝并指出文件与新旧哈希（须重走链路）。"""
        self.build_chain()
        ok = self.accept("--user-confirmed")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        drifted_sha = self.write_app(b"VALUE = 2\n")
        failed = self.accept("--user-confirmed")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("漂移", failed.stderr)
        self.assertIn("app.py", failed.stderr)
        self.assertIn(APP_SHA, failed.stderr)      # 集成时哈希
        self.assertIn(drifted_sha, failed.stderr)  # 当前哈希
        self.assertIn("team-report", failed.stderr)
        self.assert_no_state_json()


class LegacyRegressionTests(TeamAcceptTestBase):
    def build_legacy(self):
        scope_path = self.project / "scope.json"
        scope_path.write_text(json.dumps(SCOPE, ensure_ascii=False), encoding="utf-8")
        cli_json("--project", str(self.project), "init", "--input", str(scope_path))
        cli_json("--project", str(self.project), "confirm", "--revision", "1")

    def legacy_state_bytes(self):
        return (self.data / "state.json").read_bytes()

    def test_e_legacy_accept_unchanged_byte_for_byte(self):
        """(e) legacy 项目 accept 行为逐字节不回归；迁移并存项目仍走 legacy。"""
        self.build_legacy()
        before = self.legacy_state_bytes()
        failed = cli("--project", str(self.project), "accept", "--feature", "sum",
                     "--note", "legacy 探针", "--user-confirmed")
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stderr, LEGACY_ACCEPT_ERROR + "\n")
        self.assertEqual(self.legacy_state_bytes(), before)
        # team.db 与 state.json 并存（迁移项目）继续走 legacy 入口，输出一致
        migrated = cli_json("--project", str(self.project), "team-migrate", "--from-json")
        self.assertEqual(migrated["status"], "imported")
        failed_again = cli("--project", str(self.project), "accept", "--feature", "sum",
                           "--note", "legacy 探针", "--user-confirmed")
        self.assertEqual(failed_again.returncode, 2)
        self.assertEqual(failed_again.stderr, LEGACY_ACCEPT_ERROR + "\n")
        self.assertEqual(self.legacy_state_bytes(), before)

    def test_f_team_only_project_never_creates_state_json(self):
        """(f) team-only 无 state.json：全程（含拒绝与成功验收）都不产生 state.json。"""
        self.build_chain()
        self.assert_no_state_json()
        self.assertEqual(self.accept().returncode, 2)          # 无确认拒绝
        self.assert_no_state_json()
        self.assertEqual(self.accept("--user-confirmed").returncode, 0)  # 合法验收
        self.assert_no_state_json()
        self.write_app(b"VALUE = 2\n")
        self.assertEqual(self.accept("--user-confirmed").returncode, 2)  # 漂移拒绝
        self.assert_no_state_json()
        names = sorted(p.name for p in self.data.iterdir())
        self.assertTrue(all(name.startswith("team.db") for name in names),
                        "记录目录出现 team.db 之外的文件：%s" % names)


if __name__ == "__main__":
    unittest.main()

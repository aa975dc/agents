# -*- coding: utf-8 -*-
"""FIX-04（SR-01+SR-05）：团队门禁接进正常入口——探针场景负向断言与门禁矩阵。

全部经真实 subprocess 调 companion.py CLI（每步独立进程）。逐项锁定：

SR-01（复核探针 team_cli_gate_bypass 的三条断言全部转负向）：
- 从未执行的任务直接 `--status done` → 拒绝（exit 2，说明缺什么），evidence_view 0 行；
- done → ready → 拒绝（白名单外）；
- 无原因 blocked → 拒绝；--reason 后放行；
- 合法链路 add→ready→running→report→approve→done→integrate 全程 exit 0，done 与
  完成证据同一事务（evidence_view 行在、事件 seq 相邻）；
- review_required 功能：缺批准/自审拒绝；独立批准后 done 门通过；
- team-integrate：候选门（未 done 拒）、版本级回归 = 真实 subprocess（失败保持
  candidate 且 exit 3，修好后重跑同候选幂等完成）。

SR-05（复核探针 normal_chat_status_on_team_only_project 转负向）：
- 只有 team.db 的新项目：正常 `status` 返回 0 与团队视图（generation/事实源）；
  `resume` 返回门禁续接清单；`check` 做只读门禁审计（违例 exit 3）；
- legacy 项目无 team.db：status 逐字节不变；有 team.db 的 legacy 项目 legacy 入口
  也不变（NoDualMaster 契约，详见 test_r02_team_entry.py 的同款对照）。
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

SCOPE = {"title": "legacy对照", "goal": "算出总金额", "audience": "自己", "scenario": "记账",
         "out_of_scope": [], "assumptions": [],
         "features": [{"id": "sum", "title": "计算合计", "acceptance_criteria": ["20+30=50"],
                       "allowed_paths": ["ledger.py"], "check_commands": [["python3", "-c", "pass"]]}]}


def cli(project, *argv):
    return subprocess.run([sys.executable, str(COMPANION), "--project", str(project), *argv],
                          capture_output=True, text=True, timeout=120)


def cli_json(project, *argv):
    result = cli(project, *argv)
    assert result.returncode == 0, "CLI 失败(%d)：%s%s" % (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


def sha256_text(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def evidence_rows(db_path):
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    try:
        return conn.execute("SELECT evidence_id, result FROM evidence_view ORDER BY evidence_id").fetchall()
    finally:
        conn.close()


class Fix04GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = (Path(self.temp.name) / "proj").resolve()
        self.project.mkdir(parents=True)
        self.db = self.project / ".dev-companion" / "team.db"

    def team_init(self, review_required=False):
        argv = ["team-init", "--feature", "todo"]
        if review_required:
            argv.append("--review-required")
        argv.append("任务清单")
        return cli_json(self.project, *argv)

    def full_chain(self, tid, reviewer="checker/Q2", summary="实现完成"):
        """add→ready→running→report→（可选 approve）→done 的合法门禁链。"""
        sha = sha256_text({"task": tid, "files": ["app/x.py"], "body": summary})
        cli_json(self.project, "team-task-add", "--set", tid, "--feature", "todo",
                 "--kind", "impl", "--allowed-paths", "app/x.py")
        cli_json(self.project, "team-task", "--set", tid, "--feature", "todo", "--status", "ready")
        cli_json(self.project, "team-task", "--set", tid, "--feature", "todo", "--status", "running")
        cli_json(self.project, "team-report", "--task", tid, "--outcome", "succeeded",
                 "--summary", summary, "--changed-files", "app/x.py", "--artifact-sha256", sha)
        return tid, sha

    # ---- SR-01 探针场景负向断言 ----

    def test_probe_never_executed_done_rejected_and_no_evidence(self):
        """探针一：从未执行的任务直接写 done → 拒绝且不落任何事实。"""
        self.team_init()
        failed = cli(self.project, "team-task", "--set", "never-executed",
                     "--feature", "todo", "--status", "done")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("不存在", failed.stderr)
        self.assertIn("team-task-add", failed.stderr)
        # 即使任务被创建，未 running 也过不了 done 门
        cli_json(self.project, "team-task-add", "--set", "t1", "--feature", "todo", "--kind", "impl")
        failed = cli(self.project, "team-task", "--set", "t1", "--feature", "todo", "--status", "done")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("未完结 attempt", failed.stderr)
        self.assertFalse(self.db.exists() and bool(evidence_rows(self.db)),
                         "拒绝路径不得写任何完成证据")

    def test_probe_done_to_ready_rejected_by_whitelist(self):
        """探针二：done → ready 非法转换 → 白名单拒绝。"""
        self.team_init()
        self.full_chain("t1")
        cli_json(self.project, "team-approve", "--task", "t1", "--reviewer", "checker/Q2",
                 "--verdict", "approved")
        cli_json(self.project, "team-task", "--set", "t1", "--feature", "todo", "--status", "done")
        failed = cli(self.project, "team-task", "--set", "t1", "--feature", "todo", "--status", "ready")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("白名单外", failed.stderr)

    def test_probe_blocked_without_reason_rejected_then_reason_accepted(self):
        """探针三：无原因 blocked → 拒绝；--reason 后按 Z24 统一入口放行。"""
        self.team_init()
        cli_json(self.project, "team-task-add", "--set", "t1", "--feature", "todo", "--kind", "impl")
        failed = cli(self.project, "team-task", "--set", "t1", "--feature", "todo", "--status", "blocked")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("原因", failed.stderr)
        blocked = cli_json(self.project, "team-task", "--set", "t1", "--feature", "todo",
                           "--status", "blocked", "--reason", "等待上游契约冻结")
        self.assertTrue(blocked["applied"])
        view = cli_json(self.project, "team-status")
        self.assertEqual(view["tasks"]["items"][0]["status"], "blocked")
        # blocked → ready 解除阻断（白名单内），fail 亦须原因
        cli_json(self.project, "team-task", "--set", "t1", "--feature", "todo", "--status", "ready")
        failed = cli(self.project, "team-task", "--set", "t1", "--feature", "todo",
                     "--status", "failed")
        self.assertEqual(failed.returncode, 2)

    # ---- done 门前置逐项 ----

    def test_done_gate_requires_report_and_review_when_policy_on(self):
        """review_required 功能：缺回报拒、缺批准拒、自审拒、独立批准后过门。"""
        self.team_init(review_required=True)
        cli_json(self.project, "team-task-add", "--set", "t1", "--feature", "todo", "--kind", "impl")
        cli_json(self.project, "team-task", "--set", "t1", "--feature", "todo", "--status", "ready")
        cli_json(self.project, "team-task", "--set", "t1", "--feature", "todo", "--status", "running")
        # (b) 缺回报
        failed = cli(self.project, "team-task", "--set", "t1", "--feature", "todo", "--status", "done")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("回报", failed.stderr)
        sha = sha256_text({"task": "t1", "body": "实现完成"})
        cli_json(self.project, "team-report", "--task", "t1", "--outcome", "succeeded",
                 "--summary", "实现完成", "--changed-files", "app/x.py", "--artifact-sha256", sha)
        # (c) 缺独立批准
        failed = cli(self.project, "team-task", "--set", "t1", "--feature", "todo", "--status", "done")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("独立审查", failed.stderr)
        # 实现者自审拒绝（attempt 引用即实现身份）
        failed = cli(self.project, "team-approve", "--task", "t1", "--reviewer", "t1#1",
                     "--verdict", "approved")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("自审", failed.stderr)
        # 未回报的任务拒绝审查
        cli_json(self.project, "team-task-add", "--set", "t2", "--feature", "todo", "--kind", "impl")
        cli_json(self.project, "team-task", "--set", "t2", "--feature", "todo", "--status", "ready")
        cli_json(self.project, "team-task", "--set", "t2", "--feature", "todo", "--status", "running")
        failed = cli(self.project, "team-approve", "--task", "t2", "--reviewer", "checker/Q2",
                     "--verdict", "approved")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("回报", failed.stderr)
        # (c) 独立批准 → (d) done 门通过
        cli_json(self.project, "team-approve", "--task", "t1", "--reviewer", "checker/Q2",
                 "--verdict", "approved")
        done = cli_json(self.project, "team-task", "--set", "t1", "--feature", "todo", "--status", "done")
        self.assertTrue(done["applied"])
        self.assertEqual(done["artifact_sha256"], sha)

    def test_done_evidence_same_transaction_boundary(self):
        """(d) done 事件与完成证据同一提交边界：evidence_view 行在、视图状态 done。"""
        self.team_init()
        self.full_chain("t1")
        before = cli_json(self.project, "team-status")["generation"]
        cli_json(self.project, "team-task", "--set", "t1", "--feature", "todo", "--status", "done")
        rows = evidence_rows(self.db)
        self.assertIn(("task:t1", "done"), rows, "完成证据必须折叠进 evidence_view")
        view = cli_json(self.project, "team-status")
        self.assertEqual(view["generation"], before + 2, "done=状态事件+证据事件，同批次提交")
        self.assertEqual(view["tasks"]["items"][0]["status"], "done")

    # ---- team-integrate：候选门 + 真实版本级回归 ----

    def test_integrate_requires_done_tasks_and_real_regression(self):
        """候选门（未 done 拒）→ 回归命令失败保持 candidate（exit 3）→ 修好后幂等完成。"""
        self.team_init()
        self.full_chain("t1")
        cli_json(self.project, "team-approve", "--task", "t1", "--reviewer", "checker/Q2",
                 "--verdict", "approved")
        cli_json(self.project, "team-task", "--set", "t1", "--feature", "todo", "--status", "done")
        # 占位/空命令拒绝
        failed = cli(self.project, "team-integrate", "--candidate", "C1", "--tasks", "t1",
                     "--check-cmd", "")
        self.assertEqual(failed.returncode, 2)
        # 未 done 的任务不能入列候选
        cli_json(self.project, "team-task-add", "--set", "t2", "--feature", "todo", "--kind", "impl")
        failed = cli(self.project, "team-integrate", "--candidate", "C1", "--tasks", "t1,t2",
                     "--check-cmd", "python3 -c pass")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("done", failed.stderr)
        # 真实回归失败：事实照记（candidate + failed 回归），passed=false → CLI 3
        app = self.project / "app"
        app.mkdir()
        (app / "x.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.project / "smoke_fail.py").write_text(
            "import app.x\nassert app.x.VALUE == 2\n", encoding="utf-8")
        failed = cli(self.project, "team-integrate", "--candidate", "C1", "--tasks", "t1",
                     "--check-cmd", "python3 smoke_fail.py")
        self.assertEqual(failed.returncode, 3)
        report = json.loads(failed.stdout)
        self.assertFalse(report["passed"])
        self.assertEqual(report["status"], "candidate")
        self.assertNotIn("completed", [r[1] for r in evidence_rows(self.db)])
        # check 门禁审计同样如实报出"集成未完成"（exit 3）
        audit = json.loads(cli(self.project, "check", "--feature", "todo",
                               "--kind", "integration").stdout)
        self.assertFalse(audit["passed"])
        self.assertTrue(any("尚未完成" in v for v in audit["violations"]))
        # 修好命令重跑同候选：版本幂等、回归覆盖为通过 → completed
        (self.project / "smoke_ok.py").write_text("import app.x\nassert app.x.VALUE == 1\n",
                                                  encoding="utf-8")
        ok = cli_json(self.project, "team-integrate", "--candidate", "C1", "--tasks", "t1",
                      "--check-cmd", "python3 smoke_ok.py")
        self.assertTrue(ok["passed"])
        self.assertEqual(ok["status"], "completed")
        self.assertIn(("integration-version:1", "completed"), evidence_rows(self.db))
        audit = cli_json(self.project, "check", "--feature", "todo", "--kind", "integration")
        self.assertTrue(audit["passed"], audit["violations"])

    # ---- SR-05：正常入口路由 ----

    def test_normal_status_on_team_only_project_returns_team_view(self):
        """探针 normal_chat_status_on_team_only_project 转负向：exit 0 + 团队视图。"""
        self.team_init()
        proc = cli(self.project, "status", "--format", "json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        view = json.loads(proc.stdout)
        self.assertEqual(view["store"], str(self.db))
        self.assertEqual([f["feature_id"] for f in view["features"]["items"]], ["todo"])
        self.assertIn("activity", view)
        # markdown（progress 入口缺省格式）同样可用
        markdown = cli(self.project, "status")
        self.assertEqual(markdown.returncode, 0)
        self.assertIn("团队状态", markdown.stdout)
        self.assertIn(str(self.db), markdown.stdout)

    def test_resume_and_check_entries_route_to_team_facts(self):
        """resume 返回门禁续接清单；check 只读审计（违例 exit 3，无写副作用）。"""
        self.team_init()
        plan = cli_json(self.project, "resume")
        self.assertEqual(plan["mode"], "team")
        self.assertEqual(plan["store"], str(self.db))
        self.assertTrue(plan["resume_plan"])
        self.assertIn("team-task-add", plan["resume_plan"][0]["action"])
        audit = cli(self.project, "check", "--feature", "todo")
        self.assertEqual(audit.returncode, 3)  # 无任务 → 违例
        detail = json.loads(audit.stdout)
        self.assertFalse(detail["passed"])

    def test_legacy_project_status_identical_without_team_db(self):
        """legacy 项目（无 team.db）status 输出与 team.db 出现前后逐字段一致。"""
        scope_path = self.project / "scope.json"
        scope_path.write_text(json.dumps(SCOPE, ensure_ascii=False), encoding="utf-8")
        cli_json(self.project, "init", "--input", str(scope_path))
        cli_json(self.project, "confirm", "--revision", "1")
        before = cli(self.project, "status", "--format", "json")
        self.assertEqual(before.returncode, 0)
        self.team_init()
        self.full_chain("x1")
        after = cli(self.project, "status", "--format", "json")
        self.assertEqual(after.returncode, 0)

        def strip_volatile(text_value):
            value = json.loads(text_value)
            def walk(node):
                if isinstance(node, dict):
                    return {k: walk(v) for k, v in node.items()
                            if k not in ("observed_at", "updated_at")}
                if isinstance(node, list):
                    return [walk(item) for item in node]
                return node
            return walk(value)

        self.assertEqual(strip_volatile(after.stdout), strip_volatile(before.stdout),
                         "legacy 项目的 status 被 team.db 出现而改变（双主写入）")


if __name__ == "__main__":
    unittest.main()

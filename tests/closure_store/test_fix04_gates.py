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
  `resume` 返回门禁续接清单；`check` 做只读门禁审计（违例 exit 3，audit_only=true
  标明台账审计≠功能验证）；
- legacy 项目无 team.db：status 逐字节不变；有 team.db 的 legacy 项目 legacy 入口
  也不变（NoDualMaster 契约，详见 test_r02_team_entry.py 的同款对照）。

FIX04-followup（2026-09 复核 §4：批准/集成没有绑定实际被执行的源码）：
- 复核者场景负例：报告后等长同 mtime 改写 app.py → done 拒绝（指出漂移文件+两哈希）；
  重走 report（新 sha）→ 旧批准经 ReviewBoard sha 绑定自动失效 → 重新 approve →
  done 成功 → integrate completed 且证据记录新 sha 集合（合法新版本正向对照）；
- changed_files 越界（allowed_paths 仅 only.py 回报 outside.py）→ report 拒绝；
- integrate 前漂移 → 拒绝 completed；内容恢复后重跑成功；
- check 输出含 audit_only 标记。
"""
import hashlib
import json
import os
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


class GateCliTestBase(unittest.TestCase):
    """公共夹具：临时项目目录 + team-init/full_chain 两条常用链（CLI 真实 subprocess）。"""

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
        """add→ready→running→report→（可选 approve）→done 的合法门禁链。

        FIX04-followup：回报的 changed_files 必须在任务边界内且真实存在（报告时
        采集内容 sha），因此这里创建任务时声明 app/x.py 边界并写入固定内容文件。
        """
        path = self.project / "app" / "x.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = 1\n", encoding="utf-8")
        sha = sha256_text({"task": tid, "files": ["app/x.py"], "body": summary})
        cli_json(self.project, "team-task-add", "--set", tid, "--feature", "todo",
                 "--kind", "impl", "--allowed-paths", "app/x.py")
        cli_json(self.project, "team-task", "--set", tid, "--feature", "todo", "--status", "ready")
        cli_json(self.project, "team-task", "--set", tid, "--feature", "todo", "--status", "running")
        cli_json(self.project, "team-report", "--task", tid, "--outcome", "succeeded",
                 "--summary", summary, "--changed-files", "app/x.py", "--artifact-sha256", sha)
        return tid, sha


class Fix04GateTests(GateCliTestBase):

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
        """review_required 功能：缺回报拒、缺批准拒、自审拒、独立批准后过门。

        FIX04-followup 更新说明：t1 原先无边界的回报（--changed-files 无 allowed_paths、
        文件不存在）在新门禁下被拒——创建时声明边界并写入真实文件，断言内容不变。
        """
        self.team_init(review_required=True)
        cli_json(self.project, "team-task-add", "--set", "t1", "--feature", "todo",
                 "--kind", "impl", "--allowed-paths", "app/x.py")
        cli_json(self.project, "team-task", "--set", "t1", "--feature", "todo", "--status", "ready")
        cli_json(self.project, "team-task", "--set", "t1", "--feature", "todo", "--status", "running")
        # (b) 缺回报
        failed = cli(self.project, "team-task", "--set", "t1", "--feature", "todo", "--status", "done")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("回报", failed.stderr)
        xpy = self.project / "app" / "x.py"
        xpy.parent.mkdir(parents=True, exist_ok=True)
        xpy.write_text("VALUE = 1\n", encoding="utf-8")
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
        app.mkdir(exist_ok=True)  # FIX04-followup：full_chain 已建目录并写入同内容文件
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
        """legacy 项目（无 team.db）status 输出与 team.db 出现前后逐字段一致。

        FIX04-followup 更新说明：full_chain 现在会写入产物文件 app/x.py（报告门禁
        要求真实文件），而 legacy status 指纹覆盖项目文件树——因此在取 before 快照
        前先写入同内容文件，保证对照只隔离 team.db/团队事实这单一变量。
        """
        scope_path = self.project / "scope.json"
        scope_path.write_text(json.dumps(SCOPE, ensure_ascii=False), encoding="utf-8")
        cli_json(self.project, "init", "--input", str(scope_path))
        cli_json(self.project, "confirm", "--revision", "1")
        xpy = self.project / "app" / "x.py"
        xpy.parent.mkdir(parents=True, exist_ok=True)
        xpy.write_text("VALUE = 1\n", encoding="utf-8")
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


class Fix04FollowupBindingTests(GateCliTestBase):
    """FIX04-followup：attempt→工作区文件→sha→批准→done→integrate 的产物绑定链。

    全部经真实 CLI subprocess（每步独立进程），文件真实写入、哈希真实采集：
    复核报告 §4 的复现步骤逐条转负向断言，并保留合法新版本的正向对照。
    """

    @staticmethod
    def sha_bytes(data):
        return hashlib.sha256(data).hexdigest()

    def write(self, rel, content):
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def chain_to_running(self, tid="t", allowed="app.py", review_required=True):
        argv = ["team-init", "--feature", "todo"]
        if review_required:
            argv.append("--review-required")
        argv.append("任务清单")
        cli_json(self.project, *argv)
        cli_json(self.project, "team-task-add", "--set", tid, "--feature", "todo",
                 "--kind", "impl", "--allowed-paths", allowed)
        cli_json(self.project, "team-task", "--set", tid, "--feature", "todo", "--status", "ready")
        cli_json(self.project, "team-task", "--set", tid, "--feature", "todo", "--status", "running")

    def report_succeeded(self, tid, sha):
        return cli_json(self.project, "team-report", "--task", tid, "--outcome", "succeeded",
                        "--summary", "实现", "--changed-files", "app.py",
                        "--artifact-sha256", sha)

    def drift_same_length_same_mtime(self, path, old, new):
        """复核者的等长改写 + mtime 恢复：任何基于时间/大小的启发式都不应有效。"""
        stat = path.stat()
        path.write_text(new, encoding="utf-8")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    def version_level_evidence(self):
        conn = sqlite3.connect("file:%s?mode=ro" % self.db, uri=True)
        try:
            rows = conn.execute(
                "SELECT payload FROM events WHERE idempotency_key LIKE "
                "'team-integrate:version-level:%' ORDER BY seq").fetchall()
        finally:
            conn.close()
        return [json.loads(row[0])["fact"] for row in rows]

    # ---- report 收紧：越界拒绝 + 真实采集 ----

    def test_report_outside_allowed_paths_rejected(self):
        """复核探针 outside_allowed_paths_report：只允许 only.py 的任务回报 outside.py → 拒绝。"""
        self.chain_to_running(tid="t2", allowed="only.py", review_required=False)
        self.write("outside.py", "VALUE = 1\n")
        failed = cli(self.project, "team-report", "--task", "t2", "--outcome", "succeeded",
                     "--summary", "越界", "--changed-files", "outside.py",
                     "--artifact-sha256", self.sha_bytes(b"x"))
        self.assertEqual(failed.returncode, 2)
        self.assertIn("outside.py", failed.stderr)
        self.assertIn("allowed_paths", failed.stderr)
        # 拒绝未落任何事实：边界内文件补报后才有回报（attempt 仍干净可回报）
        self.write("only.py", "VALUE = 1\n")
        report = cli_json(self.project, "team-report", "--task", "t2", "--outcome", "succeeded",
                          "--summary", "合规", "--changed-files", "only.py",
                          "--artifact-sha256", self.sha_bytes(b"only"))
        self.assertEqual(report["file_shas"],
                         {"only.py": self.sha_bytes(b"VALUE = 1\n")})

    def test_report_requires_existing_file_and_records_real_sha(self):
        """报告摘要由真实采集产生：文件不存在拒绝；存在时 file_shas 是报告时内容 sha。"""
        self.chain_to_running(review_required=False)
        failed = cli(self.project, "team-report", "--task", "t", "--outcome", "succeeded",
                     "--summary", "空报告", "--changed-files", "app.py",
                     "--artifact-sha256", self.sha_bytes(b"x"))
        self.assertEqual(failed.returncode, 2)
        self.assertIn("app.py", failed.stderr)
        self.assertIn("不存在", failed.stderr)
        self.write("app.py", "VALUE = 1\n")
        report = self.report_succeeded("t", self.sha_bytes(b"VALUE = 1\n"))
        self.assertEqual(report["file_shas"],
                         {"app.py": self.sha_bytes(b"VALUE = 1\n")})

    def test_report_without_boundary_rejects_changed_files(self):
        """未声明文件边界（allowed_paths 空）的任务不能回报改动文件。"""
        self.chain_to_running(review_required=False)
        cli_json(self.project, "team-task-add", "--set", "nb", "--feature", "todo", "--kind", "impl")
        cli_json(self.project, "team-task", "--set", "nb", "--feature", "todo", "--status", "ready")
        cli_json(self.project, "team-task", "--set", "nb", "--feature", "todo", "--status", "running")
        self.write("app.py", "VALUE = 1\n")
        failed = cli(self.project, "team-report", "--task", "nb", "--outcome", "succeeded",
                     "--summary", "无边界", "--changed-files", "app.py",
                     "--artifact-sha256", self.sha_bytes(b"x"))
        self.assertEqual(failed.returncode, 2)
        self.assertIn("文件边界", failed.stderr)

    # ---- done 门绑定实际产物（复核者场景负例 + 合法重报正向对照） ----

    def test_drifted_file_done_rejected_then_legal_rechain_succeeds(self):
        """复核者场景：VALUE=1→2 等长同 mtime → done 拒绝（文件+两哈希）；重报新版本
        → 旧批准自动失效 → 重新批准 → done 成功 → integrate completed 记录新 sha 集。"""
        self.chain_to_running()
        app = self.write("app.py", "VALUE = 1\n")
        sha1 = self.sha_bytes(b"VALUE = 1\n")
        self.report_succeeded("t", sha1)
        cli_json(self.project, "team-approve", "--task", "t", "--reviewer", "reviewer/Q1",
                 "--verdict", "approved")
        self.drift_same_length_same_mtime(app, "VALUE = 1\n", "VALUE = 2\n")
        sha2 = self.sha_bytes(b"VALUE = 2\n")
        failed = cli(self.project, "team-task", "--set", "t", "--feature", "todo",
                     "--status", "done")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("app.py", failed.stderr)
        self.assertIn(sha1, failed.stderr)
        self.assertIn(sha2, failed.stderr)
        # 重报（新 sha）→ ReviewBoard sha 绑定使旧批准对新 subject 自动失效
        self.report_succeeded("t", sha2)
        failed = cli(self.project, "team-task", "--set", "t", "--feature", "todo",
                     "--status", "done")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("重新审查", failed.stderr)
        # 重新批准 → done 成功（合法新版本正向对照）
        approved = cli_json(self.project, "team-approve", "--task", "t",
                            "--reviewer", "reviewer/Q1", "--verdict", "approved")
        self.assertEqual(approved["subject_sha256"], sha2)
        done = cli_json(self.project, "team-task", "--set", "t", "--feature", "todo",
                        "--status", "done")
        self.assertEqual(done["artifact_sha256"], sha2)
        # integrate：被测内容（当前 app.py VALUE=2）就是批准/报告的新版本 → completed
        integ = cli_json(self.project, "team-integrate", "--candidate", "C1", "--tasks", "t",
                         "--check-cmd", "python3 -c 'import app; assert app.VALUE == 2'",
                         "--cwd", str(self.project))
        self.assertTrue(integ["passed"])
        self.assertEqual(integ["status"], "completed")
        self.assertEqual(integ["regressed_on"]["t"]["artifact_sha256"], sha2)
        self.assertEqual(integ["regressed_on"]["t"]["files"]["app.py"], sha2)
        fact = self.version_level_evidence()[-1]
        self.assertEqual(fact["regressed_on"]["t"]["files"]["app.py"], sha2,
                         "版本级回归证据必须记录'回归运行于 sha 集合 X'")

    def test_done_rejects_missing_changed_file(self):
        """done 时改动文件缺失 → 拒绝并指出文件与报告时哈希。"""
        self.chain_to_running(review_required=False)
        app = self.write("app.py", "VALUE = 1\n")
        sha = self.sha_bytes(b"VALUE = 1\n")
        self.report_succeeded("t", sha)
        app.unlink()
        failed = cli(self.project, "team-task", "--set", "t", "--feature", "todo",
                     "--status", "done")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("app.py", failed.stderr)
        self.assertIn(sha, failed.stderr)

    # ---- integrate 绑定冻结候选 ----

    def test_integrate_rejects_drift_after_done_and_accepts_restored_content(self):
        """done 后集成前漂移 → 拒绝 completed（无完成证据）；内容恢复（同 sha）→
        批准仍对固定版本有效 → 重跑 completed 且 regressed_on 记录报告时 sha。"""
        self.chain_to_running(review_required=False)
        app = self.write("app.py", "VALUE = 1\n")
        sha = self.sha_bytes(b"VALUE = 1\n")
        self.report_succeeded("t", sha)
        cli_json(self.project, "team-task", "--set", "t", "--feature", "todo", "--status", "done")
        self.drift_same_length_same_mtime(app, "VALUE = 1\n", "VALUE = 2\n")
        failed = cli(self.project, "team-integrate", "--candidate", "C1", "--tasks", "t",
                     "--check-cmd", "python3 -c 'import app; assert app.VALUE == 1'",
                     "--cwd", str(self.project))
        self.assertEqual(failed.returncode, 2)
        self.assertIn("偏离报告版本", failed.stderr)
        self.assertNotIn(("integration-version:1", "completed"),
                         [row for row in evidence_rows(self.db)])
        # 内容恢复：当前 sha == 报告/批准 sha → 候选重新有效 → completed
        app.write_text("VALUE = 1\n", encoding="utf-8")
        integ = cli_json(self.project, "team-integrate", "--candidate", "C1", "--tasks", "t",
                         "--check-cmd", "python3 -c 'import app; assert app.VALUE == 1'",
                         "--cwd", str(self.project))
        self.assertEqual(integ["status"], "completed")
        self.assertEqual(integ["regressed_on"]["t"]["files"]["app.py"], sha)

    # ---- check 语义澄清 ----

    def test_check_marks_audit_only(self):
        """check 输出含 audit_only=true：台账审计≠功能验证（违例与通过两种形态都在）。"""
        self.team_init()
        audit = json.loads(cli(self.project, "check", "--feature", "todo").stdout)
        self.assertFalse(audit["passed"])
        self.assertTrue(audit["audit_only"])
        # 合法闭合链后 feature 审计通过——但仍是台账审计（audit_only 不消失）
        self.full_chain("t1")
        cli_json(self.project, "team-approve", "--task", "t1", "--reviewer", "checker/Q2",
                 "--verdict", "approved")
        cli_json(self.project, "team-task", "--set", "t1", "--feature", "todo", "--status", "done")
        passed = cli_json(self.project, "check", "--feature", "todo")
        self.assertTrue(passed["passed"], passed["violations"])
        self.assertTrue(passed["audit_only"])


if __name__ == "__main__":
    unittest.main()

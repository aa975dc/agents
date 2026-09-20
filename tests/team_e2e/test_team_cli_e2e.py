# -*- coding: utf-8 -*-
"""FIX-04/SR-05 端到端：新 team-only 项目从正常 CLI 入口走完整团队闭环。

真实/模拟点（如实声明）：
- 真实：全部团队事实只经 companion.py CLI 门禁动作写入（team-init/add/ready/
  running/report/approve/done/integrate）；两个 worker 是两个独立 Python 进程，
  各自经 P5-03 OwnershipRegistry+WorkspaceManager 真实 git worktree 隔离，只写
  各自 allowed_paths（精确文件清单，不用通配）；审查者经 team-approve 由两个不同
  reviewer 身份落账（实现者自审被拒）；done 门在 CLI 内校验 attempt+回报+批准+证据；
  team-integrate 的版本级回归是真实 subprocess 在集成目录跑冒烟；集成后应用经临时
  端口真实 HTTP 读写；resume/status 在全新进程从 team.db 还原全部事实。
- 模拟：开发者"写代码"由 worker 进程内受控文件写入代替（本机无真实多模型并发）；
  集成目录合并由测试（集成者角色）做纯文件复制——合并不产生团队事实，事实全部
  走 CLI。测试内禁止手写 done：唯一的 done 都由 CLI 门禁放行（负面探针逐条断言）。
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
SCRIPTS = REPO_ROOT / "dev-companion" / "scripts"
COMPANION = SCRIPTS / "companion.py"
EXAMPLE = REPO_ROOT / "dev-companion" / "examples" / "team-e2e-app"

BACKEND, FRONTEND = "impl-backend", "impl-frontend"
BACKEND_PATHS = ["app/store.py", "app/api.py"]
FRONTEND_PATHS = ["web/index.html", "web/app.js"]

WORKER_SCRIPT = '''
import json, sys
from pathlib import Path
sys.path.insert(0, {packages!r})
from agents_kernel import digest
from agents_kernel.execution.isolation import WorkspaceManager, prepare_dispatch
from agents_kernel.services.ownership import OwnershipRegistry

task_id = sys.argv[1]
base = Path(sys.argv[2])
source = Path(sys.argv[3])
claimed = sys.argv[4].split(",")
copies = json.loads(sys.argv[5])

ownership = OwnershipRegistry(store_path=base / "ownership.json")
manager = WorkspaceManager(base / "workspaces", source)
info = prepare_dispatch(task_id, claimed, ownership, manager)  # claim + 真实 git worktree
workspace = Path(info.path)
deliverable = {{}}
for rel, example_rel in copies.items():
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(Path(example_rel).read_text(encoding="utf-8"), encoding="utf-8")
    deliverable[rel] = target.read_text(encoding="utf-8")
sha = digest.digest(deliverable)
changes = manager.changes(task_id)
print(json.dumps({{"task_id": task_id, "sha": sha, "workspace": str(workspace),
                  "changes": changes, "mode": info.mode}}, ensure_ascii=False))
'''


def run_git(*args, cwd):
    subprocess.run(["git"] + list(args), cwd=str(cwd), check=True,
                   capture_output=True, text=True)


class TeamCliEndToEndTests(unittest.TestCase):
    """setUpClass 跑完整链路一次；各 test_* 断言一个侧面。"""

    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()  # Py3.9 无 addClassCleanup
        try:
            cls.R = cls.run_scenario(Path(temp.name))
        finally:
            temp.cleanup()

    @classmethod
    def cli_json(cls, *argv):
        proc = cls.cli(*argv)
        assert proc.returncode == 0, "CLI 失败(%d)：%s%s" % (proc.returncode, proc.stdout, proc.stderr)
        return json.loads(proc.stdout)

    @classmethod
    def cli(cls, *argv):
        return subprocess.run([sys.executable, str(COMPANION),
                               "--project", str(cls.PROJECT), *argv],
                              capture_output=True, text=True, timeout=120)

    @classmethod
    def run_scenario(cls, base):
        R = {}
        # ---- scratch 项目：真实 git 仓库（worktree 来源）+ team-only 事实库 ----
        project = base / "proj"
        project.mkdir()
        (project / "README.md").write_text("团队闭环 demo\n", encoding="utf-8")
        (project / ".gitignore").write_text(".dev-companion/\ndata/\n", encoding="utf-8")
        run_git("init", "-q", cwd=project)
        run_git("config", "user.email", "e2e@example.com", cwd=project)
        run_git("config", "user.name", "e2e", cwd=project)
        run_git("add", "-A", cwd=project)
        run_git("commit", "-qm", "seed", cwd=project)
        cls.PROJECT = project

        worker_file = base / "worker.py"
        worker_file.write_text(WORKER_SCRIPT.format(packages=str(REPO_ROOT / "packages")),
                               encoding="utf-8")
        copies_backend = {rel: str(EXAMPLE.joinpath(*rel.split("/"))) for rel in BACKEND_PATHS}
        copies_frontend = {rel: str(EXAMPLE.joinpath(*rel.split("/"))) for rel in FRONTEND_PATHS}

        # ---- 正常入口即可用：team-only 项目 status/resume 返回团队视图（SR-05 探针） ----
        probe = cls.cli("status", "--format", "json")
        R["status_before_init_exit"] = probe.returncode  # 尚无任何记录 → 引导 init（exit 2）

        # ---- init(团队) + 两任务派发（文件级 allowed_paths，不用通配） ----
        cls.cli_json("team-init", "--feature", "todo", "--review-required", "任务清单应用")
        for tid, paths in ((BACKEND, BACKEND_PATHS), (FRONTEND, FRONTEND_PATHS)):
            cls.cli_json("team-task-add", "--set", tid, "--feature", "todo", "--kind", "impl",
                         "--allowed-paths", ",".join(paths))
            cls.cli_json("team-task", "--set", tid, "--feature", "todo", "--status", "ready")
        R["gate_unknown_task_done"] = cls.cli("team-task", "--set", "ghost", "--feature", "todo",
                                              "--status", "done").returncode
        R["gate_blocked_no_reason"] = cls.cli("team-task", "--set", BACKEND, "--feature", "todo",
                                              "--status", "blocked").returncode

        # ---- 两 worker：各自独立进程，claim + worktree 隔离 + 写各自 allowed_paths ----
        R["workers"] = {}
        for tid, copies in ((BACKEND, copies_backend), (FRONTEND, copies_frontend)):
            cls.cli_json("team-task", "--set", tid, "--feature", "todo", "--status", "running")
            proc = subprocess.run([sys.executable, str(worker_file), tid, str(base),
                                   str(project), ",".join(copies), json.dumps(copies)],
                                  capture_output=True, text=True, timeout=120)
            assert proc.returncode == 0, proc.stderr
            R["workers"][tid] = json.loads(proc.stdout)
            cls.cli_json("team-report", "--task", tid, "--outcome", "succeeded",
                         "--summary", "%s 完成" % tid,
                         "--changed-files", ",".join(sorted(copies)),
                         "--artifact-sha256", R["workers"][tid]["sha"])
        # done 门：缺独立审查时拒绝（review_required 已开）
        R["gate_done_without_approval"] = cls.cli("team-task", "--set", BACKEND,
                                                  "--feature", "todo", "--status", "done").returncode

        # ---- 不同 reviewer 独立批准（自审拒绝探针） ----
        R["self_review_exit"] = cls.cli("team-approve", "--task", BACKEND,
                                        "--reviewer", BACKEND + "#1",
                                        "--verdict", "approved").returncode
        for tid, reviewer in ((BACKEND, "companion-checker/Q-backend"),
                              (FRONTEND, "companion-integrator/Q-frontend")):
            cls.cli_json("team-approve", "--task", tid, "--reviewer", reviewer,
                         "--verdict", "approved")
            cls.cli_json("team-task", "--set", tid, "--feature", "todo", "--status", "done")
        R["gate_done_to_ready"] = cls.cli("team-task", "--set", BACKEND, "--feature", "todo",
                                          "--status", "ready").returncode

        # ---- 集成：合并两个工作区（纯文件复制，不产生事实）→ team-integrate ----
        integrated = base / "integrated"
        integrated.mkdir()
        for tid in (BACKEND, FRONTEND):
            workspace = Path(R["workers"][tid]["workspace"])
            for rel in (BACKEND_PATHS if tid == BACKEND else FRONTEND_PATHS):
                target = integrated / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(workspace / rel, target)
        smoke = ("import json\n"
                 "from app.api import handle\n"
                 "added = handle('add_task', {'title': '买牛奶'})\n"
                 "assert added['task']['title'] == '买牛奶', added\n"
                 "handle('complete_task', {'task_id': 1})\n"
                 "assert json.load(open('data/tasks.json', encoding='utf-8'))[0]['done'] is True\n")
        (integrated / "smoke.py").write_text(smoke, encoding="utf-8")
        # 版本级回归失败 → candidate（exit 3），事实如实
        (integrated / "smoke_bad.py").write_text("assert False, '注入的回归失败'\n", encoding="utf-8")
        failed = cls.cli("team-integrate", "--candidate", "C1", "--tasks", "%s,%s" % (BACKEND, FRONTEND),
                         "--cwd", str(integrated), "--check-cmd", "python3 smoke_bad.py")
        R["failed_integrate_exit"] = failed.returncode
        R["failed_integrate"] = json.loads(failed.stdout)
        # 修好后重跑同候选：版本幂等重建，真实冒烟通过 → completed
        R["integrate"] = cls.cli_json("team-integrate", "--candidate", "C1",
                                      "--tasks", "%s,%s" % (BACKEND, FRONTEND),
                                      "--cwd", str(integrated), "--check-cmd", "python3 smoke.py")
        shutil.rmtree(integrated / "data")

        # ---- 真实 HTTP：completed 版本上起服务，临时端口读写 ----
        shutil.rmtree(integrated / "data", ignore_errors=True)
        server = subprocess.Popen(
            [sys.executable, "-c", "from app.api import serve; serve(port=0)"],
            cwd=str(integrated), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8")
        try:
            port = int(re.search(r"127\.0\.0\.1:(\d+)", server.stdout.readline()).group(1))
            request = urllib.request.Request(
                "http://127.0.0.1:%d/api/tasks" % port,
                data=json.dumps({"action": "add_task",
                                 "payload": {"title": "HTTP 冒烟"}}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                R["http_added"] = json.load(response)
        finally:
            server.terminate()
            server.wait(timeout=5)

        # ---- 跨进程恢复：全新进程 resume/status 还原全部事实 ----
        resume = cls.cli("resume")
        R["resume_exit"] = resume.returncode
        R["resume"] = json.loads(resume.stdout)
        R["status_after"] = cls.cli_json("status", "--format", "json")
        R["check_after"] = cls.cli_json("check", "--feature", "todo", "--kind", "integration")
        R["audit_exit"] = cls.cli("check", "--feature", "todo", "--kind", "integration").returncode
        return R

    # ---- 断言 ----

    def test_normal_entries_route_to_team_mode(self):
        """team-only 项目：status 初始化前 exit 2（无记录），init 后返回团队视图。"""
        self.assertEqual(self.R["status_before_init_exit"], 2)
        self.assertEqual(self.R["status_after"]["store"],
                         str((self.PROJECT / ".dev-companion" / "team.db").resolve()))
        self.assertEqual(self.R["status_after"]["features"]["items"][0]["status"], "draft")
        self.assertEqual(self.R["status_after"]["features"]["items"][0]["feature_id"], "todo")

    def test_two_workers_isolated_by_real_worktrees_exact_paths(self):
        """两个独立进程各自真实 git worktree；变更恰为各自 allowed_paths。"""
        backend = self.R["workers"][BACKEND]
        frontend = self.R["workers"][FRONTEND]
        self.assertEqual(backend["mode"], "git-worktree")
        self.assertEqual(frontend["mode"], "git-worktree")
        self.assertNotEqual(Path(backend["workspace"]), Path(frontend["workspace"]))
        self.assertEqual(backend["changes"], [{"path": "app/", "change": "added"}])
        self.assertEqual(frontend["changes"], [{"path": "web/", "change": "added"}])

    def test_gate_probes_all_rejected(self):
        """门禁探针：未创建 done / 无因 blocked / 缺批准 done / 自审 / done→ready 全拒。"""
        self.assertEqual(self.R["gate_unknown_task_done"], 2)
        self.assertEqual(self.R["gate_blocked_no_reason"], 2)
        self.assertEqual(self.R["gate_done_without_approval"], 2)
        self.assertEqual(self.R["self_review_exit"], 2)
        self.assertEqual(self.R["gate_done_to_ready"], 2)

    def test_integration_two_level_regression_is_real_subprocess(self):
        """版本级回归真实 subprocess：失败保持 candidate（exit 3），修好后 completed。"""
        self.assertEqual(self.R["failed_integrate_exit"], 3)
        self.assertEqual(self.R["failed_integrate"]["status"], "candidate")
        self.assertFalse(self.R["failed_integrate"]["passed"])
        done = self.R["integrate"]
        self.assertEqual(done["status"], "completed")
        self.assertTrue(done["passed"])
        self.assertEqual(done["version_level"]["exit_code"], 0)
        self.assertIn("smoke.py", done["version_level"]["command"],
                      "版本级回归必须记录真实运行的命令")

    def test_http_real_request_on_completed_version(self):
        """completed 版本上真实 HTTP 写入（非 mock）。"""
        self.assertEqual(self.R["http_added"],
                         {"task": {"id": 1, "title": "HTTP 冒烟", "done": False}})

    def test_new_process_resume_and_status_restore_all_facts(self):
        """全新进程 resume/status/check 从 team.db 还原：任务、审查、集成、门禁审计。"""
        resume = self.R["resume"]
        self.assertEqual(resume["mode"], "team")
        self.assertEqual(resume["resume_plan"], [], "全部事实闭合后续接清单应为空")
        view = self.R["status_after"]
        self.assertEqual([(t["task_id"], t["status"]) for t in view["tasks"]["items"]],
                         [(BACKEND, "done"), (FRONTEND, "done")])
        activity = view["activity"]
        for tid in (BACKEND, FRONTEND):
            self.assertEqual(activity["tasks"][tid]["attempt_count"], 1)
            self.assertTrue(activity["tasks"][tid]["reported"])
            self.assertEqual(activity["tasks"][tid]["approval"], "approved")
        self.assertEqual(activity["integrations"],
                         [{"integration_version": 1, "status": "completed",
                           "version_level_passed": True}])
        self.assertEqual(self.R["check_after"]["passed"], True)
        self.assertEqual(self.R["audit_exit"], 0)


if __name__ == "__main__":
    unittest.main()

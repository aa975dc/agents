# -*- coding: utf-8 -*-
"""P5-06：真实团队端到端示例——设计→并行实现→独立审查→集成→真实操作验证。

09 §7「团队模式完成」的可执行证据（AG04/TK02/TK03/TK07/TK08）：被构建对象是
dev-companion/examples/team-e2e-app（任务清单应用：添加/完成/列出，数据存
JSON，含 Web 界面）。任务 DAG（P5-01 scheduler 真实建图与就绪集）：

    design → (impl-backend ‖ impl-frontend) → review → integration

真实模块链路（全部非 mock）：
- 隔离（P5-03）：prepare_dispatch = ownership 声明（backend: app/…；frontend:
  web/…，两集合不相交）+ WorkspaceManager 真实 git worktree；changes 恰报本
  任务目录；结束后 cleanup 且 worktree 元数据 prune。
- 租约（P5-02）：LeaseManager 真实文件锁领取/续约/释放；第二 worker 领取被拒。
- 审查（P5-04）：ReviewBoard 真实流转——自审拒绝（实现者 attempt 任审查者）、
  独立批准后 impl 才能 done；review 任务同样过独立批准门（由集成者身份批准，
  不与被审 attempt 同身份）。
- 集成（P5-05）：候选门（须批准且 sha 一致）、版本幂等（同候选集恒同哈希）、
  两级回归门（version_level 未记录拒绝 complete）→ 真实子进程冒烟后 completed；
  manifest 哈希不含回归记录、重建稳定；check_freshness 无失效。

模拟点（如实声明，见 docs/verification/team-e2e-2026-09-20.md）：开发者产出以
测试内文件写入模拟（把示例代码写进各自隔离工作区），审查者以不同 attempt 身份
角色扮演——本机无真实多模型并发派发；隔离/租约/审查/集成/回归链路本身全部由
真实 kernel 模块执行。持久化接线与 DWF 宿主派发归后续任务。
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel import digest  # noqa: E402
from agents_kernel.contracts import schemas  # noqa: E402
from agents_kernel.domain.handoff import HandoffRegistry  # noqa: E402
from agents_kernel.domain.review_record import attempt_id  # noqa: E402
from agents_kernel.domain.tasks import TaskBoard  # noqa: E402
from agents_kernel.execution.isolation import WorkspaceManager, prepare_dispatch  # noqa: E402
from agents_kernel.execution.lease import LeaseManager  # noqa: E402
from agents_kernel.services.scheduler import (ParallelReady, build_graph,  # noqa: E402
                                              order_ready, ready_set)
from agents_kernel.services.integration import IntegrationBoard  # noqa: E402
from agents_kernel.services.ownership import OwnershipRegistry  # noqa: E402
from agents_kernel.services.review_board import ReviewBoard  # noqa: E402
from agents_kernel.validation import CompanionError  # noqa: E402

EXAMPLE = REPO_ROOT / "dev-companion" / "examples" / "team-e2e-app"
DESIGN, BACKEND, FRONTEND = "design", "impl-backend", "impl-frontend"
REVIEW_TASK, INTEGRATION_TASK = "review", "integration"
BACKEND_PATHS = ["app/store.py", "app/api.py"]
FRONTEND_PATHS = ["web/index.html", "web/app.js"]
T0 = "2026-09-20T10:00:00Z"
Q2 = {"role": "companion-checker", "attempt_id": "CHK#1"}      # 独立代码审查员
I1 = {"role": "companion-integrator", "attempt_id": "INT#1"}   # 集成负责人（批准审查报告）


def run_git(*args, cwd):
    subprocess.run(["git"] + list(args), cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def read_example(*parts):
    return (EXAMPLE.joinpath(*parts)).read_text(encoding="utf-8")


def approve(review_board, tid, attempt_no, sha, reviewer, review_id, reviewed_at=T0):
    """独立审查者对已提交的固定版本落一条批准。"""
    return review_board.record({"review_id": review_id, "subject_type": "attempt",
                                "subject_ref": attempt_id(tid, attempt_no),
                                "subject_sha256": sha, "reviewer": reviewer,
                                "verdict": "approved", "blockers": [],
                                "reviewed_at": reviewed_at})


class TeamEndToEndTest(unittest.TestCase):
    """setUpClass 跑完整团队情景一次；各 test_* 断言一个侧面。"""

    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()  # Py3.9 无 addClassCleanup：情景结束即清理
        try:
            cls.R = cls.run_team_scenario(Path(temp.name))
        finally:
            temp.cleanup()

    @classmethod
    def run_team_scenario(cls, base):
        R = {}

        # ---- 项目底座：真实 git 仓库（工作区来源） ----
        source = base / "project"
        source.mkdir()
        (source / "README.md").write_text("任务清单应用（团队端到端示例）\n", encoding="utf-8")
        run_git("init", "-q", cwd=source)
        run_git("config", "user.email", "team-e2e@example.com", cwd=source)
        run_git("config", "user.name", "team-e2e", cwd=source)
        run_git("add", "-A", cwd=source)
        run_git("commit", "-qm", "init", cwd=source)

        contract = json.loads(read_example("api_contract.json"))
        R["contract_errors"] = schemas.validate_api_contract(contract)

        # ---- 台账与真实模块接线（P5-01/02/03/04/05） ----
        board = TaskBoard()
        board.add_feature("todo-app", "任务清单应用",
                          allowed_paths=BACKEND_PATHS + FRONTEND_PATHS,
                          review_required=True)
        board.add_task(DESIGN, "todo-app", "design")
        board.add_task(BACKEND, "todo-app", "impl", depends_on=[DESIGN])
        board.add_task(FRONTEND, "todo-app", "impl", depends_on=[DESIGN])
        board.add_task(REVIEW_TASK, "todo-app", "review",
                       depends_on=[BACKEND, FRONTEND])
        board.add_task(INTEGRATION_TASK, "todo-app", "integration",
                       depends_on=[REVIEW_TASK])

        graph = build_graph(board.tasks())
        R["graph"] = {tid: list(deps) for tid, deps in graph.items()}

        def ready():
            return [task["id"] for task in order_ready(ready_set(board.tasks()))]

        R["ready_initial"] = ready()

        registry = HandoffRegistry()
        review_board = ReviewBoard(board, registry)
        board.review_guard = review_board.require_approval
        integration = IntegrationBoard(board, review_board)
        ownership = OwnershipRegistry(store_path=base / "ownership.json")
        leases = LeaseManager(base / "leases")
        manager = WorkspaceManager(base / "workspaces", source)
        R["workspace_mode"] = manager.plan(BACKEND)["mode"]

        # ---- design：冻结契约（HandoffRegistry 真实登记校验） ----
        board.transition_task(DESIGN, "ready")
        board.transition_task(DESIGN, "running")
        board.start_attempt(DESIGN, T0)
        R["contract_entry"] = registry.register("api_contract", contract["contract_id"],
                                                contract, 1)
        registry.require_current(contract["contract_id"],
                                 R["contract_entry"]["sha256"])  # 实现者核对冻结契约
        R["contract_sha"] = digest.digest(contract)
        design_deliverable = {"prototype.md": read_example("prototype.md"),
                              "api_contract.json": contract}
        R["design_sha"] = digest.digest(design_deliverable)
        board.finish_attempt(DESIGN, "succeeded")  # design 不经审查门（P5-04 语义）
        R["ready_after_design"] = ready()
        R["parallel_view"] = [task["id"] for task in ParallelReady(board).ready()]

        # ---- 两路并行实现：各自隔离工作区 + 租约 + 独立审查 ----
        plans = ((BACKEND, BACKEND_PATHS,
                  {"app/store.py": ("app", "store.py"), "app/api.py": ("app", "api.py")},
                  "B2-worker", "rev-backend"),
                 (FRONTEND, FRONTEND_PATHS,
                  {"web/index.html": ("web", "index.html"), "web/app.js": ("web", "app.js")},
                  "F2-worker", "rev-frontend"))
        R["workers"] = {}
        for tid, claimed, copies, worker_id, review_id in plans:
            info = prepare_dispatch(tid, claimed, ownership, manager)  # claim+隔离（P5-03）
            workspace = Path(info.path)
            leases.acquire(tid, worker_id, ttl=120)                    # 领租约（P5-02）
            if tid == BACKEND:  # 第二 worker 领取必须被拒（真实文件锁）
                try:
                    leases.acquire(tid, "B2-other-worker", ttl=120)
                    raise AssertionError("第二 worker 领取租约未被拒绝")
                except CompanionError as error:
                    R["second_worker_error"] = str(error)
            lease = leases.renew(tid, worker_id, ttl=120)
            R["workers"][tid] = {"lease_epoch": lease.epoch, "claimed": claimed}

            board.transition_task(tid, "ready")
            board.transition_task(tid, "running")
            attempt = board.start_attempt(tid, T0)
            deliverable = {}
            for workspace_rel, example_parts in copies.items():
                target = workspace / workspace_rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(read_example(*example_parts),  # 模拟开发者产出（见模块注释）
                                  encoding="utf-8")
                deliverable[workspace_rel] = target.read_text(encoding="utf-8")
            sha = digest.digest(deliverable)

            review_board.submit_for_review(attempt, sha)
            if tid == BACKEND:  # 自审拒绝：实现者 attempt 不得任审查者
                try:
                    approve(review_board, tid, attempt["attempt_no"], sha,
                            {"role": "companion-developer",
                             "attempt_id": attempt_id(tid, attempt["attempt_no"])},
                            review_id="rev-self-" + tid)
                    raise AssertionError("实现者自审未被拒绝")
                except CompanionError as error:
                    R["self_review_error"] = str(error)
                R["backend_verdict_before_independent"] = \
                    review_board.current_approval(tid)["status"]
            approve(review_board, tid, attempt["attempt_no"], sha, Q2, review_id=review_id)
            verdict = review_board.current_approval(tid)
            R["workers"][tid].update({
                "sha": sha,
                "changes": manager.changes(tid),
                "workspace_files": sorted(
                    str(path.relative_to(workspace)) for path in workspace.rglob("*")
                    if path.is_file() and ".git" not in path.parts),
                "verdict": verdict["status"],
                "approval_sha": verdict["subject_sha256"]})
            board.finish_attempt(tid, "succeeded")
            leases.release(tid, worker_id)
            R["workers"][tid]["lease_state_after_release"] = leases.state(tid)
            ownership.release(tid)

        R["ready_after_impls"] = ready()

        # ---- review 任务（Q2 汇总报告；同样过独立批准门） ----
        board.transition_task(REVIEW_TASK, "ready")
        board.transition_task(REVIEW_TASK, "running")
        review_attempt = board.start_attempt(REVIEW_TASK, T0)
        report = {"reviews": [
            {"task_id": tid, "subject_ref": attempt_id(tid, 1),
             "subject_sha256": R["workers"][tid]["sha"],
             "verdict": R["workers"][tid]["verdict"]}
            for tid in (BACKEND, FRONTEND)]}
        R["review_sha"] = digest.digest(report)
        review_board.submit_for_review(review_attempt, R["review_sha"])
        approve(review_board, REVIEW_TASK, review_attempt["attempt_no"], R["review_sha"],
                I1, review_id="rev-report")
        R["review_verdict"] = review_board.current_approval(REVIEW_TASK)["status"]
        board.finish_attempt(REVIEW_TASK, "succeeded")

        # ---- integration：合并隔离产物 → 候选 → 版本 → 两级回归 → completed ----
        board.transition_task(INTEGRATION_TASK, "ready")
        board.transition_task(INTEGRATION_TASK, "running")
        integration_attempt = board.start_attempt(INTEGRATION_TASK, T0)
        backend_entry, frontend_entry = manager.entries()  # 按 task_id 排序
        merged = base / "integration"
        merged.mkdir()
        shutil.copytree(Path(backend_entry["path"]) / "app", merged / "app")
        shutil.copytree(Path(frontend_entry["path"]) / "web", merged / "web")

        shas = {BACKEND: R["workers"][BACKEND]["sha"],
                FRONTEND: R["workers"][FRONTEND]["sha"]}
        integration.add_candidate("C1", shas)   # 候选门：批准有效且 sha 一致才入列
        manifest = integration.build_integration_version(["C1"])
        R["integration_version"] = manifest["integration_version"]
        R["candidate_hash"] = manifest["manifest_sha256"]
        again = integration.build_integration_version(["C1"])   # 重建幂等
        R["rebuild_hash_equal"] = (again["manifest_sha256"] == R["candidate_hash"])
        try:    # 版本级回归未记录 → 拒绝完成（G-INTEGRATION）
            integration.complete(1)
            raise AssertionError("version_level 未记录时 complete 未被拒绝")
        except CompanionError as error:
            R["complete_before_regression_error"] = str(error)

        smoke = (
            "import json, sys\n"
            "from app.api import handle\n"
            "added = handle('add_task', {'title': '买牛奶'})\n"
            "assert added['task'] == {'id': 1, 'title': '买牛奶', 'done': False}, added\n"
            "handle('complete_task', {'task_id': 1})\n"
            "listed = handle('list_tasks')\n"
            "assert listed == {'tasks': [{'id': 1, 'title': '买牛奶', 'done': True}]}, listed\n"
            "persisted = json.load(open('data/tasks.json', encoding='utf-8'))\n"
            "assert persisted[0]['done'] is True, persisted\n"
            "print(json.dumps(listed, ensure_ascii=False))\n")
        proc = subprocess.run([sys.executable, "-c", smoke], cwd=str(merged),
                              capture_output=True, text=True, encoding="utf-8")
        R["smoke"] = {"exit_code": proc.returncode, "stdout": proc.stdout.strip(),
                      "stderr": proc.stderr.strip()[-400:]}
        integration.record_feature_level(1, [{"feature_id": "todo-app", "status": "current"}])
        integration.record_version_level(1, proc.returncode == 0,
                                         detail="版本级回归：真实子进程在集成目录运行"
                                                "契约入口冒烟（import + 契约调用 + JSON 落盘读回）")
        R["completed_manifest"] = integration.complete(1)
        R["completed_hash_equal"] = (R["completed_manifest"]["manifest_sha256"]
                                     == R["candidate_hash"])  # 哈希不含回归记录
        board.finish_attempt(INTEGRATION_TASK, "succeeded")
        R["freshness"] = integration.check_freshness(shas)

        # ---- 真实操作验证：completed 版本上全新子进程 + 临时端口 HTTP ----
        shutil.rmtree(merged / "data")   # 全新数据状态再验一次
        fresh = subprocess.run([sys.executable, "-c",
                                "from app.api import handle; import json;"
                                " print(json.dumps(handle('list_tasks'), ensure_ascii=False))"],
                               cwd=str(merged), capture_output=True, text=True,
                               encoding="utf-8")
        R["fresh_run"] = {"exit_code": fresh.returncode, "stdout": fresh.stdout.strip()}
        server = subprocess.Popen([sys.executable, "-c",
                                   "from app.api import serve; serve(port=0)"],
                                  cwd=str(merged), stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        try:
            port = int(re.search(r"127\.0\.0\.1:(\d+)",
                                 server.stdout.readline()).group(1))
            base_url = "http://127.0.0.1:%d" % port
            page = urllib.request.urlopen(base_url + "/", timeout=5).read().decode()
            with urllib.request.urlopen(base_url + "/app.js", timeout=5) as response:
                appjs_status = response.status
            request = urllib.request.Request(
                base_url + "/api/tasks",
                data=json.dumps({"action": "add_task",
                                 "payload": {"title": "写周报"}}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                added = json.load(response)
            request = urllib.request.Request(
                base_url + "/api/tasks",
                data=json.dumps({"action": "add_task",
                                 "payload": {"title": "  "}}).encode(),
                headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(request, timeout=5)
                refused = {}
            except urllib.error.HTTPError as exc:
                refused = {"status": exc.code, "error": json.load(exc)["error"]}
            R["http"] = {"port": port, "page_has_title": "任务清单" in page,
                         "page_links_appjs": "/app.js" in page,
                         "appjs_status": appjs_status, "added": added, "refused": refused}
        finally:
            server.terminate()
            server.wait(timeout=5)

        # ---- 收尾：在册清理、worktree prune、claim/租约归零 ----
        manager.cleanup(BACKEND)
        manager.cleanup(FRONTEND)
        R["workspace_entries_after_cleanup"] = [entry["task_id"] for entry in manager.entries()]
        R["ownership_after"] = ownership.state()
        R["final_statuses"] = {task["id"]: task["status"] for task in board.tasks()}
        return R

    # ---- 各侧面断言 ----

    def test_contract_and_design_freeze(self):
        """示例契约过真实 schema 校验并经 HandoffRegistry 冻结登记。"""
        self.assertEqual(self.R["contract_errors"], [])
        self.assertEqual(self.R["contract_entry"]["sha256"], self.R["contract_sha"])
        self.assertEqual(self.R["contract_entry"]["version"], 1)

    def test_scheduler_dag_drives_dispatch(self):
        """真实建图 + 就绪集：design 先行；design 后两 impl 同时就绪（并行视图）。"""
        self.assertEqual(self.R["ready_initial"], [DESIGN])
        self.assertEqual(self.R["graph"][BACKEND], [DESIGN])
        self.assertEqual(self.R["graph"][FRONTEND], [DESIGN])
        self.assertEqual(self.R["graph"][REVIEW_TASK], [BACKEND, FRONTEND])
        self.assertEqual(self.R["graph"][INTEGRATION_TASK], [REVIEW_TASK])
        self.assertEqual(self.R["ready_after_design"], [BACKEND, FRONTEND])
        self.assertEqual(self.R["parallel_view"], [BACKEND, FRONTEND])
        self.assertEqual(self.R["ready_after_impls"], [REVIEW_TASK])

    def test_isolated_workspaces_disjoint_ownership(self):
        """两 impl 各自真实 git worktree；ownership 不相交；变更恰为本任务目录。"""
        self.assertEqual(self.R["workspace_mode"], "git-worktree")
        backend = self.R["workers"][BACKEND]
        frontend = self.R["workers"][FRONTEND]
        self.assertEqual(set(backend["claimed"]) & set(frontend["claimed"]), set())
        self.assertEqual(backend["changes"], [{"path": "app/", "change": "added"}])
        self.assertEqual(frontend["changes"], [{"path": "web/", "change": "added"}])
        self.assertEqual(backend["workspace_files"],
                         ["README.md", "app/api.py", "app/store.py"])
        self.assertEqual(frontend["workspace_files"],
                         ["README.md", "web/app.js", "web/index.html"])
        self.assertEqual(self.R["workspace_entries_after_cleanup"], [])  # 已清理并 prune

    def test_lease_guards_worker_claim(self):
        """真实文件租约：领取/续约/释放；第二 worker 领取被拒。"""
        lease = self.R["workers"][BACKEND]
        self.assertGreaterEqual(lease["lease_epoch"], 1)
        self.assertIn("租约未过期", self.R["second_worker_error"])
        self.assertIsNone(lease["lease_state_after_release"])

    def test_review_gate_self_review_refused_then_independent_approval(self):
        """自审拒绝；独立批准后才 done；review 任务亦由集成者身份独立批准。"""
        self.assertIn("自审", self.R["self_review_error"])
        self.assertEqual(self.R["backend_verdict_before_independent"], "none")
        self.assertEqual(self.R["workers"][BACKEND]["verdict"], "approved")
        self.assertEqual(self.R["workers"][FRONTEND]["verdict"], "approved")
        self.assertEqual(self.R["workers"][BACKEND]["approval_sha"],
                         self.R["workers"][BACKEND]["sha"])
        self.assertEqual(self.R["review_verdict"], "approved")

    def test_integration_version_hash_stable_and_completed(self):
        """候选门→版本幂等→两级回归→completed；哈希不含回归记录且重建稳定。"""
        manifest = self.R["completed_manifest"]
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["integration_version"], 1)
        self.assertTrue(self.R["rebuild_hash_equal"])
        self.assertTrue(self.R["completed_hash_equal"])
        self.assertIn("version_level 回归未记录",
                      self.R["complete_before_regression_error"])
        self.assertEqual(self.R["smoke"]["exit_code"], 0)
        self.assertEqual(json.loads(self.R["smoke"]["stdout"]),
                         {"tasks": [{"id": 1, "title": "买牛奶", "done": True}]})
        self.assertEqual(self.R["freshness"], [])  # 已完成版本对批准 sha 仍新鲜

    def test_integrated_app_runs_for_real(self):
        """completed 版本真实可运行：全新子进程一次 API 调用 + 临时端口 HTTP。"""
        self.assertEqual(self.R["fresh_run"], {"exit_code": 0, "stdout": '{"tasks": []}'})
        http = self.R["http"]
        self.assertTrue(http["page_has_title"] and http["page_links_appjs"])
        self.assertEqual(http["appjs_status"], 200)
        self.assertEqual(http["added"], {"task": {"id": 1, "title": "写周报", "done": False}})
        self.assertEqual(http["refused"]["status"], 422)
        self.assertIn("任务标题不能为空", http["refused"]["error"])

    def test_all_tasks_done_and_books_drained(self):
        """终态：五任务全 done；ownership 与工作区台账清空。"""
        self.assertEqual(self.R["final_statuses"],
                         {DESIGN: "done", BACKEND: "done", FRONTEND: "done",
                          REVIEW_TASK: "done", INTEGRATION_TASK: "done"})
        self.assertEqual(self.R["ownership_after"], [])


if __name__ == "__main__":
    unittest.main()

"""Z24 收尾（R04）：把统一状态转换规则接入 legacy block/feedback 入口。

== 差异分析（改前 core.py，行为以基线 15df3f2 为准） ==

| 入口     | idle 前置                    | 目标状态 | 清理的当前证据                            |
|----------|------------------------------|----------|-------------------------------------------|
| feedback | 要求（require_idle，全项目） | blocked  | acceptance + verification + integration   |
| block    | 不要求                       | blocked  | 仅 acceptance（verification/integration 残留）|

1. idle 前置差异是语义不是缺陷：feedback 是返工语义（"已交付的成果要重做"），
   前提是没有在途执行，否则一边记录返工一边有人还在写入；block 是阻断语义
   （"现在就把受阻事实记下来"），允许对仍在写入的执行做标记——实际停止与状态
   标记分开：block 不杀进程、不假装进程已停（返回消息要求人工确认实际写入已停）。
2. 证据清理不对称是真缺陷：block 后残留的 verification/integration 会让 status
   展示与发布 binding（releases._current 的 check_ids/integration_ids）继续引用
   已失效证据。统一为共用 Project._invalidate_eligibility：清 acceptance，并使
   verification/integration 引用一并失效。失效的是"当前资格"，不是历史：
   state.events 只追加（checked/accepted 事件原样保留），journey 历史不删。
3. 转换校验统一：两入口都先过 agents_kernel.domain.tasks.check_transition（与
   内核同一校验器）+ core.LEGACY_TRANSITIONS 白名单。legacy 五状态与内核 Task
   状态名不同构（awaiting_review/accepted 是交付验收语义，内核 Task 无对应状态），
   故复用的是校验器与"进入 blocked 必须经显式原因的统一入口"这一规则，legacy 表
   在 core.py 就地定义；blocked 可再 block 与内核 _BLOCKABLE_FROM 含 blocked 一致，
   legacy 的 accepted 非终态（验收后可打回返工）是保留的兼容语义。
4. 迟到回报：receipt 本有四重门（status==running、run_id、scope_version、规划
   指纹），block 后 status!=running、重派后 run_id 变化，旧 run 回报天然被拒；
   不新增资格代号（现有门已覆盖，避免第二套机制），本文件以测试锁定该行为。

所有用例经 legacy CLI 入口（core.Project 方法与 companion.py 子进程）触发，
不直接调用内核 TaskBoard（内核自身行为归 tests/tasks/）。
"""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dev-companion" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import core
from core import LEGACY_TRANSITIONS, CompanionError, Project
from journey import Journey


def plan(project, scope):
    """完成六阶段产品设计（含 technical 的接口约定），使 check --kind integration 可用。"""
    (project.root / "prototype.md").write_text("原型：输入 [20, 30]，输出 50。\n")
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
        ("prototype", {"details": {"screens": "命令行输入", "states": "成功与无效输入", "walkthrough": "样例20+30=50"}, "feature_ids": ids, "artifacts": ["prototype.md"]}),
        ("technical", {"details": {"architecture": "Python本地模块", "data_model": "金额列表", "release_target": "本地演示"}, "scope": scope,
                       "interfaces": [{"feature_id": f["id"], "kind": "local", "contract": "金额列表返回总和",
                                       "check_commands": f["check_commands"]} for f in scope["features"]]})]
    for stage, raw in stages:
        raw.update(summary=stage + "成果", open_questions=[], decisions=[])
        raw.setdefault("artifacts", [])
        revision = (journey.status() or {}).get("revision", 0)
        journey.save(stage, raw, revision, complete=True, user_confirmed=True)
    return journey


class Z24ClosureTests(unittest.TestCase):
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

    def bootstrap(self):
        self.project.init(self.scope)
        self.project.confirm(1)

    def start(self):
        self.bootstrap()
        return self.project.packet("sum")

    def dispatch_and_implement(self):
        """重新派发并实现一轮（每轮先清掉上轮产物，保证 changed_files 与基线一致）。"""
        (self.root / "ledger.py").unlink(missing_ok=True)
        packet = self.project.packet("sum")
        (self.root / "ledger.py").write_text("def total(values): return sum(values)\n")
        self.project.receipt({"feature_id": "sum", "scope_version": packet["scope_version"], "run_id": packet["run_id"],
                              "status": "implemented", "summary": "实现合计", "changed_files": ["ledger.py"], "evidence_files": []})
        return packet

    def full_accepted(self):
        """带 journey 的完整链路：实现 → 两级检查 → 验收（有 acceptance+verification+integration）。"""
        plan(self.project, self.scope)
        self.bootstrap()
        self.dispatch_and_implement()
        self.project.check("sum")
        self.project.check("sum", kind="integration")
        self.project.accept("sum", "检查与联调通过")
        task = self.project.load()["tasks"]["sum"]
        self.assertEqual(task["status"], "accepted")
        self.assertIn("acceptance", task)
        self.assertIn("verification", task)
        self.assertIn("integration", task)

    def journey_digest(self):
        return hashlib.sha256((self.project.data / "journey.json").read_bytes()).hexdigest()

    # ---- 转换矩阵（合法/非法，全部经 legacy 入口触发） ----

    def test_legal_transition_matrix(self):
        self.bootstrap()
        # pending → blocked（未派发先受阻）
        self.project.block("sum", "依赖资料未到位")
        self.assertEqual(self.project.load()["tasks"]["sum"]["status"], "blocked")
        # blocked → running（重新派发解除阻断）
        self.dispatch_and_implement()  # blocked → running → awaiting_review
        self.assertEqual(self.project.load()["tasks"]["sum"]["status"], "awaiting_review")
        # awaiting_review → blocked
        self.project.block("sum", "发现缺陷")
        self.assertEqual(self.project.load()["tasks"]["sum"]["status"], "blocked")
        # blocked → running → blocked（执行者自己回报 blocked；本轮零修改）
        packet = self.project.packet("sum")
        self.project.receipt({"feature_id": "sum", "scope_version": packet["scope_version"], "run_id": packet["run_id"],
                              "status": "blocked", "summary": "做不下去", "blocker": "缺依赖", "changed_files": [], "evidence_files": []})
        self.assertEqual(self.project.load()["tasks"]["sum"]["status"], "blocked")
        # blocked → blocked（再次 block，更新原因；与内核 _BLOCKABLE_FROM 含 blocked 一致）
        self.project.block("sum", "换一个更准确的原因")
        self.assertEqual(self.project.load()["tasks"]["sum"]["blocker"], "换一个更准确的原因")
        # blocked → running → awaiting_review → awaiting_review（重复检查）
        self.dispatch_and_implement()
        self.assertTrue(self.project.check("sum")["passed"])
        self.assertEqual(self.project.load()["tasks"]["sum"]["status"], "awaiting_review")
        # awaiting_review → accepted
        self.project.accept("sum", "试用通过")
        self.assertEqual(self.project.load()["tasks"]["sum"]["status"], "accepted")
        # accepted → awaiting_review（对已验收再次 check 即打回复查）
        self.assertTrue(self.project.check("sum")["passed"])
        self.assertEqual(self.project.load()["tasks"]["sum"]["status"], "awaiting_review")
        # 回到 accepted，再经 feedback 打回（accepted → blocked）
        self.project.accept("sum", "再次验收通过")
        self.project.feedback("sum", "defect", "边界输入有误")
        self.assertEqual(self.project.load()["tasks"]["sum"]["status"], "blocked")

    def test_illegal_transitions_refused(self):
        self.bootstrap()
        # pending → awaiting_review/accepted：没有制作回报，check/accept 都拒绝
        with self.assertRaisesRegex(CompanionError, "先完成当前制作回报"):
            self.project.check("sum")
        with self.assertRaisesRegex(CompanionError, "缺少当前内容的成功检查"):
            self.project.accept("sum", "凭空验收")
        # running →（除 awaiting_review/blocked 外）任何直接跳转：packet/feedback 要求 idle
        running = self.project.packet("sum")
        with self.assertRaisesRegex(CompanionError, "还有制作任务进行中"):
            self.project.packet("sum")
        with self.assertRaisesRegex(CompanionError, "还有制作任务进行中"):
            self.project.feedback("sum", "defect", "执行中不能返工")
        # 先把在途执行按正常路径收尾，再验证 accepted 相关的非法转换
        (self.root / "ledger.py").write_text("def total(values): return sum(values)\n")
        self.project.receipt({"feature_id": "sum", "scope_version": running["scope_version"], "run_id": running["run_id"],
                              "status": "implemented", "summary": "实现合计", "changed_files": ["ledger.py"], "evidence_files": []})
        # accepted → accepted：没有新检查不能重复验收；accepted → blocked 经 block 合法（对照）
        self.project.check("sum")
        self.project.accept("sum", "第一次验收")
        with self.assertRaisesRegex(CompanionError, "缺少当前内容的成功检查"):
            self.project.accept("sum", "重复验收")
        # blocked → accepted / awaiting_review：block 后验收与检查都拒绝
        self.project.block("sum", "验收后受阻")
        with self.assertRaisesRegex(CompanionError, "先完成当前制作回报"):
            self.project.check("sum")
        with self.assertRaisesRegex(CompanionError, "缺少当前内容的成功检查"):
            self.project.accept("sum", "受阻后仍想验收")
        # → blocked 必须带显式原因（block 与 feedback 同一规则）
        with self.assertRaisesRegex(CompanionError, "阻塞原因不能为空"):
            self.project.block("sum", "  ")
        with self.assertRaisesRegex(CompanionError, "反馈说明不能为空"):
            self.project.feedback("sum", "defect", "")

    def test_wiring_goes_through_kernel_validator(self):
        """证明 legacy 入口确实路由到内核 check_transition（同一校验器），而非绕过。"""
        calls = []
        real = core.check_transition

        def spy(table, from_status, to_status, what):
            calls.append((table is LEGACY_TRANSITIONS, from_status, to_status, what))
            return real(table, from_status, to_status, what)

        self.bootstrap()
        with patch("core.check_transition", spy):
            self.project.block("sum", "接线验证")
        self.assertEqual(calls, [(True, "pending", "blocked", "任务")])
        # 白名单闭合性：五状态之外的目标/来源都不存在，→ blocked 从五个状态都可达
        states = {"pending", "running", "awaiting_review", "accepted", "blocked"}
        self.assertEqual(set(LEGACY_TRANSITIONS), states)
        for from_status, targets in LEGACY_TRANSITIONS.items():
            self.assertTrue(targets <= states)
            self.assertIn("blocked", targets)

    # ---- 资格失效与历史保留 ----

    def test_block_invalidates_all_current_evidence(self):
        self.full_accepted()
        before_events = copy.deepcopy(self.project.load()["events"])
        journey_before = self.journey_digest()
        result = self.project.block("sum", "上线前发现阻塞")
        # 当前资格失效：acceptance/verification/integration 全部不可再用于验收或发布
        task = self.project.load()["tasks"]["sum"]
        self.assertEqual(task["status"], "blocked")
        for key in ("acceptance", "verification", "integration"):
            self.assertNotIn(key, task)
        view = self.project.status()
        self.assertEqual(view["counts"]["blocked"], 1)
        self.assertEqual(view["overall_percent"], 0)
        self.assertIsNone(view["features"][0]["verification"])
        self.assertIsNone(view["features"][0]["acceptance"])
        self.assertEqual(view["features"][0]["blocker"], "上线前发现阻塞")
        with self.assertRaisesRegex(CompanionError, "缺少当前内容的成功检查|先完成当前制作回报"):
            self.project.accept("sum", "沿用旧证据验收")
        # 历史证据一律保留：既有事件逐条不变（哈希不变），只追加一条 blocked 事件
        after_events = self.project.load()["events"]
        self.assertEqual(after_events[:len(before_events)], before_events)
        self.assertEqual([e["kind"] for e in after_events[len(before_events):]], ["blocked"])
        self.assertEqual(self.journey_digest(), journey_before)  # journey 记录未动

    def test_feedback_invalidates_all_current_evidence_and_keeps_history(self):
        self.full_accepted()
        before_events = copy.deepcopy(self.project.load()["events"])
        journey_before = self.journey_digest()
        result = self.project.feedback("sum", "defect", "负数未拒绝")
        self.assertEqual(result["status"], "blocked")
        task = self.project.load()["tasks"]["sum"]
        for key in ("acceptance", "verification", "integration"):
            self.assertNotIn(key, task)
        self.assertEqual(task["feedback"][-1]["note"], "负数未拒绝")
        after_events = self.project.load()["events"]
        self.assertEqual(after_events[:len(before_events)], before_events)
        self.assertEqual([e["kind"] for e in after_events[len(before_events):]], ["feedback_recorded"])
        self.assertEqual(self.journey_digest(), journey_before)

    def test_feedback_idle_precondition_still_enforced(self):
        self.start()  # running
        with self.assertRaisesRegex(CompanionError, "还有制作任务进行中"):
            self.project.feedback("sum", "defect", "执行未停不能返工")
        # block 无此前置：同一场执行可以被标记为受阻（对照，差异是语义不是不对称）
        result = self.project.block("sum", "执行中记录阻塞")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("不会替你终止", result["message"])

    # ---- 迟到旧 run 回报拒绝 ----

    def test_late_receipt_after_block_rejected_and_redispatch_rotates_run(self):
        packet = self.start()
        stale = {"feature_id": "sum", "scope_version": packet["scope_version"], "run_id": packet["run_id"],
                 "status": "implemented", "summary": "迟到的旧回报", "changed_files": [], "evidence_files": []}
        self.project.block("sum", "执行已中断")
        with self.assertRaisesRegex(CompanionError, "执行回报重复、过期或不属于当前任务"):
            self.project.receipt(stale)
        # 阻断后重新派发：run_id 轮换，旧 run 回报仍被拒，当前 run 回报正常受理
        self.dispatch_and_implement()
        with self.assertRaisesRegex(CompanionError, "执行回报重复、过期或不属于当前任务"):
            self.project.receipt(stale)
        self.assertEqual(self.project.load()["tasks"]["sum"]["status"], "awaiting_review")

    def test_late_receipt_rejected_through_cli(self):
        """真实用户入口：companion.py 子进程里 block 后旧 run 回报被拒（exit 2）。"""
        packet = self.start()
        (self.root / "ledger.py").write_text("def total(values): return sum(values)\n")
        stale = {"feature_id": "sum", "scope_version": packet["scope_version"], "run_id": packet["run_id"],
                 "status": "implemented", "summary": "迟到回报", "changed_files": ["ledger.py"], "evidence_files": []}
        (self.root / "stale-receipt.json").write_text(json.dumps(stale, ensure_ascii=False))

        def cli(*arguments):
            return subprocess.run([sys.executable, str(SCRIPTS / "companion.py"), "--project", str(self.root), *arguments],
                                  capture_output=True, text=True)
        blocked = cli("block", "--feature", "sum", "--reason", "CLI阻断")
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        view = json.loads(cli("status", "--format", "json").stdout)
        self.assertEqual(view["counts"]["blocked"], 1)
        late = cli("receipt", "--input", "stale-receipt.json")
        self.assertEqual(late.returncode, 2)
        self.assertFalse(json.loads(late.stderr)["success"])

    # ---- 进程存活与状态标记分开 ----

    def test_block_does_not_kill_in_flight_process_or_accept_its_output(self):
        self.start()  # running，执行进程在途
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.5); open('ledger.py', 'w').write('late write\\n')"],
            cwd=self.root)
        self.addCleanup(lambda: (child.kill(), child.wait()) if child.poll() is None else None)
        self.project.block("sum", "执行在途时记录阻塞")
        # block 不杀进程：记录状态后进程仍存活
        self.assertIsNone(child.poll())
        self.assertTrue(child.wait(timeout=10) == 0)
        # 在途进程的产物已落盘，但不改变 blocked 资格：不复活验收，也不能凭它验收
        self.assertTrue((self.root / "ledger.py").exists())
        task = self.project.load()["tasks"]["sum"]
        self.assertEqual(task["status"], "blocked")
        for key in ("acceptance", "verification", "integration"):
            self.assertNotIn(key, task)
        view = self.project.status()
        self.assertEqual(view["counts"]["blocked"], 1)
        self.assertEqual(view["overall_percent"], 0)
        with self.assertRaisesRegex(CompanionError, "缺少当前内容的成功检查|先完成当前制作回报"):
            self.project.accept("sum", "进程说它做完了")


if __name__ == "__main__":
    unittest.main()

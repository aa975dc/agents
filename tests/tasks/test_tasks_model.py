# -*- coding: utf-8 -*-
"""P5-01：Feature/Task/Attempt 三层模型与状态转换白名单（C01/TK01/TK08 前半）。

覆盖：白名单外转换拒绝（含进入 blocked/cancelled 必须走 block()/cancel()
统一入口，Z24）；attempt 递增与 task 最终状态独立（3 次 attempt 失败 →
task failed，计数不改变完成度语义，TK08）；allowed_paths 精确路径闭集；
与 P2-02 事件库的 to_event_payload/from_event 纯函数往返一致。
"""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel.domain import tasks as tasks_domain
from agents_kernel.domain.tasks import (TaskBoard, check_transition, from_event,
                                        to_event_payload)
from agents_kernel.domain.views import EVENT_FEATURE_STATUS, EVENT_TASK_STATUS
from agents_kernel.validation import CompanionError

T0 = "2026-09-20T10:00:00Z"


def make_board(*task_specs):
    board = TaskBoard()
    board.add_feature("f1", "示例功能")
    for spec in task_specs:
        board.add_task(**spec)
    return board


def drive(board, tid, outcome, failure_reason=None, started_at=T0):
    """把任务推到 running 并结束一次 attempt（ready→running→done/failed）。"""
    board.transition_task(tid, "ready")
    board.transition_task(tid, "running")
    board.start_attempt(tid, started_at)
    return board.finish_attempt(tid, outcome, failure_reason=failure_reason)


class TransitionWhitelistTest(unittest.TestCase):
    def test_check_transition_reports_from_and_to(self):
        with self.assertRaises(CompanionError) as ctx:
            check_transition(tasks_domain.TASK_TRANSITIONS, "pending", "running", "任务")
        self.assertIn("pending", str(ctx.exception))
        self.assertIn("running", str(ctx.exception))

    def test_feature_happy_path(self):
        board = make_board()
        board.transition_feature("f1", "confirmed")
        board.transition_feature("f1", "in_progress")
        board.transition_feature("f1", "delivered")
        board.transition_feature("f1", "accepted")
        self.assertEqual(board.feature("f1")["status"], "accepted")

    def test_feature_illegal_and_terminal(self):
        board = make_board()
        with self.assertRaises(CompanionError):
            board.transition_feature("f1", "delivered")  # draft → delivered 白名单外
        board.transition_feature("f1", "confirmed")
        board.transition_feature("f1", "draft")  # 范围修订退回重新确认
        self.assertEqual(board.feature("f1")["status"], "draft")
        board.transition_feature("f1", "confirmed")
        board.transition_feature("f1", "in_progress")
        board.transition_feature("f1", "delivered")
        board.transition_feature("f1", "in_progress")  # 验收打回返工
        board.transition_feature("f1", "delivered")
        board.transition_feature("f1", "accepted")
        with self.assertRaises(CompanionError):
            board.transition_feature("f1", "draft")  # accepted 终态
        with self.assertRaises(CompanionError):
            board.transition_feature("f1", "unknown")

    def test_task_must_pass_through_ready(self):
        board = make_board({"task_id": "A", "feature_id": "f1", "kind": "impl"})
        with self.assertRaises(CompanionError):
            board.transition_task("A", "running")  # pending → running 白名单外
        board.transition_task("A", "ready")
        with self.assertRaises(CompanionError):
            board.transition_task("A", "done")
        board.transition_task("A", "running")
        board.transition_task("A", "failed")
        board.transition_task("A", "ready")  # failed → ready 重试
        self.assertEqual(board.task("A")["status"], "ready")

    def test_blocked_and_cancelled_only_via_unified_entries(self):
        board = make_board({"task_id": "A", "feature_id": "f1", "kind": "impl"})
        with self.assertRaises(CompanionError) as ctx:
            board.transition_task("A", "blocked")
        self.assertIn("block()", str(ctx.exception))
        with self.assertRaises(CompanionError) as ctx:
            board.transition_task("A", "cancelled")
        self.assertIn("cancel()", str(ctx.exception))

    def test_block_requires_reason_and_records_chain(self):
        board = make_board({"task_id": "A", "feature_id": "f1", "kind": "impl"})
        with self.assertRaises(CompanionError):
            board.block("A", "  ")  # 空泛原因拒绝（Z24 证据显式化）
        board.block("A", "上游任务失败：X", ("X",))
        task = board.task("A")
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["block_reason"], "上游任务失败：X")
        self.assertEqual(task["blocked_by"], ["X"])

    def test_unblock_clears_chain_and_done_not_blockable(self):
        board = make_board({"task_id": "A", "feature_id": "f1", "kind": "impl"})
        board.block("A", "等待上游", ("X",))
        board.transition_task("A", "ready")
        task = board.task("A")
        self.assertIsNone(task["block_reason"])
        self.assertEqual(task["blocked_by"], [])
        board.transition_task("A", "running")
        drive_attempt = board.start_attempt("A", T0)
        self.assertEqual(drive_attempt["attempt_no"], 1)
        board.finish_attempt("A", "succeeded")
        with self.assertRaises(CompanionError):
            board.block("A", "已完成不可阻断")

    def test_cancel_terminal_with_reason(self):
        board = make_board({"task_id": "A", "feature_id": "f1", "kind": "impl"})
        with self.assertRaises(CompanionError):
            board.cancel("A", "")
        board.cancel("A", "需求撤回")
        self.assertEqual(board.task("A")["status"], "cancelled")
        with self.assertRaises(CompanionError):
            board.cancel("A", "重复取消")
        with self.assertRaises(CompanionError):
            board.transition_task("A", "ready")  # 终态

    def test_add_task_validation(self):
        board = make_board()
        with self.assertRaises(CompanionError):
            board.add_task("A", "f1", "unknown-kind")
        with self.assertRaises(CompanionError):
            board.add_task("A", "f1", "impl", depends_on=("ghost",))
        with self.assertRaises(CompanionError):
            board.add_task("A", "ghost-feature", "impl")
        board.add_task("A", "f1", "impl")
        with self.assertRaises(CompanionError):
            board.add_task("A", "f1", "impl")  # 编号重复
        with self.assertRaises(CompanionError):
            board.add_task("B", "f1", "impl", depends_on=("A", "A"))  # 依赖重复
        with self.assertRaises(CompanionError):
            board.add_task("B", "f1", "impl", priority=True)  # 优先级必须是整数
        self.assertEqual(board.task("A")["status"], "pending")

    def test_feature_paths_are_exact_closure(self):
        board = TaskBoard()
        with self.assertRaises(CompanionError):
            board.add_feature("bad id", "标题")  # 编号含空格
        with self.assertRaises(CompanionError):
            board.add_feature("f1", "标题", allowed_paths=("/abs/path",))  # 绝对路径拒绝
        entry = board.add_feature("f1", "标题", allowed_paths=("src/a.py", "src/b.py"))
        self.assertEqual(entry["allowed_paths"], ["src/a.py", "src/b.py"])


class AttemptTest(unittest.TestCase):
    def test_start_attempt_requires_running(self):
        board = make_board({"task_id": "A", "feature_id": "f1", "kind": "impl"})
        with self.assertRaises(CompanionError):
            board.start_attempt("A", T0)
        board.transition_task("A", "ready")
        with self.assertRaises(CompanionError):
            board.start_attempt("A", T0)

    def test_three_failed_attempts_leave_task_failed_not_done(self):
        board = make_board({"task_id": "A", "feature_id": "f1", "kind": "impl"})
        for round_no in range(1, 4):
            board.transition_task("A", "ready")
            board.transition_task("A", "running")
            attempt = board.start_attempt("A", "t%d" % round_no)
            self.assertEqual(attempt["attempt_no"], round_no)
            board.finish_attempt("A", "failed", failure_reason="测试失败")
        self.assertEqual(board.attempt_count("A"), 3)
        self.assertEqual([a["outcome"] for a in board.attempts("A")],
                         ["failed", "failed", "failed"])
        self.assertEqual(board.task("A")["status"], "failed")  # TK08：计数不虚增完成度

    def test_retry_then_success_records_final_attempt(self):
        board = make_board({"task_id": "A", "feature_id": "f1", "kind": "impl"})
        drive(board, "A", "failed", failure_reason="第一次失败")
        drive(board, "A", "succeeded")
        self.assertEqual(board.task("A")["status"], "done")
        attempts = board.attempts("A")
        self.assertEqual([a["attempt_no"] for a in attempts], [1, 2])
        self.assertEqual(attempts[-1]["failure_reason"], None)

    def test_finish_attempt_guards(self):
        board = make_board({"task_id": "A", "feature_id": "f1", "kind": "impl"})
        board.transition_task("A", "ready")
        board.transition_task("A", "running")
        board.start_attempt("A", T0)
        with self.assertRaises(CompanionError):
            board.finish_attempt("A", "pending")
        board.finish_attempt("A", "failed", failure_reason="原因")
        with self.assertRaises(CompanionError):
            board.finish_attempt("A", "succeeded")  # 没有进行中的 attempt
        with self.assertRaises(CompanionError):
            board.finish_attempt("A", "failed")  # failure_reason 必填


class EventBridgeTest(unittest.TestCase):
    def test_task_round_trip(self):
        board = make_board({"task_id": "A", "feature_id": "f1", "kind": "impl"})
        payload = to_event_payload(board.task("A"))
        self.assertEqual(payload, {"feature_id": "f1", "status": "pending"})
        back = from_event(EVENT_TASK_STATUS, "A", payload)
        self.assertEqual(back, {"id": "A", "feature_id": "f1", "status": "pending"})

    def test_feature_round_trip(self):
        board = TaskBoard()
        board.add_feature("f1", "标题", allowed_paths=("src/a.py",))
        payload = to_event_payload(board.feature("f1"))
        self.assertEqual(payload, {"title": "标题", "status": "draft"})
        back = from_event(EVENT_FEATURE_STATUS, "f1", payload)
        self.assertEqual(back, {"id": "f1", "title": "标题", "status": "draft"})

    def test_rejects_unknown_event_type_and_status(self):
        with self.assertRaises(CompanionError):
            from_event("release_stage", "r1", {"stage": "draft"})
        with self.assertRaises(CompanionError):
            from_event(EVENT_TASK_STATUS, "A", {"feature_id": "f1", "status": "awaiting_review"})
        with self.assertRaises(CompanionError):
            from_event(EVENT_FEATURE_STATUS, "f1", {"title": "t", "status": "running"})


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""P5-02：回报幂等、有限重试退避与预算领取门（C13/TK03/TK05/SC07）。

覆盖：
- 幂等重放：同 (task_id, attempt_no, outcome) 键二次提交同内容 → 去重并返回
  首次登记；同键不同内容 → 拒绝；不同 attempt/结局互不冲突；与 P2-02 事件库
  幂等键语义的接线（deduped 后按键取回原事件、digest 核对矛盾重放）。
- 退避：注入 rng 下间隔指数增长、上限封顶、抖动只降不升；3 次失败达上限 →
  task 停在 failed，不再重试（TK08 计数不虚增完成度）。
- 预算（P3-03 → 调度语义）：预算耗尽 → paused，不领取新任务、分文不扣；
  已持租约任务照常收尾（收尾不过门）；补充预算后续跑从剩余游标完成全部，
  不重不漏。
"""
import random
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel.domain import views
from agents_kernel.domain.tasks import TaskBoard
from agents_kernel.execution.backoff import (BudgetGate, BudgetedDispatcher, RetryPolicy,
                                             RetryTracker, attempt_delay)
from agents_kernel.execution.budget import Budget
from agents_kernel.execution.idempotency import ReportRegistry, report_key
from agents_kernel.execution.lease import LeaseManager
from agents_kernel.services.scheduler import ready_set
from agents_kernel.storage import db, events
from agents_kernel.storage.events_idem import canonical_digest, event_by_idempotency_key
from agents_kernel.validation import CompanionError


class FakeClock:
    """可手动推进的墙钟替身（与 test_lease.py 同款；tests 非包故就近定义）。"""

    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def temp_dir(test):
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    return temp.name


def make_board(*task_ids):
    board = TaskBoard()
    board.add_feature("f1", "示例功能")
    for tid in task_ids:
        board.add_task(tid, "f1", "impl")
    return board


def run_and_fail(board, tid, reason="测试失败"):
    if board.task(tid)["status"] != "ready":  # 重试裁决可能已把它转回 ready
        board.transition_task(tid, "ready")
    board.transition_task(tid, "running")
    board.start_attempt(tid, "t")
    board.finish_attempt(tid, "failed", failure_reason=reason)


def make_dispatcher(test, costs, total):
    board = make_board("A", "B")
    gate = BudgetGate(Budget(total), cost_of=lambda task: costs[task["id"]])
    clock = FakeClock()
    leases = LeaseManager(temp_dir(test), clock=clock)
    dispatcher = BudgetedDispatcher(board, gate, leases, "w1", ttl=60)
    return board, dispatcher, leases


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.registry = ReportRegistry(temp_dir(self))

    def test_first_accepted_then_same_content_deduped_returning_first(self):
        first = self.registry.submit("A", 1, "succeeded", {"summary": "ok", "check": "pytest"})
        self.assertTrue(first["accepted"])
        self.assertFalse(first["deduped"])
        replay = self.registry.submit("A", 1, "succeeded", {"check": "pytest", "summary": "ok"})
        self.assertFalse(replay["accepted"])
        self.assertTrue(replay["deduped"])
        self.assertEqual(replay["report"]["digest"], first["report"]["digest"])  # 返回首次结果

    def test_same_key_different_content_rejected(self):
        self.registry.submit("A", 1, "failed", {"reason": "超时"})
        with self.assertRaises(CompanionError) as ctx:
            self.registry.submit("A", 1, "failed", {"reason": "断言失败"})
        self.assertIn("冲突", str(ctx.exception))
        files = list(Path(self.registry._dir).glob("*.json"))
        self.assertEqual(len(files), 1)  # 拒绝即无半套登记

    def test_different_attempt_or_outcome_no_conflict(self):
        a = self.registry.submit("A", 1, "succeeded", {"n": 1})
        b = self.registry.submit("A", 2, "succeeded", {"n": 1})
        c = self.registry.submit("A", 1, "failed", {"n": 1})
        self.assertTrue(all(r["accepted"] for r in (a, b, c)))
        self.assertEqual(report_key("A", 1, "succeeded"), report_key("A", 1, "succeeded"))
        self.assertNotEqual(report_key("A", 1, "succeeded"), report_key("A", 2, "succeeded"))

    def test_invalid_report_arguments_rejected(self):
        for args in (("A", 0, "succeeded"), ("A", True, "succeeded"), ("A", 1, "pending"),
                     ("A", 1, ""), ("a/b", 1, "succeeded")):
            with self.subTest(args=args), self.assertRaises(CompanionError):
                report_key(*args)
        with self.assertRaises(CompanionError):
            self.registry.submit("A", 1, "succeeded", payload=["not", "dict"])

    def test_event_store_wiring_dedup_and_conflict_detection(self):
        """接线 P2-02：append_event 幂等键 + 本模块 digest 检测矛盾重放。"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = db.Store(Path(temp.name) / "facts.sqlite")
        store.open()
        self.addCleanup(store.close)
        writer = db.acquire_writer(store)
        self.addCleanup(writer.close)
        key = report_key("A", 1, "failed")
        original = {"reason": "超时"}
        first = events.append_event(store, writer.epoch, event_type=views.EVENT_FEATURE_STATUS,
                                    entity_id="F1", payload={"status": "draft"},
                                    idempotency_key=key)
        replay = events.append_event(store, writer.epoch, event_type=views.EVENT_FEATURE_STATUS,
                                     entity_id="F1", payload={"status": "confirmed"},
                                     idempotency_key=key)
        self.assertTrue(first["applied"])
        self.assertTrue(replay["deduped"])  # 事件库按键去重，但不比内容……
        stored = event_by_idempotency_key(store, key)
        self.assertIsNotNone(stored)
        # ……消费方用规范化 digest 核对：本例 payload 与原回报异内容 → 矛盾重放，拒绝
        self.assertNotEqual(canonical_digest(original), canonical_digest(stored["payload"]))
        self.assertIsNone(event_by_idempotency_key(store, report_key("A", 2, "failed")))


class BackoffTest(unittest.TestCase):
    def test_delays_grow_exponentially(self):
        policy = RetryPolicy(max_attempts=5, base_delay=2, multiplier=3,
                             max_delay=1000, jitter=0)
        rng = random.Random(1)
        delays = [attempt_delay(policy, n, rng) for n in (1, 2, 3, 4)]
        self.assertEqual(delays, [2.0, 6.0, 18.0, 54.0])
        for earlier, later in zip(delays, delays[1:]):
            self.assertGreater(later, earlier)  # 指数增长

    def test_delay_capped_at_max_delay(self):
        policy = RetryPolicy(base_delay=10, multiplier=10, max_delay=50, jitter=0)
        rng = random.Random(1)
        self.assertEqual([attempt_delay(policy, n, rng) for n in (1, 2, 3)],
                         [10.0, 50.0, 50.0])

    def test_jitter_only_shrinks_never_exceeds(self):
        policy = RetryPolicy(base_delay=100, multiplier=1, max_delay=100, jitter=0.5)
        rng = random.Random(7)
        for n in range(1, 8):
            delay = attempt_delay(policy, n, rng)
            self.assertGreaterEqual(delay, 50.0)
            self.assertLessEqual(delay, 100.0)

    def test_three_failures_exhausted_task_stays_failed(self):
        board = make_board("A")
        tracker = RetryTracker(board, RetryPolicy(max_attempts=3, jitter=0),
                               rng=random.Random(1))
        verdicts = []
        for _ in range(3):
            run_and_fail(board, "A")
            verdicts.append(tracker.record_failure("A"))
        self.assertEqual([v.status for v in verdicts], ["retry", "retry", "exhausted"])
        self.assertEqual(board.attempt_count("A"), 3)  # TK08：3 次 attempt 不虚增完成度
        self.assertEqual(board.task("A")["status"], "failed")
        self.assertEqual(ready_set(board.tasks()), [])  # failed 不再进 ready 集
        again = tracker.record_failure("A")  # 不再无限重试
        self.assertEqual(again.status, "exhausted")
        self.assertEqual(board.task("A")["status"], "failed")
        self.assertEqual(board.attempt_count("A"), 3)

    def test_retry_verdicts_carry_growing_delays(self):
        board = make_board("A")
        tracker = RetryTracker(board, RetryPolicy(max_attempts=3, base_delay=2,
                                                  multiplier=3, jitter=0),
                               rng=random.Random(1))
        run_and_fail(board, "A")
        first = tracker.record_failure("A")
        self.assertEqual(board.task("A")["status"], "ready")  # 重试：转回 ready
        run_and_fail(board, "A")
        second = tracker.record_failure("A")
        self.assertEqual((first.delay, second.delay), (6.0, 18.0))
        self.assertGreater(second.delay, first.delay)

    def test_policy_validation(self):
        for kwargs in ({"max_attempts": 0}, {"base_delay": 0}, {"multiplier": -1},
                       {"jitter": 1.5}, {"max_delay": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CompanionError):
                RetryPolicy(**kwargs)
        with self.assertRaises(CompanionError):
            attempt_delay(RetryPolicy(), 0, random.Random(1))


class BudgetPauseTest(unittest.TestCase):
    def test_claim_holds_lease_and_finish_releases(self):
        board, dispatcher, leases = make_dispatcher(self, costs={"A": 5, "B": 4}, total=20)
        outcome = dispatcher.start_next("t1")
        self.assertEqual(outcome.status, "claimed")
        self.assertEqual(outcome.task_id, "A")  # 同优先级按编号稳定排序
        self.assertEqual(board.task("A")["status"], "running")
        self.assertEqual(leases.state("A")["worker_id"], "w1")  # 领取即持租约
        dispatcher.finish("succeeded")
        self.assertEqual(board.task("A")["status"], "done")
        self.assertIsNone(leases.state("A"))  # 收尾释放租约

    def test_claim_while_running_rejected_serial_semantics(self):
        _, dispatcher, _ = make_dispatcher(self, costs={"A": 5, "B": 4}, total=20)
        dispatcher.start_next("t1")
        with self.assertRaises(CompanionError):
            dispatcher.start_next("t2")  # require_idle 语义保留

    def test_budget_exhausted_pauses_no_new_claim_and_running_finishes(self):
        """预算耗尽：paused 不领新、分文不扣；已有租约任务可完成收尾。"""
        board, dispatcher, leases = make_dispatcher(self, costs={"A": 5, "B": 4}, total=5)
        dispatcher.start_next("t1")           # 领取 A，预算恰好用尽
        self.assertEqual(leases.state("A")["worker_id"], "w1")
        dispatcher.finish("succeeded")        # 收尾不过门：预算为 0 也允许完成
        self.assertEqual(board.task("A")["status"], "done")
        outcome = dispatcher.start_next("t2")  # 下一次领取被暂停
        self.assertEqual(outcome.status, "paused")
        self.assertIsNone(outcome.task_id)
        self.assertEqual(outcome.cursor, ("B",))  # 游标 = 剩余 ready
        self.assertEqual(board.task("B")["status"], "pending")  # 不领取新任务

    def test_resume_after_topup_completes_remaining_without_rework(self):
        """续跑从游标：补充预算后完成剩余，已完成任务不重跑（不重不漏）。"""
        board, dispatcher, _ = make_dispatcher(self, costs={"A": 5, "B": 4}, total=5)
        dispatcher.start_next("t1")
        dispatcher.finish("succeeded")
        self.assertEqual(dispatcher.start_next("t2").status, "paused")
        board2_gate = BudgetGate(Budget(9, 5),  # 续跑：预算补充（total=9, spent=5）
                                 cost_of=lambda task: {"A": 5, "B": 4}[task["id"]])
        resumed = BudgetedDispatcher(board, board2_gate,
                                     LeaseManager(temp_dir(self), clock=FakeClock()),
                                     "w1", ttl=60)
        outcome = resumed.start_next("t3")
        self.assertEqual((outcome.status, outcome.task_id), ("claimed", "B"))
        resumed.finish("succeeded")
        self.assertEqual([board.task(t)["status"] for t in ("A", "B")], ["done", "done"])
        self.assertEqual(board.attempt_count("A"), 1)  # A 不重跑
        self.assertEqual(board.attempt_count("B"), 1)
        self.assertEqual(resumed.start_next("t4").status, "drained")  # 全部完成 → 干涸


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""P5-01：纯逻辑 DAG 调度器（TK09 前半）。

覆盖：菱形依赖 ready 正确；环拒绝并报完整环路径；依赖失败传播出 blocked 链；
显式取消传播；优先级排序稳定；串行适配器任一时刻至多一个 running（旧
require_idle 语义的模型层等价物）；ParallelReady 只读视图不做并行承诺。
"""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel.domain.tasks import TaskBoard
from agents_kernel.services.scheduler import (ParallelReady, SerialExecutor, build_graph,
                                              order_ready, propagate_cancel,
                                              propagate_failure, ready_set)
from agents_kernel.validation import CompanionError


def make_board(*task_specs, features=("f1",)):
    board = TaskBoard()
    for fid in features:
        board.add_feature(fid, "功能 %s" % fid)
    for spec in task_specs:
        board.add_task(**spec)
    return board


def run(board, tid, outcome="succeeded", reason=None):
    board.transition_task(tid, "ready")
    board.transition_task(tid, "running")
    board.start_attempt(tid, "t")
    board.finish_attempt(tid, outcome, failure_reason=reason)


def diamond_board():
    return make_board(
        {"task_id": "A", "feature_id": "f1", "kind": "impl"},
        {"task_id": "B", "feature_id": "f1", "kind": "impl", "depends_on": ("A",)},
        {"task_id": "C", "feature_id": "f1", "kind": "impl", "depends_on": ("A",)},
        {"task_id": "D", "feature_id": "f1", "kind": "integration", "depends_on": ("B", "C")})


def ids(tasks):
    return [task["id"] for task in tasks]


class ReadySetTest(unittest.TestCase):
    def test_diamond_ready_progression(self):
        board = diamond_board()
        self.assertEqual(ids(ready_set(board.tasks())), ["A"])
        run(board, "A")
        self.assertEqual(ids(ready_set(board.tasks())), ["B", "C"])
        run(board, "B")
        self.assertEqual(ids(ready_set(board.tasks())), ["C"])  # D 仍缺 C
        run(board, "C")
        self.assertEqual(ids(ready_set(board.tasks())), ["D"])

    def test_running_and_blocked_not_in_ready(self):
        board = diamond_board()
        board.transition_task("A", "ready")
        board.transition_task("A", "running")
        self.assertEqual(ids(ready_set(board.tasks())), [])
        board.block("B", "上游运行中", ("A",))
        self.assertEqual(ids(ready_set(board.tasks())), [])

    def test_prebuilt_graph_is_respected(self):
        board = diamond_board()
        graph = build_graph(board.tasks())
        self.assertEqual(graph["D"], ["B", "C"])
        self.assertEqual(ids(ready_set(board.tasks(), graph)), ["A"])


class GraphGuardTest(unittest.TestCase):
    @staticmethod
    def raw(*specs):
        """build_graph 面向任意任务列表（含未来从台账水合的形态），环/缺依赖在图入口拒绝。"""
        return [{"id": tid, "depends_on": deps, "status": "pending"} for tid, deps in specs]

    def test_missing_dependency_rejected(self):
        with self.assertRaises(CompanionError):
            build_graph(self.raw(("B", ("ghost",))))

    def test_cycle_rejected_with_full_path(self):
        tasks = self.raw(("A", ("C",)), ("B", ("A",)), ("C", ("B",)))
        with self.assertRaises(CompanionError) as ctx:
            build_graph(tasks)
        self.assertEqual(str(ctx.exception), "依赖图存在环：A → C → B → A")

    def test_self_cycle_rejected(self):
        with self.assertRaises(CompanionError) as ctx:
            build_graph(self.raw(("A", ("A",))))
        self.assertIn("A → A", str(ctx.exception))

    def test_board_rejects_forward_reference_at_add_time(self):
        board = make_board({"task_id": "A", "feature_id": "f1", "kind": "impl"})
        with self.assertRaises(CompanionError):
            board.add_task("C", "f1", "impl", depends_on=("B",))  # B 尚未登记


class FailurePropagationTest(unittest.TestCase):
    def test_failure_blocks_transitive_chain(self):
        board = make_board(
            {"task_id": "A", "feature_id": "f1", "kind": "impl"},
            {"task_id": "B", "feature_id": "f1", "kind": "impl", "depends_on": ("A",)},
            {"task_id": "C", "feature_id": "f1", "kind": "impl", "depends_on": ("B",)})
        run(board, "A", outcome="failed", reason="构建失败")
        plan = propagate_failure(board.tasks(), "A")
        self.assertEqual(plan, {"B": ["A"], "C": ["B"]})

    def test_diamond_failure_records_both_branches(self):
        board = diamond_board()
        run(board, "A", outcome="failed", reason="构建失败")
        self.assertEqual(propagate_failure(board.tasks(), "A"),
                         {"B": ["A"], "C": ["A"], "D": ["B", "C"]})

    def test_done_and_running_dependents_untouched(self):
        board = make_board(
            {"task_id": "A", "feature_id": "f1", "kind": "impl"},
            {"task_id": "B", "feature_id": "f1", "kind": "impl", "depends_on": ("A",)},
            {"task_id": "C", "feature_id": "f1", "kind": "impl", "depends_on": ("A",)})
        run(board, "B")
        board.transition_task("C", "ready")
        board.transition_task("C", "running")
        run(board, "A", outcome="failed", reason="构建失败")
        self.assertEqual(propagate_failure(board.tasks(), "A"), {})

    def test_only_failed_root_propagates(self):
        board = make_board({"task_id": "A", "feature_id": "f1", "kind": "impl"})
        with self.assertRaises(CompanionError):
            propagate_failure(board.tasks(), "A")  # pending 不是失败源
        with self.assertRaises(CompanionError):
            propagate_failure(board.tasks(), "ghost")


class CancelPropagationTest(unittest.TestCase):
    def test_cancel_reaches_non_terminal_transitive_dependents(self):
        board = make_board(
            {"task_id": "R", "feature_id": "f1", "kind": "design"},
            {"task_id": "S", "feature_id": "f1", "kind": "impl", "depends_on": ("R",)},
            {"task_id": "T", "feature_id": "f1", "kind": "impl", "depends_on": ("S",)},
            {"task_id": "U", "feature_id": "f1", "kind": "impl", "depends_on": ("R",)},
            {"task_id": "V", "feature_id": "f1", "kind": "impl", "depends_on": ("R",)})
        run(board, "U")  # done：保留完成事实
        run(board, "V", outcome="failed", reason="自身失败")  # failed：保留自有失败证据
        board.block("T", "等待上游", ("S",))
        self.assertEqual(propagate_cancel(board.tasks(), "R"), ["S", "T"])


class PriorityOrderTest(unittest.TestCase):
    def test_priority_then_feature_grouping(self):
        board = make_board(
            {"task_id": "p5", "feature_id": "f1", "kind": "impl", "priority": 5},
            {"task_id": "a1", "feature_id": "f1", "kind": "impl", "priority": 1},
            {"task_id": "b1", "feature_id": "f2", "kind": "impl", "priority": 1},
            {"task_id": "c0", "feature_id": "f2", "kind": "impl", "priority": 0},
            features=("f1", "f2"))
        ordered = order_ready(ready_set(board.tasks()))
        self.assertEqual(ids(ordered), ["c0", "a1", "b1", "p5"])
        self.assertEqual(ids(order_ready(ordered)), ids(ordered))  # 排序稳定可复现


class SerialExecutorTest(unittest.TestCase):
    def test_at_most_one_running_with_two_ready(self):
        board = make_board(
            {"task_id": "A", "feature_id": "f1", "kind": "impl"},
            {"task_id": "B", "feature_id": "f1", "kind": "impl", "depends_on": ("A",)},
            {"task_id": "E", "feature_id": "f1", "kind": "impl", "priority": 1})
        executor = SerialExecutor(board)
        self.assertIsNone(executor.current())
        self.assertEqual(executor.start_next("t0"), "A")  # ready 内优先级 0 先于 1
        self.assertEqual(executor.current(), "A")
        with self.assertRaises(CompanionError):
            executor.start_next("t1")  # 串行不变式：已有 running，拒绝并发领取
        executor.finish("succeeded")
        self.assertEqual(executor.start_next("t1"), "B")
        with self.assertRaises(CompanionError):
            executor.start_next("t2")
        executor.finish("succeeded")
        self.assertEqual(executor.start_next("t2"), "E")
        executor.finish("succeeded")
        self.assertIsNone(executor.start_next("t3"))

    def test_full_drain_keeps_invariant(self):
        board = diamond_board()
        board.add_task("E", "f1", "impl", priority=2)
        executor = SerialExecutor(board)
        started = []
        while True:
            task_id = executor.start_next("t")
            if task_id is None:
                break
            started.append(task_id)
            self.assertEqual(executor.current(), task_id)
            with self.assertRaises(CompanionError):
                executor.start_next("never")  # 任一时刻至多一个 running
            executor.finish("succeeded")
        self.assertEqual(started, ["A", "B", "C", "D", "E"])

    def test_failed_finish_propagates_block_to_board(self):
        board = make_board(
            {"task_id": "R", "feature_id": "f1", "kind": "impl"},
            {"task_id": "S", "feature_id": "f1", "kind": "impl", "depends_on": ("R",)})
        executor = SerialExecutor(board)
        executor.start_next("t0")
        executor.finish("failed", failure_reason="构建失败")
        blocked = board.task("S")
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["blocked_by"], ["R"])
        self.assertEqual(blocked["block_reason"], "上游任务失败：R")
        with self.assertRaises(CompanionError):
            executor.finish("succeeded")  # 没有运行中的任务

    def test_parallel_ready_is_readonly_view(self):
        board = diamond_board()
        ready = ParallelReady(board)
        self.assertEqual(ids(ready.ready()), ["A"])  # 只返回集合，不做并行执行承诺
        run(board, "A")
        self.assertEqual(ids(ready.ready()), ["B", "C"])


if __name__ == "__main__":
    unittest.main()

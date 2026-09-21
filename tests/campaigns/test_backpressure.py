# -*- coding: utf-8 -*-
"""并发背压闸门单测（C13 尾）：峰值并发恰为 permit 数、FIFO 排队、gated_run 有界。

覆盖：8 任务/3 permit→峰值并发恰 3 且全部完成、队首先得 permit 的严格 FIFO、
permit 不可重入、无持有 release 拒绝、非法 permit 数拒绝、fn 异常传播不泄漏。
"""
import sys
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel.execution.backpressure import DEFAULT_MAX_PERMITS, Gate, gated_run
from agents_kernel.validation import CompanionError


def wait_until(predicate, timeout=5.0, message="条件未达成"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.001)
    self_fail(message)


def self_fail(message):
    raise AssertionError(message)


class GateBasicsTest(unittest.TestCase):
    def test_default_permits_is_three(self):
        self.assertEqual(Gate().max_permits, DEFAULT_MAX_PERMITS)
        self.assertEqual(Gate().max_permits, 3)

    def test_invalid_max_permits_rejected(self):
        for bad in (0, -1, True, 2.5, "3"):
            with self.subTest(max_permits=bad), self.assertRaises(CompanionError):
                Gate(bad)

    def test_release_without_acquire_rejected(self):
        gate = Gate(1)
        with self.assertRaises(CompanionError):
            gate.release()
        gate.acquire()
        gate.release()
        with self.assertRaises(CompanionError):  # 归还两次同样拒绝
            gate.release()

    def test_same_thread_multiple_permits_paired(self):
        gate = Gate(2)
        gate.acquire()
        gate.acquire()          # 同线程可持多个 permit（占位排空队列的用法）
        self.assertEqual(gate.active, 2)
        gate.release()
        gate.release()
        with self.assertRaises(CompanionError):
            gate.release()


class FifoTest(unittest.TestCase):
    def test_waiters_wake_in_queue_order(self):
        gate = Gate(3)
        held = [gate.acquire() for _ in range(3)]  # 占满：后续请求全部排队
        done = []
        go = threading.Event()  # 等待者获准后持住 permit，等主线程核完顺序再统一放行

        def waiter(name):
            gate.acquire()
            done.append(name)
            go.wait(timeout=10)
            gate.release()

        threads = []
        for i in range(4):
            thread = threading.Thread(target=waiter, args=("w%d" % i,))
            thread.start()
            threads.append(thread)
            wait_until(lambda i=i: gate.waiting == i + 1)  # 确定化入队次序 w0..w3
        for _ in held:
            gate.release()  # 只让出自己持有的 3 个；等待者持住 permit，不发生级联
        wait_until(lambda: len(done) == 3)
        self.assertEqual(done, ["w0", "w1", "w2"], "FIFO 被破坏：%s" % done)
        go.set()
        wait_until(lambda: len(done) == 4)
        self.assertEqual(done[3], "w3")  # 放行后队首 w3 独得下一个 permit
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())


class GatedRunTest(unittest.TestCase):
    def test_peak_concurrency_exactly_three_and_all_complete_in_order(self):
        gate = Gate(3)
        lock = threading.Lock()
        state = {"cur": 0, "peak": 0}
        entered = []
        # 主线程先占满 3 个 permit：8 个任务全部入队后一次放行，队列授权严格按队序
        #（确定性 FIFO 见 FifoTest）。最先进入的 3 个任务在 Barrier(3) 会合——第三名
        # 到达前全员阻塞，会合瞬间恰有 3 个任务并发在身，证明峰值恰为 3；后续任务
        # 不设屏障，避免最后一波不足 3 个而互相等死。
        held = [gate.acquire() for _ in range(3)]
        first_wave = threading.Barrier(3)

        def make(i):
            def fn():
                with lock:
                    state["cur"] += 1
                    state["peak"] = max(state["peak"], state["cur"])
                    entered.append(i)
                    rank = len(entered)
                if rank <= 3:
                    first_wave.wait(timeout=10)  # 会合失败抛 BrokenBarrierError
                with lock:
                    state["cur"] -= 1
                return i
            return fn

        result_box = {}

        def run():
            result_box["results"] = gated_run(gate, [make(i) for i in range(8)])

        thread = threading.Thread(target=run)
        thread.start()
        wait_until(lambda: gate.waiting == 8)
        for _ in held:
            gate.release()
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive(), "gated_run 卡死")
        self.assertEqual(result_box["results"], list(range(8)))   # 全部完成、按提交序
        self.assertEqual(sorted(entered), list(range(8)))          # 无遗漏
        self.assertEqual(state["peak"], 3)                         # 峰值恰为 3
        self.assertLessEqual(state["peak"], gate.max_permits)      # 结构上限从未突破
        self.assertEqual(gate.active, 0)                           # permit 不泄漏
        self.assertEqual(gate.waiting, 0)

    def test_gated_run_empty_and_generator_input(self):
        gate = Gate(2)
        self.assertEqual(gated_run(gate, []), [])
        self.assertEqual(gated_run(gate, (lambda i=i: i for i in (1, 2))), [1, 2])

    def test_exception_propagates_and_gate_not_leaked(self):
        gate = Gate(2)
        others = []

        def boom():
            raise ValueError("boom")

        def fine():
            others.append(1)
            return "ok"

        with self.assertRaises(ValueError):
            gated_run(gate, [fine, boom, fine])
        # 无取消语义：未抛错的任务照常完成（首尾两个 fine），异常在全部收尾后上抛
        self.assertEqual(others, [1, 1])
        self.assertEqual(gate.active, 0)
        self.assertEqual(gate.waiting, 0)


if __name__ == "__main__":
    unittest.main()

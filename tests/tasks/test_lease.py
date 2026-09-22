# -*- coding: utf-8 -*-
"""P5-02：任务租约——文件锁 + TTL + epoch 接管（C12/TK04 前半）。

覆盖：acquire/renew/steal/release 全生命周期；未过期被他方持有拒绝；只有
未过期持约者可续约/释放；TTL 过期后他人接管（接管事件入日志）；原持约者
复活（续约/释放/再领）被 epoch 拒；损坏租约按陈旧处理可接管；崩溃注入——
持约子进程 os._exit 强杀不 release，TTL 过后另一 worker 接管干净。
时钟注入（FakeClock）做 TTL 判定，进程内测试不真睡；仅崩溃注入用真实短 TTL。
"""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel.execution.lease import TAKEOVER_LOG, Lease, LeaseManager
from agents_kernel.validation import CompanionError


class FakeClock:
    """可手动推进的墙钟替身（进程内 TTL 测试不真睡）。"""

    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_manager(**kwargs):
    temp = tempfile.TemporaryDirectory()
    clock = FakeClock()
    kwargs.setdefault("clock", clock)
    manager = LeaseManager(temp.name, **kwargs)
    return manager, clock, temp


class LeaseLifecycleTest(unittest.TestCase):
    def test_acquire_renew_release_full_lifecycle(self):
        mgr, clock, _ = make_manager()
        lease = mgr.acquire("A", "w1", ttl=30)
        self.assertEqual(lease, Lease("A", "w1", 1, 1030.0))
        state = mgr.state("A")
        self.assertEqual((state["worker_id"], state["epoch"], state["expired"]),
                         ("w1", 1, False))
        clock.advance(10)
        renewed = mgr.renew("A", "w1", ttl=30)
        self.assertEqual(renewed.epoch, 1)          # 续约不换 epoch
        self.assertEqual(renewed.expires_at, 1040.0)  # 从当前时刻重新计 TTL
        mgr.release("A", "w1")
        self.assertIsNone(mgr.state("A"))

    def test_acquire_while_held_by_other_rejected(self):
        mgr, _, _ = make_manager()
        mgr.acquire("A", "w1", ttl=30)
        with self.assertRaises(CompanionError) as ctx:
            mgr.acquire("A", "w2", ttl=30)
        self.assertIn("w1", str(ctx.exception))

    def test_reacquire_after_clean_release_starts_new_epoch(self):
        mgr, _, _ = make_manager()
        mgr.acquire("A", "w1", ttl=30)
        mgr.release("A", "w1")
        lease = mgr.acquire("A", "w2", ttl=30)
        self.assertEqual(lease.epoch, 1)  # 干净释放后无陈旧持有者，epoch 从头计

    def test_release_and_renew_only_by_holder(self):
        mgr, _, _ = make_manager()
        mgr.acquire("A", "w1", ttl=30)
        with self.assertRaises(CompanionError):
            mgr.renew("A", "w2", ttl=30)
        with self.assertRaises(CompanionError):
            mgr.release("A", "w2")
        self.assertEqual(mgr.state("A")["worker_id"], "w1")  # 拒绝即无半套变更

    def test_double_release_rejected(self):
        mgr, _, _ = make_manager()
        mgr.acquire("A", "w1", ttl=30)
        mgr.release("A", "w1")
        with self.assertRaises(CompanionError):
            mgr.release("A", "w1")

    def test_expired_holder_loses_renew_and_release(self):
        mgr, clock, _ = make_manager()
        mgr.acquire("A", "w1", ttl=30)
        clock.advance(30)  # now >= expires_at 即过期
        with self.assertRaises(CompanionError) as ctx:
            mgr.renew("A", "w1", ttl=30)
        self.assertIn("过期", str(ctx.exception))
        with self.assertRaises(CompanionError):
            mgr.release("A", "w1")
        self.assertTrue(mgr.state("A")["expired"])

    def test_steal_after_ttl_records_takeover_event(self):
        mgr, clock, temp = make_manager()
        mgr.acquire("A", "w1", ttl=30)
        clock.advance(31)
        stolen = mgr.acquire("A", "w2", ttl=30)
        self.assertEqual(stolen.epoch, 2)  # 接管 epoch 递增
        self.assertEqual(mgr.state("A")["worker_id"], "w2")
        takeovers = mgr.takeovers("A")
        self.assertEqual(len(takeovers), 1)
        self.assertEqual(takeovers[0]["from"], {"worker_id": "w1", "epoch": 1})
        self.assertEqual(takeovers[0]["to"], {"worker_id": "w2", "epoch": 2})
        self.assertTrue((Path(temp.name) / TAKEOVER_LOG).exists())

    def test_no_takeover_event_on_clean_path(self):
        mgr, _, _ = make_manager()
        mgr.acquire("A", "w1", ttl=30)
        mgr.release("A", "w1")
        mgr.acquire("A", "w2", ttl=30)
        self.assertEqual(mgr.takeovers(), [])

    def test_revived_holder_rejected_after_steal(self):
        """崩溃模拟（进程内）：持约者"死亡"→TTL 过→接管→复活被拒（epoch 不符）。"""
        mgr, clock, _ = make_manager()
        mgr.acquire("A", "w1", ttl=30)
        clock.advance(31)              # w1 消失，租约过期
        mgr.acquire("A", "w2", ttl=30)  # w2 接管
        clock.advance(1)               # w1 "复活"
        with self.assertRaises(CompanionError) as ctx:
            mgr.renew("A", "w1", ttl=30)
        self.assertIn("不是持约者", str(ctx.exception))
        with self.assertRaises(CompanionError):
            mgr.release("A", "w1")
        with self.assertRaises(CompanionError):
            mgr.acquire("A", "w1", ttl=30)  # 未过期，仍归 w2
        self.assertEqual(mgr.state("A")["worker_id"], "w2")

    def test_corrupt_lease_treated_as_stale_and_stealable(self):
        mgr, clock, temp = make_manager()
        mgr.acquire("A", "w1", ttl=30)
        lease_path = Path(temp.name) / "A.lease"
        lease_path.write_text('{"worker_id": "w1", "ep', encoding="utf-8")  # 半写
        stolen = mgr.acquire("A", "w2", ttl=30)  # 损坏按陈旧（恒过期）接管
        self.assertEqual(stolen.epoch, 1)
        self.assertEqual(mgr.state("A")["worker_id"], "w2")

    def test_invalid_arguments_rejected(self):
        mgr, _, _ = make_manager()
        for args in (("A", "", 30), ("A", "w1", 0), ("A", "w1", -1), ("A", "w1", "30"),
                     ("", "w1", 30), ("a/b", "w1", 30), ("..", "w1", 30)):
            with self.subTest(args=args), self.assertRaises(CompanionError):
                mgr.acquire(*args)
        mgr.acquire("A", "w1", ttl=30)
        with self.assertRaises(CompanionError):
            mgr.renew("A", "w1", ttl=True)


class CrashedHolderTest(unittest.TestCase):
    """崩溃注入：子进程持约后 os._exit 强杀（绝不 release），另一 worker 接管。"""

    def test_killed_holder_lease_stolen_after_real_ttl(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        code = ("import os, sys;"
                "sys.path.insert(0, %r);"
                "from agents_kernel.execution.lease import LeaseManager;"
                "mgr = LeaseManager(%r);"
                "mgr.acquire('T-crash', 'worker-dead', ttl=0.5);"
                "os._exit(9)") % (str(REPO_ROOT / "packages"), temp.name)
        proc = subprocess.Popen([sys.executable, "-c", code])
        self.assertEqual(proc.wait(), 9)  # 强杀：没有 release，租约文件遗留在盘上

        mgr = LeaseManager(temp.name)
        self.assertEqual(mgr.state("T-crash")["worker_id"], "worker-dead")
        with self.assertRaises(CompanionError):
            mgr.acquire("T-crash", "worker-alive", ttl=5)  # 未过期不可抢

        expires_at = mgr.state("T-crash")["expires_at"]
        time.sleep(max(0.0, expires_at - time.time()) + 0.05)
        stolen = mgr.acquire("T-crash", "worker-alive", ttl=5)  # TTL 过后接管干净
        self.assertEqual((stolen.epoch, stolen.worker_id), (2, "worker-alive"))
        takeovers = mgr.takeovers("T-crash")
        self.assertEqual(len(takeovers), 1)
        self.assertEqual(takeovers[0]["from"]["worker_id"], "worker-dead")
        # 原 worker 复活：一切操作被 epoch/持约者校验拒绝
        with self.assertRaises(CompanionError):
            mgr.renew("T-crash", "worker-dead", ttl=5)
        with self.assertRaises(CompanionError):
            mgr.release("T-crash", "worker-dead")
        self.assertEqual(mgr.state("T-crash")["worker_id"], "worker-alive")
        # 接管后的新租约完全可用
        mgr.renew("T-crash", "worker-alive", ttl=5)
        self.assertEqual(json.loads((Path(temp.name) / "T-crash.lease")
                                    .read_text(encoding="utf-8"))["epoch"], 2)


if __name__ == "__main__":
    unittest.main()

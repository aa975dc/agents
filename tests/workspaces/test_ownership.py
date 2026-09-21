# -*- coding: utf-8 -*-
"""P5-03：文件 ownership 台账——精确路径归属、共享白名单单 owner 串行（TK09 后半/C07 前半）。

覆盖：claim 全有或全无（非共享重叠 → StructuredConflict 报出双方 task 与路径，
holder 台账不动）；共享白名单内 → 默认排队、release 时 FIFO 尽力晋升（被阻塞
者可被后到者越过，串行不变式不破）、queue=False 显式拒绝（retryable）；重复
claim/释放未在册/非法路径与任务编号拒绝；store_path 持久化往返（重启恢复归属
与队列）、版本不符拒绝。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel.services.ownership import (DEFAULT_SHARED_WHITELIST, OwnershipRegistry,
                                              StructuredConflict)
from agents_kernel.validation import CompanionError


def statuses(registry):
    return {item["task_id"]: item["status"] for item in registry.state()}


class ClaimBasicsTest(unittest.TestCase):
    def test_claim_state_release_roundtrip(self):
        registry = OwnershipRegistry()
        claim = registry.claim("A", ["src/a.py", "src/b.py"])
        self.assertEqual((claim.task_id, claim.status), ("A", "active"))
        self.assertEqual(claim.paths, ("src/a.py", "src/b.py"))
        self.assertEqual(statuses(registry), {"A": "active"})
        registry.release("A")
        self.assertEqual(registry.state(), [])

    def test_reclaim_requires_release_first(self):
        registry = OwnershipRegistry()
        registry.claim("A", ["a.txt"])
        with self.assertRaises(CompanionError):
            registry.claim("A", ["a.txt"])
        registry.release("A")
        registry.claim("A", ["a.txt"])          # 释放后可重新声明
        self.assertEqual(statuses(registry), {"A": "active"})

    def test_invalid_inputs_rejected(self):
        registry = OwnershipRegistry()
        for task_id, paths in (("A", "a.txt"),          # 非列表
                               ("A", []),               # 空列表
                               ("A", ["/abs/x"]),       # 绝对路径
                               ("A", ["../x"]),         # 逃逸路径
                               ("../bad", ["a.txt"]),   # 任务编号不可作键
                               ("", ["a.txt"])):
            with self.subTest(args=(task_id, paths)), self.assertRaises(CompanionError):
                registry.claim(task_id, paths)
        registry.claim("A", ["a.txt", "a.txt"])
        self.assertEqual(registry.state()[0]["paths"], ["a.txt"])  # 去重保留单条

    def test_release_unknown_rejected(self):
        registry = OwnershipRegistry()
        with self.assertRaises(CompanionError):
            registry.release("ghost")


class HardConflictTest(unittest.TestCase):
    def test_overlap_outside_whitelist_refused(self):
        registry = OwnershipRegistry()
        registry.claim("A", ["src/a.py", "src/common.py"])
        with self.assertRaises(StructuredConflict) as ctx:
            registry.claim("B", ["src/b.py", "src/common.py"])  # queue=True 也不排队
        conflict = ctx.exception
        self.assertTrue(issubclass(StructuredConflict, CompanionError))  # 捕获面兼容
        self.assertEqual(conflict.holder_task, "A")
        self.assertEqual(conflict.requester_task, "B")
        self.assertEqual(conflict.paths, ["src/common.py"])
        self.assertFalse(conflict.shared)
        self.assertFalse(conflict.retryable)                    # 终局拒绝
        self.assertIn("A", str(conflict))
        self.assertIn("src/common.py", str(conflict))
        self.assertEqual(statuses(registry), {"A": "active"})   # holder 台账不动
        self.assertNotIn("B", statuses(registry))               # 不部分授予

    def test_shared_whitelist_default_rules(self):
        registry = OwnershipRegistry()
        self.assertTrue(registry.is_shared("contracts/api.json"))   # 目录前缀
        self.assertTrue(registry.is_shared("package-lock.json"))    # 精确名
        self.assertFalse(registry.is_shared("src/a.py"))
        custom = OwnershipRegistry(shared_whitelist=("shared/",))
        self.assertFalse(custom.is_shared("package-lock.json"))     # 白名单可替换
        self.assertTrue(custom.is_shared("shared/x.bin"))


class SharedWhitelistTest(unittest.TestCase):
    def test_shared_conflict_queues_then_promotes_on_release(self):
        registry = OwnershipRegistry()
        registry.claim("A", ["contracts/x.json", "a.txt"])
        claim = registry.claim("B", ["contracts/x.json"])       # 默认排队
        self.assertEqual(claim.status, "queued")
        self.assertEqual(statuses(registry), {"A": "active", "B": "queued"})
        registry.release("A")
        self.assertEqual(statuses(registry), {"B": "active"})   # FIFO 晋升

    def test_shared_conflict_explicit_refusal_when_queue_false(self):
        registry = OwnershipRegistry()
        registry.claim("A", ["package-lock.json"])
        with self.assertRaises(StructuredConflict) as ctx:
            registry.claim("B", ["package-lock.json"], queue=False)
        conflict = ctx.exception
        self.assertTrue(conflict.shared)
        self.assertTrue(conflict.retryable)                     # 语义显式：可重试
        self.assertEqual((conflict.holder_task, conflict.requester_task), ("A", "B"))
        self.assertEqual(conflict.paths, ["package-lock.json"])
        self.assertEqual(statuses(registry), {"A": "active"})

    def test_fifo_promotion_with_blocking_paths_and_no_reservation(self):
        registry = OwnershipRegistry()
        registry.claim("C", ["package-lock.json"])              # C 占另一共享路径
        registry.claim("A", ["contracts/x.json"])
        queued = registry.claim("B", ["contracts/x.json", "package-lock.json"])
        self.assertEqual(queued.status, "queued")               # 两条共享路径都忙
        registry.release("A")                                   # x 空了，但 B 还等 lock
        self.assertEqual(statuses(registry)["B"], "queued")
        granted = registry.claim("D", ["contracts/x.json"])     # 排队不预留：D 可越过
        self.assertEqual(granted.status, "active")
        registry.release("C")
        self.assertEqual(statuses(registry)["B"], "queued")     # 仍被 D 挡（串行不破）
        registry.release("D")
        self.assertEqual(statuses(registry), {"B": "active"})   # 路径全空后晋升

    def test_hard_conflict_takes_precedence_over_shared_queueing(self):
        registry = OwnershipRegistry()
        registry.claim("A", ["contracts/x.json"])               # 共享忙
        registry.claim("C", ["z.txt"])                          # 非共享被持
        with self.assertRaises(StructuredConflict) as ctx:
            registry.claim("B", ["contracts/x.json", "z.txt"])
        self.assertFalse(ctx.exception.shared)                  # 硬冲突优先，终局拒绝
        self.assertNotIn("B", statuses(registry))

    def test_queued_release_cancels_wait_without_promotion(self):
        registry = OwnershipRegistry()
        registry.claim("A", ["contracts/x.json"])
        registry.claim("B", ["contracts/x.json"])
        registry.release("B")                                   # 排队者自行退出
        registry.release("A")
        self.assertEqual(registry.state(), [])                  # 已退出者不被晋升

    def test_default_whitelist_constant_shape(self):
        rules = DEFAULT_SHARED_WHITELIST
        self.assertIn("contracts/", rules)
        self.assertTrue(any(rule == "package-lock.json" for rule in rules))


class PersistenceTest(unittest.TestCase):
    def test_store_roundtrip_recovers_claims_and_queue(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Path(temp.name) / "ownership.json"
        first = OwnershipRegistry(store_path=store)
        first.claim("A", ["contracts/x.json"])
        first.claim("B", ["contracts/x.json"])                  # queued
        second = OwnershipRegistry(store_path=store)            # 重启恢复
        self.assertEqual(json_dump(second.state()), json_dump(first.state()))
        self.assertEqual(statuses(second), {"A": "active", "B": "queued"})
        second.release("A")                                     # 晋升随释放落盘
        self.assertEqual(statuses(second), {"B": "active"})
        third = OwnershipRegistry(store_path=store)
        self.assertEqual(statuses(third), {"B": "active"})

    def test_store_version_mismatch_rejected(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Path(temp.name) / "ownership.json"
        store.write_text('{"version": 99, "claims": {}, "queue": []}', encoding="utf-8")
        with self.assertRaises(CompanionError):
            OwnershipRegistry(store_path=store)


def json_dump(items):
    return json.dumps(items, sort_keys=True, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()

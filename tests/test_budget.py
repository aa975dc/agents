# -*- coding: utf-8 -*-
"""活动预算单测（CV02 前半/CV05）：整额扣减、批边界暂停、检查点续跑不重不漏。

覆盖：预算不足分文不扣、暂停返回续跑游标且已处理集为严格前缀（不抽样）、
跨活动 resume 后总处理量==全量（不重不漏）、文件锚游标往返、非法参数拒绝。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel.execution import budget
from agents_kernel.execution.budget import (Budget, BudgetOutcome, load_checkpoint,
                                            resume, save_checkpoint, spend_batches)
from agents_kernel.validation import CompanionError


def make_batches(costs):
    return [(i, ["claim-%d" % i], cost) for i, cost in enumerate(costs)]


class BudgetTest(unittest.TestCase):
    def test_try_consume_atomic_pass_or_nothing(self):
        b = Budget(100)
        self.assertTrue(b.try_consume(60))
        self.assertEqual(b.remaining, 40)
        self.assertFalse(b.try_consume(41))     # 不足：拒绝且分文不扣
        self.assertEqual(b.spent, 60)
        self.assertTrue(b.try_consume(40))      # 恰好耗尽
        self.assertEqual((b.spent, b.remaining), (100, 0))
        self.assertFalse(b.try_consume(1))

    def test_zero_cost_batch_always_fits(self):
        b = Budget(0)
        self.assertTrue(b.try_consume(0))

    def test_invalid_arguments_rejected(self):
        for total, spent in ((-1, 0), ("100", 0), (True, 0), (100, -1), (100, 101), (100, 1.5)):
            with self.subTest(total=total, spent=spent), self.assertRaises(CompanionError):
                Budget(total, spent)
        b = Budget(10)
        for bad in (-1, "5", True, 0.5):
            with self.subTest(consume=bad), self.assertRaises(CompanionError):
                b.try_consume(bad)


class SpendBatchesTest(unittest.TestCase):
    def test_complete_when_budget_covers_all(self):
        batches = make_batches([10, 20, 30])
        b = Budget(60)
        outcome = spend_batches(batches, b)
        self.assertEqual(outcome.status, "complete")
        self.assertEqual((outcome.processed, outcome.cursor, outcome.spent), (3, 3, 60))

    def test_pause_at_batch_boundary_with_prefix_processed(self):
        batches = make_batches([10, 20, 30, 40])
        b = Budget(35)
        outcome = spend_batches(batches, b)
        self.assertEqual(outcome.status, "paused")
        self.assertEqual(outcome.cursor, 2)          # 前两批入账，第三批放不下
        self.assertEqual(outcome.processed, 2)
        self.assertEqual(outcome.spent, 30)
        # 已处理的是严格前缀 [0, cursor)：不是抽样子集
        self.assertEqual([b[0] for b in batches[:outcome.cursor]], [0, 1])

    def test_no_sampled_result_when_budget_insufficient(self):
        # 预算只够第 1 批：结果必须是前缀，而不是"挑了几条"的抽样
        batches = make_batches([50, 1, 1, 1])
        b = Budget(50)
        outcome = spend_batches(batches, b)
        self.assertEqual(outcome.status, "paused")
        self.assertEqual((outcome.processed, outcome.cursor), (1, 1))

    def test_resume_cursor_and_start_offset(self):
        batches = make_batches([10] * 6)
        b = Budget(20)
        outcome = spend_batches(batches, b, start_cursor=4)
        self.assertEqual((outcome.status, outcome.processed, outcome.cursor), ("complete", 2, 6))

    def test_invalid_start_cursor_rejected(self):
        b = Budget(100)
        for bad in (-1, 3, True, "0"):
            with self.subTest(cursor=bad), self.assertRaises(CompanionError):
                spend_batches(make_batches([1, 1]), b, start_cursor=bad)

    def test_empty_batches_completes_immediately(self):
        outcome = spend_batches([], Budget(10))
        self.assertEqual(outcome.status, "complete")
        self.assertEqual(outcome.cursor, 0)


class CheckpointResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="budget-test-")
        self.path = os.path.join(self.tmp.name, "budget-checkpoint.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_pause_checkpoint_resume_covers_everything_no_gap_no_dup(self):
        batches = make_batches([10, 20, 30, 40, 50])
        total_cost = sum(c for _, _, c in batches)
        # 活动一：预算 55 → 处理 0、1 批后暂停（第三批 30 放不下剩余 25）
        act1 = Budget(55)
        outcome = spend_batches(batches, act1)
        self.assertEqual(outcome.status, "paused")
        save_checkpoint(self.path, act1, outcome.cursor)
        # 活动二（新进程/新活动）：从检查点恢复，同一台账续上新增拨付
        restored, cursor = resume(self.path)
        self.assertEqual((restored.spent, cursor), (30, 2))
        act2 = Budget(total_cost, restored.spent)
        outcome2 = spend_batches(batches, act2, start_cursor=cursor)
        self.assertEqual(outcome2.status, "complete")
        self.assertEqual(act2.spent, total_cost)
        # 跨活动总处理量 == 全量：批 0..4 各恰好一次（不重不漏）
        processed = [b[0] for b in batches[:outcome.cursor]] + \
                    [b[0] for b in batches[cursor:outcome2.cursor]]
        self.assertEqual(sorted(processed), [0, 1, 2, 3, 4])
        self.assertEqual(len(processed), 5)

    def test_checkpoint_marks_estimation(self):
        save_checkpoint(self.path, Budget(10, 3), 1)
        data = load_checkpoint(self.path)
        self.assertTrue(data["estimation"])          # 估算口径显式声明，不宣称精确 token
        self.assertEqual(data["version"], budget.CHECKPOINT_VERSION)

    def test_file_anchor_cursor_roundtrip(self):
        # 游标不限于整数 batch_index：文件锚（路径:行号）同样可持久化
        save_checkpoint(self.path, Budget(10), "src/big.py:1200")
        restored, cursor = resume(self.path)
        self.assertEqual(cursor, "src/big.py:1200")

    def test_extra_payload_roundtrip(self):
        save_checkpoint(self.path, Budget(10), 0, extra={"run": "run-1"})
        self.assertEqual(load_checkpoint(self.path)["extra"], {"run": "run-1"})

    def test_corrupt_checkpoint_rejected(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(CompanionError):
            load_checkpoint(self.path)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"version": 99, "total_tokens": 1, "spent": 0, "cursor": 0}')
        with self.assertRaises(CompanionError):
            load_checkpoint(self.path)


if __name__ == "__main__":
    unittest.main()

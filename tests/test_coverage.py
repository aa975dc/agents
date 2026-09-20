# -*- coding: utf-8 -*-
"""分维度覆盖账（C11）单测：三个维度各自的分母/已覆盖/缺口与总账闭合语义。

03_CAPACITY §2：分母未知不等于通过；只取前 N 项不得冒充完成；
covered 超过分母是记账错误，必须拒绝而不是钳制。
"""
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel.services.coverage import DIMENSIONS, CoverageLedger


class CoverageLedgerTests(unittest.TestCase):
    def test_account_starts_incomplete_with_unknown_denominators(self):
        account = CoverageLedger("run-1", generation=3).to_account()
        self.assertFalse(account["complete"])
        self.assertEqual(account["schema_version"], 1)
        self.assertEqual(account["run_id"], "run-1")
        self.assertEqual(account["generation"], 3)
        self.assertEqual(set(account["dimensions"]), set(DIMENSIONS))
        for name, dim in account["dimensions"].items():
            self.assertIsNone(dim["denominator"], name)
            self.assertEqual(dim["covered"], 0, name)
            self.assertFalse(dim["complete"], name)
            self.assertEqual(dim["gaps"], [], name)

    def test_complete_dimension_requires_known_denominator_exact_cover_and_no_gaps(self):
        ledger = CoverageLedger("run-1")
        ledger.set_denominator("independent_review", 10)
        ledger.record_covered("independent_review", 10)
        self.assertTrue(ledger.complete("independent_review"))
        ledger.record_covered("independent_review", 9, gaps=["分页超上限，2 条未复核"])
        self.assertFalse(ledger.complete("independent_review"))
        self.assertEqual(ledger.to_account()["dimensions"]["independent_review"]["gaps"],
                         ["分页超上限，2 条未复核"])
        ledger.record_covered("independent_review", 10)
        self.assertTrue(ledger.complete("independent_review"), "清除缺口后应重新闭合")

    def test_unknown_denominator_stays_incomplete_even_when_covered_matches(self):
        ledger = CoverageLedger("run-1")
        ledger.record_covered("index_files", 0)
        self.assertFalse(ledger.complete("index_files"))
        account = ledger.to_account()
        self.assertFalse(account["complete"])
        self.assertIsNone(account["dimensions"]["index_files"]["denominator"])

    def test_total_complete_requires_every_dimension_closed(self):
        ledger = CoverageLedger("run-2", generation=7)
        ledger.set_denominator("index_files", 5)
        ledger.record_covered("index_files", 5)
        ledger.set_denominator("semantics_deep", 5)
        ledger.record_covered("semantics_deep", 5)
        self.assertFalse(ledger.to_account()["complete"])
        ledger.set_denominator("independent_review", 2)
        ledger.record_covered("independent_review", 2)
        self.assertTrue(ledger.to_account()["complete"])

    def test_covered_over_denominator_is_rejected_not_clamped(self):
        ledger = CoverageLedger("run-1")
        ledger.set_denominator("semantics_deep", 3)
        with self.assertRaises(ValueError):
            ledger.record_covered("semantics_deep", 4)
        ledger.record_covered("semantics_deep", 3)
        with self.assertRaises(ValueError, msg="分母不得降到已覆盖数之下"):
            ledger.set_denominator("semantics_deep", 2)

    def test_invalid_inputs_are_rejected(self):
        ledger = CoverageLedger("run-1")
        with self.assertRaises(ValueError):
            ledger.set_denominator("coverage", 1)
        with self.assertRaises(ValueError):
            ledger.record_covered("nope", 1)
        with self.assertRaises(ValueError):
            ledger.complete("unknown")
        for bad in (-1, 1.5, True, "3", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ledger.set_denominator("index_files", bad)

    def test_account_is_json_serializable(self):
        ledger = CoverageLedger("run-1", generation=1)
        ledger.set_denominator("independent_review", 4)
        ledger.record_covered("independent_review", 3, gaps=["A6 分页超上限，剩余 1 条未复核"])
        account = ledger.to_account()
        decoded = json.loads(json.dumps(account, ensure_ascii=False))
        self.assertEqual(decoded, account)
        self.assertEqual(decoded["dimensions"]["independent_review"]["covered"], 3)
        self.assertFalse(decoded["dimensions"]["independent_review"]["complete"])


if __name__ == "__main__":
    unittest.main()

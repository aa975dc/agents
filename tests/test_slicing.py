# -*- coding: utf-8 -*-
"""token 切片单测（C10 前半/CV01/CV05）：估算口径、切片边界、分批全覆盖。

覆盖：空文件、单超长行硬截断、中文密度、恰好整除、无换行结尾、多分块流式读取、
claims 分批全覆盖且批间无重叠无遗漏、单条超预算显式拒绝（不抽样不截断）。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel.indexing import slicing
from agents_kernel.indexing.slicing import estimate_tokens, slice_claims, slice_file
from agents_kernel.validation import CompanionError


def write_file(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class EstimateTokensTest(unittest.TestCase):
    def test_empty_and_ascii_density(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("abcd"), 1)          # 4 字符 ≈ 1 token
        self.assertEqual(estimate_tokens("abc"), 1)           # 余数进一位
        self.assertEqual(estimate_tokens("abcdefgh"), 2)      # 恰好整除

    def test_cjk_density_is_one_per_char(self):
        self.assertEqual(estimate_tokens("中文字符"), 4)
        self.assertEqual(estimate_tokens("中"), 1)

    def test_mixed_text(self):
        # 3 个 ASCII（≈1）+ 2 个中文（2）
        self.assertEqual(estimate_tokens("abc中文"), 3)

    def test_estimation_is_heuristic_not_exact(self):
        # 口径常量：ASCII 密度 4 字符/token；估算标 estimation，不宣称精确
        self.assertEqual(slicing._ASCII_CHARS_PER_TOKEN, 4)


class SliceFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="slicing-test-")
        self.path = os.path.join(self.tmp.name, "src.py")

    def tearDown(self):
        self.tmp.cleanup()

    def slices(self, text, max_tokens):
        write_file(self.path, text)
        return list(slice_file(self.path, max_tokens))

    def test_empty_file_yields_no_slices(self):
        self.assertEqual(self.slices("", 100), [])

    def test_every_slice_within_budget_with_anchor(self):
        text = "".join("line %03d abcdefghij\n" % i for i in range(1, 51))
        slices = self.slices(text, 60)
        self.assertGreater(len(slices), 1)
        for s in slices:
            self.assertLessEqual(s.est_tokens, 60)
            self.assertTrue(s.anchor.startswith(self.path + ":"))
            self.assertTrue(s.text.startswith(s.anchor + "\n"))
            self.assertFalse(s.truncated_line)

    def test_slices_are_continuous_ordered_and_complete(self):
        text = "".join("row-%02d 中文内容\n" % i for i in range(1, 31))
        slices = self.slices(text, 40)
        self.assertEqual(slices[0].start_line, 1)
        self.assertEqual(slices[-1].end_line, 30)
        for prev, nxt in zip(slices, slices[1:]):
            self.assertEqual(prev.end_line + 1, nxt.start_line)
        # 行内容不重不漏：按行号展开与原文一致
        lines_by_no = {}
        for s in slices:
            for offset, line in enumerate(s.text.split("\n")[1:]):
                lines_by_no[s.start_line + offset] = line
        self.assertEqual("".join(lines_by_no[i] + "\n" for i in range(1, 31)), text)

    def test_exact_fit_single_slice(self):
        # 10 行、每行 10 ASCII 字符（含换行 11→3 token/行），锚 ≈ 小额；预算给足恰好整除
        text = "".join("012345678\n" for _ in range(10))
        slices = self.slices(text, 10_000)
        self.assertEqual(len(slices), 1)
        self.assertEqual((slices[0].start_line, slices[0].end_line), (1, 10))

    def test_oversize_single_line_hard_truncated_and_marked(self):
        long_line = "a" * 5_000 + "\n"
        slices = self.slices(long_line + "tail\n", 100)
        self.assertEqual(len(slices), 2)
        head, tail = slices
        self.assertTrue(head.truncated_line)
        self.assertEqual((head.start_line, head.end_line), (1, 1))
        self.assertLessEqual(head.est_tokens, 100)
        self.assertLess(len(head.text), len("a" * 5_000))  # 前缀被硬截断
        self.assertIn("[truncated]", head.anchor)
        self.assertEqual((tail.start_line, tail.end_line), (2, 2))
        self.assertEqual(tail.text.split("\n")[1], "tail")
        self.assertFalse(tail.truncated_line)

    def test_extremely_long_line_bounded_memory(self):
        # 远超 char_cap（4×预算）的物理行：截断前缀有界、剩余部分被丢弃并标记
        slices = self.slices("b" * 400_000 + "\n" + "ok\n", 50)
        self.assertEqual(len(slices), 2)
        self.assertTrue(slices[0].truncated_line)
        self.assertLessEqual(slices[0].est_tokens, 50)
        self.assertTrue(all(len(s.text) <= 50 * 4 + 100 for s in slices))

    def test_no_trailing_newline_last_line_kept(self):
        slices = self.slices("one\ntwo", 100)
        self.assertEqual(len(slices), 1)
        self.assertEqual(slices[0].end_line, 2)
        self.assertEqual(slices[0].text.split("\n")[2], "two")

    def test_chinese_density_respected(self):
        text = "".join("中文注释" * 10 + "\n" for _ in range(20))  # 每行 40 中文 ≈ 41 token
        slices = self.slices(text, 100)
        for s in slices:
            self.assertLessEqual(s.est_tokens, 100)
        self.assertGreater(len(slices), 1)

    def test_streaming_across_many_chunks(self):
        # ~2.5 MiB 文件、默认 1MiB 分块口径：多块累积下切片仍连续完整
        write_file(self.path, ("x" * 127 + "\n") * 20_000)
        slices = list(slice_file(self.path, 500))
        self.assertEqual(slices[0].start_line, 1)
        self.assertEqual(slices[-1].end_line, 20_000)
        for prev, nxt in zip(slices, slices[1:]):
            self.assertEqual(prev.end_line + 1, nxt.start_line)

    def test_invalid_budget_rejected(self):
        write_file(self.path, "x\n")
        for bad in (0, -1, "100", 1.5, True):
            with self.subTest(bad=bad), self.assertRaises(CompanionError):
                list(slice_file(self.path, bad))


class SliceClaimsTest(unittest.TestCase):
    def claim(self, i):
        return {"id": "c%d" % i, "claim": "结论内容 %d，包含中文与 words" % i,
                "source_role": "A3", "evidence_refs": ["src/a.py:1"]}

    def test_full_coverage_no_overlap_no_gap_ordered(self):
        claims = [self.claim(i) for i in range(57)]
        batches = slice_claims(claims, 300)
        self.assertGreater(len(batches), 1)
        flattened = [c for _, batch, _ in batches for c in batch]
        self.assertEqual(flattened, claims)               # 全覆盖、顺序一致（无重叠无遗漏）
        self.assertEqual([i for i, _, _ in batches], list(range(len(batches))))
        for _, _, est in batches:
            self.assertLessEqual(est, 300)

    def test_batch_count_unbounded_and_bound_respected(self):
        claims = [self.claim(i) for i in range(500)]
        batches = slice_claims(claims, 120)
        self.assertEqual(sum(len(b) for _, b, _ in batches), 500)
        self.assertTrue(all(est <= 120 for _, _, est in batches))

    def test_empty_input_zero_batches(self):
        self.assertEqual(slice_claims([], 100), [])

    def test_single_claim_over_budget_rejected_not_sampled(self):
        big = {"id": "big", "claim": "长" * 500, "source_role": "A3",
               "evidence_refs": ["src/a.py:1"]}
        with self.assertRaises(CompanionError):
            slice_claims([self.claim(0), big, self.claim(1)], 100)

    def test_invalid_budget_and_unserializable_claim_rejected(self):
        with self.assertRaises(CompanionError):
            slice_claims([self.claim(0)], 0)
        with self.assertRaises(CompanionError):
            slice_claims([{"id": set()}], 100)

    def test_exact_fit_claims_all_in_one_batch(self):
        claims = [self.claim(i) for i in range(3)]
        batches = slice_claims(claims, 10_000)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0][0], 0)
        self.assertEqual(batches[0][1], claims)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""深读活动（full_deep campaign）单测（C10 后半/C13 尾/CV02/CV05）。

覆盖：预算只够 7 项→paused 于 7/8 边界且分文不扣、续跑补齐 20 项无重无漏、
read_partial/failed 记原因并计入缺口（部分读≠抽样）、清单锚定失效作废重读并
记录 generation 变化、严格前缀断裂拒绝续跑、未读完不产生 coverage account。
"""
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel import digest
from agents_kernel.execution.budget import Budget
from agents_kernel.indexing import slicing
from agents_kernel.services import campaign as campaign_mod
from agents_kernel.services.campaign import DeepReadCampaign
from agents_kernel.validation import CompanionError

SLICE_TOKENS = 16000


def write_file(root, rel, body):
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    return digest.sha256_bytes(body.encode("utf-8"))


def make_manifest(root, count, body_fn=None):
    entries = []
    for i in range(count):
        rel = "src/f%02d.py" % i
        body = body_fn(i) if body_fn else "x" * 400
        sha = write_file(root, rel, body)
        entries.append({"path": rel, "sha256": sha})
    return entries


def item_cost(root, rel):
    """单文件深读的估算成本（与 next_batch 同口径：slicing 全片估算之和）。"""
    return sum(s.est_tokens for s in slicing.slice_file(str(root / rel), SLICE_TOKENS))


def read_all(c, batch_size=1):
    """循环领取并全部记账 read_fully；返回 (acquired 批次列表, 最后一个 BatchResult)。"""
    batches = []
    while c.status != campaign_mod.STATUS_COMPLETED:
        result = c.next_batch(batch_size)
        if result.status == campaign_mod.STATUS_PAUSED:
            return batches, result
        batches.append(result)
        for item in result.items:
            c.record_result(item, campaign_mod.OUTCOME_FULL)
    return batches, None


class PauseResumeTest(unittest.TestCase):
    def test_pause_at_boundary_and_resume_completes_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = make_manifest(root, 20)
            costs = [item_cost(root, e["path"]) for e in manifest]
            run_dir = root / "run"
            budget_total = sum(costs[:7])  # 恰好够 7 项
            c = DeepReadCampaign.open(manifest, root, budget_total, run_dir,
                                      "camp-1", "gen-2026-09-20", slice_tokens=SLICE_TOKENS)
            # 逐项领取记账：7 项后预算耗尽
            for i in range(7):
                result = c.next_batch(1)
                self.assertEqual(result.status, "acquired")
                self.assertEqual(result.items[0].index, i)
                c.record_result(result.items[0].path, "read_fully")
            paused = c.next_batch(1)
            self.assertEqual((paused.status, paused.items, paused.est_tokens), ("paused", [], 0))
            self.assertEqual((c.status, c.cursor), ("paused", 7))
            self.assertEqual(c.budget.spent, budget_total)   # 分文不差
            self.assertEqual(c.budget.remaining, 0)
            # 再试仍 paused：游标不动、分文不扣
            again = c.next_batch(1)
            self.assertEqual(again.status, "paused")
            self.assertEqual((c.cursor, c.budget.spent), (7, budget_total))
            # 未读完不产生 coverage account
            with self.assertRaises(CompanionError):
                c.coverage_account()
            # 续跑：从检查点恢复（新对象，跨活动），显式追加预算补齐全部 20 项
            c2 = DeepReadCampaign.resume(run_dir, "camp-1")
            self.assertEqual((c2.status, c2.cursor), ("paused", 7))
            c2.top_up(sum(costs[7:]))
            batches, _ = read_all(c2)
            self.assertTrue(all(len(b.items) == 1 for b in batches))
            self.assertEqual(c2.status, "completed")
            self.assertEqual([r["path"] for r in c2.results], [e["path"] for e in manifest])
            self.assertEqual([r["index"] for r in c2.results], list(range(20)))  # 无跳项
            self.assertEqual(len({r["path"] for r in c2.results}), 20)           # 无重项
            self.assertEqual(c2.budget.spent, budget_total + sum(costs[7:]))
            account = c2.coverage_account()
            dim = account["dimensions"]["semantics_deep"]
            self.assertEqual((dim["denominator"], dim["covered"], dim["gaps"]), (20, 20, []))
            self.assertTrue(dim["complete"])

    def test_batch_atomicity_pause_consumes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = make_manifest(root, 4)
            costs = [item_cost(root, e["path"]) for e in manifest]
            c = DeepReadCampaign.open(manifest, root, sum(costs[:2]), root / "run",
                                      "camp-b", "g")
            result = c.next_batch(4)  # 一批 4 项 > 预算 2 项：整批拒绝，不拆分不扣减
            self.assertEqual(result.status, "paused")
            self.assertEqual((c.cursor, c.budget.spent, c.budget.remaining),
                             (0, 0, sum(costs[:2])))


class OutcomeSemanticsTest(unittest.TestCase):
    def test_partial_and_failed_require_reason_and_become_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = make_manifest(root, 4)
            costs = [item_cost(root, e["path"]) for e in manifest]
            c = DeepReadCampaign.open(manifest, root, sum(costs), root / "run",
                                      "camp-o", "g", slice_tokens=SLICE_TOKENS)
            batch = c.next_batch(4)
            self.assertEqual(batch.status, "acquired")
            # read_partial / failed 必须带原因；read_fully 不带原因
            with self.assertRaises(CompanionError):
                c.record_result(batch.items[0].path, "read_partial")
            with self.assertRaises(CompanionError):
                c.record_result(batch.items[1].path, "failed", reason="  ")
            with self.assertRaises(CompanionError):
                c.record_result(batch.items[2].path, "read_fully", reason="不该有")
            with self.assertRaises(CompanionError):
                c.record_result(batch.items[2].path, "skipped")  # 未知 outcome
            c.record_result(batch.items[0].path, "read_fully")
            c.record_result(batch.items[1].path, "read_partial",
                            reason="尾部超预算待下一 generation 复核", notes="读了 1-40 行")
            c.record_result(batch.items[2].path, "read_fully")
            c.record_result(batch.items[3].path, "failed", reason="读取时文件被截断")
            self.assertEqual(c.status, "completed")
            # failed 项保留在账本中
            failed = [r for r in c.results if r["outcome"] == "failed"]
            self.assertEqual([r["index"] for r in failed], [3])
            self.assertEqual(failed[0]["reason"], "读取时文件被截断")
            # 部分读计入缺口：complete 不了
            account = c.coverage_account()
            dim = account["dimensions"]["semantics_deep"]
            self.assertEqual((dim["denominator"], dim["covered"]), (4, 2))
            self.assertFalse(dim["complete"])
            self.assertFalse(account["complete"])
            self.assertEqual([(g["outcome"], g["reason"]) for g in dim["gaps"]],
                             [("read_partial", "尾部超预算待下一 generation 复核"),
                              ("failed", "读取时文件被截断")])

    def test_record_out_of_order_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = make_manifest(root, 3)
            c = DeepReadCampaign.open(manifest, root, 10 ** 9, root / "run",
                                      "camp-r", "g", slice_tokens=SLICE_TOKENS)
            batch = c.next_batch(3)
            with self.assertRaises(CompanionError):
                c.record_result(batch.items[1].path, "read_fully")   # 跳项
            with self.assertRaises(CompanionError):
                c.record_result("src/zz.py", "read_fully")           # 清单外
            with self.assertRaises(CompanionError):
                c.next_batch(1)                                       # 上批未记完不得再领


class AnchorInvalidationTest(unittest.TestCase):
    def test_content_change_voids_anchor_and_rereads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = make_manifest(root, 3)
            costs = [item_cost(root, e["path"]) for e in manifest]
            c = DeepReadCampaign.open(manifest, root, sum(costs) * 2, root / "run",
                                      "camp-a", "gen-a", slice_tokens=SLICE_TOKENS)
            # 清单锚定后第 1 项内容变化
            new_sha = write_file(root, "src/f01.py", "y" * 400)
            batch = c.next_batch(3)
            self.assertEqual(batch.status, "acquired")
            self.assertEqual(len(c.invalidations), 1)
            event = c.invalidations[0]
            self.assertEqual((event["index"], event["path"]), (1, "src/f01.py"))
            self.assertEqual((event["old_sha256"], event["new_sha256"], event["generation"]),
                             (manifest[1]["sha256"], new_sha, 2))
            self.assertEqual(c.manifest[1]["sha256"], new_sha)   # 旧锚作废、重锚
            item1 = batch.items[1]
            self.assertEqual((item1.generation, item1.sha256), (2, new_sha))
            self.assertEqual(batch.items[0].generation, 1)
            for item in batch.items:
                c.record_result(item, "read_fully")
            self.assertEqual(c.results[1]["generation"], 2)      # 重读记在新 generation 下
            self.assertEqual(c.results[0]["generation"], 1)
            self.assertEqual(DeepReadCampaign.resume(root / "run", "camp-a").invalidations,
                             c.invalidations)


class StrictPrefixTest(unittest.TestCase):
    def _open_with_one_recorded(self, root):
        manifest = make_manifest(root, 3)
        c = DeepReadCampaign.open(manifest, root, 10 ** 9, root / "run",
                                  "camp-p", "g", slice_tokens=SLICE_TOKENS)
        batch = c.next_batch(3)
        c.record_result(batch.items[0].path, "read_fully")
        return manifest

    def test_resume_rejects_broken_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._open_with_one_recorded(root)
            path = root / "run" / "camp-p.campaign.json"
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
            for mutate in (
                lambda d: d["results"].pop(0),                        # 记账丢失→cursor 断裂
                lambda d: d["results"][0].__setitem__("path", "src/f99.py"),  # 锚不一致
                lambda d: d["results"][0].__setitem__("index", 1),    # 跳项
                lambda d: d.update({"cursor": 2}),                    # 游标越过记账
                lambda d: d.update({"status": "completed"}),          # 状态与游标矛盾
            ):
                broken = json.loads(json.dumps(data))
                mutate(broken)
                path.write_text(json.dumps(broken), encoding="utf-8")
                with self.subTest(mutate=mutate):
                    with self.assertRaises(CompanionError):
                        DeepReadCampaign.resume(root / "run", "camp-p")

    def test_resume_clean_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._open_with_one_recorded(root)
            c = DeepReadCampaign.resume(root / "run", "camp-p")
            self.assertEqual((c.cursor, c.status, len(c.results)), (1, "running", 1))


class ManifestValidationTest(unittest.TestCase):
    def test_open_rejects_bad_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = {"path": "src/a.py", "sha256": "a" * 64}
            bad_manifests = [
                [],  # 空清单：full_deep 没有分母不成立
                [{"path": "src/a.py", "sha256": "a" * 64}, {"path": "src/a.py", "sha256": "b" * 64}],
                [{"path": "src/a.py", "sha256": "SHORT"}],
                [{"path": "src/a.py", "sha256": "a" * 64, "extra": 1}],
                [{"path": "/abs/a.py", "sha256": "a" * 64}],
                [{"path": "src/a.py"}],
            ]
            for manifest in bad_manifests:
                with self.subTest(manifest=manifest):
                    with self.assertRaises(CompanionError):
                        DeepReadCampaign.open(manifest, root, 1000, root / "run",
                                              "camp-v", "g", slice_tokens=SLICE_TOKENS)
            with self.assertRaises(CompanionError):  # budget 形状
                DeepReadCampaign.open([good], root, -1, root / "run", "camp-v", "g")

    def test_open_rejects_existing_campaign_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = make_manifest(root, 1)
            DeepReadCampaign.open(manifest, root, 10 ** 6, root / "run", "camp-x", "g",
                                  slice_tokens=SLICE_TOKENS)
            with self.assertRaises(CompanionError):
                DeepReadCampaign.open(manifest, root, 10 ** 6, root / "run", "camp-x", "g",
                                      slice_tokens=SLICE_TOKENS)


if __name__ == "__main__":
    unittest.main()

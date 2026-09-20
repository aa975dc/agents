# -*- coding: utf-8 -*-
"""跨会话续接台账（ResumeLedger）单测（C12 尾/SC08/TK03 尾/TK04 尾/IX08 尾）。

覆盖：崩溃（无 fail 记录）后新会话打开即得续接清单与游标摘要、检查点缺失/
损坏→broken（不猜游标）、epoch 围栏拒旧写者、源锚变更→stale_anchor 不自动
续跑、campaign/index_scan 的建议调用与真实 API 续跑游标一致（不跑模型）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel import digest
from agents_kernel.execution import budget as budget_mod
from agents_kernel.execution.budget import Budget
from agents_kernel.indexing import slicing
from agents_kernel.services import campaign as campaign_mod
from agents_kernel.services import resume as resume_mod
from agents_kernel.services.campaign import DeepReadCampaign
from agents_kernel.services.resume import ResumeLedger
from agents_kernel.validation import CompanionError

SLICE_TOKENS = 16000


def write_file(root, rel, body):
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    return digest.sha256_bytes(body.encode("utf-8"))


def make_manifest(root, count):
    entries = []
    for i in range(count):
        rel = "src/f%02d.py" % i
        sha = write_file(root, rel, "x" * 400)
        entries.append({"path": rel, "sha256": sha})
    return entries


def manifest_anchor(manifest):
    """被分析清单的源锚：排序后 JSON 的 sha256（与 campaign 固定顺序同口径）。"""
    canonical = json.dumps(sorted(manifest, key=lambda e: e["path"]),
                           ensure_ascii=False, sort_keys=True)
    return digest.sha256_bytes(canonical.encode("utf-8"))


def item_cost(root, rel):
    return sum(s.est_tokens for s in slicing.slice_file(str(root / rel), SLICE_TOKENS))


class CrashResumeTest(unittest.TestCase):
    def test_crash_then_new_session_plans_resume(self):
        """登记→心跳→崩溃（无 fail）→新会话打开→续接清单列出活动+游标摘要。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            checkpoint = run_root / "run" / "scan.checkpoint.json"
            checkpoint.parent.mkdir(parents=True)
            budget_mod.save_checkpoint(checkpoint, Budget(1000, 300), 7)

            first = ResumeLedger.open(run_root, "session-a")
            first.register("index_scan", "scan-1", str(checkpoint), 1,
                           source_anchor="head-abc")
            first.heartbeat("index_scan", "scan-1", note="第 7 批完成")
            # —— 此处进程崩溃：无 complete/fail 记录 ——

            second = ResumeLedger.open(run_root, "session-b")
            self.assertEqual((second.epoch, first.epoch), (2, 1))
            plan = second.resume_plan()
            self.assertEqual(len(plan), 1)
            item = plan[0]
            self.assertEqual((item["kind"], item["id"], item["ledger_status"]),
                             ("index_scan", "scan-1", "running"))
            self.assertEqual(item["condition"], "ok")
            self.assertEqual(item["cursor_summary"], "cursor=7")
            self.assertEqual(item["resume_call"], "budget.resume(%r)" % str(checkpoint))

    def test_completed_not_listed_failed_listed_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            ledger = ResumeLedger.open(run_root, "session-a")
            done = run_root / "done.json"
            budget_mod.save_checkpoint(done, Budget(10, 10), 5)
            dead = run_root / "dead.json"
            budget_mod.save_checkpoint(dead, Budget(10, 2), 1)
            ledger.register("index_scan", "done", str(done), 1)
            ledger.register("index_scan", "dead", str(dead), 1)
            ledger.complete("index_scan", "done")
            ledger.fail("index_scan", "dead", "外部副作用未核查，BLOCKED")
            plan = ledger.resume_plan()
            self.assertEqual([item["id"] for item in plan], ["dead"])
            self.assertIn("外部副作用未核查", plan[0]["detail"])


class BrokenCheckpointTest(unittest.TestCase):
    def test_missing_checkpoint_is_broken(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            checkpoint = run_root / "gone.checkpoint.json"
            ledger = ResumeLedger.open(run_root, "session-a")
            ledger.register("custom", "ghost", str(checkpoint), 1)
            plan = ledger.resume_plan()
            self.assertEqual(plan[0]["condition"], "broken")
            self.assertIsNone(plan[0]["cursor_summary"])   # 不猜游标
            self.assertIsNone(plan[0]["resume_call"])

    def test_corrupt_checkpoint_is_broken(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            checkpoint = run_root / "half.checkpoint.json"
            checkpoint.write_text('{"cursor": 3, "tot', encoding="utf-8")  # 半写截断
            ledger = ResumeLedger.open(run_root, "session-a")
            ledger.register("custom", "torn", str(checkpoint), 1)
            plan = ledger.resume_plan()
            self.assertEqual(plan[0]["condition"], "broken")
            self.assertIsNone(plan[0]["cursor_summary"])

    def test_unknown_structure_honest_no_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            checkpoint = run_root / "odd.json"
            checkpoint.write_text('{"rows": 12}', encoding="utf-8")
            ledger = ResumeLedger.open(run_root, "session-a")
            ledger.register("custom", "odd", str(checkpoint), 1)
            plan = ledger.resume_plan()
            self.assertEqual(plan[0]["condition"], "ok")
            self.assertIsNone(plan[0]["cursor_summary"])   # 结构未知：如实为 None
            self.assertIsNone(plan[0]["resume_call"])


class EpochFenceTest(unittest.TestCase):
    def test_takeover_rejects_old_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            checkpoint = run_root / "c.json"
            budget_mod.save_checkpoint(checkpoint, Budget(10, 1), 1)
            first = ResumeLedger.open(run_root, "session-a")
            first.register("index_scan", "scan", str(checkpoint), 1)
            ledger_bytes = (run_root / resume_mod.LEDGER_FILENAME).read_bytes()

            second = ResumeLedger.open(run_root, "session-b")
            second.register("index_scan", "scan-2", str(checkpoint), 1)

            with self.assertRaises(CompanionError):
                first.heartbeat("index_scan", "scan")       # 旧 epoch 更新被拒
            with self.assertRaises(CompanionError):
                first.register("index_scan", "scan-3", str(checkpoint), 1)
            # 被拒的写入没有落盘：台账仍只有 second 的两行
            current = json.loads((run_root / resume_mod.LEDGER_FILENAME).read_text())
            self.assertEqual(current["writer_epoch"], 2)
            self.assertEqual(sorted(row["id"] for row in current["activities"]),
                             ["scan", "scan-2"])
            self.assertNotEqual(
                (run_root / resume_mod.LEDGER_FILENAME).read_bytes(), ledger_bytes)
            takeovers = json.loads(
                (run_root / resume_mod.TAKEOVER_LOG).read_text().splitlines()[0])
            self.assertEqual((takeovers["type"],
                              takeovers["from"]["epoch"],
                              takeovers["to"]["epoch"]), ("resume_takeover", 1, 2))

    def test_corrupt_ledger_refused_not_reset(self):
        """台账损坏 → 拒绝打开，不造空台账掩盖事实。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            (run_root / resume_mod.LEDGER_FILENAME).write_text("{oops", encoding="utf-8")
            with self.assertRaises(CompanionError):
                ResumeLedger.open(run_root, "session-a")


class StaleAnchorTest(unittest.TestCase):
    def test_anchor_change_marks_stale_no_auto_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            checkpoint = run_root / "scan.checkpoint.json"
            budget_mod.save_checkpoint(checkpoint, Budget(100, 40), 4)
            ledger = ResumeLedger.open(run_root, "session-a")
            ledger.register("index_scan", "scan", str(checkpoint), 1,
                            source_anchor="head-old")

            same = ledger.resume_plan(current_anchor="head-old")
            self.assertEqual((same[0]["condition"], same[0]["cursor_summary"]),
                             ("ok", "cursor=4"))

            changed = ledger.resume_plan(current_anchor="head-new")
            self.assertEqual(changed[0]["condition"], "stale_anchor")
            self.assertIsNone(changed[0]["cursor_summary"])  # 游标不可信，不给
            self.assertIsNone(changed[0]["resume_call"])     # 不自动续跑
            self.assertIn("head-old", changed[0]["detail"])
            self.assertIn("head-new", changed[0]["detail"])

    def test_generation_change_does_not_stitch_results(self):
        """IX08：旧锚下的游标续到新锚数据上会拼出假完整结果——台账拒绝给调用。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            root = Path(tmp) / "target"
            manifest = make_manifest(root, 3)
            anchor = manifest_anchor(manifest)
            run_dir = run_root / "run"
            c = DeepReadCampaign.open(manifest, root, sum(item_cost(root, e["path"])
                                                         for e in manifest[:2]),
                                      run_dir, "camp-ix", "gen-a", slice_tokens=SLICE_TOKENS)
            for _ in range(2):
                result = c.next_batch(1)
                c.record_result(result.items[0].path, campaign_mod.OUTCOME_FULL)

            ledger = ResumeLedger.open(run_root, "session-a")
            ledger.register("campaign", "camp-ix", str(c.path), campaign_mod.CHECKPOINT_VERSION,
                            source_anchor=anchor)
            # 源版本更换：改一个文件内容 → 新锚（旧游标绑定旧 generation）
            write_file(root, manifest[2]["path"], "changed body")
            rescanned = [{"path": e["path"], "sha256": digest.sha256_file(str(root / e["path"]))}
                         for e in manifest]
            new_anchor = manifest_anchor(rescanned)
            plan = ledger.resume_plan(current_anchor=new_anchor)
            self.assertEqual(plan[0]["condition"], "stale_anchor")

            same = ledger.resume_plan(current_anchor=anchor)
            self.assertEqual(same[0]["condition"], "ok")
            # 崩溃可能停在 running（批次已领未记账）；游标仍与 results 严格前缀一致
            self.assertEqual(same[0]["cursor_summary"], "cursor=2/3 status=running")


class CampaignIntegrationTest(unittest.TestCase):
    def test_campaign_pause_resume_via_plan(self):
        """P6-03 campaign 检查点登记后续接：建议调用真跑，游标一致（不跑模型）。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            root = run_root / "target"
            manifest = make_manifest(root, 8)
            costs = [item_cost(root, e["path"]) for e in manifest]
            anchor = manifest_anchor(manifest)
            run_dir = run_root / "run"
            campaign = DeepReadCampaign.open(manifest, root, sum(costs[:3]), run_dir,
                                             "camp-1", "gen-2026-09-20",
                                             slice_tokens=SLICE_TOKENS)
            for _ in range(3):
                result = campaign.next_batch(1)
                self.assertEqual(result.status, campaign_mod.BATCH_ACQUIRED)
                campaign.record_result(result.items[0].path, campaign_mod.OUTCOME_FULL)
            paused = campaign.next_batch(1)
            self.assertEqual(paused.status, campaign_mod.STATUS_PAUSED)
            cursor_before = campaign.cursor
            # 旧会话登记活动后崩溃：无 complete/fail 记录
            old = ResumeLedger.open(run_root, "session-a")
            old.register("campaign", "camp-1", str(campaign.path),
                         campaign_mod.CHECKPOINT_VERSION, source_anchor=anchor)
            old.heartbeat("campaign", "camp-1", note="3/8 已记账，预算耗尽暂停")

            new = ResumeLedger.open(run_root, "new-session")
            plan = new.resume_plan(current_anchor=anchor)
            self.assertEqual(len(plan), 1)
            item = plan[0]
            self.assertEqual((item["kind"], item["ledger_status"], item["condition"]),
                             ("campaign", "running", "ok"))
            self.assertEqual(item["cursor_summary"],
                             "cursor=%d/8 status=paused" % cursor_before)
            suggested = item["resume_call"]
            self.assertEqual(
                suggested,
                "DeepReadCampaign.resume(run_dir=%r, campaign_id=%r)"
                % (str(run_dir), "camp-1"))

            # 按建议调用真跑一遍（同参数的 API 调用，不 eval 字符串）
            resumed = DeepReadCampaign.resume(run_dir, "camp-1")
            self.assertEqual(resumed.cursor, cursor_before)
            self.assertEqual(resumed.status, campaign_mod.STATUS_PAUSED)

            # 补预算续跑到完成，登记完结后清单不再列出
            resumed.top_up(sum(costs[3:]))
            while resumed.status != campaign_mod.STATUS_COMPLETED:
                result = resumed.next_batch(1)
                resumed.record_result(result.items[0].path, campaign_mod.OUTCOME_FULL)
            new.register("campaign", "camp-1", str(resumed.path),
                         campaign_mod.CHECKPOINT_VERSION, source_anchor=anchor)
            new.complete("campaign", "camp-1")
            self.assertEqual(new.resume_plan(current_anchor=anchor), [])

    def test_budget_checkpoint_resume_via_plan(self):
        """index_scan（budget 检查点）：建议调用 budget.resume 真跑，游标一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            checkpoint = run_root / "index.scan.checkpoint.json"
            budget_mod.save_checkpoint(checkpoint, Budget(900, 400), 12)
            ledger = ResumeLedger.open(run_root, "session-a")
            ledger.register("index_scan", "full-scan", str(checkpoint), 1,
                            source_anchor="head-abc")
            item = ledger.resume_plan(current_anchor="head-abc")[0]
            self.assertEqual(item["cursor_summary"], "cursor=12")
            # 建议调用串与真实 API 一一对应；真跑一遍核对游标
            self.assertEqual(item["resume_call"], "budget.resume(%r)" % str(checkpoint))
            restored, cursor = budget_mod.resume(checkpoint)
            self.assertIsInstance(restored, Budget)
            self.assertEqual((cursor, restored.remaining), (12, 500))

    def test_integration_kind_generic_until_service_lands(self):
        """integration 服务未合入：只做通用处理（存在性+游标摘要），无专属语义。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            checkpoint = run_root / "integration.json"
            checkpoint.write_text(json.dumps({"version": 1, "cursor": {"p": 4}}),
                                  encoding="utf-8")
            ledger = ResumeLedger.open(run_root, "session-a")
            ledger.register("integration", "integ-1", str(checkpoint), 1)
            item = ledger.resume_plan()[0]
            self.assertEqual(item["cursor_summary"], 'cursor={"p": 4}')
            self.assertIsNone(item["resume_call"])   # 无统一入口：人工核对，不编造


if __name__ == "__main__":
    unittest.main()

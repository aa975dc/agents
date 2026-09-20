# -*- coding: utf-8 -*-
"""P5-05：固定候选集成——候选门、integration 版本幂等、两级回归门与失效（C06 后半/TK07/ST07 尾）。

覆盖：review_required policy 开启且无有效批准 → 拒绝入列，有批准 → 入列且集成
sha 与批准固定版本一致性核对；同候选集恒同版本号同哈希（幂等、顺序无关），加
任务 → 新版本号；version_level 未记录/失败 → integration 保持 candidate，通过 →
completed（feature_level 只记录不拦截）；候选 sha 变化 → superseded（check_freshness
显式核对与重建时比对两条路径），重建后新版本；manifest 内容哈希确定性（独立
台账、不同入列顺序同哈希）；与 P2-02 事件库 to_event_payload/from_event 纯函数
往返。持久化接线归后续 CLI 任务，不在本文件。
"""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel.services.evidence_freshness import EvidenceStatus  # noqa: E402
from agents_kernel.services.integration import (EVENT_INTEGRATION_VERSION,  # noqa: E402
                                                IntegrationBoard, from_event,
                                                to_event_payload)
from agents_kernel.services.review_board import ReviewBoard  # noqa: E402
from agents_kernel.domain.tasks import TaskBoard  # noqa: E402
from agents_kernel.validation import CompanionError  # noqa: E402

T0 = "2026-09-20T10:00:00Z"
T1 = "2026-09-20T11:00:00Z"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
REVIEWER = {"role": "companion-checker", "attempt_id": "CHK#1"}


def make_stack(review_required=True, task_ids=("T1", "T2"), wire_guard=True):
    board = TaskBoard()
    board.add_feature("f1", "示例功能", review_required=review_required)
    for tid in task_ids:
        board.add_task(tid, "f1", "impl")
    review = ReviewBoard(board)
    if review_required and wire_guard:
        board.review_guard = review.require_approval
    integration = IntegrationBoard(board, review)
    return board, review, integration


def run_task(board, review, tid, sha, review_id="rev-1"):
    """完整任务流：ready → running → attempt → 固定版本提交审查 → 批准 → done。"""
    board.transition_task(tid, "ready")
    board.transition_task(tid, "running")
    attempt = board.start_attempt(tid, T0)
    review.submit_for_review(attempt, sha)
    review.record({"review_id": review_id, "subject_type": "attempt",
                   "subject_ref": "%s#%d" % (tid, attempt["attempt_no"]),
                   "subject_sha256": sha, "reviewer": REVIEWER,
                   "verdict": "approved", "blockers": [], "reviewed_at": T1})
    board.finish_attempt(tid, "succeeded")
    return attempt


def approve_candidate(board, review, integration, candidate_id="C1", shas=None):
    """每个任务走完整审查流（ready→running→attempt→批准→done）后入列候选并建版本。"""
    shas = shas or {"T1": SHA_A, "T2": SHA_A}
    for tid, sha in shas.items():
        run_task(board, review, tid, sha, review_id="rev-" + tid)
    candidate = integration.add_candidate(candidate_id, shas)
    return candidate, integration.build_integration_version([candidate_id])


class CandidateGateTest(unittest.TestCase):
    def test_policy_on_without_approval_refused(self):
        """policy 开启 + 批准被审后重提失效 → 拒绝入列，报出失效原因。"""
        board, review, integration = make_stack()
        run_task(board, review, "T1", SHA_A)
        run_task(board, review, "T2", SHA_A, review_id="rev-2")
        review.submit_for_review(board.attempts("T1")[-1], SHA_B)  # 审后重提 → 批准失效
        with self.assertRaises(CompanionError) as ctx:
            integration.add_candidate("C1", {"T1": SHA_B, "T2": SHA_A})
        self.assertIn("缺少有效独立审查批准", str(ctx.exception))
        self.assertIn("已变化", str(ctx.exception))
        with self.assertRaises(CompanionError):
            integration.build_integration_version(["C1"])

    def test_policy_on_without_review_board_refused(self):
        """集成台未接审查台（policy 开启）：任务即使有批准，入列也拒绝。"""
        board, review, _ = make_stack()
        run_task(board, review, "T1", SHA_A)
        with self.assertRaises(CompanionError) as ctx:
            IntegrationBoard(board).add_candidate("C1", {"T1": SHA_A})
        self.assertIn("未接入审查台", str(ctx.exception))

    def test_with_approval_accepted_and_reference_kept(self):
        board, review, integration = make_stack()
        candidate, manifest = approve_candidate(board, review, integration)
        self.assertEqual([t["task_id"] for t in candidate["tasks"]], ["T1", "T2"])
        entry = candidate["tasks"][0]
        self.assertEqual(entry["attempt_id"], "T1#1")
        self.assertEqual(entry["approval"]["status"], "approved")
        self.assertEqual(entry["approval"]["subject_ref"], "T1#1")
        self.assertEqual(manifest["status"], "candidate")
        self.assertEqual(manifest["candidates"], [candidate])

    def test_declared_sha_mismatch_with_approval_refused(self):
        """集成对象必须就是批准的固定版本：声明 sha 与批准不一致 → 拒绝。"""
        board, review, integration = make_stack()
        run_task(board, review, "T1", SHA_A)
        run_task(board, review, "T2", SHA_A, review_id="rev-2")
        with self.assertRaises(CompanionError) as ctx:
            integration.add_candidate("C1", {"T1": SHA_C, "T2": SHA_A})
        self.assertIn("与批准固定版本不一致", str(ctx.exception))

    def test_policy_off_without_approval_allowed(self):
        """policy 关闭（向后兼容）：无审查台、无批准也可入列，approval 引用为空。"""
        board, _, integration = make_stack(review_required=False, task_ids=("T1",))
        board.transition_task("T1", "ready")
        board.transition_task("T1", "running")
        board.start_attempt("T1", T0)
        board.finish_attempt("T1", "succeeded")
        candidate = integration.add_candidate("C1", {"T1": SHA_A})
        self.assertIsNone(candidate["tasks"][0]["approval"])

    def test_pending_attempt_and_unknown_task_refused(self):
        board, _, integration = make_stack(task_ids=("T1",))
        board.transition_task("T1", "ready")
        board.transition_task("T1", "running")
        board.start_attempt("T1", T0)  # outcome 仍 pending，无成功产出
        with self.assertRaises(CompanionError) as ctx:
            integration.add_candidate("C1", {"T1": SHA_A})
        self.assertIn("尚无成功产出", str(ctx.exception))
        with self.assertRaises(CompanionError):
            integration.add_candidate("C1", {"T9": SHA_A})
        with self.assertRaises(CompanionError):
            integration.add_candidate("C1", {})


class VersionIdempotencyTest(unittest.TestCase):
    def test_same_candidate_set_same_version_and_hash(self):
        board, review, integration = make_stack()
        approve_candidate(board, review, integration)
        v1 = integration.version(1)
        again = integration.build_integration_version(["C1"])
        self.assertEqual(again["integration_version"], 1)
        self.assertEqual(again["manifest_sha256"], v1["manifest_sha256"])
        self.assertEqual(again["candidates"], v1["candidates"])
        self.assertEqual(len(integration.versions()), 1)

    def test_build_order_and_candidate_order_irrelevant(self):
        board, review, integration = make_stack()
        run_task(board, review, "T1", SHA_A)
        run_task(board, review, "T2", SHA_A, review_id="rev-2")
        integration.add_candidate("C2", {"T2": SHA_A})   # 先入列后任务候选
        integration.add_candidate("C1", {"T1": SHA_A})
        first = integration.build_integration_version(["C1", "C2"])
        second = integration.build_integration_version(["C2", "C1"])
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
        self.assertEqual(first["integration_version"], second["integration_version"])

    def test_extra_task_yields_new_version(self):
        board, review, integration = make_stack(task_ids=("T1", "T2", "T3"))
        approve_candidate(board, review, integration)
        run_task(board, review, "T3", SHA_B, review_id="rev-T3")
        integration.add_candidate("C2", {"T3": SHA_B})
        manifest = integration.build_integration_version(["C1", "C2"])
        self.assertEqual(manifest["integration_version"], 2)
        self.assertNotEqual(manifest["manifest_sha256"], integration.version(1)["manifest_sha256"])
        self.assertEqual(len(integration.versions()), 2)

    def test_unknown_candidate_refused(self):
        _, _, integration = make_stack()
        with self.assertRaises(CompanionError) as ctx:
            integration.build_integration_version(["nope"])
        self.assertIn("候选不存在", str(ctx.exception))
        with self.assertRaises(CompanionError):
            integration.build_integration_version([])


class RegressionGateTest(unittest.TestCase):
    def test_feature_level_records_normalized_and_sorted(self):
        board, review, integration = make_stack()
        approve_candidate(board, review, integration)
        recorded = integration.record_feature_level(1, [
            EvidenceStatus("f2", "stale", ("m/a.py",), ("m",), True, False),
            {"feature_id": "f1", "status": "current"}])
        self.assertEqual([r["feature_id"] for r in recorded], ["f1", "f2"])
        self.assertEqual(recorded[1]["status"], "stale")
        self.assertEqual(recorded[1]["stale_files"], ["m/a.py"])  # NamedTuple 元组 → 列表
        self.assertEqual(recorded[0]["transitive"], False)
        self.assertEqual(integration.version(1)["regression"]["feature_level"], recorded)
        with self.assertRaises(CompanionError):
            integration.record_feature_level(1, [{"feature_id": "f1", "status": "fresh"}])

    def test_version_level_missing_or_failed_stays_candidate(self):
        board, review, integration = make_stack()
        approve_candidate(board, review, integration)
        with self.assertRaises(CompanionError) as ctx:
            integration.complete(1)
        self.assertIn("未记录", str(ctx.exception))
        self.assertEqual(integration.version(1)["status"], "candidate")
        integration.record_feature_level(1, [
            EvidenceStatus("f1", "stale", (), (), False, False)])  # 功能级 stale 不拦截
        integration.record_version_level(1, False, detail="联调 E_CHECK 分支失败")
        self.assertEqual(integration.version(1)["regression"]["version_level"]["status"], "failed")
        with self.assertRaises(CompanionError) as ctx:
            integration.complete(1)
        self.assertIn("未通过", str(ctx.exception))
        self.assertEqual(integration.version(1)["status"], "candidate")

    def test_version_level_passed_completes(self):
        board, review, integration = make_stack()
        approve_candidate(board, review, integration)
        integration.record_feature_level(1, [{"feature_id": "f1", "status": "current"}])
        integration.record_version_level(1, True, detail="全量回归通过")
        manifest = integration.complete(1)
        self.assertEqual(manifest["status"], "completed")
        with self.assertRaises(CompanionError):  # 完成后不得改写回归记录
            integration.record_version_level(1, False, detail="补跑")

    def test_version_level_requires_bool(self):
        board, review, integration = make_stack()
        approve_candidate(board, review, integration)
        with self.assertRaises(CompanionError):
            integration.record_version_level(1, "passed")


class SupersededTest(unittest.TestCase):
    def _completed_v1(self):
        board, review, integration = make_stack()
        _, manifest = approve_candidate(board, review, integration)
        integration.record_version_level(1, True, detail="通过")
        integration.complete(1)
        return board, review, integration, manifest

    def test_check_freshness_marks_changed_version_superseded(self):
        board, review, integration, _ = self._completed_v1()
        self.assertEqual(integration.check_freshness({"T1": SHA_B, "T2": SHA_A}), [1])
        superseded = integration.version(1)
        self.assertEqual(superseded["status"], "superseded")
        self.assertIn("T1", superseded["superseded_reason"])
        self.assertEqual(integration.check_freshness({"T1": SHA_B}), [], "幂等：已失效不再重复")
        with self.assertRaises(CompanionError):  # 失效版本不得再记录回归或完成
            integration.record_version_level(1, True, detail="旧证据补录")
        with self.assertRaises(CompanionError):
            integration.complete(1)

    def _resubmit(self, board, review, tid, sha, review_id):
        """同一最新 attempt 重提新固定版本并重审（subject 身份变化，done 是终态不动）。"""
        review.submit_for_review(board.attempts(tid)[-1], sha)
        review.record({"review_id": review_id, "subject_type": "attempt",
                       "subject_ref": "%s#%d" % (tid, board.attempts(tid)[-1]["attempt_no"]),
                       "subject_sha256": sha, "reviewer": REVIEWER,
                       "verdict": "approved", "blockers": [], "reviewed_at": T1})

    def test_rebuild_after_change_creates_new_version(self):
        board, review, integration, v1 = self._completed_v1()
        integration.check_freshness({"T1": SHA_B, "T2": SHA_A})
        self._resubmit(board, review, "T1", SHA_B, "rev-T1b")
        integration.add_candidate("C1", {"T1": SHA_B, "T2": SHA_A})
        v2 = integration.build_integration_version(["C1"])
        self.assertEqual(v2["integration_version"], 2)
        self.assertNotEqual(v2["manifest_sha256"], v1["manifest_sha256"])
        self.assertEqual(integration.version(1)["status"], "superseded")
        self.assertEqual(v2["status"], "candidate")  # 新版本须重新两级回归
        integration.record_version_level(2, True, detail="重建后全量回归通过")
        self.assertEqual(integration.complete(2)["status"], "completed")

    def test_rebuild_alone_supersedes_stale_versions(self):
        """不走显式 check_freshness：重建内容与旧版本候选 sha 不一致即失效旧版本。"""
        board, review, integration, _ = self._completed_v1()
        self._resubmit(board, review, "T2", SHA_C, "rev-T2c")
        integration.add_candidate("C1", {"T1": SHA_A, "T2": SHA_C})
        v2 = integration.build_integration_version(["C1"])
        self.assertEqual(integration.version(1)["status"], "superseded")
        self.assertEqual(v2["integration_version"], 2)


class ManifestHashTest(unittest.TestCase):
    def test_independent_boards_same_content_same_hash(self):
        """内容哈希确定性：独立台账、不同候选入列顺序，同候选集 → 同哈希。"""
        shas = {}
        for order in (("T1", "T2"), ("T2", "T1")):
            board, review, integration = make_stack(task_ids=order)
            for tid in order:
                run_task(board, review, tid, SHA_A,
                         review_id="rev-%s" % tid)
            integration.add_candidate("C1", {tid: SHA_A for tid in order})
            manifest = integration.build_integration_version(["C1"])
            shas[order] = manifest["manifest_sha256"]
        self.assertEqual(shas[("T1", "T2")], shas[("T2", "T1")])

    def test_hash_excludes_regression_results(self):
        board, review, integration = make_stack()
        approve_candidate(board, review, integration)
        before = integration.version(1)["manifest_sha256"]
        integration.record_version_level(1, True, detail="通过")
        self.assertEqual(integration.version(1)["manifest_sha256"], before)

    def test_version_copy_is_isolated(self):
        board, review, integration = make_stack()
        approve_candidate(board, review, integration)
        snapshot = integration.version(1)
        snapshot["candidates"][0]["tasks"][0]["subject_sha256"] = SHA_C
        snapshot["regression"]["feature_level"].append({"feature_id": "x"})
        self.assertEqual(integration.version(1)["candidates"][0]["tasks"][0]["subject_sha256"], SHA_A)


class EventBridgeTest(unittest.TestCase):
    def test_payload_round_trip(self):
        board, review, integration = make_stack()
        approve_candidate(board, review, integration)
        manifest = integration.version(1)
        payload = to_event_payload(manifest)
        self.assertEqual(payload, {"integration_version": 1,
                                   "manifest_sha256": manifest["manifest_sha256"],
                                   "status": "candidate"})
        self.assertEqual(EVENT_INTEGRATION_VERSION, "integration_version")
        fragment = from_event("integration-v1", payload)
        self.assertEqual(fragment, {"id": "integration-v1", "integration_version": 1,
                                    "manifest_sha256": manifest["manifest_sha256"],
                                    "status": "candidate"})
        with self.assertRaises(CompanionError):
            from_event("integration-v1", dict(payload, status="shipped"))
        with self.assertRaises(CompanionError):
            from_event("integration-v1", dict(payload, manifest_sha256="xyz"))
        with self.assertRaises(CompanionError):
            to_event_payload({"integration_version": 0, "status": "candidate",
                              "manifest_sha256": SHA_A})


if __name__ == "__main__":
    unittest.main()

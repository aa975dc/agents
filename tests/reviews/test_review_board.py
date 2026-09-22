# -*- coding: utf-8 -*-
"""P5-04：固定版本审查台——attempt 审查记录、独立性与失效、done 门（C06 前半/TK06/H03 静态半）。

覆盖：正常批准流（有有效批准后 impl 任务才可 done）；自审拒绝（实现者 attempt
不得担任审查者）；审查期间/审后 subject 变化的拒绝与批准失效；返工新 attempt
必须重审；changes_requested 逐条 blockers 传递；feature review_required policy
缺省关闭时完全向后兼容；handoff 类 subject 委托 HandoffRegistry——登记→引用→
换版→失效。真实宿主角色的审查 E2E 不在本文件（归 P5-06）。
"""
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel import digest  # noqa: E402
from agents_kernel.contracts import schemas  # noqa: E402
from agents_kernel.domain.handoff import HandoffRegistry  # noqa: E402
from agents_kernel.domain.review_record import ReviewRecord, attempt_id  # noqa: E402
from agents_kernel.domain.tasks import TaskBoard  # noqa: E402
from agents_kernel.services.review_board import ReviewBoard  # noqa: E402
from agents_kernel.validation import CompanionError  # noqa: E402

T0 = "2026-09-20T10:00:00Z"
T1 = "2026-09-20T11:00:00Z"
SHA_A = "a" * 64
SHA_B = "b" * 64
IMPL = "IMPL"
REVIEWER = {"role": "companion-checker", "attempt_id": "CHK#1"}
BRIEF_ID = "board-status-v1"
BRIEF_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "design" / "board-status-example.json"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def make_board(review_required=True):
    board = TaskBoard()
    board.add_feature("f1", "示例功能", review_required=review_required)
    board.add_task(IMPL, "f1", "impl")
    return board


def wire(board, registry=None):
    review_board = ReviewBoard(board, registry)
    board.review_guard = review_board.require_approval
    return review_board


def start_impl(board, tid=IMPL):
    board.transition_task(tid, "ready")
    board.transition_task(tid, "running")
    return board.start_attempt(tid, T0)


def submit_and_approve(review_board, attempt, sha=SHA_A, review_id="rev-1",
                       reviewer=REVIEWER, subject_type="attempt", subject_ref=None):
    """标准两步：实现者固定版本提交 → 独立审查者批准。"""
    review_board.submit_for_review(attempt, sha, subject_type=subject_type,
                                   subject_ref=subject_ref)
    return review_board.record({
        "review_id": review_id, "subject_type": subject_type,
        "subject_ref": subject_ref or attempt_id(attempt["task_id"], attempt["attempt_no"]),
        "subject_sha256": sha, "reviewer": reviewer,
        "verdict": "approved", "blockers": [], "reviewed_at": T1})


class ApprovalFlowTest(unittest.TestCase):
    def test_approved_attempt_allows_done(self):
        board = make_board()
        review_board = wire(board)
        attempt = start_impl(board)
        record = submit_and_approve(review_board, attempt)
        self.assertEqual(record["subject_ref"], "IMPL#1")
        self.assertEqual(record["reviewer"], REVIEWER)
        verdict = review_board.current_approval(IMPL)
        self.assertEqual(verdict["status"], "approved")
        self.assertEqual(verdict["subject_sha256"], SHA_A)
        board.finish_attempt(IMPL, "succeeded")
        self.assertEqual(board.task(IMPL)["status"], "done")

    def test_self_review_refused(self):
        board = make_board()
        review_board = wire(board)
        attempt = start_impl(board)
        review_board.submit_for_review(attempt, SHA_A)
        with self.assertRaises(CompanionError) as ctx:
            review_board.record({"review_id": "rev-self", "subject_type": "attempt",
                                 "subject_ref": "IMPL#1", "subject_sha256": SHA_A,
                                 "reviewer": {"role": "companion-developer", "attempt_id": "IMPL#1"},
                                 "verdict": "approved", "blockers": [], "reviewed_at": T1})
        self.assertIn("自审", str(ctx.exception))
        verdict = review_board.current_approval(IMPL)
        self.assertEqual(verdict["status"], "none")  # 被拒记录不落账
        with self.assertRaises(CompanionError):
            board.finish_attempt(IMPL, "succeeded")

    def test_sha_change_during_review_refused(self):
        board = make_board()
        review_board = wire(board)
        attempt = start_impl(board)
        review_board.submit_for_review(attempt, SHA_A)
        with self.assertRaises(CompanionError) as ctx:
            review_board.record({"review_id": "rev-1", "subject_type": "attempt",
                                 "subject_ref": "IMPL#1", "subject_sha256": SHA_B,
                                 "reviewer": REVIEWER, "verdict": "approved",
                                 "blockers": [], "reviewed_at": T1})
        self.assertIn("审查期间 subject 已变化", str(ctx.exception))
        self.assertEqual(review_board.current_approval(IMPL)["status"], "none")

    def test_record_without_request_refused(self):
        review_board = ReviewBoard(make_board())
        with self.assertRaises(CompanionError) as ctx:
            review_board.record({"review_id": "rev-1", "subject_type": "attempt",
                                 "subject_ref": "IMPL#1", "subject_sha256": SHA_A,
                                 "reviewer": REVIEWER, "verdict": "approved",
                                 "blockers": [], "reviewed_at": T1})
        self.assertIn("未提交审查请求", str(ctx.exception))

    def test_stale_attempt_submission_refused(self):
        board = make_board()
        review_board = wire(board)
        attempt1 = start_impl(board)
        review_board.submit_for_review(attempt1, SHA_A)
        board.finish_attempt(IMPL, "failed", failure_reason="返工")
        board.transition_task(IMPL, "ready")  # failed → ready：重试重开新 attempt
        board.transition_task(IMPL, "running")
        attempt2 = board.start_attempt(IMPL, T1)
        with self.assertRaises(CompanionError) as ctx:
            review_board.submit_for_review(attempt1, SHA_A)
        self.assertIn("最新 attempt", str(ctx.exception))
        submit_and_approve(review_board, attempt2, review_id="rev-2")
        self.assertEqual(review_board.current_approval(IMPL)["status"], "approved")

    def test_record_shape_and_schema(self):
        record = {"review_id": "rev-1", "subject_type": "attempt", "subject_ref": "IMPL#1",
                  "subject_sha256": SHA_A, "reviewer": REVIEWER, "verdict": "approved",
                  "blockers": [], "reviewed_at": T1}
        self.assertEqual(schemas.validate_review_record(record), [])
        bad = dict(record, verdict="rejected")
        self.assertIn("verdict", schemas.validate_review_record(bad)[0]["path"])
        bad = dict(record, blockers=["批准不应带阻塞项"])
        self.assertEqual(schemas.validate_review_record(bad)[0]["path"], "blockers")
        bad = dict(record, reviewed_at=None)
        self.assertIn("reviewed_at", schemas.validate_review_record(bad)[0]["path"])
        bad = dict(record, subject_type="directory")
        self.assertIn("subject_type", schemas.validate_review_record(bad)[0]["path"])
        coerced = ReviewRecord.coerce(record)
        copy = coerced.to_dict()
        copy["blockers"].append("污染")
        self.assertEqual(coerced.to_dict()["blockers"], [], "to_dict 必须返回副本")
        with self.assertRaises(CompanionError):
            ReviewRecord.coerce("approved")
        self.assertEqual(attempt_id("IMPL", 2), "IMPL#2")
        with self.assertRaises(CompanionError):
            attempt_id("IMPL", True)

    def test_duplicate_review_id_refused(self):
        board = make_board()
        review_board = wire(board)
        attempt = start_impl(board)
        submit_and_approve(review_board, attempt, review_id="rev-1")
        with self.assertRaises(CompanionError) as ctx:
            submit_and_approve(review_board, attempt, review_id="rev-1")
        self.assertIn("已存在", str(ctx.exception))


class InvalidationTest(unittest.TestCase):
    def test_revision_after_approval_invalidates_and_done_refused(self):
        """审后重提固定版本：原批准失效（invalid_reason），done 被拒且不留半套状态。"""
        board = make_board()
        review_board = wire(board)
        attempt = start_impl(board)
        submit_and_approve(review_board, attempt, sha=SHA_A)
        review_board.submit_for_review(attempt, SHA_B)  # 实现者审后修改并重提
        verdict = review_board.current_approval(IMPL)
        self.assertEqual(verdict["status"], "invalid")
        self.assertIn("已变化", verdict["invalid_reason"])
        with self.assertRaises(CompanionError) as ctx:
            board.finish_attempt(IMPL, "succeeded")
        self.assertIn("缺少有效独立审查批准", str(ctx.exception))
        self.assertEqual(board.task(IMPL)["status"], "running")
        self.assertEqual(board.attempts(IMPL)[-1]["outcome"], "pending")
        submit_and_approve(review_board, attempt, sha=SHA_B, review_id="rev-2")
        board.finish_attempt(IMPL, "succeeded")
        self.assertEqual(board.task(IMPL)["status"], "done")

    def test_rework_requires_fresh_approval_for_new_attempt(self):
        board = make_board()
        review_board = wire(board)
        attempt1 = start_impl(board)
        submit_and_approve(review_board, attempt1, review_id="rev-1")
        board.finish_attempt(IMPL, "failed", failure_reason="返工")
        board.transition_task(IMPL, "ready")
        board.transition_task(IMPL, "running")
        board.start_attempt(IMPL, T1)
        verdict = review_board.current_approval(IMPL)
        self.assertEqual(verdict["status"], "none")
        self.assertIn("旧 attempt", verdict["invalid_reason"])
        with self.assertRaises(CompanionError):
            board.finish_attempt(IMPL, "succeeded")
        submit_and_approve(review_board, board.attempts(IMPL)[-1], review_id="rev-2")
        board.finish_attempt(IMPL, "succeeded")
        self.assertEqual(board.task(IMPL)["status"], "done")


class BlockersTest(unittest.TestCase):
    def test_changes_requested_blocks_done_with_blockers(self):
        board = make_board()
        review_board = wire(board)
        attempt = start_impl(board)
        review_board.submit_for_review(attempt, SHA_A)
        blockers = ["错误路径未覆盖：E_CHECK 分支无测试", "越界：改动了 allowed_paths 之外文件"]
        review_board.record({"review_id": "rev-1", "subject_type": "attempt",
                             "subject_ref": "IMPL#1", "subject_sha256": SHA_A,
                             "reviewer": REVIEWER, "verdict": "changes_requested",
                             "blockers": blockers, "reviewed_at": T1})
        verdict = review_board.current_approval(IMPL)
        self.assertEqual(verdict["status"], "changes_requested")
        self.assertEqual(verdict["blockers"], blockers)  # 逐条传递，拒绝空泛否决
        with self.assertRaises(CompanionError) as ctx:
            board.finish_attempt(IMPL, "succeeded")
        self.assertIn("错误路径未覆盖", str(ctx.exception))


class PolicyCompatTest(unittest.TestCase):
    def test_policy_off_keeps_old_behavior(self):
        """policy 缺省关闭：不接审查台、不提交审查，impl 任务照常 done（向后兼容）。"""
        board = make_board(review_required=False)
        self.assertIsNone(board.review_guard)
        start_impl(board)
        board.finish_attempt(IMPL, "succeeded")
        self.assertEqual(board.task(IMPL)["status"], "done")

    def test_policy_on_without_board_refuses_done(self):
        board = make_board()  # review_required=True 但未接线
        start_impl(board)
        with self.assertRaises(CompanionError) as ctx:
            board.finish_attempt(IMPL, "succeeded")
        self.assertIn("未接入审查台", str(ctx.exception))
        self.assertEqual(board.task(IMPL)["status"], "running")

    def test_gate_covers_only_impl_and_review(self):
        board = make_board()
        wire(board)
        board.add_task("DESIGN", "f1", "design")
        board.add_task("CHK", "f1", "review")
        board.transition_task("DESIGN", "ready")
        board.transition_task("DESIGN", "running")
        board.start_attempt("DESIGN", T0)
        board.finish_attempt("DESIGN", "succeeded")  # design 不经过审查门
        self.assertEqual(board.task("DESIGN")["status"], "done")
        board.transition_task("CHK", "ready")
        board.transition_task("CHK", "running")
        board.start_attempt("CHK", T0)
        with self.assertRaises(CompanionError):
            board.finish_attempt("CHK", "succeeded")  # review 任务同样要求批准

    def test_direct_done_transition_also_gated(self):
        board = make_board()
        wire(board)
        start_impl(board)
        with self.assertRaises(CompanionError):
            board.transition_task(IMPL, "done")  # 与 finish_attempt 同一道门


class HandoffIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.registry = HandoffRegistry()
        self.brief = load(BRIEF_FIXTURE)
        self.brief_sha = digest.digest(self.brief)
        self.registry.register("design_brief", BRIEF_ID, self.brief, 1)
        self.board = make_board()
        self.review_board = wire(self.board, self.registry)
        self.attempt = start_impl(self.board)

    def revised_sha(self):
        revised = load(BRIEF_FIXTURE)
        revised["non_goals"].append("不做移动端专属布局")
        return revised, digest.digest(revised)

    def test_registration_reference_change_invalidation(self):
        """登记→以固定版本提交审查→批准→登记表换版→批准失效→重登记重审→通过。"""
        submit_and_approve(self.review_board, self.attempt, sha=self.brief_sha,
                           subject_type="handoff", subject_ref=BRIEF_ID)
        self.assertEqual(self.review_board.current_approval(IMPL)["status"], "approved")
        revised, new_sha = self.revised_sha()
        self.registry.register("design_brief", BRIEF_ID, revised, 2)
        verdict = self.review_board.current_approval(IMPL)
        self.assertEqual(verdict["status"], "invalid")
        self.assertIn("已变更", verdict["invalid_reason"])  # 失效原因委托登记表
        with self.assertRaises(CompanionError):
            self.board.finish_attempt(IMPL, "succeeded")
        with self.assertRaises(CompanionError):
            self.review_board.submit_for_review(self.attempt, self.brief_sha,
                                                subject_type="handoff", subject_ref=BRIEF_ID)
        submit_and_approve(self.review_board, self.attempt, sha=new_sha, review_id="rev-2",
                           subject_type="handoff", subject_ref=BRIEF_ID)
        self.board.finish_attempt(IMPL, "succeeded")
        self.assertEqual(self.board.task(IMPL)["status"], "done")

    def test_registry_bump_during_review_refuses_record(self):
        self.review_board.submit_for_review(self.attempt, self.brief_sha,
                                            subject_type="handoff", subject_ref=BRIEF_ID)
        revised, _ = self.revised_sha()
        self.registry.register("design_brief", BRIEF_ID, revised, 2)
        with self.assertRaises(CompanionError) as ctx:
            self.review_board.record({"review_id": "rev-1", "subject_type": "handoff",
                                      "subject_ref": BRIEF_ID, "subject_sha256": self.brief_sha,
                                      "reviewer": REVIEWER, "verdict": "approved",
                                      "blockers": [], "reviewed_at": T1})
        self.assertIn("已变更", str(ctx.exception))

    def test_handoff_submit_requires_registered_version(self):
        with self.assertRaises(CompanionError):
            self.review_board.submit_for_review(self.attempt, SHA_A,
                                                subject_type="handoff", subject_ref=BRIEF_ID)
        with self.assertRaises(CompanionError) as ctx:
            self.review_board.submit_for_review(self.attempt, SHA_A,
                                                subject_type="handoff", subject_ref="unregistered")
        self.assertIn("未登记", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""IX05 定点核对：同 mtime+size 内容变化时，验收/集成/审查是否正确失效（与索引缓存分离）。

已知限制（IX05 原始发现）：packages/agents_kernel/indexing/scanner.py 在
size+mtime_ns 与上一代一致时沿用旧 sha256（索引缓存口径）。本文件回答的不是
"索引缓存是否刷新"，而是：同 mtime、同 size 但内容变化时，最终审查（review）、
已接受成果（acceptance）、集成门（integration gate）是否正确失效——即最终验收
是否依赖索引缓存。读码结论（本测试行为化验证）：

- acceptance（core.Project）：packet/check/accept 写路径的 fingerprint 来自
  snapshot() → _scan_snapshot()，每个文件每次 sha256_file 全量现算，无 mtime
  短路；RequestCache 只在 status 只读作用域开启，check 前后对比不受缓存影响。
  → 内容变化必然改变 fingerprint，已接受成果按既有语义回落 awaiting_review。
- integration（IntegrationBoard）：候选/版本的 subject_sha256 由登记时的内容
  哈希背书（API 只收 sha，不收路径/mtime）；check_freshness 与重建比对均为
  sha 对 sha → 用当前内容哈希核对即判 superseded。
- review（ReviewBoard）：请求与结论绑定固定 subject_sha256；对当前内容的结论
  与旧 pin 不一致直接拒绝；审后重提新固定版本使既有批准 invalid（带 reason）。
- indexing（scanner）：同一变化确实不检测（size+mtime 复用旧 sha）——这是
  **缓存限制 ≠ 验收缺陷**：索引只是增量分析输入，验收正确性不依赖它；
  验收各环在代码上均不读索引库。

统一构造：等长不同内容覆写 + os.utime 恢复原 atime/mtime_ns——size 与
mtime_ns（索引复用判据）完全一致，仅内容哈希不同。
"""
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages"))
sys.path.insert(0, str(REPO_ROOT / "dev-companion" / "scripts"))

from agents_kernel import digest  # noqa: E402
from agents_kernel.domain.tasks import TaskBoard  # noqa: E402
from agents_kernel.indexing import content_hash, scanner  # noqa: E402
from agents_kernel.services.integration import IntegrationBoard  # noqa: E402
from agents_kernel.services.review_board import ReviewBoard  # noqa: E402
from agents_kernel.storage import db  # noqa: E402
from agents_kernel.validation import CompanionError  # noqa: E402

from core import Project  # noqa: E402  （经 kernel_bootstrap 解析到 _kernel_vendor，与 packages/ 同步）
from journey import Journey  # noqa: E402

T0 = "2026-09-20T10:00:00Z"
T1 = "2026-09-20T11:00:00Z"
REVIEWER = {"role": "companion-checker", "attempt_id": "CHK#1"}

# 等长（36 字节）且均为合法 Python、均通过 total([20,30])==50 的两版实现。
LEDGER_V1 = b"def total(xs):\n    return sum(xs)  \n"
LEDGER_V2 = b"def total(xs):\n    return sum(xs)+0\n"
# 等长（25 字节）的集成候选产物两版。
ARTIFACT_V1 = b"integration artifact v1.0\n"
ARTIFACT_V2 = b"integration artifact v1.1\n"


class SameStatMutationMixin(unittest.TestCase):
    """统一构造：等长不同内容 + 恢复 atime/mtime_ns，确证 stat 面完全一致。"""

    def rewrite_same_stat(self, path, payload, previous_sha):
        before = path.stat()
        self.assertEqual(before.st_size, len(payload), "测试构造要求等长内容")
        path.write_bytes(payload)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        self.assertEqual((after.st_size, after.st_mtime_ns),
                         (before.st_size, before.st_mtime_ns))
        self.assertNotEqual(digest.sha256_file(str(path))[0], previous_sha)


def plan(project, scope):
    """补齐 journey 六阶段（与 test_companion_lifecycle 同款），使 packet/check/accept 可用。"""
    (project.root / "prototype.md").write_text("命令行原型：输入 [20, 30]，输出 50；负数返回错误。\n")
    journey = Journey(project)
    product = copy.deepcopy(scope)
    for feature in product["features"]:
        feature.pop("allowed_paths")
        feature.pop("check_commands")
    ids = [f["id"] for f in product["features"]]
    stages = [
        ("concept", {"details": {"audience": "自己", "problem": "需要汇总", "scenario": "录入开支", "outcome": "看到合计"}}),
        ("requirements", {"details": {"constraints": "本地使用", "priorities": "先实现合计"}}),
        ("product", {"details": {"positioning": "本地记账"}, "scope": product}),
        ("flow", {"details": {"main_path": "输入20和30得到50", "alternatives": "拒绝负数", "data_changes": "本次不持久化"}, "feature_ids": ids}),
        ("prototype", {"details": {"screens": "命令行输入", "states": "成功与无效输入", "walkthrough": "命令行样例20+30=50"}, "feature_ids": ids, "artifacts": ["prototype.md"]}),
        ("technical", {"details": {"architecture": "Python本地模块", "data_model": "金额列表", "release_target": "本地演示"}, "scope": scope,
                       "interfaces": [{"feature_id": f["id"], "kind": "local", "contract": "金额列表返回总和",
                                       "check_commands": f["check_commands"]} for f in scope["features"]]})]
    for stage, raw in stages:
        raw.update(summary=stage + "成果", open_questions=[], decisions=[])
        raw.setdefault("artifacts", [])
        revision = (journey.status() or {}).get("revision", 0)
        journey.save(stage, raw, revision, complete=True, user_confirmed=True)
    return journey


class AcceptanceFreshnessTests(SameStatMutationMixin):
    """(a) 已接受成果：同 mtime+size 内容变化 → fingerprint 变化 → 资格失效。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = Project(self.root)
        self.scope = {"title": "记账", "goal": "汇总", "audience": "自己", "scenario": "录入开支",
                      "out_of_scope": [], "assumptions": [], "features": [{
                          "id": "sum", "title": "合计", "acceptance_criteria": ["20+30=50"],
                          "allowed_paths": ["ledger.py"], "requires_user_acceptance": False,
                          "check_commands": [[sys.executable, "-c", "from ledger import total; assert total([20,30]) == 50"]]}]}

    def implement(self):
        """start（init/confirm/packet）→ 实现 → receipt，返回回报 payload 供重放断言。"""
        plan(self.project, self.scope)
        self.project.init(self.scope)
        self.project.confirm(1)
        packet = self.project.packet("sum")
        (self.root / "ledger.py").write_bytes(LEDGER_V1)
        receipt = {"feature_id": "sum", "scope_version": 1, "run_id": packet["run_id"],
                   "status": "implemented", "summary": "实现合计", "changed_files": ["ledger.py"], "evidence_files": []}
        self.project.receipt(receipt)
        return receipt

    def test_same_stat_content_change_invalidates_accepted_result(self):
        receipt = self.implement()
        self.project.check("sum")
        self.project.check("sum", kind="integration")
        self.project.accept("sum", "检查通过")
        accepted = self.project.status(include_release=False)
        self.assertEqual(accepted["overall_percent"], 100)
        fingerprint_at_accept = accepted["source_fingerprint"]
        digest_at_accept = self.project.snapshot()["files"]["ledger.py"]

        self.rewrite_same_stat(self.root / "ledger.py", LEDGER_V2,
                               digest.sha256_bytes(LEDGER_V1))

        # fingerprint 是内容口径：同 mtime+size 内容变化必然改变。
        self.assertNotEqual(self.project.snapshot()["files"]["ledger.py"], digest_at_accept)
        view = self.project.status(include_release=False)
        self.assertNotEqual(view["source_fingerprint"], fingerprint_at_accept)
        feature = view["features"][0]
        self.assertEqual(feature["status"], "awaiting_review", "已接受成果资格失效")
        self.assertTrue(feature["evidence_stale"])
        self.assertEqual(view["counts"]["accepted"], 0)
        self.assertEqual(view["overall_percent"], 0)
        # accept 拒绝沿用旧检查证据；旧 receipt 重放被拒（status 已不是 running）。
        with self.assertRaises(CompanionError):
            self.project.accept("sum", "继续沿用旧检查")
        with self.assertRaises(CompanionError):
            self.project.receipt(receipt)
        # 恢复路径：重新 check（内容口径现算）→ 重新验收，资格可重建。
        self.project.check("sum")
        self.project.check("sum", kind="integration")
        self.project.accept("sum", "内容变化后重新检查通过")
        self.assertEqual(self.project.status(include_release=False)["overall_percent"], 100)

    def test_receipt_replay_rejected(self):
        receipt = self.implement()
        self.project.check("sum")
        self.project.check("sum", kind="integration")
        self.project.accept("sum", "检查通过")
        self.rewrite_same_stat(self.root / "ledger.py", LEDGER_V2, digest.sha256_bytes(LEDGER_V1))
        with self.assertRaises(CompanionError) as ctx:
            self.project.receipt(receipt)
        self.assertIn("执行回报重复、过期或不属于当前任务", str(ctx.exception))


class IntegrationGateFreshnessTests(SameStatMutationMixin):
    """(b) 集成门：候选/版本 subject sha 为登记时内容哈希 → 同 stat 内容变化判 superseded。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.artifact = Path(self.temp.name) / "artifact.txt"
        self.artifact.write_bytes(ARTIFACT_V1)
        self.sha_v1 = digest.sha256_file(str(self.artifact))[0]
        self.board = TaskBoard()
        self.board.add_feature("f1", "示例功能", review_required=True)
        self.board.add_task("T1", "f1", "impl")
        self.review = ReviewBoard(self.board)
        self.board.review_guard = self.review.require_approval
        self.integration = IntegrationBoard(self.board, self.review)

    def approve_and_complete_v1(self):
        attempt = self.submit_current(self.sha_v1, "rev-1")
        self.board.finish_attempt("T1", "succeeded")
        self.integration.add_candidate("C1", {"T1": self.sha_v1})
        version = self.integration.build_integration_version(["C1"])
        self.integration.record_version_level(1, True, detail="回归通过")
        self.assertEqual(self.integration.complete(1)["status"], "completed")
        return attempt, version

    def submit_current(self, sha, review_id):
        self.board.transition_task("T1", "ready")
        self.board.transition_task("T1", "running")
        attempt = self.board.start_attempt("T1", T0)
        self.review.submit_for_review(attempt, sha)
        self.review.record({"review_id": review_id, "subject_type": "attempt",
                            "subject_ref": "T1#%d" % attempt["attempt_no"],
                            "subject_sha256": sha, "reviewer": REVIEWER,
                            "verdict": "approved", "blockers": [], "reviewed_at": T1})
        return attempt

    def test_same_stat_content_change_supersedes_version_and_rebuild_succeeds(self):
        self.approve_and_complete_v1()
        self.rewrite_same_stat(self.artifact, ARTIFACT_V2, self.sha_v1)
        sha_v2 = digest.sha256_file(str(self.artifact))[0]  # 当前内容哈希（内容口径核对）
        self.assertEqual(self.integration.check_freshness({"T1": sha_v2}), [1])
        superseded = self.integration.version(1)
        self.assertEqual(superseded["status"], "superseded")
        self.assertIn("T1", superseded["superseded_reason"])
        self.assertEqual(self.integration.check_freshness({"T1": sha_v2}), [], "幂等")
        with self.assertRaises(CompanionError):
            self.integration.record_version_level(1, True)
        with self.assertRaises(CompanionError):
            self.integration.complete(1)
        # 重建：重提当前内容固定版本并重审 → 新候选内容哈希 → 新版本可完成。
        self.review.submit_for_review(self.board.attempts("T1")[-1], sha_v2)
        self.review.record({"review_id": "rev-2", "subject_type": "attempt",
                            "subject_ref": "T1#%d" % self.board.attempts("T1")[-1]["attempt_no"],
                            "subject_sha256": sha_v2, "reviewer": REVIEWER,
                            "verdict": "approved", "blockers": [], "reviewed_at": T1})
        self.integration.add_candidate("C1", {"T1": sha_v2})
        rebuilt = self.integration.build_integration_version(["C1"])
        self.assertEqual(rebuilt["integration_version"], 2)
        self.assertNotEqual(rebuilt["manifest_sha256"], superseded["manifest_sha256"])
        self.integration.record_version_level(2, True, detail="重建后回归通过")
        self.assertEqual(self.integration.complete(2)["status"], "completed")


class ReviewFreshnessTests(SameStatMutationMixin):
    """(c) 审查：结论绑定固定 sha → 同 stat 内容变化 → 拒绝错配结论/既有批准 invalid。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.artifact = Path(self.temp.name) / "artifact.txt"
        self.artifact.write_bytes(ARTIFACT_V1)
        self.sha_v1 = digest.sha256_file(str(self.artifact))[0]
        self.board = TaskBoard()
        self.board.add_feature("f1", "示例功能", review_required=True)
        self.board.add_task("T1", "f1", "impl")
        self.review = ReviewBoard(self.board)
        self.board.review_guard = self.review.require_approval

    def test_same_stat_content_change_invalidates_approval_with_reason(self):
        self.board.transition_task("T1", "ready")
        self.board.transition_task("T1", "running")
        attempt = self.board.start_attempt("T1", T0)
        self.review.submit_for_review(attempt, self.sha_v1)
        self.review.record({"review_id": "rev-1", "subject_type": "attempt",
                            "subject_ref": "T1#%d" % attempt["attempt_no"],
                            "subject_sha256": self.sha_v1, "reviewer": REVIEWER,
                            "verdict": "approved", "blockers": [], "reviewed_at": T1})
        self.assertEqual(self.review.current_approval("T1")["status"], "approved")

        self.rewrite_same_stat(self.artifact, ARTIFACT_V2, self.sha_v1)
        sha_v2 = digest.sha256_file(str(self.artifact))[0]
        # 对当前内容的审查结论与请求 pin（旧内容 sha）不一致 → 拒绝落账。
        with self.assertRaises(CompanionError) as ctx:
            self.review.record({"review_id": "rev-stale", "subject_type": "attempt",
                                "subject_ref": "T1#%d" % attempt["attempt_no"],
                                "subject_sha256": sha_v2, "reviewer": REVIEWER,
                                "verdict": "approved", "blockers": [], "reviewed_at": T1})
        self.assertIn("审查期间 subject 已变化", str(ctx.exception))
        # 实现者重提当前内容固定版本 → 既有批准立即失效并带 invalid_reason。
        self.review.submit_for_review(self.board.attempts("T1")[-1], sha_v2)
        verdict = self.review.current_approval("T1")
        self.assertEqual(verdict["status"], "invalid")
        self.assertIn("已变化", verdict["invalid_reason"])
        self.assertIn(sha_v2, verdict["invalid_reason"],
                      "失效原因锚定当前固定版本（旧批准不适用）")
        with self.assertRaises(CompanionError):
            self.board.transition_task("T1", "done")  # done 门不再放行
        # 恢复：对当前内容重新审查批准。
        self.review.record({"review_id": "rev-2", "subject_type": "attempt",
                            "subject_ref": "T1#%d" % attempt["attempt_no"],
                            "subject_sha256": sha_v2, "reviewer": REVIEWER,
                            "verdict": "approved", "blockers": [], "reviewed_at": T1})
        self.assertEqual(self.review.current_approval("T1")["status"], "approved")
        self.assertEqual(self.review.current_approval("T1")["subject_sha256"], sha_v2)


class IndexCacheContrastTests(SameStatMutationMixin):
    """(d) 对照面：scanner 对同一变化不检测——缓存限制 ≠ 验收缺陷（IX05 原始发现）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name) / "tree"
        self.tree.mkdir()
        self.target = self.tree / "artifact.txt"
        self.target.write_bytes(ARTIFACT_V1)
        self.store = db.Store(Path(self.temp.name) / "facts.sqlite")
        self.store.open()
        self.addCleanup(self.store.close)

    def scan(self):
        writer = db.acquire_writer(self.store)
        try:
            return scanner.IndexScanner(self.store).scan(
                self.tree, writer, hash_hook=content_hash.ContentHasher())
        finally:
            writer.close()

    def test_scanner_reuses_old_sha_where_acceptance_detects_change(self):
        sha_v1 = digest.sha256_file(str(self.target))[0]
        first = self.scan()
        self.assertEqual(first.hash_computed, 1)
        row = lambda: self.store.query_one("SELECT sha256 FROM files WHERE path = 'artifact.txt'")["sha256"]
        self.assertEqual(row(), sha_v1)

        # 与 (a)/(b)/(c) 同一构造：等长不同内容 + 恢复 mtime_ns——验收环全部失效，
        # 索引环仍沿用旧 sha（size+mtime_ns 复用判据，IX05 已知限制）。
        self.rewrite_same_stat(self.target, ARTIFACT_V2, sha_v1)
        second = self.scan()
        self.assertEqual((second.hash_reused, second.hash_computed), (1, 0))
        self.assertEqual(row(), sha_v1, "索引沿用旧 sha：缓存不刷新（限制，非验收缺陷）")
        self.assertNotEqual(digest.sha256_file(str(self.target))[0], sha_v1)
        # 分离结论：同一变化下验收口径（内容哈希）与索引口径行为不同——
        # 验收/集成/审查各环均不读索引库，最终验收正确性不依赖索引缓存。


if __name__ == "__main__":
    unittest.main()

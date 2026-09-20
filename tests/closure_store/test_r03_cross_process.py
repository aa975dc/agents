# -*- coding: utf-8 -*-
"""R03：集成/审查/回归/superseded 的跨进程持久化（tests/closure_store）。

每个步骤都是独立真实进程（subprocess 调 _r03_worker.py，禁止同对象内测恢复）：
- 进程 A 写（候选→版本→审批→保存），进程 B 只读持久记录还原 candidate hash、
  批准引用、回归状态，与 A 写入一致；
- 中断边界：A 在"审批后未保存"os._exit，B 看到上一个一致状态（候选在、审批不在），
  新进程重做审批后恢复；
- superseded：A 建版本 → B 改 subject sha → C 校验出失效 → D 重建新版本成功；
- 围栏：旧 epoch 台账更新被拒；迟到回报（旧 attempt sha）不能覆盖新候选；
  重复回报幂等（同版本号同哈希，检查点零变化）。
"""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).resolve().parent / "_r03_worker.py"

SHA_A = "a" * 64
SHA_B = "b" * 64


class CrossProcessTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = (Path(self.temp.name) / "run").resolve()

    def worker(self, *argv):
        """独立进程执行一个 worker 步骤，返回解析后的 JSON。"""
        result = subprocess.run([sys.executable, str(WORKER), *argv],
                                capture_output=True, text=True, cwd=str(REPO_ROOT))
        self.assertEqual(result.returncode, 0,
                         "worker 失败(%d)：%s%s" % (result.returncode, result.stdout, result.stderr))
        return json.loads(result.stdout)

    def hard_worker(self, *argv):
        """预期以 os._exit(9) 终止的进程。"""
        return subprocess.run([sys.executable, str(WORKER), *argv],
                              capture_output=True, text=True, cwd=str(REPO_ROOT))


class RestoreAcrossProcessesTest(CrossProcessTestBase):
    def test_candidate_approval_regression_survive_process_boundary(self):
        """2 个进程：A 建候选→版本→审批→保存退出；B 只读持久记录还原并逐项一致。"""
        first = self.worker("full", "--run-dir", str(self.run_dir), "--sha", SHA_A,
                            "--save-candidate", "--regression", "--approve", "--save-final")
        self.assertEqual(first["version"]["integration_version"], 1)
        self.assertEqual(first["version"]["status"], "candidate")
        second = self.worker("inspect", "--run-dir", str(self.run_dir))
        # candidate hash 与批准引用一致
        self.assertEqual(second["candidate"]["tasks"][0]["subject_sha256"], SHA_A)
        self.assertEqual(second["candidate"]["tasks"][0]["attempt_id"], "T1#1")
        self.assertEqual(second["approvals"][0]["verdict"], "approved")
        self.assertEqual(second["approvals"][0]["subject_sha256"], SHA_A)
        self.assertEqual(second["approvals"][0]["subject_ref"], "T1#1")
        # 还原探测：审查台语义面读回有效批准，且与候选固定 sha 一致
        self.assertEqual(second["current_approval"]["status"], "approved")
        self.assertEqual(second["current_approval"]["subject_sha256"], SHA_A)
        # 两级回归状态随检查点还原
        version = second["versions"][0]
        self.assertEqual(version["regression"]["version_level"],
                         {"status": "passed", "detail": "跨进程全量回归通过"})
        self.assertEqual(version["regression"]["feature_level"],
                         [{"feature_id": "f1", "status": "current", "stale_files": [],
                           "hit_modules": [], "transitive": False, "conservative": False}])
        # 台账登记（kind=integration）指向检查点，已完成
        activity = second["ledger_activities"][0]
        self.assertEqual(activity["kind"], "integration")
        self.assertEqual(activity["status"], "completed")
        self.assertTrue(activity["checkpoint_path"].endswith("integration_board.json"))
        self.assertEqual(activity["source_anchor"], first["version"]["manifest_sha256"])


class InterruptBoundaryTest(CrossProcessTestBase):
    def test_exit_after_unsaved_approval_sees_previous_consistent_state(self):
        """4 个进程：A 审批后未保存即 os._exit；B 见"候选在、审批不在"；C 重做审批；D 确认恢复。"""
        died = self.hard_worker("full", "--run-dir", str(self.run_dir), "--sha", SHA_A,
                                "--save-candidate", "--approve", "--exit-hard")
        self.assertEqual(died.returncode, 9, "进程应死于审批后的 os._exit(9)")
        witness = self.worker("inspect", "--run-dir", str(self.run_dir))
        self.assertEqual(witness["candidate"]["tasks"][0]["subject_sha256"], SHA_A,
                         "候选应停留在上一个已保存的一致状态")
        self.assertEqual(witness["approvals"], [], "未保存的审批不得出现在持久记录")
        self.assertEqual(witness["versions"][0]["regression"]["version_level"], None)
        self.assertEqual(witness["ledger_activities"][0]["status"], "running",
                         "中断的活动在台账如实呈现为 running")
        redo = self.worker("approve-redo", "--run-dir", str(self.run_dir), "--sha", SHA_A,
                           "--review-id", "rev-1")
        self.assertEqual(redo["record"]["verdict"], "approved")
        recovered = self.worker("inspect", "--run-dir", str(self.run_dir))
        self.assertEqual(recovered["approvals"][0]["verdict"], "approved")
        self.assertEqual(recovered["current_approval"]["status"], "approved")
        self.assertEqual(recovered["current_approval"]["subject_sha256"], SHA_A)


class SupersededChainTest(CrossProcessTestBase):
    def test_version_change_detected_then_rebuilt_across_four_processes(self):
        """4 个进程：A 建版本 v1 → B 换 subject sha → C 校验出 v1 superseded → D 重建 v2 完成。"""
        first = self.worker("full", "--run-dir", str(self.run_dir), "--sha", SHA_A,
                            "--save-candidate")
        v1_sha = first["version"]["manifest_sha256"]
        resubmit = self.worker("resubmit", "--run-dir", str(self.run_dir), "--sha", SHA_B)
        self.assertEqual(resubmit["candidate"]["tasks"][0]["attempt_id"], "T1#2")
        self.assertEqual(resubmit["candidate"]["tasks"][0]["subject_sha256"], SHA_B)
        detected = self.worker("freshness", "--run-dir", str(self.run_dir), "--sha", SHA_B)
        self.assertEqual(detected["superseded"], [1], "进程 C 应校验出 v1 失效")
        rebuilt = self.worker("rebuild", "--run-dir", str(self.run_dir), "--sha", SHA_B,
                              "--current-anchor", SHA_B)
        v2 = rebuilt["version"]
        self.assertEqual(v2["integration_version"], 2)
        self.assertNotEqual(v2["manifest_sha256"], v1_sha)
        self.assertEqual(rebuilt["completed"]["status"], "completed")
        self.assertEqual(rebuilt["stale_anchor_conditions"], ["stale_anchor"],
                         "台账旧锚与当前主体不符须呈现 stale_anchor，不自动续跑")
        # 新进程只读复核：v1 失效持久化、v2 带通过的两级回归
        final = self.worker("inspect", "--run-dir", str(self.run_dir))
        self.assertEqual(final["versions"][0]["status"], "superseded")
        self.assertIn("T1", final["versions"][0]["superseded_reason"])
        self.assertEqual(final["versions"][1]["status"], "completed")
        self.assertEqual(final["versions"][1]["manifest_sha256"], v2["manifest_sha256"])


class FenceAndIdempotencyTest(CrossProcessTestBase):
    def test_stale_epoch_ledger_update_rejected(self):
        """2 个并发进程：旧 epoch 持有者在新 epoch 接管后的心跳被拒（复验围栏）。"""
        hold = subprocess.Popen([sys.executable, str(WORKER), "ledger-hold",
                                 "--run-dir", str(self.run_dir), "--writer", "stale-A"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                cwd=str(REPO_ROOT))
        try:
            ready = self.run_dir / ".hold-ready"
            for _ in range(200):
                if ready.exists():
                    break
                if hold.poll() is not None:
                    self.fail("holder 提前退出：%s%s" % hold.communicate())
                time.sleep(0.05)
            takeover = self.worker("ledger-open", "--run-dir", str(self.run_dir),
                                   "--writer", "fresh-B")
            self.assertEqual(takeover["epoch"], 2)
            (self.run_dir / ".hold-stop").write_text("stop", encoding="utf-8")
        finally:
            out, err = hold.communicate(timeout=30)
        self.assertEqual(hold.returncode, 0, err)
        result = json.loads(out)
        self.assertTrue(result["heartbeat_rejected"],
                        "旧 epoch 的更新必须被拒：%s" % out)
        self.assertIn("接管", result["error"])

    def test_late_report_cannot_overwrite_newer_candidate(self):
        """4 个进程：A 固定 attempt#1/sha_A → B 换成 attempt#2/sha_B → C 迟到回报旧 sha 被拒 →
        D 复核候选仍是新事实。"""
        self.worker("full", "--run-dir", str(self.run_dir), "--sha", SHA_A, "--save-candidate")
        self.worker("resubmit", "--run-dir", str(self.run_dir), "--sha", SHA_B)
        late = self.worker("late-report", "--run-dir", str(self.run_dir), "--sha", SHA_A)
        self.assertTrue(late["rejected"], "旧 attempt 的迟到回报必须被拒")
        self.assertIn("迟到回报", late["error"])
        witness = self.worker("inspect", "--run-dir", str(self.run_dir))
        self.assertEqual(witness["candidate"]["tasks"][0]["subject_sha256"], SHA_B)
        self.assertEqual(witness["candidate"]["tasks"][0]["attempt_id"], "T1#2")

    def test_duplicate_report_is_idempotent(self):
        """2 个进程：同内容候选重报 → 同版本号同哈希、不产生新版本、检查点零变化。"""
        first = self.worker("full", "--run-dir", str(self.run_dir), "--sha", SHA_A,
                            "--save-candidate")
        again = self.worker("duplicate", "--run-dir", str(self.run_dir), "--sha", SHA_A)
        self.assertEqual(again["version_no"], first["version"]["integration_version"])
        self.assertEqual(again["manifest_sha256"], first["version"]["manifest_sha256"])
        self.assertEqual(again["versions_count"], 1)
        self.assertTrue(again["checkpoint_unchanged"], "重复回报后检查点应零变化")


if __name__ == "__main__":
    unittest.main()

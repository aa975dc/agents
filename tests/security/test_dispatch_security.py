# -*- coding: utf-8 -*-
"""派发面对抗性安全回归（SEC01/FS06）：ownership 冲突、workspace 清理不跟 symlink、
precheck 敏感拒绝与 containment 复验（全部真实执行，precheck 走 subprocess 真实退出码）。

- ownership：非共享路径越权重叠 → StructuredConflict（结构化字段、终局拒绝）；
  共享白名单 queue=False → retryable 冲突；敏感路径（.env 等）进不了台账。
- workspace cleanup：工作区内 symlink 指向外部目录 → 清理只 unlink 链接本身，
  外部目标与其内容必须幸存；manifest 被篡改为 root 外路径 → 清理拒绝。
- precheck：run_root 落在 .aws 内 → exit 4；outputs 越出 run_root → exit 3；
  干净根 → exit 0（证明 exit 4/3 不是"永远失败"）。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
PRECHECK = REPO_ROOT / "code-analysis-swarm" / "scripts" / "precheck.py"

from agents_kernel.execution.isolation import WorkspaceManager
from agents_kernel.services.ownership import OwnershipRegistry, StructuredConflict
from agents_kernel.validation import CompanionError


def run_precheck(sub, payload, cwd):
    proc = subprocess.run(
        [sys.executable, str(PRECHECK), sub, "--json", json.dumps(payload)],
        capture_output=True, text=True, cwd=str(cwd))
    return proc.returncode, proc.stdout


class OwnershipConflictTests(unittest.TestCase):
    """P5-03 复验：冲突必须结构化拒绝，且被拒方零授权。"""

    def setUp(self):
        self.registry = OwnershipRegistry()

    def test_non_shared_overlap_rejected_with_structured_conflict(self):
        self.registry.claim("task-a", ["src/a.py"])
        with self.assertRaises(StructuredConflict) as ctx:
            self.registry.claim("task-b", ["src/a.py"])
        conflict = ctx.exception
        self.assertEqual(conflict.holder_task, "task-a")
        self.assertEqual(conflict.requester_task, "task-b")
        self.assertEqual(conflict.paths, ["src/a.py"])
        self.assertFalse(conflict.shared and conflict.retryable)
        # 被拒方零授权：台账里只有持有方
        self.assertEqual([item["task_id"] for item in self.registry.state()], ["task-a"])

    def test_shared_file_without_queue_rejected_retryable(self):
        self.registry.claim("task-a", ["package-lock.json"])
        with self.assertRaises(StructuredConflict) as ctx:
            self.registry.claim("task-b", ["package-lock.json"], queue=False)
        self.assertTrue(ctx.exception.shared)
        self.assertTrue(ctx.exception.retryable)

    def test_sensitive_and_escape_paths_cannot_enter_registry(self):
        for name in (".env", ".ssh/id_rsa", "../outside.txt", "/abs/path"):
            with self.assertRaises(CompanionError, msg=name):
                self.registry.claim("task-x", [name])
        self.assertEqual(self.registry.state(), [])

    def test_release_then_reclaim_resolves_conflict(self):
        self.registry.claim("task-a", ["src/a.py"])
        with self.assertRaises(StructuredConflict):
            self.registry.claim("task-b", ["src/a.py"])
        self.registry.release("task-a")
        claim = self.registry.claim("task-b", ["src/a.py"])
        self.assertEqual(claim.status, "active")


class WorkspaceCleanupSymlinkTests(unittest.TestCase):
    """FS06 对抗性：workspace 内 symlink 指向外部 → 清理不删除外部目标。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "src"
        self.source.mkdir()
        (self.source / "a.txt").write_text("hello", encoding="utf-8")
        self.root = self.base / "wsroot"
        self.root.mkdir()
        self.manager = WorkspaceManager(self.root, self.source)

    def test_cleanup_unlinks_inner_symlink_and_spares_external_target(self):
        info = self.manager.create("t1")
        victim = self.base / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("KEEP", encoding="utf-8")
        os.symlink(victim, Path(info.path) / "innocent_name")
        self.manager.cleanup("t1")
        self.assertFalse(Path(info.path).exists())          # 工作区连同链接一起移除
        self.assertTrue(victim.is_dir())                     # 外部目标幸存
        self.assertEqual((victim / "keep.txt").read_text(encoding="utf-8"), "KEEP")
        self.assertEqual(self.manager.entries(), [])

    def test_cleanup_rejects_tampered_manifest_path_outside_root(self):
        """manifest 被篡改为 root 外路径 → cleanup 拒绝且外部目录零损伤。"""
        info = self.manager.create("t2")
        victim = self.base / "victim"
        victim.mkdir()
        manifest_path = self.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["workspaces"]["t2"]["path"] = str(victim)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(CompanionError):
            self.manager.cleanup("t2")
        self.assertTrue(victim.is_dir())
        self.assertIn("t2", [entry["task_id"] for entry in self.manager.entries()])
        self.assertTrue(Path(info.path).exists())  # 原工作区不动，仍可在册


class PrecheckContainmentTests(unittest.TestCase):
    """precheck 复验：敏感拒绝 exit 4、outputs 越界 exit 3、干净根 exit 0。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.workspace = self.base / "workspace"
        self.source.mkdir()
        self.workspace.mkdir()

    def test_run_root_inside_aws_rejected_exit_4(self):
        code, out = run_precheck(
            "plan", {"source_root": str(self.source),
                     "run_root_parent": str(self.base / ".aws" / "runs")},
            cwd=self.workspace)
        self.assertEqual(code, 4)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "sensitive")

    def test_output_escaping_run_root_rejected_exit_3(self):
        run_root = self.base / "run"
        run_root.mkdir()
        outside = self.base / "outside-artifact.txt"
        outside.write_text("x", encoding="utf-8")
        code, out = run_precheck(
            "verify", {"source_root": str(self.source), "run_root": str(run_root),
                       "outputs": [str(outside)]},
            cwd=self.workspace)
        self.assertEqual(code, 3)
        self.assertFalse(json.loads(out)["ok"])

    def test_clean_roots_pass_exit_0(self):
        """对照组：干净输入真实 exit 0，证明上面的拒绝是策略而非故障。"""
        code, out = run_precheck(
            "verify", {"source_root": str(self.source), "run_root": str(self.base / "new-run"),
                       "outputs": [str(self.base / "new-run" / "report.md")]},
            cwd=self.workspace)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])


if __name__ == "__main__":
    unittest.main()

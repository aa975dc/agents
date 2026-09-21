# -*- coding: utf-8 -*-
"""P5-03：工作区隔离——worktree/受控副本、精确变更检查、在册清理与派发门
（C07 后半/TK02 后半/FS03）。

覆盖：git 仓库 plan 不落盘、worktree 创建/单文件变更恰报 1/基线固定/清理后
worktree 元数据 prune；非 git 副本排除清单、三态变更（改/删/增恰各报 1）、
排除路径不入变更；manifest 安全——不在册目录拒绝清理（防误删）、重复创建
拒绝、中断残留（creating 标记）被下次 sweep 接管且 ready 在册不受影响；
派发门——claim 冲突与 workspace 创建失败均回滚干净（无 claim、无目录、无
在册条目）。git 子进程全部跑在真实临时仓库上，不触碰本仓库。
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel.execution.isolation import (MANIFEST_FILE, WorkspaceManager,
                                               prepare_dispatch)
from agents_kernel.services.ownership import OwnershipRegistry, StructuredConflict
from agents_kernel.validation import CompanionError


def run_git(*args, cwd):
    subprocess.run(["git"] + list(args), cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def make_git_source(base):
    src = base / "src"
    src.mkdir()
    (src / "a.txt").write_text("one", encoding="utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "b.txt").write_text("bee", encoding="utf-8")
    run_git("init", "-q", cwd=src)
    run_git("config", "user.email", "t@example.com", cwd=src)
    run_git("config", "user.name", "t", cwd=src)
    run_git("add", "-A", cwd=src)
    run_git("commit", "-qm", "init", cwd=src)
    return src


def make_copy_source(base):
    src = base / "copysrc"
    src.mkdir()
    (src / "a.txt").write_text("one", encoding="utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "b.txt").write_text("bee", encoding="utf-8")
    (src / "node_modules").mkdir()
    (src / "node_modules" / "dep.js").write_text("dep", encoding="utf-8")
    (src / "vendor").mkdir()
    (src / "vendor" / "v.txt").write_text("v", encoding="utf-8")
    return src


def as_pairs(changes):
    return sorted((item["path"], item["change"]) for item in changes)


class GitWorktreeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.src = make_git_source(self.base)
        self.root = self.base / "ws"
        self.manager = WorkspaceManager(self.root, self.src)

    def test_plan_reports_without_creating(self):
        report = self.manager.plan("T1")
        self.assertEqual(report["mode"], "git-worktree")
        self.assertEqual(report["task_id"], "T1")
        self.assertEqual(Path(report["path"]), self.root.resolve() / "T1")
        self.assertGreaterEqual(report["estimated_files"], 2)
        self.assertGreater(report["estimated_bytes"], 0)
        self.assertFalse((self.root / "T1").exists())       # dry-run 不建目录
        self.assertFalse((self.root / MANIFEST_FILE).exists())  # 也不写 manifest

    def test_create_changes_cleanup_roundtrip(self):
        info = self.manager.create("T1")
        self.assertEqual((info.mode, info.status), ("git-worktree", "ready"))
        workspace = Path(info.path)
        self.assertEqual((workspace / "a.txt").read_text(encoding="utf-8"), "one")
        self.assertEqual(self.manager.changes("T1"), [])    # 干净基线：零变更

        (workspace / "a.txt").write_text("two", encoding="utf-8")  # 改 1 文件
        self.assertEqual(as_pairs(self.manager.changes("T1")),
                         [("a.txt", "modified")])           # 恰报 1
        (workspace / "new.txt").write_text("new", encoding="utf-8")
        self.assertEqual(as_pairs(self.manager.changes("T1")),
                         [("a.txt", "modified"), ("new.txt", "added")])

        entry, = self.manager.entries()
        self.assertEqual((entry["task_id"], entry["status"]), ("T1", "ready"))
        self.assertEqual(entry["source"], str(self.src.resolve()))
        self.manager.cleanup("T1")
        self.assertEqual(self.manager.entries(), [])
        self.assertFalse(workspace.exists())
        worktrees = subprocess.run(["git", "-C", str(self.src), "worktree", "list",
                                    "--porcelain"], capture_output=True, text=True,
                                   check=True).stdout
        self.assertNotIn("T1", worktrees)                   # 源仓库元数据已 prune

    def test_base_commit_pins_baseline(self):
        first = subprocess.run(["git", "-C", str(self.src), "rev-parse", "HEAD"],
                               capture_output=True, text=True, check=True).stdout.strip()
        (self.src / "a.txt").write_text("changed in source", encoding="utf-8")
        run_git("commit", "-aqm", "second", cwd=self.src)
        info = self.manager.create("T2", base_commit=first)
        self.assertEqual((Path(info.path) / "a.txt").read_text(encoding="utf-8"), "one")
        self.assertEqual(self.manager.changes("T2"), [])    # 固定在旧基线上

    def test_create_refuses_existing_unmanaged_dir(self):
        stray = self.root / "T3"
        stray.mkdir(parents=True)
        (stray / "user.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(CompanionError):
            self.manager.create("T3")
        with self.assertRaises(CompanionError):
            self.manager.plan("T3")
        self.assertEqual((stray / "user.txt").read_text(encoding="utf-8"), "keep")
        self.assertEqual(self.manager.entries(), [])


class CopyWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.src = make_copy_source(self.base)
        self.root = self.base / "ws"
        self.manager = WorkspaceManager(self.root, self.src)

    def test_copy_excludes_and_three_state_changes(self):
        info = self.manager.create("C1")
        self.assertEqual((info.mode, info.status), ("copy", "ready"))
        workspace = Path(info.path)
        self.assertFalse((workspace / "node_modules").exists())  # 排除清单生效
        self.assertFalse((workspace / "vendor").exists())
        entry, = self.manager.entries()
        self.assertEqual(entry["snapshotted_files"], 2)          # 只快照受控文件
        self.assertNotIn("snapshot", entry)

        (workspace / "a.txt").write_text("changed", encoding="utf-8")
        (workspace / "sub" / "b.txt").unlink()
        (workspace / "new.txt").write_text("new", encoding="utf-8")
        self.assertEqual(as_pairs(self.manager.changes("C1")),
                         [("a.txt", "modified"), ("new.txt", "added"),
                          ("sub/b.txt", "deleted")])             # 三态各恰报 1

    def test_changes_ignore_excluded_paths(self):
        self.manager.create("C2")
        (self.root / "C2" / "node_modules").mkdir()
        (self.root / "C2" / "node_modules" / "extra.js").write_text("x", encoding="utf-8")
        (self.root / "C2" / "vendor").mkdir()
        (self.root / "C2" / "vendor" / "new.bin").write_text("y", encoding="utf-8")
        self.assertEqual(self.manager.changes("C2"), [])         # 排除路径不算变更

    def test_duplicate_create_rejected_until_cleanup(self):
        self.manager.create("C1")
        with self.assertRaises(CompanionError):
            self.manager.create("C1")
        self.manager.cleanup("C1")
        self.manager.create("C1")                                # 清理后可重建
        self.assertEqual(len(self.manager.entries()), 1)


class ManifestSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.src = make_copy_source(self.base)
        self.root = self.base / "ws"
        self.manager = WorkspaceManager(self.root, self.src)

    def test_cleanup_refuses_unmanaged_dir(self):
        ghost = self.root / "ghost"
        ghost.mkdir(parents=True)
        (ghost / "user.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(CompanionError) as ctx:
            self.manager.cleanup("ghost")
        self.assertIn("不在册", str(ctx.exception))
        self.assertTrue((ghost / "user.txt").exists())           # 用户目录原封不动
        with self.assertRaises(CompanionError):
            self.manager.cleanup("never-seen")

    def test_sweep_adopts_interrupted_residue_only(self):
        self.manager.create("T-live")                            # ready 在册
        manifest = json.loads((self.root / MANIFEST_FILE).read_text(encoding="utf-8"))
        dead_dir = self.root.resolve() / "T-dead"   # create 落盘的是 resolved 路径
        dead_dir.mkdir(parents=True)
        (dead_dir / "partial.txt").write_text("half", encoding="utf-8")
        manifest["workspaces"]["T-dead"] = {                     # 模拟"标记后中断"
            "version": 1, "task_id": "T-dead", "mode": "copy",
            "path": str(dead_dir), "source": str(self.src), "created_at": 1.0,
            "status": "creating", "excludes": []}
        (self.root / MANIFEST_FILE).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        adopted = WorkspaceManager(self.root, self.src).sweep()  # 下次实例接管
        self.assertEqual(adopted, ["T-dead"])
        self.assertFalse(dead_dir.exists())
        self.assertTrue((self.root / "T-live" / "a.txt").exists())  # ready 不受影响
        self.assertEqual([entry["task_id"] for entry in self.manager.entries()],
                         ["T-live"])
        self.assertEqual(self.manager.sweep(), [])               # 幂等：无残留可扫


class DispatchGateTest(unittest.TestCase):
    """派发前置门：claim 成功 + workspace 创建成功才启动；任一失败无残留。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "ws"

    def test_gate_grants_only_when_both_succeed(self):
        registry = OwnershipRegistry()
        manager = WorkspaceManager(self.root, make_copy_source(self.base))
        info = prepare_dispatch("T1", ["a.txt", "contracts/api.json"], registry, manager)
        self.assertEqual(info.status, "ready")
        self.assertEqual([(s["task_id"], s["status"]) for s in registry.state()],
                         [("T1", "active")])
        self.assertTrue((self.root / "T1" / "a.txt").exists())

    def test_claim_conflict_leaves_nothing(self):
        registry = OwnershipRegistry()
        manager = WorkspaceManager(self.root, make_copy_source(self.base))
        registry.claim("A", ["a.txt"])
        with self.assertRaises(StructuredConflict):
            prepare_dispatch("B", ["a.txt"], registry, manager)
        self.assertEqual([s["task_id"] for s in registry.state()], ["A"])
        self.assertFalse((self.root / "B").exists())
        self.assertEqual(manager.entries(), [])

    def test_workspace_failure_rolls_back_claim(self):
        registry = OwnershipRegistry()
        src = make_copy_source(self.base)
        manager = WorkspaceManager(self.root, src)
        shutil.rmtree(src)                                       # 来源消失 → create 必败
        with self.assertRaises(OSError):
            prepare_dispatch("C", ["a.txt"], registry, manager)
        self.assertEqual(registry.state(), [])                   # claim 已回滚释放
        self.assertFalse((self.root / "C").exists())
        self.assertEqual(manager.entries(), [])                  # 无在册条目、无半成品


if __name__ == "__main__":
    unittest.main()

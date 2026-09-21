# -*- coding: utf-8 -*-
"""容量判定缓存（收尾 R05/ST06/SC05）：普通 status 不读源码内容。

验收口径（R05 实测 FAIL 后的修复回归）：
- 超限项目的普通 status 必须零文件打开/零内容读取（首次只做 stat-only 普查）；
- 判定缓存使后续 status 连 os.walk 都为零；删 capacity-verdict.json 强制重测；
- 写路径（check/packet）在超限项目上拒绝且同样不读内容；
- 未超限项目行为与从前完全一致（快照路径回归由既有套件守护）。
"""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "dev-companion" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "packages"))

import core  # noqa: E402
from agents_kernel.atomicio import write_json  # noqa: E402


def _make_project(root: Path, oversize: bool) -> Path:
    import sys
    (root / ".dev-companion").mkdir(parents=True)
    src = root / "src"
    src.mkdir()
    if oversize:
        # 稀疏文件：st_size 计入总量但不产生真实磁盘/读取成本
        with open(src / "big.bin", "wb") as fh:
            fh.truncate(101 * 1024 * 1024)  # 101MiB > 100MiB 上限
        (src / "a.py").write_text("x = 1\n", encoding="utf-8")
    else:
        (src / "a.py").write_text("x = 1\n", encoding="utf-8")
        (src / "b.py").write_text("y = 2\n", encoding="utf-8")
    project = core.Project(root)
    scope = {"title": "容量判定", "goal": "验证", "audience": "自己", "scenario": "测试",
             "out_of_scope": [], "assumptions": [],
             "features": [{"id": "a", "title": "功能a", "acceptance_criteria": ["可用"],
                           "allowed_paths": ["src/a.py"], "requires_user_acceptance": False,
                           "check_commands": [[sys.executable, "-c", "pass"]]}]}
    result = project.init(scope)
    project.confirm(result["revision"])
    return root


class CapacityVerdictTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix="cap-verdict-")
        self.root = _make_project(Path(self.tmp.name) / "proj", oversize=True)
        self.project = core.Project(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_status_does_not_open_source_files(self):
        opened = []
        real_open = open

        def guarded_open(file, *a, **kw):
            if str(self.root) in str(file):
                opened.append(str(file))
            return real_open(file, *a, **kw)

        import builtins
        real = builtins.open
        builtins.open = guarded_open
        try:
            view = self.project.status()
        finally:
            builtins.open = real
        self.assertEqual(view["capacity_status"], "paused")
        self.assertIsNone(view["source_fingerprint"])
        self.assertEqual(opened, [], "超限项目的普通 status 不得打开任何源码文件")

    def test_verdict_cache_avoids_rewalk(self):
        self.project.status()  # 首次：一次 stat-only 普查并写缓存
        walks = []
        real_walk = core.os.walk

        def counting_walk(path, **kw):
            walks.append(str(path))
            return iter(())

        core.os.walk = counting_walk
        try:
            view = self.project.status()
        finally:
            core.os.walk = real_walk
        self.assertEqual(view["capacity_status"], "paused")
        source_walks = [w for w in walks if str(self.root) in w]
        self.assertEqual(source_walks, [], "缓存判定后不得再 walk 源码树")

    def test_delete_verdict_forces_remeasure(self):
        self.project.status()
        self.project._capacity_verdict_path().unlink()
        view = self.project.status()
        self.assertEqual(view["capacity_status"], "paused")
        self.assertTrue(self.project._capacity_verdict_path().is_file())

    def test_verdict_is_fail_safe_refusal_only(self):
        # 判定只用于拒绝：缓存被篡改为 oversize 时，未超限项目也会被拒（宁可拒绝不放行）
        small = _make_project(Path(self.tmp.name) / "small", oversize=False)
        project = core.Project(small)
        write_json(project._capacity_verdict_path(), {"schema_version": 1, "oversize": True,
                                                      "files": 0, "bytes": 0, "measured_at": "fake"})
        with self.assertRaises(core.CapacityExceeded):
            project.snapshot()
        verdict = project.status()
        self.assertEqual(verdict["capacity_status"], "paused")

    def test_write_path_packet_refuses_without_reading_content(self):
        self.project.status()  # 写入判定缓存
        hashed = []
        real = core.sha256_file

        def guard(path):
            hashed.append(str(path))
            return real(path)

        core.sha256_file = guard
        try:
            with self.assertRaises(core.CapacityExceeded):
                self.project.packet("a")
        finally:
            core.sha256_file = real
        self.assertEqual([h for h in hashed if str(self.root) in h], [])


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""kernel 写路径对抗性安全回归（SEC01/SEC02）：路径逃逸、敏感路径、文件名攻击、原子写竞态。

全部用真实 tempfile 攻击用例，按实现的真实行为断言（不做预期伪造）：
- 文件级 symlink 作为写入目标：os.replace 替换链接本身，绝不写穿到链接目标
  （仓库外目标必须原样幸存）；目录级 symlink 组件由 validation.safe_file 拒绝。
- 敏感路径：kernel 敏感清单拒绝 .ssh/.aws/.gnupg 等；对 swarm precheck 同口径
  抽验（subprocess 真实退出码 exit 4）。
- 文件名攻击：../ 混入相对段、绝对路径、NUL 字节——断言得到干净的
  CompanionError/ValueError 而非崩溃；换行文件名是 POSIX 合法数据（非逃逸）。
- 原子写竞态：before_replace 守卫抛错 → 目标保持旧内容、不留 .tmp 半成品。

已知边界（本套件锁定、不视为漏洞）：atomicio 是无策略底层原语，带 symlink
父目录组件的原始路径会写穿——项目文件必须经 validation.safe_file 门（拒绝
任何 symlink 组件），kernel 内所有项目文件写入均走该门。
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

from agents_kernel import atomicio, paths, validation
from agents_kernel.validation import CompanionError, relative_path, safe_file


def run_precheck(payload):
    proc = subprocess.run(
        [sys.executable, str(PRECHECK), "plan", "--json", json.dumps(payload)],
        capture_output=True, text=True, cwd=str(tempfile.gettempdir()))
    return proc.returncode, proc.stdout


class PathEscapeTests(unittest.TestCase):
    """symlink 指向仓库外的写入必须被拒绝或至少不能触达仓库外目标。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "target.txt").write_text("ORIGINAL", encoding="utf-8")
        self.project = self.base / "project"
        self.project.mkdir()

    def test_write_atomic_to_symlink_target_replaces_link_not_external_file(self):
        """写入 symlink 目标 = 换掉链接本身；仓库外原文件内容分毫不动。"""
        link = self.project / "state.json"
        link.symlink_to(self.outside / "target.txt")
        atomicio.write_atomic(link, b"NEW")
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_bytes(), b"NEW")
        self.assertEqual((self.outside / "target.txt").read_text(encoding="utf-8"), "ORIGINAL")

    def test_safe_file_rejects_symlink_file_target(self):
        (self.project / "link.txt").symlink_to(self.outside / "target.txt")
        with self.assertRaises(CompanionError):
            safe_file(self.project, "link.txt")

    def test_safe_file_rejects_symlink_directory_component(self):
        """目录级 symlink 组件（写穿逃逸的入口）同样被拒。"""
        (self.project / "sub").symlink_to(self.outside)
        with self.assertRaises(CompanionError):
            safe_file(self.project, "sub/evil.json")
        self.assertEqual(list(self.outside.iterdir()), [self.outside / "target.txt"])

    def test_kernel_inside_and_realpath_agree_on_escape(self):
        inside_file = self.project / "ok.txt"
        outside_file = self.outside / "t.txt"
        self.assertTrue(paths.inside(paths.realpath(inside_file), paths.realpath(self.project)))
        self.assertFalse(paths.inside(paths.realpath(outside_file), paths.realpath(self.project)))


class SensitivePathTests(unittest.TestCase):
    """.ssh/.aws/.gnupg 等敏感路径作为写入目标必须被 kernel 门拒绝。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()

    def test_relative_path_rejects_sensitive_names_as_any_component(self):
        for name in (".ssh/id_rsa", ".aws/config", ".gnupg/secring.gpg",
                     "src/.env", "id_ed25519", "server.pem", ".git/config",
                     "keys/credentials.json", "secrets/a.txt", ".netrc"):
            with self.assertRaises(CompanionError, msg=name):
                relative_path(name)

    def test_safe_file_rejects_sensitive_target_and_creates_nothing(self):
        with self.assertRaises(CompanionError):
            safe_file(self.project, ".ssh/id_rsa")
        self.assertFalse((self.project / ".ssh").exists())

    def test_kernel_sensitive_list_covers_precheck_directory_names(self):
        """口径抽验：precheck 的敏感目录在 kernel 敏感清单中全部命中。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("precheck_mod", str(PRECHECK))
        precheck = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(precheck)
        self.assertTrue(set(precheck.SENSITIVE_NAMES).issubset(paths.SENSITIVE_NAMES))

    def test_known_gap_kernel_cannot_match_config_gcloud_pair(self):
        """已知低危缺口（报告记录，本任务不修）：kernel 敏感清单按单段匹配，
        表达不了 precheck 的 (".config", "gcloud") 相邻段组合——单独 ".config"
        是大量项目在用的合法目录，补齐需为 paths.sensitive 引入相邻段语义并
        同步 kernel_vendor，属共享内核 API 扩展，超出对抗性回归的修复边界。"""
        self.assertFalse(paths.sensitive(".config") or paths.sensitive("gcloud"))

    def test_precheck_rejects_source_root_inside_ssh_exit_4(self):
        """同口径真实执行：source_root 落在 .ssh 内 → precheck exit 4。"""
        base = Path(self.temp.name)
        source = base / ".ssh" / "victim-repo"
        source.mkdir(parents=True)
        code, out = run_precheck({"source_root": str(source)})
        self.assertEqual(code, 4)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["kind"], "sensitive")


class FilenameAttackTests(unittest.TestCase):
    """恶意文件名：断言得到干净错误或数据级处理，绝不崩溃、绝不逃逸。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()

    def test_rejects_parent_segments_mixed_into_relative_path(self):
        for name in ("..", "a/../b.txt", "a/../../b.txt", "a/b/../..", "a/./b.txt"):
            with self.assertRaises(CompanionError, msg=name):
                relative_path(name)

    def test_rejects_absolute_and_windows_style_paths(self):
        for name in ("/etc/passwd", "C:/x.txt", "a\\b.txt"):
            with self.assertRaises(CompanionError, msg=name):
                relative_path(name)

    def test_nul_byte_yields_clean_error_not_crash(self):
        """NUL 在 os 层被拒：得到可捕获的 ValueError，无 .tmp 残留、无残根。
        （pathlib 的 stat 包装会吞 ValueError，safe_file 因此放行 NUL 名——
        记录该真实行为：真正拒绝发生在写入时的 os 层，同样是干净错误而非崩溃。）"""
        path = safe_file(self.project, "a\x00b")  # 门未拦（行为记录）
        with self.assertRaises(ValueError):
            path.write_bytes(b"x")
        with self.assertRaises(ValueError):
            atomicio.write_atomic(self.project / "a\x00b", b"x")
        self.assertEqual(list(self.project.iterdir()), [])

    def test_newline_filename_is_legal_data_and_cannot_escape(self):
        """换行文件名是 POSIX 合法数据：被接受且始终落在 project 内（非逃逸）。"""
        name = "a\nb.txt"
        self.assertEqual(relative_path(name), name)  # 记录真实行为：按数据接受
        path = safe_file(self.project, name)
        path.write_text("data", encoding="utf-8")
        self.assertTrue(paths.inside(paths.realpath(path), paths.realpath(self.project)))
        self.assertEqual(path.read_text(encoding="utf-8"), "data")


class AtomicWriteRaceTests(unittest.TestCase):
    """before_replace 守卫竞态：目标必须保持旧内容（不半写、不留临时文件）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name) / "state.json"
        atomicio.write_json(self.target, {"epoch": 1})
        self.old_raw = self.target.read_bytes()

    def test_guard_error_keeps_old_content_and_cleans_temp(self):
        def guard():
            raise CompanionError("epoch 已前移，拒绝覆盖")

        with self.assertRaises(CompanionError):
            atomicio.write_json(self.target, {"epoch": 2}, before_replace=guard)
        self.assertEqual(self.target.read_bytes(), self.old_raw)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), {"epoch": 1})
        self.assertEqual(list(self.target.parent.glob(".tmp-*")), [])

    def test_guard_error_leaves_no_partial_content_even_after_prior_success(self):
        """连续两次竞态失败后文件仍与首版逐字节一致。"""
        def guard():
            raise OSError("simulated lost race")

        for _ in range(2):
            with self.assertRaises(OSError):
                atomicio.write_json(self.target, {"epoch": 9}, before_replace=guard)
        self.assertEqual(self.target.read_bytes(), self.old_raw)
        self.assertEqual(list(self.target.parent.glob(".tmp-*")), [])


if __name__ == "__main__":
    unittest.main()

"""跨平台健壮性（P7-03）：Z22 GBK 输出、Z27尾 超时配置化、Z23尾/FS07 目录 fsync。

局限声明：GBK 用例在 macOS/Linux 上以 PYTHONIOENCODING=gbk 近似 Windows GBK 代码页
管道行为（子进程 stdio 以 GBK+strict（stdout）/GBK+backslashreplace（stderr）启动，被
main() 重配为 UTF-8+replace）；真实 Windows 控制台的代码页/WriteConsoleW 路径未在本
环境实测（NOT_RUN）。强制 UTF-8 后可编码字符不会触发替换符，errors="replace" 仅为
不可重配流的兜底，故断言内容完整而非替换符。
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dev-companion" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from core import Project
from releases import TIMEOUT_ENV, ReleaseStore, check_timeout

SOURCE = SCRIPTS / "archives.py"
sys.path.insert(0, str(SOURCE.parent))  # P2-05：archives.py 以文件路径加载时，同目录 kernel_bootstrap 需可 import
SPEC = importlib.util.spec_from_file_location("companion_archives", SOURCE)
archives = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archives)


class CompanionGbkOutputTests(unittest.TestCase):
    """Z22：PYTHONIOENCODING=gbk 近似 Windows GBK 终端，错误路径不得二次 traceback。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, PYTHONIOENCODING="gbk")

    def cli(self, *arguments):
        return subprocess.run([sys.executable, str(SCRIPTS / "companion.py"), "--project", str(self.root), *arguments],
                              env=self.env, capture_output=True)

    def test_error_path_with_non_gbk_character_stays_single_line_json(self):
        archive_id = "存档🚀缺失"  # U+1F680 不在 GBK 内；旧实现此处 UnicodeEncodeError 崩溃
        result = self.cli("preview-restore", "--archive", archive_id)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(b"Traceback", result.stderr)
        payload = json.loads(result.stderr.decode("utf-8"))
        self.assertFalse(payload["success"])
        self.assertIn(archive_id, payload["error"])

    def test_success_path_prints_utf8_json_under_gbk_startup(self):
        result = self.cli("doctor")
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout.decode("utf-8"))
        self.assertTrue(payload["project"])

    def test_result_with_non_gbk_content_does_not_crash_stdout(self):
        # 旧实现的真实崩溃面：stdout 默认 strict，含 GBK 外字符（🚀）的结果 JSON
        # 直接 UnicodeEncodeError 二次 traceback；stderr 默认 backslashreplace 不崩
        # 但 GBK 字节非 UTF-8，宿主不可解码——重配后两条流统一 UTF-8。
        (self.root / "ledger.py").write_text("ok", encoding="utf-8")
        result = self.cli("save", "--paths", "ledger.py", "--summary", "摘要🚀含特殊字符")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(b"Traceback", result.stderr)
        payload = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(payload["summary"], "摘要🚀含特殊字符")


class ReleaseTimeoutConfigTests(unittest.TestCase):
    """Z27尾：DEV_COMPANION_CHECK_TIMEOUT 生效；非法值回退默认 120 并警告。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ReleaseStore(Project(Path(self.temp.name)))

    def test_unset_or_empty_env_keeps_compatible_default(self):
        with patch.dict(os.environ):
            os.environ.pop(TIMEOUT_ENV, None)
            self.assertEqual(check_timeout(), 120)
            os.environ[TIMEOUT_ENV] = "  "
            self.assertEqual(check_timeout(), 120)

    def test_valid_value_is_used_for_release_commands(self):
        with patch.dict(os.environ, {"DEV_COMPANION_CHECK_TIMEOUT": "30"}):
            self.assertEqual(check_timeout(), 30)
            result = self.store._execute([sys.executable, "-c", "print('ok')"])
        self.assertTrue(result["executed"])
        self.assertEqual(result["exit_code"], 0)

    def test_invalid_values_fall_back_to_default_with_warning(self):
        with patch.dict(os.environ, {"DEV_COMPANION_CHECK_TIMEOUT": "bad"}):
            for raw in ("bad", "0", "-3", "3.5"):
                os.environ["DEV_COMPANION_CHECK_TIMEOUT"] = raw
                with contextlib.redirect_stderr(io.StringIO()) as captured:
                    self.assertEqual(check_timeout(), 120)
                self.assertIn("警告", captured.getvalue())
                self.assertIn(raw, captured.getvalue())

    def test_small_timeout_triggers_timed_out_evidence(self):
        command = [sys.executable, "-c", "import time; time.sleep(5)"]
        with patch.dict(os.environ, {"DEV_COMPANION_CHECK_TIMEOUT": "1"}):
            result = self.store._execute(command)
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["exit_code"])
        self.assertIn("超时", result["output"])
        self.assertIn("1秒", result["output"])


class ArchiveParentFsyncTests(unittest.TestCase):
    """Z23尾/FS07：恢复替换/删除后对父目录 fsync（实际 syscall 平台不支持时静默跳过）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.store = archives.ArchiveStore(self.project)

    def spy(self, calls):
        real = archives.fsync_directory

        def wrapper(directory):
            calls.append(Path(os.path.realpath(directory)))  # macOS /var→/private/var
            real(directory)

        return wrapper

    def test_apply_entry_fsyncs_parent_directory_after_replace(self):
        calls = []
        data = b"restored-content"
        digest = hashlib.sha256(data).hexdigest()
        entry = {"sha256": digest, "size": len(data), "mode": 0o644}
        with patch.object(archives, "fsync_directory", side_effect=self.spy(calls)):
            self.store._apply_entry("nested/dir/note.txt", entry, {digest: data})
        restored = self.project / "nested/dir/note.txt"
        self.assertEqual(restored.read_bytes(), data)
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o644)
        self.assertIn(self.store.project / "nested/dir", calls)

    def test_delete_entry_fsyncs_parent_directory_after_unlink(self):
        target = self.project / "note.txt"
        target.write_text("gone soon")
        calls = []
        with patch.object(archives, "fsync_directory", side_effect=self.spy(calls)):
            self.store._apply_entry("note.txt", None, {})
        self.assertFalse(target.exists())
        self.assertIn(self.store.project, calls)


if __name__ == "__main__":
    unittest.main()

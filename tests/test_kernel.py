"""agents_kernel 单测：原子写顺序与 fd 异常安全、敏感清单并集、分块哈希一致性。

这些测试锁定 P2-01 下沉后的共享内核行为；原 132 项套件仍是行为等价门。
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel import atomicio, digest, paths, process, validation


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_write_atomic_replaces_durably_and_fsyncs_parent_after_replace(self):
        final = self.directory / "state.json"
        calls = []
        real_replace, real_fsync = os.replace, os.fsync

        def replace(src, dst):
            calls.append("replace")
            return real_replace(src, dst)

        def fsync(fd):
            calls.append("file-fsync")
            return real_fsync(fd)

        with mock.patch.object(atomicio.os, "replace", replace), \
                mock.patch.object(atomicio.os, "fsync", fsync), \
                mock.patch.object(atomicio, "fsync_directory",
                                  lambda directory: calls.append(("dir-fsync", Path(directory)))):
            atomicio.write_atomic(final, b"payload", prefix="state-", suffix=".tmp")
        self.assertEqual(calls[0], "file-fsync")
        self.assertEqual(calls[1], "replace")
        self.assertEqual(calls[2], ("dir-fsync", final.parent))
        self.assertEqual(final.read_bytes(), b"payload")
        self.assertEqual(list(self.directory.glob("state-*.tmp")), [])

    def test_write_json_matches_legacy_state_flavor(self):
        final = self.directory / "journey.json"
        value = {"标题": "开发陪伴", "nested": {"b": 1, "a": [1, 2]}}
        atomicio.write_json(final, value, prefix="journey-", suffix=".tmp")
        expected = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        self.assertEqual(final.read_bytes(), expected)
        self.assertEqual(json.loads(final.read_text(encoding="utf-8")), value)

    def test_write_failure_closes_fd_and_cleans_temporary(self):
        final = self.directory / "release.json"
        closed = []
        real_fdopen = os.fdopen

        class FailingHandle:
            def __init__(self, handle):
                self._handle = handle

            def write(self, data):
                raise OSError("simulated disk full")

            def flush(self):
                pass

            def fileno(self):
                return self._handle.fileno()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._handle.close()
                closed.append(exc[0] is not None)
                return False

        def fdopen(fd, *args, **kwargs):
            return FailingHandle(real_fdopen(fd, *args, **kwargs))

        with mock.patch.object(atomicio.os, "fdopen", fdopen):
            with self.assertRaisesRegex(OSError, "disk full"):
                atomicio.write_atomic(final, b"payload", prefix="release-", suffix=".tmp")
        self.assertEqual(closed, [True])  # 写入抛错时 fd 已在 with 退出中关闭
        self.assertFalse(final.exists())
        self.assertEqual(list(self.directory.glob("release-*.tmp")), [])

    def test_fsync_directory_is_best_effort(self):
        with mock.patch.object(atomicio.os, "open", side_effect=OSError("no dir fds here")):
            atomicio.fsync_directory(self.directory)  # 不抛错即通过
        with mock.patch.object(atomicio.os, "fsync", side_effect=OSError("EINVAL")):
            atomicio.fsync_directory(self.directory)

    def test_read_json_rejects_invalid_file_with_shared_error(self):
        bad = self.directory / "bad.json"
        bad.write_text("{")
        with self.assertRaisesRegex(validation.CompanionError, "无法读取有效的 JSON"):
            atomicio.read_json(bad)


class SensitiveListTests(unittest.TestCase):
    def test_union_covers_every_entry_of_both_legacy_lists(self):
        # 原 core.sensitive 全部条目（core.py:117-121）
        core_legacy = [".env", ".env.local", ".env.production", "id_rsa", "id_ed25519",
                       "credentials.json", ".npmrc", ".pypirc",
                       "server.pem", "server.key", "cert.p12", "cert.pfx"]
        # 原 archives._validate_path 全部条目（archives.py:152-157）
        archives_legacy = [".git", ".dev-companion", ".ssh", ".aws", ".gnupg", ".kube", ".docker",
                           ".netrc", ".git-credentials", ".dockercfg", "auth.json",
                           ".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials",
                           "secrets", "service-account",
                           "token.pem", "token.key", "token.p12", "token.pfx", "token.keystore", "token.keychain"]
        for name in core_legacy + archives_legacy:
            self.assertTrue(paths.sensitive(name), name)
            self.assertTrue(paths.sensitive(name.upper()), name)  # 大小写不敏感语义保留
        for benign in ("ledger.py", "readme.md", "notes.txt", "package.json", "git-keeper.txt"):
            self.assertFalse(paths.sensitive(benign), benign)

    def test_relative_path_still_rejects_old_and_new_sensitive_segments(self):
        for name in (".env", "secrets.yaml", ".ssh/config", "build/key.pem"):
            with self.assertRaisesRegex(validation.CompanionError, "不在支持的普通项目文件范围"):
                validation.relative_path(name)
        self.assertEqual(validation.relative_path("src/ledger.py"), "src/ledger.py")


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_chunked_file_digest_matches_whole_file_hash(self):
        payload = bytes(bytearray((index * 7 + 13) % 251 for index in range(digest.READ_SIZE * 2 + 12345)))
        path = Path(self.temp.name) / "big.bin"
        path.write_bytes(payload)
        expected = hashlib.sha256(payload).hexdigest()
        for read_size in (1, 4096, digest.READ_SIZE):
            sha256_hex, size = digest.sha256_file(path, read_size=read_size)
            self.assertEqual(sha256_hex, expected)
            self.assertEqual(size, len(payload))
        sha256_hex, size = digest.sha256_file(path)
        self.assertEqual((sha256_hex, size), (expected, len(payload)))

    def test_file_entry_digest_shape_is_shared(self):
        sha256_hex = hashlib.sha256(b"payload").hexdigest()
        self.assertEqual(digest.content_digest(sha256_hex, 0o644),
                         digest.digest({"sha256": sha256_hex, "mode": 0o644}))

    def test_json_domains_keep_their_separator_contracts(self):
        # core state 域：默认分隔符；archives manifest 域：紧凑分隔符（存量数据依赖，见 digest.py 注释）。
        self.assertEqual(digest.digest({"b": 1, "a": "值"}),
                         hashlib.sha256(json.dumps({"b": 1, "a": "值"}, sort_keys=True,
                                                   ensure_ascii=False).encode("utf-8")).hexdigest())
        self.assertEqual(digest.canonical_bytes({"b": 1, "a": "值"}), b'{"a":"\xe5\x80\xbc","b":1}')

    def test_sha256_bytes_matches_hashlib(self):
        self.assertEqual(digest.sha256_bytes(b"data"), hashlib.sha256(b"data").hexdigest())


class ValidationAndProcessTests(unittest.TestCase):
    def test_feature_id_rule_accepts_unique_ascii_and_rejects_the_rest(self):
        # 与原 core/journey 两处内联规则逐字等价：空串与"-"/"_"本身合法，
        # 空值由调用点先经 text() 拦截，重复由 seen 拦截。
        seen = set()
        self.assertEqual(validation.feature_id("sum-2_x", seen), "sum-2_x")
        for identifier in ("汇总", "a b", "a/b"):
            with self.assertRaisesRegex(validation.CompanionError, "功能编号须唯一"):
                validation.feature_id(identifier, set())
        with self.assertRaisesRegex(validation.CompanionError, "功能编号须唯一"):
            validation.feature_id("sum", {"sum"})

    def test_inside_matches_equality_and_ancestry(self):
        base = Path("/tmp/project/.dev-companion")
        self.assertTrue(paths.inside(base / "board.html", base))
        self.assertTrue(paths.inside(base, base))
        self.assertFalse(paths.inside(Path("/tmp/project/out.md"), base))

    def test_redact_output_hashes_raw_and_redacts_secrets(self):
        result = process.redact_output("api_token=FAKE_SECRET_123\n")
        self.assertNotIn("FAKE_SECRET_123", result["output"])
        self.assertIn("[REDACTED]", result["output"])
        self.assertTrue(result["output_redacted"])
        self.assertEqual(result["output_sha256"], hashlib.sha256(b"api_token=FAKE_SECRET_123\n").hexdigest())

    def test_now_is_utc_seconds_precision(self):
        self.assertRegex(process.now(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


if __name__ == "__main__":
    unittest.main()

"""Archive tests operate exclusively in disposable temporary projects."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "dev-companion" / "scripts" / "archives.py"
sys.path.insert(0, str(SOURCE.parent))  # P2-05：archives.py 以文件路径加载时，同目录 kernel_bootstrap 需可 import
SPEC = importlib.util.spec_from_file_location("companion_archives", SOURCE)
archives = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archives)
ArchiveStore = archives.ArchiveStore
ArchiveError = archives.ArchiveError


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.store = ArchiveStore(self.project)

    def write(self, name, text):
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def restore(self, archive_id):
        preview = self.store.preview_restore(archive_id)
        return self.store.restore(archive_id, preview["token"])

    def test_restore_reverses_add_modify_delete_and_can_undo(self):
        self.write("app.py", "version one")
        self.write("nested/deleted.txt", "restore me")
        self.write("untouched.txt", "outside managed scope")
        first = self.store.save(["app.py", "nested/deleted.txt"], "initial")
        self.write("app.py", "version two")
        (self.project / "nested/deleted.txt").unlink()
        self.write("new.py", "new feature")
        self.store.save(["new.py"], "adopt new file")
        result = self.restore(first["archive_id"])
        self.assertEqual((self.project / "app.py").read_text(), "version one")
        self.assertEqual((self.project / "nested/deleted.txt").read_text(), "restore me")
        self.assertFalse((self.project / "new.py").exists())
        self.assertEqual((self.project / "untouched.txt").read_text(), "outside managed scope")
        self.assertEqual(set(result["changed_paths"]), {"app.py", "nested/deleted.txt", "new.py"})
        self.restore(result["safety_archive_id"])
        self.assertEqual((self.project / "app.py").read_text(), "version two")
        self.assertFalse((self.project / "nested/deleted.txt").exists())
        self.assertEqual((self.project / "new.py").read_text(), "new feature")
        self.assertEqual(len(self.store.history()), 4)
        self.assertTrue(all(item["integrity"] == "verified" for item in self.store.history()))

    def test_no_change_restore_still_has_valid_safety_snapshot(self):
        self.write("app.py", "same")
        first = self.store.save(["app.py"], "initial")
        result = self.restore(first["archive_id"])
        self.assertEqual(result["changed_paths"], [])
        safety = next(item for item in self.store.history() if item["archive_id"] == result["safety_archive_id"])
        self.assertEqual(safety["kind"], "safety")
        self.assertEqual(safety["integrity"], "verified")
        self.assertEqual(self.restore(safety["archive_id"])["status"], "restored")

    def test_preview_is_bound_to_target_content_and_single_use(self):
        self.write("app.py", "first")
        first = self.store.save(["app.py"], "initial")
        self.write("app.py", "second")
        second = self.store.save(["app.py"], "second")
        preview = self.store.preview_restore(first["archive_id"])
        with self.assertRaises(ArchiveError):
            self.store.restore(second["archive_id"], preview["token"])
        with self.assertRaises(ArchiveError):
            self.store.restore(first["archive_id"], "wrong token")
        self.write("app.py", "unsaved edit after preview")
        with self.assertRaisesRegex(ArchiveError, "发生变化"):
            self.store.restore(first["archive_id"], preview["token"])
        self.assertEqual((self.project / "app.py").read_text(), "unsaved edit after preview")
        preview = self.store.preview_restore(first["archive_id"])
        self.store.restore(first["archive_id"], preview["token"])
        with self.assertRaises(ArchiveError):
            self.store.restore(first["archive_id"], preview["token"])

    def test_scope_expansion_invalidates_preview(self):
        self.write("app.py", "initial")
        first = self.store.save(["app.py"], "initial")
        preview = self.store.preview_restore(first["archive_id"])
        self.write("added.txt", "keep this")
        self.store.save(["added.txt"], "adopt another file")
        with self.assertRaisesRegex(ArchiveError, "发生变化"):
            self.store.restore(first["archive_id"], preview["token"])
        self.assertEqual((self.project / "added.txt").read_text(), "keep this")

    def test_unsafe_paths_and_directory_are_rejected(self):
        bad_paths = ["/tmp/a", "../outside", "a/../outside", "a//b", "./app.py", ".git/config",
                     ".dev-companion/state.json", ".env", ".env.example", "config/.ENV.local",
                     "id_ed25519", ".ssh/config", "key.pem", "cert.key", "credentials.json", "C:\\file",
                     ".npmrc", ".pypirc", ".netrc", ".git-credentials", ".docker/config.json", "auth.json"]
        for name in bad_paths:
            with self.subTest(name=name), self.assertRaises(ArchiveError):
                self.store.save([name], "unsafe")
        (self.project / "directory").mkdir()
        with self.assertRaisesRegex(ArchiveError, "普通文件"):
            self.store.save(["directory"], "directory")
        with self.assertRaises(ArchiveError):
            self.store.save(["missing", "missing-other", "missing/child"], "conflict")
        self.assertEqual(self.store.history(), [])

    def test_symlink_files_parents_and_storage_root_are_rejected(self):
        self.write("original.txt", "original")
        (self.project / "link.txt").symlink_to(self.project / "original.txt")
        with self.assertRaisesRegex(ArchiveError, "符号链接"):
            self.store.save(["link.txt"], "link")
        (self.project / "real").mkdir()
        (self.project / "linked").symlink_to(self.project / "real", target_is_directory=True)
        with self.assertRaisesRegex(ArchiveError, "符号链接"):
            self.store.save(["linked/missing.txt"], "linked parent")
        (self.project / ".dev-companion").symlink_to(self.project / "real", target_is_directory=True)
        with self.assertRaisesRegex(ArchiveError, "符号链接"):
            self.store.save(["original.txt"], "linked storage")
        self.assertEqual(list((self.project / "real").iterdir()), [])

    def test_directory_collision_blocks_restore_without_touching_contents(self):
        self.write("app.py", "first")
        first = self.store.save(["app.py"], "initial")
        (self.project / "app.py").unlink()
        self.write("app.py/unmanaged.txt", "do not remove")
        with self.assertRaisesRegex(ArchiveError, "普通文件"):
            self.store.preview_restore(first["archive_id"])
        self.assertEqual((self.project / "app.py/unmanaged.txt").read_text(), "do not remove")

    def test_tampered_blob_and_manifest_never_restore(self):
        self.write("app.py", "first")
        first = self.store.save(["app.py"], "initial")
        directory = self.store.root / first["archive_id"]
        blob = next((directory / "files").iterdir())
        original = blob.read_bytes()
        blob.write_bytes(b"tampered")
        self.assertEqual(self.store.history()[0]["integrity"], "failed")
        with self.assertRaisesRegex(ArchiveError, "校验失败"):
            self.store.preview_restore(first["archive_id"])
        blob.write_bytes(original)
        manifest = directory / "manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b" ")
        with self.assertRaisesRegex(ArchiveError, "校验失败"):
            self.store.preview_restore(first["archive_id"])
        self.assertEqual((self.project / "app.py").read_text(), "first")

    def test_partial_failure_keeps_verified_protection_and_blocks_more_writes(self):
        self.write("a.txt", "a old")
        self.write("b.txt", "b old")
        first = self.store.save(["a.txt", "b.txt"], "initial")
        self.write("a.txt", "a current")
        self.write("b.txt", "b current")
        preview = self.store.preview_restore(first["archive_id"])
        original_apply = self.store._apply_entry

        def fail_second(name, entry, blobs):
            if name == "b.txt":
                raise OSError("simulated full disk")
            original_apply(name, entry, blobs)

        with patch.object(self.store, "_apply_entry", side_effect=fail_second):
            with self.assertRaises(ArchiveError) as caught:
                self.store.restore(first["archive_id"], preview["token"])
        safety_id = caught.exception.safety_archive_id
        self.assertIsNotNone(safety_id)
        self.assertIn(safety_id, str(caught.exception))
        self.assertEqual((self.project / "a.txt").read_text(), "a old")
        self.assertEqual((self.project / "b.txt").read_text(), "b current")
        journal = json.loads((self.store.root / "restore-pending.json").read_text())
        self.assertEqual(journal["status"], "failed")
        self.assertEqual(journal["safety_archive_id"], safety_id)
        protection = next(item for item in self.store.history() if item["archive_id"] == safety_id)
        self.assertEqual(protection["integrity"], "verified")
        _, entries, blobs = self.store._load_archive(safety_id, self.store._catalog())
        self.assertEqual(blobs[entries["a.txt"]["sha256"]], b"a current")
        self.assertEqual(blobs[entries["b.txt"]["sha256"]], b"b current")
        for action in (lambda: self.store.save(["a.txt"], "retry"),
                       lambda: self.store.preview_restore(safety_id),
                       lambda: self.store.restore(first["archive_id"], preview["token"])):
            with self.assertRaisesRegex(ArchiveError, "暂停写入"):
                action()

    def test_safety_archive_failure_prevents_overwrite(self):
        self.write("app.py", "old")
        first = self.store.save(["app.py"], "initial")
        self.write("app.py", "new")
        preview = self.store.preview_restore(first["archive_id"])
        with patch.object(self.store, "_save_snapshot", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.restore(first["archive_id"], preview["token"])
        self.assertEqual((self.project / "app.py").read_text(), "new")
        self.assertFalse((self.store.root / "restore-pending.json").exists())

    def test_file_size_limit_preserves_existing_archive(self):
        self.write("app.py", "old")
        first = self.store.save(["app.py"], "initial")
        self.write("app.py", "too large")
        with patch.object(archives, "MAX_FILE_BYTES", 4):
            with self.assertRaisesRegex(ArchiveError, "上限"):
                self.store.save(["app.py"], "too big")
        self.assertEqual([item["archive_id"] for item in self.store.history()], [first["archive_id"]])

    def test_executable_permission_is_restored(self):
        path = self.write("run.sh", "echo hi")
        path.chmod(0o755)
        first = self.store.save(["run.sh"], "executable")
        path.chmod(0o644)
        self.restore(first["archive_id"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o755)

    def test_intervening_write_during_safety_capture_prevents_restore(self):
        self.write("app.py", "original")
        first = self.store.save(["app.py"], "initial")
        self.write("app.py", "before safety")
        preview = self.store.preview_restore(first["archive_id"])
        original_save = self.store._save_snapshot

        def capture_then_external_write(*args, **kwargs):
            result = original_save(*args, **kwargs)
            self.write("app.py", "external editor changed this")
            return result

        with patch.object(self.store, "_save_snapshot", side_effect=capture_then_external_write):
            with self.assertRaises(ArchiveError) as caught:
                self.store.restore(first["archive_id"], preview["token"])
        self.assertIsNotNone(caught.exception.safety_archive_id)
        self.assertEqual((self.project / "app.py").read_text(), "external editor changed this")
        self.assertFalse((self.store.root / "restore-pending.json").exists())

    def test_deletion_since_preview_invalidates_token(self):
        self.write("app.py", "original")
        first = self.store.save(["app.py"], "initial")
        preview = self.store.preview_restore(first["archive_id"])
        (self.project / "app.py").unlink()
        with self.assertRaisesRegex(ArchiveError, "发生变化"):
            self.store.restore(first["archive_id"], preview["token"])
        self.assertFalse((self.project / "app.py").exists())

    def test_restore_rejects_symlink_replacement_after_preview(self):
        self.write("app.py", "original")
        first = self.store.save(["app.py"], "initial")
        preview = self.store.preview_restore(first["archive_id"])
        target = self.write("outside-scope.txt", "must remain")
        (self.project / "app.py").unlink()
        (self.project / "app.py").symlink_to(target)
        with self.assertRaisesRegex(ArchiveError, "符号链接"):
            self.store.restore(first["archive_id"], preview["token"])
        self.assertEqual(target.read_text(), "must remain")

    def test_total_size_limit_and_file_count_limit(self):
        self.write("a.txt", "12345")
        self.write("b.txt", "67890")
        with patch.object(archives, "MAX_TOTAL_BYTES", 8):
            with self.assertRaisesRegex(ArchiveError, "上限"):
                self.store.save(["a.txt", "b.txt"], "too much")
        with patch.object(archives, "MAX_FILES", 1):
            with self.assertRaisesRegex(ArchiveError, "最多"):
                self.store.save(["a.txt", "b.txt"], "too many")
        self.assertEqual(self.store.history(), [])


if __name__ == "__main__":
    unittest.main()

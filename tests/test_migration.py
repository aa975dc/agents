"""旧三 JSON → SQLite 迁移测试（P2-04：C16 + Z13 迁移半 + Z14 存量半）。

覆盖：干净导入视图计数、损坏 JSON 明确报错零写入、dry_run 零写入、重复导入
去重/拒绝、导入中途强杀（子进程 os._exit）后幂等接管、fallback 导出可被现有
core.Project 读为合法项目。数据库与项目一律建在 tempfile（ST06），全真实跑。
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages"))

from agents_kernel.domain import views
from agents_kernel.storage import db, migration
from agents_kernel.validation import CompanionError

SCRIPTS = REPO_ROOT / "dev-companion" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load_script(name, module_name):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


archives = _load_script("archives", "companion_archives_migration")
core = _load_script("core", "companion_core_migration")

TOTAL_EVENTS = 14  # 3 功能×3 + verification/integration/acceptance + journey 1 + release 1


class MigrationTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # 与 core/legacy 相同的 realpath 口径：macOS 临时目录 /var → /private/var
        self.project = (Path(self.temp.name) / "proj").resolve()
        self.data = self.project / ".dev-companion"
        self.data.mkdir(parents=True)
        self.db_path = Path(self.temp.name) / "facts.sqlite"

    def write_source(self, name, value):
        """按旧实现的写出口径（indent 2 + 末尾换行）生成来源 JSON；value 为 str 时原样写（制造损坏）。"""
        path = self.data / name
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        path.write_text(text, encoding="utf-8")
        return path

    def build_legacy(self):
        """三份合法旧 JSON：f-login 已验收（含 verification/integration/acceptance）、f-report 受阻、f-export 待开始。"""
        self.scope = {"title": "演示项目", "goal": "做出可用演示", "audience": "内部用户",
                      "scenario": "本地演示", "out_of_scope": ["多租户"], "assumptions": ["单机使用"],
                      "features": [
                          {"id": "f-login", "title": "登录", "acceptance_criteria": ["能登录"],
                           "allowed_paths": ["app/login.py"], "check_commands": [["python3", "-c", "pass"]],
                           "requires_user_acceptance": True},
                          {"id": "f-report", "title": "报表", "acceptance_criteria": ["能出报表"],
                           "allowed_paths": ["app/report.py"], "check_commands": [["python3", "-c", "pass"]],
                           "requires_user_acceptance": False},
                          {"id": "f-export", "title": "导出", "acceptance_criteria": ["能导出"],
                           "allowed_paths": ["app/export.py"], "check_commands": [["python3", "-c", "pass"]],
                           "requires_user_acceptance": False}]}
        self.tasks = {
            "f-login": {"status": "accepted",
                        "verification": {"id": "ver-1", "at": "2026-01-01T00:00:00+00:00", "passed": True,
                                         "kind": "feature", "planning_fingerprint": None,
                                         "fingerprint": "a" * 64, "results": []},
                        "integration": {"id": "int-1", "at": "2026-01-01T00:00:00+00:00", "passed": True,
                                        "kind": "integration", "fingerprint": "a" * 64, "results": []},
                        "acceptance": {"at": "2026-01-01T00:00:00+00:00", "note": "用户已试用通过",
                                       "user_confirmed": True, "verification_id": "ver-1"}},
            "f-report": {"status": "blocked", "blocker": "缺少数据源资料"},
            "f-export": {"status": "pending"}}
        state = {"schema_version": 1, "project": str(self.project), "revision": 5,
                 "scope_version": 2, "confirmed": True, "scope": self.scope, "tasks": self.tasks,
                 "events": [{"revision": 1, "at": "t1", "kind": "scope_drafted"},
                            {"revision": 5, "at": "t5", "kind": "accepted", "feature_id": "f-login"}],
                 "updated_at": "t5"}
        journey = {"schema_version": 1, "project": str(self.project), "revision": 3, "updated_at": "t3",
                   "records": {"product": {"summary": "轻量内部工具", "details": {"positioning": "内部"},
                                           "open_questions": [], "artifacts": [], "decisions": [],
                                           "scope": {"title": "演示项目", "goal": "做出可用演示",
                                                     "audience": "内部用户", "scenario": "本地演示",
                                                     "out_of_scope": [], "assumptions": [],
                                                     "features": []},
                                           "complete": True, "stale": False, "user_confirmed": True,
                                           "revision": 2, "updated_at": "t2", "artifact_hashes": {}}},
                   "history": [{"revision": 1}, {"revision": 2}, {"revision": 3}]}
        release = {"schema_version": 1, "project": str(self.project), "revision": 1,
                   "status": "prepared", "updated_at": "t4",
                   "config": {"version": "0.1.0", "target": "本机", "environment": "local",
                              "summary": "首次发布", "rollback_plan": "还原目录",
                              "verification_notes": "", "deploy_commands": [["echo", "deploy"]],
                              "verify_commands": [["echo", "verify"]], "rollback_commands": []},
                   "binding": {"scope_version": 2, "fingerprint": "b" * 64, "check_ids": {},
                               "integration_ids": {}, "journey": None},
                   "evidence": {}, "history": [{"revision": 1, "kind": "prepared"}]}
        self.write_source("state.json", state)
        self.write_source("journey.json", journey)
        self.write_source("release.json", release)
        self.originals = {name: (self.data / name).read_bytes()
                          for name in ("state.json", "journey.json", "release.json")}

    def source_bytes(self):
        return {name: (self.data / name).read_bytes() for name in ("state.json", "journey.json", "release.json")}

    def store_counts(self):
        store = db.Store(self.db_path)
        store.open()
        self.addCleanup(store.close)
        return store


class CleanImportTests(MigrationTestBase):
    def test_clean_import_folds_views_counts_and_manifest(self):
        self.build_legacy()
        report = migration.import_json_store(self.project, self.db_path)
        self.assertEqual(report["status"], "imported")
        self.assertEqual(report["planned_total"], TOTAL_EVENTS)
        self.assertEqual(report["applied"], TOTAL_EVENTS)
        self.assertEqual(report["head_seq"], TOTAL_EVENTS)
        self.assertEqual(report["planned"], {"scope_revision": 3, "feature_status": 3, "task_status": 3,
                                             "evidence_registered": 4, "release_stage": 1})
        self.assertEqual(report["history_counts"], {"state_events": 2, "journey_history": 3,
                                                    "release_history": 1})
        store = self.store_counts()
        self.assertEqual(views.list_features(store)["items"][0]["scope"], ["app/export.py"])
        features = {item["feature_id"]: item for item in views.list_features(store, limit=10)["items"]}
        self.assertEqual(set(features), {"f-login", "f-report", "f-export"})
        self.assertEqual(features["f-login"]["status"], "accepted")
        self.assertEqual(features["f-login"]["title"], "登录")
        self.assertEqual(features["f-login"]["scope_version"], 2)
        tasks = {item["task_id"]: item["status"] for item in views.list_tasks(store, limit=10)["items"]}
        self.assertEqual(tasks, {"f-login": "accepted", "f-report": "blocked", "f-export": "pending"})
        evidence = views.list_evidence(store, limit=10)["items"]
        self.assertEqual(len(evidence), 4)
        by_id = {item["evidence_id"]: item for item in evidence}
        self.assertEqual(by_id["ver-1"]["result"], "passed")
        self.assertEqual(by_id["int-1"]["kind"], "check")
        self.assertEqual(by_id["accept:f-login"]["kind"], "acceptance")
        self.assertEqual(by_id["journey:product"]["result"], "complete")
        releases = views.list_releases(store)["items"]
        self.assertEqual([(item["release_id"], item["stage"]) for item in releases], [("release", "prepared")])
        manifest = migration.read_manifest(store)
        self.assertEqual(manifest["store_schema_version"], db.SCHEMA_VERSION)
        self.assertEqual(manifest["project"], str(self.project))
        self.assertTrue(manifest["imported_at"])
        self.assertTrue(all(source["sha256"] and source["lines"] >= 1
                            for source in manifest["sources"].values()))
        self.assertEqual(manifest["legacy"]["confirmed"], True)
        self.assertEqual(manifest["legacy"]["scope_context"]["title"], "演示项目")

    def test_journey_only_project_imports_with_warning(self):
        self.build_legacy()
        (self.data / "state.json").unlink()
        (self.data / "release.json").unlink()
        report = migration.import_json_store(self.project, self.db_path)
        self.assertEqual(report["status"], "imported")
        self.assertTrue(any("state.json 缺失" in warning for warning in report["warnings"]))
        store = self.store_counts()
        self.assertEqual(views.list_features(store)["items"], [])
        self.assertEqual(len(views.list_evidence(store)["items"]), 1)


class CorruptAndDryRunTests(MigrationTestBase):
    def test_corrupt_json_fails_clearly_and_writes_nothing(self):
        self.build_legacy()
        self.write_source("state.json", '{"schema_version": 1,')  # 截断损坏
        with self.assertRaises(CompanionError) as caught:
            migration.import_json_store(self.project, self.db_path)
        self.assertIn("state.json 不是有效的 JSON", str(caught.exception))
        self.assertFalse(self.db_path.exists())  # 未写库

    def test_inconsistent_state_and_orphan_release_fail_before_writing(self):
        self.build_legacy()
        state = json.loads(self.originals["state.json"].decode("utf-8"))
        state["tasks"]["ghost"] = {"status": "pending"}
        self.write_source("state.json", state)
        with self.assertRaises(CompanionError) as caught:
            migration.import_json_store(self.project, self.db_path)
        self.assertIn("功能清单与任务记录不一致", str(caught.exception))
        self.assertFalse(self.db_path.exists())

        (self.data / "state.json").unlink()
        (self.data / "journey.json").unlink()
        with self.assertRaises(CompanionError) as caught:
            migration.import_json_store(self.project, self.db_path)
        self.assertIn("STATE_MISSING_WITH_RELEASE", str(caught.exception))
        self.assertFalse(self.db_path.exists())

    def test_dry_run_reports_counts_and_writes_nothing(self):
        self.build_legacy()
        before = self.source_bytes()
        first = migration.import_json_store(self.project, self.db_path, dry_run=True)
        second = migration.import_json_store(self.project, self.db_path, dry_run=True)
        self.assertEqual((first["status"], first["dry_run"]), ("planned", True))
        self.assertEqual(first["planned_total"], TOTAL_EVENTS)
        self.assertEqual(first["sources_sha256"], second["sources_sha256"])
        self.assertEqual(self.source_bytes(), before)  # 来源零改动
        self.assertFalse(self.db_path.exists())        # 零写入
        self.write_source("journey.json", "{broken")
        with self.assertRaises(CompanionError):
            migration.import_json_store(self.project, self.db_path, dry_run=True)


class IdempotencyTests(MigrationTestBase):
    def test_repeat_import_dedupes_and_changed_source_is_rejected(self):
        self.build_legacy()
        migration.import_json_store(self.project, self.db_path)
        repeat = migration.import_json_store(self.project, self.db_path)
        self.assertEqual((repeat["status"], repeat["applied"]), ("deduped", 0))
        self.assertEqual(repeat["deduped"], TOTAL_EVENTS)
        store = self.store_counts()
        self.assertEqual(store.query_one("SELECT COUNT(*) AS c FROM events")["c"], TOTAL_EVENTS)
        self.assertEqual(store.query_one("SELECT value FROM store_meta WHERE key = ?",
                                         (db.HEAD_SEQ_KEY,))[0], str(TOTAL_EVENTS))
        store.close()

        # 来源变化后再导：明确拒绝，不混杂新旧，事件数不变
        state = json.loads(self.originals["state.json"].decode("utf-8"))
        state["tasks"]["f-report"]["blocker"] = "阻塞原因已更新"
        self.write_source("state.json", state)
        with self.assertRaises(CompanionError) as caught:
            migration.import_json_store(self.project, self.db_path)
        self.assertIn("重复导入被拒", str(caught.exception))
        store = self.store_counts()
        self.assertEqual(store.query_one("SELECT COUNT(*) AS c FROM events")["c"], TOTAL_EVENTS)


CRASH_CHILD = '''
import os, sys
sys.path.insert(0, {packages!r})
from agents_kernel.storage import db, migration
real_commit = db.Store._commit
counter = {{"commits": 0}}
def crashing_commit(self, conn):
    counter["commits"] += 1
    if counter["commits"] > {limit}:
        os._exit(9)  # 模拟导入中途强杀（跳过一切清理与 atexit）
    real_commit(self, conn)
db.Store._commit = crashing_commit
migration.import_json_store({project!r}, {db_path!r})
'''


class CrashRecoveryTests(MigrationTestBase):
    def test_crash_mid_import_leaves_consistent_prefix_and_rerun_takes_over(self):
        self.build_legacy()
        code = CRASH_CHILD.format(packages=str(REPO_ROOT / "packages"), limit=5,
                                  project=str(self.project), db_path=str(self.db_path))
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 9, proc.stderr)

        store = self.store_counts()  # 强杀后重开：已提交事务完整、无半套数据
        committed = store.query_one("SELECT COUNT(*) AS c FROM events")["c"]
        self.assertEqual(committed, 4)  # 首次提交是 epoch 升级，其后每事件一提交
        self.assertEqual(events_head(store), committed)
        self.assertIsNone(migration.read_manifest(store))  # manifest 未写 → 属崩溃残留
        store.close()

        report = migration.import_json_store(self.project, self.db_path)  # 重跑幂等接管
        self.assertEqual(report["status"], "imported")
        self.assertEqual((report["applied"], report["deduped"]), (TOTAL_EVENTS - 4, 4))
        store = self.store_counts()
        self.assertEqual(store.query_one("SELECT COUNT(*) AS c FROM events")["c"], TOTAL_EVENTS)
        self.assertIsNotNone(migration.read_manifest(store))
        self.assertEqual({item["task_id"]: item["status"]
                          for item in views.list_tasks(store, limit=10)["items"]},
                         {"f-login": "accepted", "f-report": "blocked", "f-export": "pending"})
        store.close()
        self.assertFalse(Path(str(self.db_path) + ".writer").exists())  # 子进程遗留锁已被接管清理


def events_head(store):
    return int(store.query_one("SELECT value FROM store_meta WHERE key = ?",
                               (db.HEAD_SEQ_KEY,))[0])


class FallbackExportTests(MigrationTestBase):
    def test_fallback_export_requires_manifest(self):
        self.build_legacy()
        store = db.Store(self.db_path)
        store.open()
        store.close()
        with self.assertRaises(CompanionError):
            migration.fallback_export(self.project, Path(self.temp.name) / "out")

    def test_exported_state_loads_as_legal_project_via_archives_export_compat(self):
        self.build_legacy()
        # 导入到默认库位（项目 .dev-companion/store.db），export_compat 按默认路径发现它
        migration.import_json_store(self.project, self.data / "store.db")
        before = self.source_bytes()
        out_dir = Path(self.temp.name) / "side-export"
        result = archives.export_compat(self.project, out_dir)  # 经 archives 调用点
        self.assertEqual(set(result["files"]), {"state.json", "journey.json", "release.json"})
        self.assertEqual(self.source_bytes(), before)  # 导出绝不改写原 JSON
        for name in result["files"]:
            draft = json.loads((out_dir / name).read_text(encoding="utf-8"))
            self.assertTrue(draft["exported_from_store"])

        # 模拟切换：原 JSON 移交备份后，草稿放入原位即可被现有 core 读为合法项目
        backup = self.data / "legacy-backup"
        backup.mkdir()
        for name in ("state.json", "journey.json", "release.json"):
            shutil.move(str(self.data / name), str(backup / name))
            shutil.copy(str(out_dir / name), str(self.data / name))
        state = core.Project(self.project).load()
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["project"], str(self.project))
        self.assertEqual([feature["id"] for feature in state["scope"]["features"]],
                         ["f-login", "f-report", "f-export"])
        self.assertEqual(state["tasks"]["f-login"]["status"], "accepted")
        self.assertEqual(state["tasks"]["f-login"]["acceptance"]["verification_id"], "ver-1")
        self.assertEqual(state["tasks"]["f-report"]["status"], "blocked")
        self.assertEqual(state["tasks"]["f-export"]["status"], "pending")

        journey = json.loads((out_dir / "journey.json").read_text(encoding="utf-8"))
        self.assertEqual(journey["schema_version"], 1)
        self.assertEqual(journey["revision"], 3)
        self.assertEqual(list(journey["records"]), ["product"])
        self.assertEqual(journey["history"], [])
        release = json.loads((out_dir / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(release["schema_version"], 1)
        self.assertEqual(release["status"], "prepared")
        self.assertEqual(release["config"]["environment"], "local")
        self.assertEqual(release["binding"]["scope_version"], 2)


if __name__ == "__main__":
    unittest.main()

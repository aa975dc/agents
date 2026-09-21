"""事实存储单测：schema 迁移、单协调写者/epoch、幂等键、CAS、崩溃原子性、分页与规模。

这些测试锁定 P2-02 新增的 agents_kernel.storage/domain 行为（Z13 核心存储）；
数据库一律建在 tempfile 目录，查询绝不触碰源码树（ST06）。
"""
import builtins
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel.domain import views
from agents_kernel.storage import db, events
from agents_kernel.validation import CompanionError


class StorageTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "facts.sqlite"
        self.store = db.Store(self.db_path)
        self.store.open()
        self.addCleanup(self.store.close)

    def acquire(self, store=None):
        writer = db.acquire_writer(store or self.store)
        self.addCleanup(writer.close)
        return writer

    def emit(self, writer, event_type, entity_id, payload, **kwargs):
        return events.append_event(self.store, writer.epoch, event_type=event_type,
                                   entity_id=entity_id, payload=payload, **kwargs)

    def reopen(self):
        self.store.close()
        self.store = db.Store(self.db_path)
        self.store.open()
        self.addCleanup(self.store.close)
        return self.store


class SchemaTests(StorageTestBase):
    def test_open_creates_versioned_schema_and_reopen_is_idempotent(self):
        self.assertEqual(
            self.store.query_one("SELECT MAX(version) AS v FROM schema_version")["v"],
            db.SCHEMA_VERSION)
        self.assertEqual(
            self.store.query_one("SELECT COUNT(*) AS c FROM schema_version")["c"], 1)
        self.assertEqual(
            self.store.query_one("PRAGMA journal_mode")["journal_mode"].lower(), "wal")
        self.reopen()
        self.assertEqual(
            self.store.query_one("SELECT MAX(version) AS v FROM schema_version")["v"], 1)
        self.assertEqual(
            self.store.query_one("SELECT COUNT(*) AS c FROM schema_version")["c"], 1)


class WriterTests(StorageTestBase):
    def test_writer_lock_excludes_second_writer_until_released(self):
        first = self.acquire()
        with self.assertRaises(CompanionError):
            db.acquire_writer(self.store)
        first.close()
        second = self.acquire()
        self.assertEqual(second.epoch, first.epoch + 1)

    def test_stale_lock_from_dead_process_is_taken_over(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()  # 进程已退出，pid 已死
        lock_path = Path(str(self.db_path) + ".writer")
        lock_path.write_text(json.dumps({"pid": proc.pid}), encoding="utf-8")
        writer = self.acquire()
        self.assertEqual(writer.epoch, 1)
        info = json.loads(lock_path.read_text(encoding="utf-8"))
        self.assertEqual(info["pid"], os.getpid())  # 接管后锁内容换成本进程
        writer.close()
        self.assertFalse(lock_path.exists())

    def test_old_epoch_write_rejected_and_changes_nothing(self):
        first = self.acquire()
        self.emit(first, views.EVENT_FEATURE_STATUS, "F-001", {"status": "drafting"},
                  expect_seq=0)
        first.close()
        second = self.acquire()
        with self.assertRaises(CompanionError):
            events.append_event(self.store, first.epoch, event_type=views.EVENT_FEATURE_STATUS,
                                entity_id="F-001", payload={"status": "done"}, expect_seq=1)
        self.assertEqual(events.view_head(self.store), 1)
        self.assertEqual(
            self.store.query_one("SELECT COUNT(*) AS c FROM events")["c"], 1)
        self.assertEqual(views.list_features(self.store)["items"][0]["status"], "drafting")
        self.emit(second, views.EVENT_FEATURE_STATUS, "F-001", {"status": "done"},
                  expect_seq=1)
        self.assertEqual(events.view_head(self.store), 2)


class EventTests(StorageTestBase):
    def test_idempotency_key_dedupes_replay(self):
        writer = self.acquire()
        first = self.emit(writer, views.EVENT_FEATURE_STATUS, "F-001",
                          {"status": "drafting"}, idempotency_key="biz-1", expect_seq=0)
        self.assertTrue(first["applied"])
        replay = self.emit(writer, views.EVENT_FEATURE_STATUS, "F-001",
                           {"status": "done"}, idempotency_key="biz-1", expect_seq=99)
        self.assertFalse(replay["applied"])
        self.assertTrue(replay["deduped"])
        self.assertEqual(replay["seq"], first["seq"])  # 重放不产生新事件、不校验新 CAS
        self.assertEqual(events.view_head(self.store), 1)
        self.assertEqual(
            self.store.query_one("SELECT COUNT(*) AS c FROM events")["c"], 1)
        self.assertEqual(views.list_features(self.store)["items"][0]["status"], "drafting")

    def test_unknown_event_type_rejected_before_any_write(self):
        writer = self.acquire()
        with self.assertRaises(CompanionError):
            self.emit(writer, "hack_event", "X-1", {"status": "x"})
        self.assertEqual(events.view_head(self.store), 0)
        self.assertEqual(
            self.store.query_one("SELECT COUNT(*) AS c FROM events")["c"], 0)

    def test_all_event_types_fold_into_views(self):
        writer = self.acquire()
        self.emit(writer, views.EVENT_FEATURE_STATUS, "F-001",
                  {"status": "drafting", "title": "登录"}, expect_seq=0)
        self.emit(writer, views.EVENT_SCOPE_REVISION, "F-001",
                  {"scope_version": 3, "items": ["a.py", "b.py"]}, expect_seq=1)
        self.emit(writer, views.EVENT_TASK_STATUS, "T-1",
                  {"feature_id": "F-001", "status": "ready"}, expect_seq=2)
        self.emit(writer, views.EVENT_EVIDENCE_REGISTERED, "E-1",
                  {"kind": "check", "subject_id": "T-1", "result": "passed",
                   "detail": "pytest 147 green"}, expect_seq=3)
        self.emit(writer, views.EVENT_RELEASE_STAGE, "R-1",
                  {"feature_id": "F-001", "stage": "candidate"}, expect_seq=4)
        self.assertEqual(events.view_head(self.store), 5)
        feature = views.list_features(self.store)["items"][0]
        self.assertEqual(feature["feature_id"], "F-001")
        self.assertEqual(feature["title"], "登录")
        self.assertEqual(feature["status"], "drafting")
        self.assertEqual(feature["scope_version"], 3)
        self.assertEqual(feature["scope"], ["a.py", "b.py"])
        self.assertEqual(feature["updated_seq"], 2)
        task = views.list_tasks(self.store)["items"][0]
        self.assertEqual((task["feature_id"], task["status"]), ("F-001", "ready"))
        evidence = views.list_evidence(self.store)["items"][0]
        self.assertEqual((evidence["kind"], evidence["subject_id"], evidence["result"]),
                         ("check", "T-1", "passed"))
        release = views.list_releases(self.store)["items"][0]
        self.assertEqual((release["feature_id"], release["stage"]), ("F-001", "candidate"))
        # 视图行只有一行/实体：事件是历史，视图是当前状态
        for table, count in (("feature_view", 1), ("task_view", 1),
                             ("evidence_view", 1), ("release_view", 1)):
            self.assertEqual(
                self.store.query_one("SELECT COUNT(*) AS c FROM %s" % table)["c"], count)


class CasAndCrashTests(StorageTestBase):
    def test_stale_expect_seq_rejected_and_view_unchanged(self):
        writer = self.acquire()
        self.emit(writer, views.EVENT_FEATURE_STATUS, "F-001", {"status": "drafting"},
                  expect_seq=0)
        self.emit(writer, views.EVENT_FEATURE_STATUS, "F-001", {"status": "review"},
                  expect_seq=1)
        with self.assertRaises(CompanionError):
            self.emit(writer, views.EVENT_FEATURE_STATUS, "F-001", {"status": "done"},
                      expect_seq=1)  # head 已是 2，过期
        self.assertEqual(events.view_head(self.store), 2)
        self.assertEqual(
            self.store.query_one("SELECT COUNT(*) AS c FROM events")["c"], 2)
        self.assertEqual(views.list_features(self.store)["items"][0]["status"], "review")

    def test_crash_before_commit_keeps_confirmed_facts_and_replays_once(self):
        writer = self.acquire()
        self.emit(writer, views.EVENT_FEATURE_STATUS, "F-001", {"status": "drafting"},
                  idempotency_key="biz-draft", expect_seq=0)
        writer.close()
        real_commit = self.store._commit

        def crashing_commit(conn):
            raise OSError("模拟提交前进程崩溃")

        with mock.patch.object(self.store, "_commit", crashing_commit):
            with self.assertRaises(OSError):
                self.emit(writer, views.EVENT_FEATURE_STATUS, "F-001",
                          {"status": "review"}, idempotency_key="biz-review",
                          expect_seq=1)
        reopened = self.reopen()
        # 已确认事实完整；崩溃的事务无痕（无事件、视图与 head 均停在 seq=1）
        self.assertEqual(events.view_head(reopened), 1)
        self.assertEqual(reopened.query_one("SELECT COUNT(*) AS c FROM events")["c"], 1)
        feature = views.list_features(reopened)["items"][0]
        self.assertEqual((feature["status"], feature["updated_seq"]), ("drafting", 1))
        # 恢复后重放同一业务意图：恰好应用一次，不双重
        replay_writer = self.acquire(reopened)
        result = events.append_event(reopened, replay_writer.epoch,
                                     event_type=views.EVENT_FEATURE_STATUS,
                                     entity_id="F-001", payload={"status": "review"},
                                     idempotency_key="biz-review", expect_seq=1)
        self.assertTrue(result["applied"])
        self.assertEqual(events.view_head(reopened), 2)
        self.assertEqual(reopened.query_one("SELECT COUNT(*) AS c FROM events")["c"], 2)
        feature = views.list_features(reopened)["items"][0]
        self.assertEqual((feature["status"], feature["updated_seq"]), ("review", 2))

    def test_uncommitted_connection_leaves_no_trace_after_reopen(self):
        writer = self.acquire()
        self.emit(writer, views.EVENT_FEATURE_STATUS, "F-001", {"status": "drafting"},
                  expect_seq=0)
        raw = sqlite3.connect(str(self.db_path), isolation_level=None)
        try:
            raw.execute("BEGIN IMMEDIATE")
            raw.execute(
                """INSERT INTO events (event_id, entity_type, entity_id, event_type,
                                       payload, idempotency_key, writer_epoch, created_at)
                   VALUES ('fake', 'feature', 'F-009', 'feature_status', '{}',
                           NULL, 1, '1970')""")
            # 不提交，直接关闭连接模拟进程死亡
        finally:
            raw.close()
        reopened = self.reopen()
        self.assertEqual(reopened.query_one("SELECT COUNT(*) AS c FROM events")["c"], 1)
        self.assertEqual(events.view_head(reopened), 1)


class PaginationTests(StorageTestBase):
    def test_view_pagination_round_trips(self):
        writer = self.acquire()
        for index in range(25):
            self.emit(writer, views.EVENT_FEATURE_STATUS, "F-%03d" % index,
                      {"status": "drafting"}, expect_seq=index)
        for index in range(4):
            self.emit(writer, views.EVENT_TASK_STATUS, "T-%d" % index,
                      {"feature_id": "F-%03d" % index, "status": "ready"},
                      expect_seq=25 + index)
        pages = [views.list_features(self.store, offset=off, limit=10)
                 for off in (0, 10, 20)]
        self.assertTrue(pages[0]["has_more"] and pages[1]["has_more"])
        self.assertFalse(pages[2]["has_more"])
        self.assertEqual([len(page["items"]) for page in pages], [10, 10, 5])
        seen = [item["feature_id"] for page in pages for item in page["items"]]
        self.assertEqual(seen, ["F-%03d" % i for i in range(25)])  # 顺序稳定、无重无漏
        mine = views.list_tasks(self.store, limit=10, feature_id="F-001")["items"]
        self.assertEqual([item["task_id"] for item in mine], ["T-1"])

    def test_read_events_keyset_paging(self):
        writer = self.acquire()
        for index in range(9):
            self.emit(writer, views.EVENT_FEATURE_STATUS, "F-%03d" % index,
                      {"status": "drafting"}, expect_seq=index)
        self.emit(writer, views.EVENT_SCOPE_REVISION, "F-000",
                  {"scope_version": 1, "items": []}, expect_seq=9)
        page1 = events.read_events(self.store, limit=5)
        page2 = events.read_events(self.store, limit=5,
                                   after_seq=page1["items"][-1]["seq"])
        self.assertTrue(page1["has_more"] and page2["has_more"])
        seqs = [item["seq"] for item in page1["items"] + page2["items"]]
        self.assertEqual(seqs, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        only_scope = events.read_events(self.store, entity_type="feature")  # 实体过滤
        self.assertEqual(len(only_scope["items"]), 10)

    def test_read_queries_touch_no_source_tree(self):
        writer = self.acquire()
        for index in range(6):
            self.emit(writer, views.EVENT_FEATURE_STATUS, "F-%03d" % index,
                      {"status": "drafting"}, expect_seq=index)
        recorded = []
        real_open = builtins.open

        def recording_open(file, *args, **kwargs):
            recorded.append(str(file))
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", recording_open):
            views.list_features(self.store, offset=0, limit=3)
            views.list_tasks(self.store, offset=3, limit=3)
            views.list_evidence(self.store)
            views.list_releases(self.store)
            events.read_events(self.store, limit=3)
            events.view_head(self.store)
        self.assertEqual(
            [path for path in recorded if str(REPO_ROOT) in path], [], recorded)

    def test_single_view_read_does_not_replay_events(self):
        writer = self.acquire()
        for index in range(1, 51):
            self.emit(writer, views.EVENT_SCOPE_REVISION, "F-001",
                      {"scope_version": index, "items": ["a.py", "b.py"]},
                      expect_seq=index - 1)
        statements = []
        self.store._conn.set_trace_callback(statements.append)
        try:
            page = views.list_features(self.store, limit=5)
        finally:
            self.store._conn.set_trace_callback(None)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["scope_version"], 50)
        self.assertTrue(statements, "读取应产生 SQL")
        self.assertFalse(any("events" in sql.lower() for sql in statements), statements)
        plan = self.store.query_all(
            "EXPLAIN QUERY PLAN SELECT * FROM feature_view "
            "ORDER BY feature_id LIMIT 5 OFFSET 0")
        self.assertTrue(any("feature_view" in row["detail"] for row in plan))
        self.assertFalse(any("events" in row["detail"] for row in plan))


class ScaleTests(StorageTestBase):
    def test_thousand_events_stay_linear_and_views_materialized(self):
        writer = self.acquire()
        payload = {"items": ["src/module_a.py", "src/module_b.py", "docs/notes.md"]}
        for index in range(1, 101):
            self.emit(writer, views.EVENT_SCOPE_REVISION, "F-scale",
                      dict(payload, scope_version=index), expect_seq=index - 1)
        self.store.checkpoint()
        size_at_100 = self.db_path.stat().st_size
        for index in range(101, 1001):
            self.emit(writer, views.EVENT_SCOPE_REVISION, "F-scale",
                      dict(payload, scope_version=index), expect_seq=index - 1)
        self.store.checkpoint()
        size_at_1000 = self.db_path.stat().st_size
        self.assertEqual(
            self.store.query_one("SELECT COUNT(*) AS c FROM events")["c"], 1000)
        self.assertEqual(events.view_head(self.store), 1000)
        # 事件 payload 恒定大小（不嵌套拷贝历史——journey 全量重写的病根）
        self.assertLess(
            self.store.query_one("SELECT MAX(LENGTH(payload)) AS m FROM events")["m"], 1000)
        # 库文件随事件数近线性增长（全量重写式存储此比值趋近 100）
        self.assertGreaterEqual(size_at_1000, size_at_100)
        self.assertLess(size_at_1000, size_at_100 * 30,
                        "库文件增长疑似超线性：%d -> %d" % (size_at_100, size_at_1000))
        # 视图始终是折叠后的当前状态：单行、最终 scope_version
        features = views.list_features(self.store)["items"]
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["scope_version"], 1000)
        self.assertEqual(features[0]["scope"], payload["items"])


if __name__ == "__main__":
    unittest.main()

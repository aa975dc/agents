# -*- coding: utf-8 -*-
"""事实库对抗性安全回归（SEC03）：旧写者 epoch、伪造 CAS、幂等矛盾重放、库文件完整性。

复验 P2-02 单写者 epoch 并做对抗性加强（伪造高 seq 绕不过 CAS）；幂等矛盾重放
锁定设计口径——写侧 deduped 不比较内容（storage/events_idem 模块 docstring 声明），
消费方必须用 canonical_digest 核对后拒绝；库文件被替换为目录/损坏字节时得到
结构化错误；注入字符串进 payload/entity 字段由参数化查询天然隔离。
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel.storage import db, events, events_idem
from agents_kernel.validation import CompanionError

VT = "scope_revision"


def scope_payload(version):
    return {"scope_version": version, "items": ["src/a.py"]}


class StoreSecurityTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "facts.sqlite"

    def open_store(self, path=None):
        store = db.Store(self.db_path if path is None else path)
        store.open()
        self.addCleanup(store.close)
        return store

    def acquire(self, store):
        writer = db.acquire_writer(store)
        self.addCleanup(writer.close)
        return writer


class EpochSecurityTests(StoreSecurityTestBase):
    """P2-02 复验 + 对抗性：任何携带非当前 epoch / 错误 CAS 的写都零落盘。"""

    def test_stale_writer_epoch_rejected_and_nothing_written(self):
        store = self.open_store()
        w1 = self.acquire(store)
        first = events.append_event(store, w1.epoch, event_type=VT,
                                    entity_id="p", payload=scope_payload(1))
        w1.close()
        w2 = self.acquire(store)  # epoch 单调递增：w1 的 epoch 已作废
        self.assertGreater(w2.epoch, w1.epoch)
        with self.assertRaises(CompanionError):
            events.append_event(store, w1.epoch, event_type=VT,
                                entity_id="p", payload=scope_payload(2))
        self.assertEqual(events.view_head(store), first["seq"])
        self.assertEqual(len(store.query_all("SELECT * FROM events")), 1)
        row = store.query_one("SELECT scope_version FROM feature_view WHERE feature_id = 'p'")
        self.assertEqual(tuple(row), (1,))

    def test_forged_high_epoch_rejected(self):
        store = self.open_store()
        writer = self.acquire(store)
        with self.assertRaises(CompanionError):
            events.append_event(store, 999, event_type=VT,
                                entity_id="p", payload=scope_payload(1))
        self.assertEqual(events.view_head(store), 0)
        self.assertEqual(len(store.query_all("SELECT * FROM events")), 0)

    def test_forged_high_expect_seq_cannot_bypass_cas(self):
        """对抗性：伪造 expect_seq=head+N 抢写未来位次 → CAS 拒绝，不落任何变更。"""
        store = self.open_store()
        writer = self.acquire(store)
        first = events.append_event(store, writer.epoch, event_type=VT,
                                    entity_id="p", payload=scope_payload(1))
        with self.assertRaises(CompanionError):
            events.append_event(store, writer.epoch, event_type=VT,
                                entity_id="p", payload=scope_payload(2),
                                expect_seq=first["seq"] + 500)
        self.assertEqual(events.view_head(store), first["seq"])
        self.assertEqual(len(store.query_all("SELECT * FROM events")), 1)


class IdempotencyConflictTests(StoreSecurityTestBase):
    """幂等键：同内容去重放行；同键异 payload 必须可被消费方判定并拒绝。"""

    def test_same_key_same_payload_dedupes_with_matching_digest(self):
        store = self.open_store()
        writer = self.acquire(store)
        first = events.append_event(store, writer.epoch, event_type=VT, entity_id="p",
                                    payload=scope_payload(1), idempotency_key="k1")
        replay = events.append_event(store, writer.epoch, event_type=VT, entity_id="p",
                                     payload=scope_payload(1), idempotency_key="k1")
        self.assertTrue(replay["deduped"] and not replay["applied"])
        self.assertEqual(replay["seq"], first["seq"])
        old = events_idem.event_by_idempotency_key(store, "k1")
        self.assertEqual(events_idem.canonical_digest(old["payload"]),
                         events_idem.canonical_digest(scope_payload(1)))

    def test_same_key_conflicting_payload_is_detectable_and_never_double_applied(self):
        store = self.open_store()
        writer = self.acquire(store)
        events.append_event(store, writer.epoch, event_type=VT, entity_id="p",
                            payload=scope_payload(1), idempotency_key="k1")
        replay = events.append_event(store, writer.epoch, event_type=VT, entity_id="p",
                                     payload=scope_payload(2), idempotency_key="k1")
        # 写侧真实行为：deduped=True、视图与 head 不动（设计口径见 events_idem docstring）
        self.assertTrue(replay["deduped"] and not replay["applied"])
        old = events_idem.event_by_idempotency_key(store, "k1")
        # 消费方口径：canonical_digest 不一致 → 必须按矛盾重放拒绝
        self.assertNotEqual(events_idem.canonical_digest(old["payload"]),
                            events_idem.canonical_digest(scope_payload(2)))
        self.assertEqual(events.view_head(store), replay["seq"])
        self.assertEqual(len(store.query_all("SELECT * FROM events")), 1)
        row = store.query_one("SELECT scope_version FROM feature_view WHERE feature_id = 'p'")
        self.assertEqual(tuple(row), (1,))


class DatabaseIntegrityTests(StoreSecurityTestBase):
    """库文件被调包/损坏 → 结构化错误；注入字符串经参数化查询天然隔离。"""

    def test_directory_at_db_path_raises_structured_error(self):
        dir_path = self.db_path.with_suffix(".d")
        dir_path.mkdir()
        store = db.Store(dir_path)
        with self.assertRaises(sqlite3.OperationalError):
            store.open()
        self.assertIsNone(store._conn)
        store.close()

    def test_corrupted_db_file_raises_structured_error(self):
        self.db_path.write_bytes(b"definitely not a sqlite database" * 32)
        store = db.Store(self.db_path)
        with self.assertRaises(sqlite3.DatabaseError):
            store.open()
        self.assertIsNone(store._conn)
        store.close()

    def test_sql_injection_in_entity_and_payload_fields_is_parameterized(self):
        store = self.open_store()
        writer = self.acquire(store)
        evil_entity = "p'; DROP TABLE events; --"
        evil_payload = {"scope_version": 3, "items": ["x'); DROP TABLE feature_view; --"]}
        events.append_event(store, writer.epoch, event_type=VT,
                            entity_id=evil_entity, payload=evil_payload)
        found = events.read_events(store, entity_id=evil_entity)
        self.assertEqual(len(found["items"]), 1)  # 注入串按字面数据存取
        self.assertEqual(found["items"][0]["entity_id"], evil_entity)
        self.assertEqual(len(store.query_all("SELECT * FROM events")), 1)
        self.assertEqual(len(store.query_all("SELECT * FROM feature_view")), 1)
        self.assertEqual(events.view_head(store), 1)


if __name__ == "__main__":
    unittest.main()

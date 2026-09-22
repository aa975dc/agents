"""事件追加（append-only）与 CAS 引用更新（stdlib only, Python 3.9+）。

写路径是单事务：幂等键去重 → epoch 校验（db.require_epoch）→ CAS（expect_seq 对比
view_head_seq，不符即拒、整体回滚）→ 事件插入（AUTOINCREMENT 单调 seq）→ 视图折叠
（domain.views.apply_event）→ head 前移，同一事务提交。因此"CAS 落盘后、DB 提交前"
崩溃在恢复后既不丢已确认事实、也不会双重应用——sqlite 事务原子性天然达成
（tests/test_storage.py 注入提交失败证明）。

只暴露协调者入口：所有写函数要求 acquire_writer 颁发的 epoch；读函数（view_head、
read_events）无副作用。大 payload 不进事件——事件只放类型化小 JSON，重物放 CAS 文件
再按 hash 引用（07_STORAGE_MIGRATION_AND_COMPAT §2，由上层组合）。
"""
import json
import uuid

from agents_kernel.domain import views
from agents_kernel.storage import db
from agents_kernel.validation import CompanionError, text


def append_event(store, epoch, *, event_type, entity_id, payload,
                 idempotency_key=None, expect_seq=None):
    """追加一条事件并同事务折叠进视图；返回 {seq, event_id, applied, deduped}。

    - idempotency_key：同键重放去重，返回原事件（applied=False），视图与 head 不动。
    - expect_seq：CAS——要求当前视图 head 等于该值，否则拒绝且不落任何变更。
    """
    if not isinstance(payload, dict):
        raise CompanionError("事件 payload 必须是对象")
    event_type = text(event_type, "事件类型")
    entity_id = text(entity_id, "实体标识")
    entity_type = views.entity_type_for(event_type)  # 未知类型开事务前即拒
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with store.transaction() as conn:
        db.require_epoch(conn, epoch)
        if idempotency_key is not None:
            idempotency_key = text(idempotency_key, "幂等键")
            existing = conn.execute(
                "SELECT seq, event_id FROM events WHERE idempotency_key = ?",
                (idempotency_key,)).fetchone()
            if existing is not None:
                return {"seq": existing[0], "event_id": existing[1],
                        "applied": False, "deduped": True}
        head = int(db.get_meta(conn, db.HEAD_SEQ_KEY, "0"))
        if expect_seq is not None and expect_seq != head:
            raise CompanionError("CAS 冲突：期望 head=%r，实际 head=%d" % (expect_seq, head))
        event_id = uuid.uuid4().hex
        cursor = conn.execute(
            """INSERT INTO events
               (event_id, entity_type, entity_id, event_type, payload,
                idempotency_key, writer_epoch, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, entity_type, entity_id, event_type, serialized,
             idempotency_key, int(epoch), db.utcnow()))
        seq = cursor.lastrowid
        views.apply_event(conn, seq, event_type, entity_id, payload)
        db.set_meta(conn, db.HEAD_SEQ_KEY, seq)
    return {"seq": seq, "event_id": event_id, "applied": True, "deduped": False}


def view_head(store):
    """当前视图 head：已折叠到的最新事件 seq（未写事件时为 0）。"""
    row = store.query_one("SELECT value FROM store_meta WHERE key = ?", (db.HEAD_SEQ_KEY,))
    return int(row[0]) if row is not None else 0


def read_events(store, *, entity_type=None, entity_id=None, after_seq=0, limit=200):
    """事件审计分页（keyset：seq > after_seq），只读。"""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise CompanionError("limit 必须在 1..1000")
    clauses, params = [], []
    if entity_type is not None:
        clauses.append("entity_type = ?")
        params.append(text(entity_type, "实体类型"))
    if entity_id is not None:
        clauses.append("entity_id = ?")
        params.append(text(entity_id, "实体标识"))
    if after_seq:
        if not isinstance(after_seq, int) or isinstance(after_seq, bool) or after_seq < 0:
            raise CompanionError("after_seq 必须是非负整数")
        clauses.append("seq > ?")
        params.append(after_seq)
    sql = "SELECT * FROM events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY seq LIMIT ?"
    rows = store.query_all(sql, tuple(params) + (limit,))
    items = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        items.append(item)
    return {"items": items, "has_more": len(rows) == limit}

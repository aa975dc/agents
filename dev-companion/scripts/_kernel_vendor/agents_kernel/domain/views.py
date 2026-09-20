"""当前视图物化与只读分页查询（stdlib only, Python 3.9+）。

事件语义最小集（够测试与 P2-03/P2-04 接入即可）：feature/task 状态变更、scope 修订、
验收/检查证据登记、release 阶段——每类一个事件类型，折叠（apply_event）进对应物化表。
新事件类型在此登记折叠规则、实体归类与视图字段。

查询面只读物化表（LIMIT/OFFSET 分页），绝不重放 events、绝不写库、绝不触碰源码树
（ST06）。禁止 worker 直改台账：本模块不提供任何写入口；唯一变更路径是
storage.events.append_event（要求协调者 epoch），折叠与其同事务提交。
"""
import json

from agents_kernel.validation import CompanionError, text

EVENT_FEATURE_STATUS = "feature_status"
EVENT_SCOPE_REVISION = "scope_revision"
EVENT_TASK_STATUS = "task_status"
EVENT_EVIDENCE_REGISTERED = "evidence_registered"
EVENT_RELEASE_STAGE = "release_stage"

_ENTITY_TYPES = {
    EVENT_FEATURE_STATUS: "feature",
    EVENT_SCOPE_REVISION: "feature",
    EVENT_TASK_STATUS: "task",
    EVENT_EVIDENCE_REGISTERED: "evidence",
    EVENT_RELEASE_STAGE: "release",
}

_EVIDENCE_KINDS = ("acceptance", "check")
_MAX_LIMIT = 500


def entity_type_for(event_type):
    try:
        return _ENTITY_TYPES[event_type]
    except KeyError:
        raise CompanionError("未知事件类型：%s" % event_type)


def apply_event(conn, seq, event_type, entity_id, payload):
    """把一条事件折叠进物化视图（在 events 的写事务内调用）。"""
    if event_type == EVENT_FEATURE_STATUS:
        _apply_feature_status(conn, seq, entity_id, payload)
    elif event_type == EVENT_SCOPE_REVISION:
        _apply_scope_revision(conn, seq, entity_id, payload)
    elif event_type == EVENT_TASK_STATUS:
        _apply_task_status(conn, seq, entity_id, payload)
    elif event_type == EVENT_EVIDENCE_REGISTERED:
        _apply_evidence_registered(conn, seq, entity_id, payload)
    elif event_type == EVENT_RELEASE_STAGE:
        _apply_release_stage(conn, seq, entity_id, payload)
    else:
        raise CompanionError("未知事件类型：%s" % event_type)


def _apply_feature_status(conn, seq, feature_id, payload):
    title = payload.get("title", "")
    if not isinstance(title, str):
        raise CompanionError("title 必须是字符串")
    status = text(payload.get("status"), "功能状态")
    conn.execute(
        """INSERT INTO feature_view (feature_id, title, status, scope_version, scope_json, updated_seq)
           VALUES (?, ?, ?, 0, '[]', ?)
           ON CONFLICT(feature_id) DO UPDATE SET
             title = CASE WHEN excluded.title = '' THEN feature_view.title ELSE excluded.title END,
             status = excluded.status,
             updated_seq = excluded.updated_seq""",
        (feature_id, title.strip(), status, seq))


def _apply_scope_revision(conn, seq, feature_id, payload):
    version = payload.get("scope_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise CompanionError("scope_version 必须是正整数")
    items = json.dumps(_strings(payload.get("items"), "范围条目"),
                       ensure_ascii=False, sort_keys=True)
    conn.execute(
        """INSERT INTO feature_view (feature_id, title, status, scope_version, scope_json, updated_seq)
           VALUES (?, '', 'unknown', ?, ?, ?)
           ON CONFLICT(feature_id) DO UPDATE SET
             scope_version = excluded.scope_version,
             scope_json = excluded.scope_json,
             updated_seq = excluded.updated_seq""",
        (feature_id, version, items, seq))


def _apply_task_status(conn, seq, task_id, payload):
    conn.execute(
        """INSERT INTO task_view (task_id, feature_id, status, updated_seq)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(task_id) DO UPDATE SET
             feature_id = excluded.feature_id,
             status = excluded.status,
             updated_seq = excluded.updated_seq""",
        (task_id, text(payload.get("feature_id"), "所属功能"), text(payload.get("status"), "任务状态"), seq))


def _apply_evidence_registered(conn, seq, evidence_id, payload):
    kind = payload.get("kind")
    if kind not in _EVIDENCE_KINDS:
        raise CompanionError("证据 kind 必须是 acceptance 或 check")
    detail = payload.get("detail", "")
    if not isinstance(detail, str):
        raise CompanionError("detail 必须是字符串")
    conn.execute(
        """INSERT INTO evidence_view (evidence_id, kind, subject_id, result, detail, updated_seq)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(evidence_id) DO UPDATE SET
             result = excluded.result,
             detail = excluded.detail,
             updated_seq = excluded.updated_seq""",
        (evidence_id, kind, text(payload.get("subject_id"), "证据主体"),
         text(payload.get("result"), "证据结果"), detail, seq))


def _apply_release_stage(conn, seq, release_id, payload):
    conn.execute(
        """INSERT INTO release_view (release_id, feature_id, stage, updated_seq)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(release_id) DO UPDATE SET
             feature_id = excluded.feature_id,
             stage = excluded.stage,
             updated_seq = excluded.updated_seq""",
        (release_id, text(payload.get("feature_id"), "所属功能"), text(payload.get("stage"), "发布阶段"), seq))


def _strings(value, label):
    if not isinstance(value, list):
        raise CompanionError(label + "必须是列表")
    return [text(item, label) for item in value]


def _check_page(offset, limit):
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise CompanionError("offset 必须是非负整数")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_LIMIT:
        raise CompanionError("limit 必须在 1..%d" % _MAX_LIMIT)


def list_features(store, *, offset=0, limit=50):
    _check_page(offset, limit)
    rows = store.query_all(
        "SELECT * FROM feature_view ORDER BY feature_id LIMIT ? OFFSET ?", (limit, offset))
    items = []
    for row in rows:
        item = dict(row)
        item["scope"] = json.loads(item.pop("scope_json"))
        items.append(item)
    return {"items": items, "has_more": len(rows) == limit}


def list_tasks(store, *, offset=0, limit=50, feature_id=None):
    _check_page(offset, limit)
    sql, params = "SELECT * FROM task_view", []
    if feature_id is not None:
        sql += " WHERE feature_id = ?"
        params.append(text(feature_id, "功能编号"))
    sql += " ORDER BY task_id LIMIT ? OFFSET ?"
    rows = store.query_all(sql, tuple(params) + (limit, offset))
    return {"items": [dict(row) for row in rows], "has_more": len(rows) == limit}


def list_evidence(store, *, offset=0, limit=50, kind=None, subject_id=None):
    _check_page(offset, limit)
    clauses, params = [], []
    if kind is not None:
        if kind not in _EVIDENCE_KINDS:
            raise CompanionError("证据 kind 必须是 acceptance 或 check")
        clauses.append("kind = ?")
        params.append(kind)
    if subject_id is not None:
        clauses.append("subject_id = ?")
        params.append(text(subject_id, "证据主体"))
    sql = "SELECT * FROM evidence_view"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY evidence_id LIMIT ? OFFSET ?"
    rows = store.query_all(sql, tuple(params) + (limit, offset))
    return {"items": [dict(row) for row in rows], "has_more": len(rows) == limit}


def list_releases(store, *, offset=0, limit=50, feature_id=None):
    _check_page(offset, limit)
    sql, params = "SELECT * FROM release_view", []
    if feature_id is not None:
        sql += " WHERE feature_id = ?"
        params.append(text(feature_id, "功能编号"))
    sql += " ORDER BY release_id LIMIT ? OFFSET ?"
    rows = store.query_all(sql, tuple(params) + (limit, offset))
    return {"items": [dict(row) for row in rows], "has_more": len(rows) == limit}

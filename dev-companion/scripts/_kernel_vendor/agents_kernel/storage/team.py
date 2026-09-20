"""team 事实库的 CLI 适配层（R02：新 team 模式的真实用户入口）。

旧 legacy 命令（init/status/check/…）走 core.py 的三 JSON，完全不感知本模块；
team-* 命令经这里落到 SQLite 事实库（storage.db + domain.views 物化视图）。
每项目存储选择规则（07_STORAGE_MIGRATION_AND_COMPAT §4"按阶段激活，不一次强制
升级"）：项目 .dev-companion/team.db 存在 → team 命令用它；不存在 → 仅 team-init
创建，其余 team 命令明确报错（不静默造空库）。legacy 三 JSON 永不被本模块读写
（team-migrate 只读导入，原文件不动；回退 = 删 team.db，legacy 原样可用）。

事实源唯一：feature/task 状态只经 events.append_event 追加（单协调写者 epoch +
可选 CAS），读取只走物化视图分页（views.list_features/list_tasks，LIMIT/OFFSET），
不重放事件、不现场 COUNT 全表。
"""
from pathlib import Path

from agents_kernel.domain import views
from agents_kernel.domain.tasks import TASK_STATUSES
from agents_kernel.storage import db, events, migration
from agents_kernel.validation import CompanionError, feature_id, text

TEAM_DB_FILENAME = "team.db"
# 读分页上限与 views._MAX_LIMIT 对齐（本模块不复制第二套校验，直接复用其查询函数）。
_MAX_LIMIT = 500


def team_db_path(project_dir):
    root = Path(project_dir)
    if not root.is_dir():
        raise CompanionError("项目目录不存在：" + str(root))
    return root / ".dev-companion" / TEAM_DB_FILENAME


def _open_existing(path):
    """打开既有 team 事实库；不存在时报错引导 team-init，绝不静默建空库。"""
    if not path.exists():
        raise CompanionError("team 事实库不存在：%s；请先运行 team-init" % path)
    store = db.Store(path)
    store.open()
    return store


def init_feature(project_dir, feature_id_value, title):
    """team-init：建库（首次）+ 登记 feature（feature_status 事件，epoch 注册写者）。

    同 feature 重复 init 以幂等键去重（applied=False），事实不变。
    """
    fid = feature_id(text(feature_id_value, "功能编号"), set())
    title = text(title, "功能标题")
    path = team_db_path(project_dir)
    store = db.Store(path)
    store.open()
    try:
        writer = db.acquire_writer(store)
        try:
            result = events.append_event(
                store, writer.epoch, event_type=views.EVENT_FEATURE_STATUS,
                entity_id=fid, payload={"title": title, "status": "draft"},
                idempotency_key="team-init:" + fid)
        finally:
            writer.close()
        return {"store": str(path), "feature_id": fid, "title": title,
                "status": "draft", "seq": result["seq"], "applied": result["applied"],
                "generation": events.view_head(store)}
    finally:
        store.close()


def set_task_status(project_dir, task_id, feature_id_value, status, *, expect_seq=None):
    """team-task：经 task_status 事件改任务状态（单协调写者；expect_seq 给定时 CAS）。

    expect_seq = 调用方从 team-status 读到的 generation：与当前视图 head 不符即拒
    （整体回滚，不落任何变更）。状态词按 domain.tasks 白名单收紧。
    """
    tid = text(task_id, "任务编号")
    fid = text(feature_id_value, "功能编号")
    if status not in TASK_STATUSES:
        raise CompanionError("未知任务状态：%s（允许：%s）" % (status, "/".join(TASK_STATUSES)))
    if expect_seq is not None and (isinstance(expect_seq, bool) or not isinstance(expect_seq, int)
                                   or expect_seq < 0):
        raise CompanionError("expect_seq 必须是非负整数")
    path = team_db_path(project_dir)
    store = _open_existing(path)
    try:
        if store.query_one("SELECT feature_id FROM feature_view WHERE feature_id = ?", (fid,)) is None:
            raise CompanionError("功能 %s 未在 team 事实库登记；请先 team-init" % fid)
        writer = db.acquire_writer(store)
        try:
            key = None if expect_seq is None else "team-task:%s:%s@%d" % (tid, status, expect_seq)
            result = events.append_event(
                store, writer.epoch, event_type=views.EVENT_TASK_STATUS,
                entity_id=tid, payload={"feature_id": fid, "status": status},
                idempotency_key=key, expect_seq=expect_seq)
        finally:
            writer.close()
        applied = result["applied"]
        return {"store": str(path), "task_id": tid, "feature_id": fid, "status": status,
                "seq": result["seq"], "applied": applied,
                "deduped": result["deduped"], "generation": events.view_head(store)}
    finally:
        store.close()


def read_status(project_dir, *, offset=0, limit=50):
    """team-status：物化视图分页只读（features/tasks 同一游标），含 generation 与事实源路径。"""
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise CompanionError("offset 必须是非负整数")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_LIMIT:
        raise CompanionError("limit 必须在 1..%d" % _MAX_LIMIT)
    path = team_db_path(project_dir)
    store = _open_existing(path)
    try:
        return {"store": str(path), "generation": events.view_head(store),
                "offset": offset, "limit": limit,
                "features": views.list_features(store, offset=offset, limit=limit),
                "tasks": views.list_tasks(store, offset=offset, limit=limit)}
    finally:
        store.close()


def migrate_from_json(project_dir, *, dry_run=False):
    """team-migrate：旧三 JSON → team.db 一次性导入（复用 storage.migration，不重实现）。

    只读原 JSON、只写 team.db（manifest 与事件同库）；dry_run 零写入。回退 =
    删除 team.db（含 -wal/-shm/.writer），legacy 原样可用。
    """
    path = team_db_path(project_dir)
    report = migration.import_json_store(project_dir, path, dry_run=dry_run)
    return dict(report, store=str(path))

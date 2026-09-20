"""team 事实库的 CLI 适配层（R02：新 team 模式的真实用户入口）。

旧 legacy 命令（init/status/check/…）走 core.py 的三 JSON，完全不感知本模块；
team-* 命令经这里落到 SQLite 事实库（storage.db + domain.views 物化视图）。
每项目存储选择规则（07_STORAGE_MIGRATION_AND_COMPAT §4"按阶段激活，不一次强制
升级"）：项目 .dev-companion/team.db 存在 → team 命令用它；不存在 → 仅 team-init
创建，其余 team 命令明确报错（不静默造空库）。legacy 三 JSON 永不被本模块读写
（team-migrate 只读导入，原文件不动；回退 = 删 team.db，legacy 原样可用）。

事实源唯一：feature/task 状态只经 events.append_event 追加（单协调写者 epoch +
可选 CAS），读取只走物化视图分页（views.list_features/list_tasks，LIMIT/OFFSET），
不重放事件、不现场 COUNT 全表。项目目录先 realpath 归一（与 core.Project 同一口径，
绝对/相对/带斜杠/经 symlink 的别名落到同一个 team.db），且记录目录与库文件本身
不允许是符号链接——SQLite 会静默追随 symlink，外链库会把团队事实写到项目边界之外
（2026-09 探针确证：team.db→外部库时 team-status 读到外部事实、team-task 越界改写
外部文件）。回退不靠手工删库：team-rollback 按事实分级放行，杜绝静默丢新写入。
"""
import json
import os
from pathlib import Path

from agents_kernel.atomicio import write_json
from agents_kernel.domain import views
from agents_kernel.domain.tasks import TASK_STATUSES
from agents_kernel.paths import inside, realpath
from agents_kernel.storage import db, events, migration
from agents_kernel.validation import CompanionError, feature_id, text

TEAM_DB_FILENAME = "team.db"
# 读分页上限与 views._MAX_LIMIT 对齐（本模块不复制第二套校验，直接复用其查询函数）。
_MAX_LIMIT = 500


def team_db_path(project_dir):
    """事实库路径：项目目录 realpath 归一后拼接（身份唯一，与 core.Project 一致）。

    记录目录/库文件自身是指向任意目标的符号链接即拒绝（fail-safe，不追随、不区分
    指向内外——与 core.safe_file 对受管文件"链接不在支持范围"同一口径）；项目根自身
    的 symlink 是合法别名，归一到真实目录，不视为越界。
    """
    root = Path(project_dir)
    if not root.is_dir():
        raise CompanionError("项目目录不存在：" + str(root))
    root = realpath(root)
    data_dir = root / ".dev-companion"
    if data_dir.is_symlink():
        raise CompanionError("项目记录目录不能是文件链接：" + str(data_dir))
    path = data_dir / TEAM_DB_FILENAME
    if path.is_symlink():
        raise CompanionError(
            "team 事实库不能是文件链接（指向 %s）；追随链接会把团队事实读写到项目边界之外"
            % os.readlink(path))
    return path


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

    只读原 JSON、只写 team.db（manifest 与事件同库）；dry_run 零写入。回退经
    team-rollback（按事实分级放行，见 rollback_store），legacy 原样可用。
    """
    path = team_db_path(project_dir)
    report = migration.import_json_store(project_dir, path, dry_run=dry_run)
    return dict(report, store=str(path))


def _store_event_counts(store):
    """库内事件计数：(总数, 迁移导入数, 活动事实数)。

    迁移事件一律带 "migrate:" 幂等键前缀；team-init 的功能登记带 "team-init:" 前缀；
    其余（team-task 等，键为 NULL 或其他前缀）都是任务/审批/集成等活动事实。
    """
    total = store.query_one("SELECT COUNT(*) AS n FROM events")["n"]
    imported = store.query_one(
        "SELECT COUNT(*) AS n FROM events WHERE idempotency_key LIKE 'migrate:%'")["n"]
    activity = store.query_one(
        """SELECT COUNT(*) AS n FROM events WHERE idempotency_key IS NULL OR
           (idempotency_key NOT LIKE 'migrate:%' AND idempotency_key NOT LIKE 'team-init:%')""")["n"]
    return total, imported, activity


def _dump_events(store, out_path):
    """全量事件日志导出（append-only 事实的完整保真记录，含迁移后新写入）。"""
    items = []
    for row in store.query_all(
            """SELECT seq, event_type, entity_id, payload, idempotency_key,
                      writer_epoch, created_at FROM events ORDER BY seq"""):
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        items.append(item)
    write_json(out_path, {"schema_version": 1, "events": items})
    return items


def _export_facts(project_dir, db_path, store, manifest, total, out_dir):
    """回退前导出全量事实：旧 schema 草稿（复用 migration.fallback_export，可再次
    team-migrate 读入）+ team_events.json 全量事件日志（迁移后新写入只在这里完整
    保真——team-init 功能在库中本无 scope/allowed_paths 事实，草稿不伪造）。
    校验回读条数与库内总数一致后才返回，短写即拒绝回退。"""
    out_dir = Path(out_dir)
    if inside(realpath(out_dir), realpath(db_path.parent)):
        raise CompanionError("导出目录不得在项目记录目录内（避免覆盖 legacy 原件）：" + str(out_dir))
    exported = {"out_dir": str(out_dir), "drafts": {}, "events_log": str(out_dir / "team_events.json")}
    if manifest is not None:
        exported["drafts"] = migration.fallback_export(project_dir, out_dir, db_path=db_path)["files"]
    items = _dump_events(store, out_dir / "team_events.json")
    written = json.loads((out_dir / "team_events.json").read_text(encoding="utf-8"))["events"]
    if len(written) != total or len(items) != total:
        raise CompanionError(
            "导出校验失败：事件日志 %d/%d 条（库内 %d 条）；回退中止，team.db 保留" %
            (len(written), len(items), total))
    exported["events_exported"] = len(written)
    return exported


def _remove_store_files(path):
    for suffix in ("", "-wal", "-shm", ".writer"):
        try:
            Path(str(path) + suffix).unlink()
        except FileNotFoundError:
            pass


def rollback_store(project_dir, *, export_first=None):
    """team-rollback：按事实分级回退——删库绝不能是静默丢事实的操作。

    - 撤销空初始化（无 manifest 且只有 team-init 功能登记，无任何任务/审批/集成
      活动事实）→ 允许直接删 team.db（含 -wal/-shm/.writer；manifest 在库内随之删）；
    - 迁移后无新写入（事件数 == manifest 导入计数）→ 允许回退，报告带核对信息
      （manifest 导入计数 vs 库内实数）；
    - 有会丢失的活动事实 → 无 --export-first 明确拒绝并报出条数：迁移过的库以导入
      为基线，任何新事件（含 team-init 登记的新功能）都不容静默丢弃；未迁移的纯
      team 库则以活动事实为界。给 --export-first 先导出全量事实（旧 schema 草稿 +
      全量事件日志）并校验条数覆盖，再允许回退。
    legacy 三 JSON 本模块从不读写，回退后原样可用。
    """
    path = team_db_path(project_dir)
    if not path.exists():
        raise CompanionError("team 事实库不存在：%s；无需回退" % path)
    lock = Path(str(path) + ".writer")
    if lock.exists():
        raise CompanionError("存在写者锁 %s；请确认无 team 命令在运行后再回退" % lock)
    store = db.Store(path)
    store.open()
    try:
        manifest = migration.read_manifest(store)
        total, imported, activity = _store_event_counts(store)
        at_risk = (total - imported) if manifest is not None else activity
        if at_risk > 0 and export_first is None:
            if manifest is None:
                detail = "%d 条任务/审批/集成等活动事实（库内共 %d 条事件）" % (at_risk, total)
            else:
                detail = "%d 条迁移后新写入的事件（库内共 %d 条，其中导入 %d 条）" % (at_risk, total, imported)
            raise CompanionError(
                "检测到" + detail + "；直接删库会丢失这些事实。"
                "请先 --export-first <目录> 导出全量事实后再回退")
        export = None
        if export_first is not None:
            export = _export_facts(project_dir, path, store, manifest, total, Path(export_first))
    finally:
        store.close()
    _remove_store_files(path)
    return {"status": "rolled_back", "mode": "migrated" if manifest is not None else "empty_init",
            "store": str(path), "events_total": total, "imported_events": imported,
            "activity_events": activity, "at_risk_events": at_risk,
            "manifest_counts": (manifest or {}).get("counts", {}),
            "legacy_files": [name for name in migration.SOURCE_NAMES
                             if (path.parent / name).exists()],
            "export": export}

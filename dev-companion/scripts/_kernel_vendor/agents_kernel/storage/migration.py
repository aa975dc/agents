"""旧三 JSON → SQLite 一次性导入器与兼容导出（P2-04：C16 + Z13 迁移半 + Z14 存量半）。

导入映射（旧字段 → P2-02 事件，只折"最新一份全量"当前态；旧 history 一律只记条数、
不逐事件重放，避免 journey 全量重写造成的 O(n²) 复制）：

| 旧字段                                | 事件类型            | entity_id            | payload 要点 |
|---------------------------------------|--------------------|----------------------|--------------|
| state.scope.features[i]               | scope_revision     | feature_id           | scope_version、items=allowed_paths、feature=完整功能定义 |
| state.tasks[fid].status               | feature_status     | feature_id           | title、status |
| state.tasks[fid].status               | task_status        | feature_id（任务同 id）| feature_id、status |
| tasks[fid].verification               | evidence_registered| verification.id      | kind=check、role=verification、subject_id、result、verification 原件 |
| tasks[fid].integration                | evidence_registered| integration.id       | kind=check、role=integration、同上 |
| tasks[fid].acceptance                 | evidence_registered| "accept:"+feature_id | kind=acceptance、subject_id、result=accepted、acceptance 原件 |
| journey.records[stage]                | evidence_registered| "journey:"+stage     | kind=check、subject_id、result=complete/draft、record 原件 |
| release.json 当前态                   | release_stage      | "release"            | feature_id="project"、stage=status、release={config,binding,evidence,…} 原件 |

幂等与崩溃恢复：每条事件带 "migrate:<源指纹>:<稳定键>" 幂等键；导入 = 逐事件独立
事务 + 最后单独的 manifest 事务，任意时刻强杀只会留下"已提交的完整事件"（SQLite
事务原子性，无半套数据）。重跑守门（_guard_rerun）：已完成（manifest 在）→ 短路
去重报告，零写入；无 manifest 但源指纹变化 → 明确拒绝，不混杂新旧；无 manifest 且
同指纹（崩溃残留）→ 按幂等键去重接管补齐。manifest（来源 sha256/字节/行数、
schema_version、时间、legacy 快照字段）写 store_meta，与事件同库持久。

fallback_export：从库只读重建旧 schema 形状的 JSON 草稿（state 可被 core.Project
直接 load；执行中任务降级为 pending），写出目录由调用方指定，绝不触碰项目内
原 JSON。大 payload（verification/integration/release 证据原件）随事件一次性入库，
不在重放中反复复制。
"""
import json
from pathlib import Path

from agents_kernel.atomicio import write_json
from agents_kernel.digest import digest, sha256_bytes
from agents_kernel.paths import realpath
from agents_kernel.storage import db, events
from agents_kernel.validation import CompanionError, validate_scope

MANIFEST_KEY = "migration_manifest"
MIGRATION_VERSION = 1

SOURCE_NAMES = ("state.json", "journey.json", "release.json")
LEGACY_TASK_STATUSES = {"pending", "running", "awaiting_review", "accepted", "blocked"}
LEGACY_STAGES = ("concept", "requirements", "product", "flow", "prototype", "technical")
LEGACY_RELEASE_STATUSES = {
    "prepared", "deploying", "verifying", "rolling_back", "deployed_unverified",
    "deploy_failed", "verify_failed", "local_verified", "staging_verified", "published",
    "rolled_back_unverified", "rollback_failed", "interrupted"}
SCOPE_CONTEXT_KEYS = ("title", "goal", "audience", "scenario", "out_of_scope", "assumptions")


def _read_source(data_dir, name):
    """读单个来源文件：返回 {present, value, sha256, bytes, lines}；损坏 JSON 明确报错。"""
    path = data_dir / name
    record = {"present": False, "value": None, "sha256": None, "bytes": 0, "lines": 0}
    if not path.exists():
        return record
    try:
        data = path.read_bytes()
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise CompanionError("%s 不是有效的 JSON（%s）；迁移中止，未写任何数据" % (name, exc)) from exc
    lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    record.update(present=True, value=value, sha256=sha256_bytes(data),
                  bytes=len(data), lines=lines)
    return record


def _check_header(name, value, root):
    if not isinstance(value, dict):
        raise CompanionError(name + " 顶层必须是 JSON 对象；迁移中止，未写任何数据")
    if value.get("schema_version") != 1:
        raise CompanionError(name + " schema_version 不是 1，属不支持的旧版格式")
    if value.get("project") != str(root):
        raise CompanionError(name + " 属于其他项目（project 字段与目标目录不一致）")
    if type(value.get("revision")) is not int or value["revision"] < 1:
        raise CompanionError(name + " revision 缺失或非法")


def _parse_state(record, root):
    value = record["value"]
    _check_header("state.json", value, root)
    for key in ("scope_version", "confirmed", "scope", "tasks", "events"):
        if key not in value:
            raise CompanionError("state.json 缺少字段：" + key)
    if type(value["scope_version"]) is not int or value["scope_version"] < 1:
        raise CompanionError("state.json scope_version 非法")
    if type(value["confirmed"]) is not bool:
        raise CompanionError("state.json confirmed 必须是布尔值")
    if not isinstance(value["tasks"], dict) or not isinstance(value["events"], list):
        raise CompanionError("state.json tasks/events 类型非法")
    scope = validate_scope(value["scope"])
    if set(value["tasks"]) != {f["id"] for f in scope["features"]}:
        raise CompanionError("state.json 功能清单与任务记录不一致")
    for fid, task in value["tasks"].items():
        if not isinstance(task, dict) or task.get("status") not in LEGACY_TASK_STATUSES:
            raise CompanionError("state.json 任务 %s 状态无法识别" % fid)
    return {"scope": scope, "tasks": value["tasks"], "revision": value["revision"],
            "scope_version": value["scope_version"], "confirmed": value["confirmed"],
            "history_count": len(value["events"])}


def _parse_journey(record, root):
    value = record["value"]
    _check_header("journey.json", value, root)
    records, history = value.get("records"), value.get("history")
    if not isinstance(records, dict) or not isinstance(history, list):
        raise CompanionError("journey.json records/history 类型非法")
    parsed = {}
    for stage, item in records.items():
        if stage not in LEGACY_STAGES or not isinstance(item, dict):
            raise CompanionError("journey.json 阶段记录损坏：" + str(stage))
        for key in ("complete", "stale", "user_confirmed"):
            if type(item.get(key)) is not bool:
                raise CompanionError("journey.json 阶段 %s 的 %s 必须是布尔值" % (stage, key))
        if type(item.get("revision")) is not int or not isinstance(item.get("artifact_hashes"), dict):
            raise CompanionError("journey.json 阶段 %s 记录损坏" % stage)
        parsed[stage] = item
    return {"records": parsed, "revision": value["revision"], "history_count": len(history)}


def _parse_release(record, root):
    value = record["value"]
    _check_header("release.json", value, root)
    status = value.get("status")
    if not isinstance(status, str) or status not in LEGACY_RELEASE_STATUSES:
        raise CompanionError("release.json status 无法识别：" + str(status))
    for key in ("config", "binding", "evidence"):
        if not isinstance(value.get(key), dict):
            raise CompanionError("release.json 缺少有效的 " + key)
    if not isinstance(value.get("history"), list):
        raise CompanionError("release.json history 类型非法")
    return {"status": status, "config": value["config"], "binding": value["binding"],
            "evidence": value["evidence"], "revision": value["revision"],
            "updated_at": value.get("updated_at", ""), "history_count": len(value["history"])}


def _plan_state_events(state):
    """state 当前态 → 事件序列（scope 顺序即导入顺序，fallback 按此还原功能顺序）。"""
    planned = []
    for feature in state["scope"]["features"]:
        fid = feature["id"]
        task = state["tasks"][fid]
        planned.append(("scope_revision", fid,
                        {"scope_version": state["scope_version"], "items": feature["allowed_paths"],
                         "feature": feature}, "scope:%s:%d" % (fid, state["scope_version"])))
        planned.append(("feature_status", fid, {"title": feature["title"], "status": task["status"]},
                        "feature:%s:%d" % (fid, state["scope_version"])))
        planned.append(("task_status", fid, {"feature_id": fid, "status": task["status"]},
                        "task:%s:%d" % (fid, state["scope_version"])))
        # verification/integration 原件随事件入库（一次性），fallback 导出与审计都要用。
        for field, role in (("verification", "verification"), ("integration", "integration")):
            evidence = task.get(field)
            if isinstance(evidence, dict) and evidence.get("id"):
                planned.append(("evidence_registered", str(evidence["id"]),
                                {"kind": "check", "role": role, "subject_id": fid,
                                 "result": "passed" if evidence.get("passed") is True else "failed",
                                 "detail": role, field: evidence},
                                "evidence:%s" % evidence["id"]))
        acceptance = task.get("acceptance")
        if isinstance(acceptance, dict):
            planned.append(("evidence_registered", "accept:" + fid,
                            {"kind": "acceptance", "subject_id": fid, "result": "accepted",
                             "detail": acceptance.get("note", ""), "acceptance": acceptance},
                            "acceptance:%s" % fid))
    return planned


def _plan_journey_events(journey):
    return [("evidence_registered", "journey:" + stage,
             {"kind": "check", "subject_id": "journey:" + stage,
              "result": "complete" if record.get("complete") else "draft",
              "detail": record.get("summary") if isinstance(record.get("summary"), str) else "",
              "journey_stage": stage, "record": record},
             "journey:%s" % stage)
            for stage, record in sorted(journey["records"].items())]


def _plan_release_events(release):
    return [("release_stage", "release",
             {"feature_id": "project", "stage": release["status"],
              "release": {"config": release["config"], "binding": release["binding"],
                          "evidence": release["evidence"], "revision": release["revision"],
                          "updated_at": release["updated_at"]}},
             "release:%d" % release["revision"])]


def _analyze(project_dir):
    """解析三 JSON → 计划事件列表 + 来源指纹 + manifest；任何损坏在此报错，绝不写库。"""
    root = realpath(project_dir)
    if not root.is_dir():
        raise CompanionError("项目目录不存在：" + str(root))
    data_dir = root / ".dev-companion"
    sources = {name: _read_source(data_dir, name) for name in SOURCE_NAMES}
    if not any(source["present"] for source in sources.values()):
        raise CompanionError("没有可导入的旧项目记录（.dev-companion 下无 state/journey/release.json）")
    if not sources["state.json"]["present"] and sources["release.json"]["present"]:
        raise CompanionError(
            "state.json 缺失但 release.json 仍保留（STATE_MISSING_WITH_RELEASE）；"
            "不会自动重建状态，也不会把旧发布当作当前已验收。请先从备份恢复 state.json 再迁移")
    warnings = [] if sources["state.json"]["present"] else [
        "state.json 缺失；仅导入产品阶段记录，不生成任务/发布视图"]
    state = _parse_state(sources["state.json"], root) if sources["state.json"]["present"] else None
    journey = _parse_journey(sources["journey.json"], root) if sources["journey.json"]["present"] else None
    release = _parse_release(sources["release.json"], root) if sources["release.json"]["present"] else None
    planned = []
    if state:
        planned += _plan_state_events(state)
    if journey:
        planned += _plan_journey_events(journey)
    if release:
        planned += _plan_release_events(release)
    fingerprint = digest({name: source["sha256"] for name, source in sources.items()})
    counts = {}
    for event_type, _, _, _ in planned:
        counts[event_type] = counts.get(event_type, 0) + 1
    manifest = {
        "migration_version": MIGRATION_VERSION,
        "store_schema_version": db.SCHEMA_VERSION,
        "project": str(root),
        "sources": {name: {"sha256": source["sha256"], "bytes": source["bytes"],
                           "lines": source["lines"], "present": source["present"]}
                    for name, source in sources.items()},
        "sources_sha256": fingerprint,
        "counts": counts,
        "history_counts": {"state_events": state["history_count"] if state else 0,
                           "journey_history": journey["history_count"] if journey else 0,
                           "release_history": release["history_count"] if release else 0},
        "legacy": ({"revision": state["revision"], "scope_version": state["scope_version"],
                    "confirmed": state["confirmed"],
                    "scope_context": {key: state["scope"][key] for key in SCOPE_CONTEXT_KEYS},
                    "journey_revision": journey["revision"] if journey else 0,
                    "release_revision": release["revision"] if release else 0} if state else None),
        "warnings": warnings,
    }
    return {"root": root, "sources": sources, "sources_sha256": fingerprint,
            "planned": planned, "planned_counts": counts, "manifest": manifest}


def _guard_rerun(store, fingerprint):
    """重复导入守门：已完成（manifest 在）→ 短路去重；源指纹变化且已有痕迹 → 拒绝。"""
    raw = db.get_meta(store._conn, MANIFEST_KEY)
    if raw is not None:
        manifest = json.loads(raw)
        if manifest.get("sources_sha256") != fingerprint:
            raise CompanionError(
                "该项目已完成过一次迁移（manifest 来源指纹不同），重复导入被拒；"
                "如确需按新源重新迁移，请使用新的数据库文件并人工核对")
        return True  # manifest 写在全部事件之后：已完成导入，短路去重，零写入
    row = store.query_one(
        "SELECT idempotency_key FROM events WHERE idempotency_key LIKE 'migrate:%' LIMIT 1")
    if row is not None and row[0].split(":")[1] != fingerprint:
        raise CompanionError(
            "库中已有另一份源（指纹前缀 %s…）的导入痕迹，与当前源不一致；"
            "为避免新旧混杂，请人工核对后更换数据库文件" % row[0].split(":")[1][:12])
    return False


def import_json_store(project_dir, db_path, *, dry_run=False):
    """一次性导入：三 JSON 当前态 → 事件序列。dry_run=True 只解析+报告，零写入。

    幂等：同源指纹重复导入短路去重（applied=0，零写入）；崩溃残留（无 manifest）
    按幂等键去重接管补齐；源指纹变化且已有导入痕迹则拒绝。
    """
    analysis = _analyze(project_dir)
    report = {"project": str(analysis["root"]), "db_path": str(db_path),
              "sources_sha256": analysis["sources_sha256"],
              "sources": analysis["manifest"]["sources"],
              "planned": analysis["planned_counts"],
              "planned_total": len(analysis["planned"]),
              "history_counts": analysis["manifest"]["history_counts"],
              "warnings": analysis["manifest"]["warnings"]}
    if dry_run:
        return dict(report, status="planned", dry_run=True)
    store = db.Store(db_path)
    store.open()
    try:
        if _guard_rerun(store, analysis["sources_sha256"]):
            return dict(report, status="deduped", dry_run=False, applied=0,
                        deduped=len(analysis["planned"]), head_seq=events.view_head(store),
                        manifest=read_manifest(store))
        writer = db.acquire_writer(store)
        try:
            applied = deduped = 0
            for event_type, entity_id, payload, key in analysis["planned"]:
                result = events.append_event(
                    store, writer.epoch, event_type=event_type, entity_id=entity_id,
                    payload=payload,
                    idempotency_key="migrate:%s:%s" % (analysis["sources_sha256"], key))
                if result["applied"]:
                    applied += 1
                else:
                    deduped += 1
            manifest = dict(analysis["manifest"], imported_at=db.utcnow())
            with store.transaction() as conn:
                db.set_meta(conn, MANIFEST_KEY, json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        finally:
            writer.close()
        return dict(report, status="imported" if applied else "deduped", dry_run=False,
                    applied=applied, deduped=deduped, head_seq=events.view_head(store),
                    manifest=manifest)
    finally:
        store.close()


def read_manifest(store):
    raw = db.get_meta(store._conn, MANIFEST_KEY)
    return json.loads(raw) if raw is not None else None


def fallback_export(project_dir, out_dir, *, db_path=None):
    """从 SQLite 事实库只读导出旧 schema 形状的 JSON 草稿到 out_dir（调用方指定目录）。

    - state.json 草稿可被 core.Project(project).load() 直接读取（执行中任务降级 pending、
      证据缺失的 accepted 降级 awaiting_review，均记入 warnings）。
    - 每份草稿带 exported_from_store:true 标记；绝不写项目 .dev-companion 下的原文件。
    - 未迁移过的库（无 manifest）明确报错。
    """
    root = realpath(project_dir)
    db_path = Path(db_path) if db_path is not None else root / ".dev-companion" / "store.db"
    if not db_path.exists():
        raise CompanionError("事实库不存在，无法导出：%s" % db_path)
    store = db.Store(db_path)
    store.open()
    try:
        manifest = read_manifest(store)
        if manifest is None:
            raise CompanionError("该库尚未执行过 JSON 导入（无 manifest），没有可导出的迁移数据")
        legacy = manifest.get("legacy") or {}
        warnings = []
        features, verification, integration, acceptance, journey_records = {}, {}, {}, {}, {}
        for row in store.query_all(
                "SELECT entity_id, event_type, payload FROM events ORDER BY seq"):
            payload = json.loads(row["payload"])
            if row["event_type"] == "scope_revision":
                feature = payload.get("feature")
                if isinstance(feature, dict) and feature.get("id") not in features:
                    features[feature["id"]] = feature
            elif row["event_type"] == "evidence_registered":
                stage = payload.get("journey_stage")
                if stage is not None:
                    journey_records[stage] = payload.get("record")
                elif payload.get("kind") == "acceptance":
                    acceptance[payload.get("subject_id")] = payload.get("acceptance")
                elif payload.get("role") == "integration":
                    integration[payload.get("subject_id")] = payload.get("integration")
                elif payload.get("role") == "verification":
                    verification[payload.get("subject_id")] = payload.get("verification")
        statuses = {row["task_id"]: row["status"]
                    for row in store.query_all("SELECT task_id, status FROM task_view")}
        tasks = {}
        for feature in features.values():
            fid = feature["id"]
            status = statuses.get(fid, "pending")
            task = {"status": status}
            if fid in verification:
                task["verification"] = verification[fid]
            if fid in integration:
                task["integration"] = integration[fid]
            if fid in acceptance:
                task["acceptance"] = acceptance[fid]
            if status == "running":
                task = {"status": "pending"}
                warnings.append("任务 %s 导入时正在执行，草稿降级为 pending" % fid)
            if task["status"] == "accepted":
                check, record = task.get("verification") or {}, task.get("acceptance") or {}
                if (check.get("passed") is not True or not check.get("id") or
                        record.get("verification_id") != check.get("id") or not record.get("note")):
                    task["status"] = "awaiting_review"
                    warnings.append("任务 %s 的验收证据不完整，草稿降级为 awaiting_review" % fid)
            tasks[fid] = task
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written = {}
        if features:
            state = {"schema_version": 1, "project": str(root),
                     "revision": legacy.get("revision", 1),
                     "scope_version": legacy.get("scope_version", 1),
                     "confirmed": legacy.get("confirmed", False),
                     "scope": dict(legacy.get("scope_context", {}), features=list(features.values())),
                     "tasks": tasks, "events": [],
                     "updated_at": manifest.get("imported_at", ""),
                     "exported_from_store": True, "exported_at": db.utcnow()}
            path = out_dir / "state.json"
            write_json(path, state)
            written["state.json"] = str(path)
        if journey_records:
            ordered = {stage: journey_records[stage]
                       for stage in LEGACY_STAGES if stage in journey_records}
            journey = {"schema_version": 1, "project": str(root),
                       "revision": legacy.get("journey_revision", 1),
                       "records": ordered, "history": [],
                       "exported_from_store": True, "exported_at": db.utcnow()}
            path = out_dir / "journey.json"
            write_json(path, journey)
            written["journey.json"] = str(path)
        row = store.query_one(
            """SELECT payload FROM events WHERE event_type = 'release_stage'
               ORDER BY seq DESC LIMIT 1""")
        if row is not None:
            release = (json.loads(row["payload"]).get("release") or {})
            stage_row = store.query_one("SELECT stage FROM release_view WHERE release_id = 'release'")
            draft = {"schema_version": 1, "project": str(root),
                     "revision": release.get("revision", legacy.get("release_revision", 1)),
                     "config": release.get("config", {}), "binding": release.get("binding", {}),
                     "status": stage_row["stage"] if stage_row else "prepared",
                     "evidence": release.get("evidence", {}), "history": [],
                     "updated_at": release.get("updated_at", ""),
                     "exported_from_store": True, "exported_at": db.utcnow()}
            path = out_dir / "release.json"
            write_json(path, draft)
            written["release.json"] = str(path)
        return {"out_dir": str(out_dir), "files": written, "head_seq": events.view_head(store),
                "warnings": warnings, "sources_sha256": manifest.get("sources_sha256")}
    finally:
        store.close()

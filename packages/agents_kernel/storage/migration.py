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

fallback_export（FIX-03，SR-04）：从库只读重建旧 schema 形状的 JSON 草稿（state 可被
core.Project 直接 load）。事实源是事件日志全量折叠——迁移后新增的任务/审批/集成事件
一律参与；无法映射到旧 schema 的事实（team-init 登记的功能、同功能的子任务、非
verification/integration/acceptance 形状的证据等）不静默丢弃：写入 team_sidecar.json
完整导出并在 warnings/export_manifest.json 逐条列出。状态映射显式成表（ST03：done
不升级成 accepted，映射为 awaiting_review 待重新验收）。只读打开（mode=ro），
不初始化/迁移 schema；绝不触碰项目内原 JSON。
"""
import json
import sqlite3
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


# 团队/迁移事件里的任务状态 → legacy 任务状态（FIX-03，ST03：done 不升级成 accepted）。
# 值为 (legacy 状态, 降级说明)；说明为 None 表示恒等映射、无需警告。迁移导入的事件
# 携带 legacy 状态（awaiting_review/accepted/blocked），故同表覆盖。
_STATUS_TO_LEGACY = {
    "pending": ("pending", None),
    "ready": ("pending", "团队状态 ready 无 legacy 对应，导出为 pending"),
    "running": ("pending", "团队状态 running 降级为 pending（legacy 草稿无执行记录，需重新制作）"),
    "done": ("awaiting_review",
             "团队状态 done 降级映射为 awaiting_review，需 legacy 重新验收（不升级为 accepted）"),
    "failed": ("blocked", "团队状态 failed 映射为 blocked，需人工处理"),
    "blocked": ("blocked", None),
    "cancelled": ("pending", "团队状态 cancelled 映射为 pending，待重新安排"),
    "awaiting_review": ("awaiting_review", None),
    "accepted": ("accepted", None),  # accepted 另行校验证据完整性，不完整降级 awaiting_review
}


def _fold_store_events(store):
    """全量事件折叠（FIX-03）：事件日志是唯一事实源，迁移后新增的任务/审批/集成
    事件一律参与折叠，各实体取最新态。返回 (features, feature_titles, task_states,
    evidence, release, unknown_events, events_total)。"""
    features, titles, tasks, evidence = {}, {}, {}, {}
    release, unknown, total = None, [], 0
    for row in store.query_all(
            "SELECT seq, event_type, entity_id, payload FROM events ORDER BY seq"):
        total += 1
        payload = json.loads(row["payload"])
        kind = row["event_type"]
        if kind == "scope_revision":
            feature = payload.get("feature")
            if isinstance(feature, dict) and feature.get("id"):
                features[feature["id"]] = feature  # 最新 scope 胜出（与视图折叠一致）
        elif kind == "feature_status":
            titles[row["entity_id"]] = {"title": payload.get("title", ""),
                                        "status": payload.get("status", "")}
        elif kind == "task_status":
            tasks[row["entity_id"]] = {"feature_id": payload.get("feature_id", ""),
                                       "status": payload.get("status", "")}
        elif kind == "evidence_registered":
            evidence[row["entity_id"]] = payload
        elif kind == "release_stage":
            release = payload
        else:  # 未知事件类型不静默丢弃，进 sidecar
            unknown.append({"seq": row["seq"], "event_type": kind, "entity_id": row["entity_id"]})
    return features, titles, tasks, evidence, release, unknown, total


def _classify_evidence(evidence, features, warnings, sidecar_evidence):
    """证据事件按 legacy 形状归类：journey/acceptance/integration/verification 进草稿，
    其余（如 review 记录）或主体不在草稿功能集的证据进 sidecar 并逐条警告。"""
    journey, verification, integration, acceptance = {}, {}, {}, {}
    for entity_id, payload in evidence.items():
        stage = payload.get("journey_stage")
        subject = payload.get("subject_id")
        if stage is not None:
            journey[stage] = payload.get("record")
        elif payload.get("kind") == "acceptance" and subject in features:
            acceptance[subject] = payload.get("acceptance")
        elif payload.get("role") == "integration" and subject in features:
            integration[subject] = payload.get("integration")
        elif payload.get("role") == "verification" and subject in features:
            verification[subject] = payload.get("verification")
        else:
            sidecar_evidence.append(dict(payload, evidence_id=entity_id))
            warnings.append("证据 %s 无法映射到旧 schema（主体 %s），完整原件见 team_sidecar.json"
                            % (entity_id, subject))
    return journey, verification, integration, acceptance


def fallback_export(project_dir, out_dir, *, db_path=None):
    """从 SQLite 事实库只读导出旧 schema 形状的 JSON 草稿到 out_dir（调用方指定目录）。

    FIX-03（SR-04）：
    - 事实源 = 事件日志全量折叠（含迁移后新增的任务/审批/集成事件），导出 manifest
      记录 events_total 等计数，调用方据此核对导出完整性。
    - 状态映射显式（_STATUS_TO_LEGACY）：done→awaiting_review（不升级 accepted），
      ready/running/cancelled→pending，failed→blocked；每种非恒等映射在 warnings
      逐条声明。accepted 仍校验证据完整性，不完整降级 awaiting_review。
    - 无法映射到旧 schema 的事实（team-init 功能、同功能子任务、review 等异形证据、
      未知事件）不静默丢弃：写 team_sidecar.json 完整导出 + warnings 逐条列出。
    - 写出 export_manifest.json（计数/文件清单/警告），供回退前核对。
    - state.json 草稿可被 core.Project(project).load() 直接读取；只读打开，不初始化
      /迁移 schema；绝不写项目 .dev-companion 下的原文件；未迁移过的库（无 manifest）
      明确报错。
    """
    root = realpath(project_dir)
    db_path = Path(db_path) if db_path is not None else root / ".dev-companion" / "store.db"
    if not db_path.exists():
        raise CompanionError("事实库不存在，无法导出：%s" % db_path)
    store = db.Store(db_path)
    try:
        try:
            store.open_readonly()
            manifest = read_manifest(store)
        except sqlite3.Error as exc:
            raise CompanionError(
                "事实库损坏或不是有效的 SQLite 库：%s（%s）" % (db_path, exc)) from exc
        if manifest is None:
            raise CompanionError("该库尚未执行过 JSON 导入（无 manifest），没有可导出的迁移数据")
        legacy = manifest.get("legacy") or {}
        warnings, sidecar_evidence = [], []
        features, titles, task_states, evidence, release, unknown, events_total = \
            _fold_store_events(store)
        journey_records, verification, integration, acceptance = _classify_evidence(
            evidence, features, warnings, sidecar_evidence)

        # team-init 登记的功能没有 scope/验收事实，旧 schema 无法表达（草稿不伪造）
        for fid, info in titles.items():
            if fid not in features:
                warnings.append("功能 %s 由 team 登记且无范围/验收事实，无法进入旧 schema，"
                                "完整事实见 team_sidecar.json" % fid)
        # 旧 schema 每功能恰好一个与功能同 id 的任务：子任务/归属不符的任务进 sidecar
        own_status = {}
        sidecar_tasks = []
        for tid, state in task_states.items():
            fid = state["feature_id"]
            if tid == fid and fid in features:
                own_status[fid] = state["status"]
            else:
                sidecar_tasks.append(dict(state, task_id=tid))
                warnings.append("任务 %s（功能 %s）无法用旧 schema 表达（旧格式每功能仅一个"
                                "任务），完整事实见 team_sidecar.json" % (tid, fid))

        tasks = {}
        for fid, feature in features.items():
            raw = own_status.get(fid)
            if raw is None:
                warnings.append("功能 %s 无任务状态事件，草稿记为 pending" % fid)
                legacy_status, note = "pending", None
            else:
                legacy_status, note = _STATUS_TO_LEGACY.get(
                    raw, ("pending", "团队状态 %s 无映射，导出为 pending" % raw))
            if note is not None:
                warnings.append("任务 %s %s" % (fid, note))
            task = {"status": legacy_status}
            if fid in verification:
                task["verification"] = verification[fid]
            if fid in integration:
                task["integration"] = integration[fid]
            if fid in acceptance:
                task["acceptance"] = acceptance[fid]
            if legacy_status == "accepted":
                check, record = task.get("verification") or {}, task.get("acceptance") or {}
                if (check.get("passed") is not True or not check.get("id") or
                        record.get("verification_id") != check.get("id") or not record.get("note")):
                    task["status"] = "awaiting_review"
                    warnings.append("任务 %s 的验收证据不完整，草稿降级为 awaiting_review" % fid)
            tasks[fid] = task

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written = {}
        head_seq = events.view_head(store)
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
        if release is not None:
            inner = release.get("release") or {}
            draft = {"schema_version": 1, "project": str(root),
                     "revision": inner.get("revision", legacy.get("release_revision", 1)),
                     "config": inner.get("config", {}), "binding": inner.get("binding", {}),
                     "status": release.get("stage", "prepared"),
                     "evidence": inner.get("evidence", {}), "history": [],
                     "updated_at": inner.get("updated_at", ""),
                     "exported_from_store": True, "exported_at": db.utcnow()}
            path = out_dir / "release.json"
            write_json(path, draft)
            written["release.json"] = str(path)
        sidecar = None
        if sidecar_tasks or sidecar_evidence or unknown or any(
                fid not in features for fid in titles):
            sidecar_payload = {"schema_version": 1, "project": str(root),
                               "exported_from_store": True, "exported_at": db.utcnow(),
                               "sources_sha256": manifest.get("sources_sha256"),
                               "head_seq": head_seq, "events_total": events_total,
                               "features": [dict(titles[fid], feature_id=fid)
                                            for fid in titles if fid not in features],
                               "tasks": sidecar_tasks, "evidence": sidecar_evidence,
                               "events": unknown, "warnings": warnings}
            sidecar = str(out_dir / "team_sidecar.json")
            write_json(Path(sidecar), sidecar_payload)
        counts = {"events_total": events_total, "features": len(features),
                  "tasks": len(tasks), "journey_records": len(journey_records),
                  "release": 1 if release is not None else 0,
                  "sidecar_features": sum(1 for fid in titles if fid not in features),
                  "sidecar_tasks": len(sidecar_tasks),
                  "sidecar_evidence": len(sidecar_evidence),
                  "unknown_events": len(unknown)}
        export_manifest = {"schema_version": 1, "exported_from_store": True,
                           "project": str(root), "store": str(db_path),
                           "sources_sha256": manifest.get("sources_sha256"),
                           "head_seq": head_seq, "counts": counts,
                           "files": dict(written,
                                         **({"team_sidecar.json": sidecar} if sidecar else {})),
                           "events_log": "team_events.json",
                           "warnings": warnings, "exported_at": db.utcnow()}
        manifest_path = out_dir / "export_manifest.json"
        write_json(manifest_path, export_manifest)
        return {"out_dir": str(out_dir), "files": written, "sidecar": sidecar,
                "manifest": str(manifest_path), "head_seq": head_seq,
                "warnings": warnings, "sources_sha256": manifest.get("sources_sha256"),
                "counts": counts}
    finally:
        store.close()

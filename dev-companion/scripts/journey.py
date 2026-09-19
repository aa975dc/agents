"""Persist the six product-design stages without changing legacy execution records."""
import copy
import hashlib
import json
import os
import stat
import tempfile

from core import CompanionError, digest, now, read_json, relative_path, safe_file, strings, text, validate_scope


STAGES = ("concept", "requirements", "product", "flow", "prototype", "technical")
DETAILS = {
    "concept": ("audience", "problem", "scenario", "outcome"),
    "requirements": ("constraints", "priorities"),
    "product": ("positioning",),
    "flow": ("main_path", "alternatives", "data_changes"),
    "prototype": ("screens", "states", "walkthrough"),
    "technical": ("architecture", "data_model", "release_target"),
}
NEXT_STEPS = {
    "concept": "用日常场景说明使用者、问题和预期结果，保存概念卡。",
    "requirements": "逐步回答关键需求问题，记录决定及未决事项。",
    "product": "确认首版功能、范围和验收例子，并记录用户对产品方案的确认。",
    "flow": "梳理所有首版功能的操作、异常分支和数据变化。",
    "prototype": "保存页面、状态和走查证据，区分代理走查与真实用户试用。",
    "technical": "落实实现范围、接口约定、检查命令及发布目标。",
    "implementation": "产品设计已完整记录；确认可执行需求后派发开发任务。",
}


def product_scope(raw, complete=True):
    """Validate product meaning without asking a novice to invent technical paths."""
    if not isinstance(raw, dict):
        raise CompanionError("产品需求必须是 JSON 对象")
    result = {key: text(raw.get(key), key) for key in ("title", "goal", "audience", "scenario")
              if complete or key in raw}
    for key in ("out_of_scope", "assumptions"):
        if complete or key in raw:
            result[key] = strings(raw.get(key, []), key)
    if not complete and "features" not in raw:
        return result
    features = raw.get("features")
    if not isinstance(features, list) or (complete and not features):
        raise CompanionError("首版至少需要一项功能")
    result["features"], seen = [], set()
    for item in features:
        if not isinstance(item, dict):
            raise CompanionError("功能必须是对象")
        feature = {}
        if complete or "id" in item:
            identifier = text(item.get("id"), "功能编号")
            if identifier in seen or not all(c.isascii() and (c.isalnum() or c in "-_") for c in identifier):
                raise CompanionError("功能编号须唯一且仅含英文字母、数字、下划线或短横线")
            seen.add(identifier)
            feature["id"] = identifier
        if complete or "title" in item:
            feature["title"] = text(item.get("title"), "功能名称")
        if complete or "acceptance_criteria" in item:
            feature["acceptance_criteria"] = strings(item.get("acceptance_criteria"), "验收条件", complete)
        if complete or "requires_user_acceptance" in item:
            needs_user = item.get("requires_user_acceptance", True)
            if type(needs_user) is not bool:
                raise CompanionError("requires_user_acceptance 必须为布尔值")
            feature["requires_user_acceptance"] = needs_user
        result["features"].append(feature)
    return result


class Journey:
    def __init__(self, project):
        self.project = project
        self.path = project.data / "journey.json"

    def _guard_path(self):
        if self.project.data.is_symlink() or self.path.is_symlink():
            raise CompanionError("产品阶段记录不能是文件链接")

    def _normalize(self, stage, raw, records=None, complete=False):
        if stage not in STAGES:
            raise CompanionError("无法识别产品阶段：" + str(stage))
        if not isinstance(raw, dict) or not isinstance(raw.get("details"), dict):
            raise CompanionError("阶段记录必须包含 summary 和 details 对象")
        result = {"summary": text(raw.get("summary"), "阶段总结"),
                  "details": {key: text(raw["details"].get(key), key) for key in DETAILS[stage]
                              if complete or key in raw["details"]},
                  "open_questions": strings(raw.get("open_questions", []), "未决问题"),
                  "artifacts": strings(raw.get("artifacts", []), "阶段产物"), "decisions": []}
        if len(set(result["artifacts"])) != len(result["artifacts"]):
            raise CompanionError("阶段产物不能重复")
        if complete and stage == "prototype" and not result["artifacts"]:
            raise CompanionError("原型完成至少需要一个真实文件产物，例如页面草图或模块调用样例")
        for name in result["artifacts"]:
            safe_file(self.project.root, name)
        decisions = raw.get("decisions", [])
        if not isinstance(decisions, list):
            raise CompanionError("决定记录必须为列表")
        for decision in decisions:
            if not isinstance(decision, dict) or decision.get("source") not in ("user", "recommendation", "assumption"):
                raise CompanionError("决定来源必须为 user、recommendation 或 assumption")
            result["decisions"].append({"question": text(decision.get("question"), "决定问题"),
                                         "answer": text(decision.get("answer"), "决定内容"),
                                         "source": decision["source"]})
        if stage == "product" and (complete or "scope" in raw):
            result["scope"] = product_scope(raw.get("scope"), complete)
        if stage in {"flow", "prototype"} and (complete or "feature_ids" in raw):
            ids = strings(raw.get("feature_ids"), "覆盖功能", complete)
            if len(ids) != len(set(ids)):
                raise CompanionError("覆盖功能编号不能重复")
            result["feature_ids"] = ids
        if stage == "technical":
            if complete:
                result["scope"] = validate_scope(raw.get("scope"))
                if any(not f["check_commands"] for f in result["scope"]["features"]):
                    raise CompanionError("技术方案的每项功能都需要非空检查命令")
            elif "scope" in raw:
                result["scope"] = product_scope(raw["scope"], complete=False)
                for feature, item in zip(result["scope"].get("features", []), raw["scope"].get("features", [])):
                    if "allowed_paths" in item:
                        feature["allowed_paths"] = list(dict.fromkeys(relative_path(p) for p in strings(item["allowed_paths"], "可修改文件")))
                    if "check_commands" in item:
                        if not isinstance(item["check_commands"], list):
                            raise CompanionError("检查命令必须为参数数组的列表")
                        feature["check_commands"] = [strings(c, "检查命令参数", True) for c in item["check_commands"]]
            interfaces = raw.get("interfaces", [])
            if not isinstance(interfaces, list) or (complete and not interfaces):
                raise CompanionError("技术方案需要接口约定列表，完成时不得为空")
            if complete or "interfaces" in raw:
                result["interfaces"] = []
            for item in interfaces:
                if not isinstance(item, dict):
                    raise CompanionError("接口约定必须为对象")
                interface = {}
                if (complete or "kind" in item) and item.get("kind") not in ("http", "local"):
                    raise CompanionError("接口类型必须为 http 或 local")
                for key in ("feature_id", "kind", "contract"):
                    if complete or key in item:
                        interface[key] = text(item.get(key), key)
                if complete or "check_commands" in item:
                    commands = item.get("check_commands")
                    if not isinstance(commands, list) or (complete and not commands):
                        raise CompanionError("每个接口完成时需要非空检查命令")
                    interface["check_commands"] = [strings(c, "接口检查命令参数", True) for c in commands]
                result["interfaces"].append(interface)
            if complete and {i["feature_id"] for i in result["interfaces"]} != {f["id"] for f in result["scope"]["features"]}:
                raise CompanionError("接口约定必须准确覆盖所有首版功能")
        if complete and records is not None and stage in {"flow", "prototype", "technical"}:
            if "product" not in records or "scope" not in records["product"] or not records["product"]["complete"]:
                raise CompanionError("请先保存产品方案，再记录流程、原型或技术方案")
            scope = records["product"]["scope"]
            if stage in {"flow", "prototype"} and set(result["feature_ids"]) != {f["id"] for f in scope["features"]}:
                raise CompanionError("流程和原型必须准确覆盖产品方案的所有功能")
            if stage == "technical" and product_scope(result["scope"]) != scope:
                raise CompanionError("技术方案的产品含义必须与产品方案一致；改变需求请先更新产品方案")
        return result

    def _load(self):
        self._guard_path()
        if not self.path.exists():
            return None
        state = read_json(self.path)
        if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1 or
                state.get("project") != str(self.project.root) or type(state.get("revision")) is not int or
                state["revision"] < 1 or not isinstance(state.get("records"), dict) or
                not isinstance(state.get("history"), list) or not state["records"]):
            raise CompanionError("产品阶段记录不兼容、属于其他项目或已损坏，请保留原文件后检查")
        for stage, record in state["records"].items():
            if (stage not in STAGES or not isinstance(record, dict) or type(record.get("complete")) is not bool or
                    type(record.get("stale")) is not bool or type(record.get("user_confirmed")) is not bool or
                    type(record.get("revision")) is not int or not 1 <= record["revision"] <= state["revision"] or
                    not isinstance(record.get("artifact_hashes"), dict)):
                raise CompanionError("产品阶段记录格式已损坏")
            normalized = self._normalize(stage, record, complete=record["complete"])
            if any(record.get(key) != value for key, value in normalized.items()):
                raise CompanionError("产品阶段内容不是有效的规范记录")
            hashes = record["artifact_hashes"]
            if set(hashes) != set(record["artifacts"]) or any(
                    not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
                    for value in hashes.values()):
                raise CompanionError("阶段产物缺少有效哈希记录")
            if record["complete"] and (record["open_questions"] or (stage == "product" and not record["user_confirmed"])):
                raise CompanionError("已完成阶段缺少问题解决或产品确认记录")
        return state

    def _artifact_hashes(self, names, exists=False):
        result = {}
        for name in names:
            path = safe_file(self.project.root, name, exists=exists)
            if not path.exists():
                result[name] = None
                continue
            try:
                before = path.stat()
                content = path.read_bytes()
                after = path.stat()
                if (before.st_mtime_ns, before.st_ctime_ns, before.st_mode, before.st_size) != (
                        after.st_mtime_ns, after.st_ctime_ns, after.st_mode, len(content)):
                    raise CompanionError("阶段产物正在变化，请等待写入结束后重试：" + name)
                result[name] = digest({"sha256": hashlib.sha256(content).hexdigest(), "mode": stat.S_IMODE(before.st_mode)})
            except OSError as exc:
                raise CompanionError("无法读取阶段产物：" + name) from exc
        return result

    def _view(self, state):
        records, stale_stages, upstream_stale = {}, [], False
        for stage in STAGES:
            if stage not in state["records"]:
                continue
            record = copy.deepcopy(state["records"][stage])
            current = self._artifact_hashes(record["artifacts"])
            record["stale"] = upstream_stale or record["stale"] or current != record["artifact_hashes"]
            record["current_artifact_hashes"] = current
            records[stage] = record
            if record["stale"]:
                stale_stages.append(stage)
                upstream_stale = True
        current_stage = next((stage for stage in STAGES if stage not in records or not records[stage]["complete"] or
                              records[stage]["stale"]), "implementation")
        return {"revision": state["revision"], "current_stage": current_stage,
                "complete": current_stage == "implementation", "records": records, "stale_stages": stale_stages,
                "next_step": NEXT_STEPS[current_stage]}

    def status(self):
        state = self._load()
        return self._view(state) if state else None

    def context(self):
        status = self.status()
        if status is None:
            return None
        context = {key: status[key] for key in ("revision", "current_stage", "records")}
        return {**context, "fingerprint": digest(context)}

    def require_ready(self, scope):
        status = self.status()
        if status is None:
            return
        if not status["complete"]:
            raise CompanionError("产品设计尚未完成或已经过期：" + status["next_step"])
        if validate_scope(scope) != status["records"]["technical"]["scope"]:
            raise CompanionError("可执行需求与当前技术方案不同，请先同步产品与技术方案")

    def save(self, stage, raw, revision, complete=False, user_confirmed=False):
        if type(revision) is not int or revision < 0:
            raise CompanionError("阶段版本必须是非负整数；首次保存使用 0")
        if type(complete) is not bool or type(user_confirmed) is not bool:
            raise CompanionError("阶段完成与用户确认标记必须为布尔值")
        self._guard_path()
        with self.project.locked():
            state = self._load() or {"schema_version": 1, "project": str(self.project.root),
                                     "revision": 0, "records": {}, "history": []}
            if state["revision"] != revision:
                raise CompanionError("产品阶段记录已更新，请重新读取后再保存")
            if self.project.state_path.exists() or self.project.state_path.is_symlink():
                execution = self.project.load()
                if any(task["status"] == "running" for task in execution["tasks"].values()):
                    raise CompanionError("仍有正在制作的任务，完成或停止后再修改产品阶段")
            payload = self._normalize(stage, raw, state["records"], complete=complete)
            if complete:
                view = self._view(state)
                if any(previous not in view["records"] or not view["records"][previous]["complete"] or
                       view["records"][previous]["stale"] for previous in STAGES[:STAGES.index(stage)]):
                    raise CompanionError("完成本阶段前，必须先完成且重新核验之前的阶段")
                if payload["open_questions"]:
                    raise CompanionError("本阶段仍有未决问题，请解决后再完成")
                if stage == "product" and not user_confirmed:
                    raise CompanionError("产品方案完成需要记录真实用户确认；已有确认可直接沿用")
            previous_records = copy.deepcopy(state["records"])
            timestamp = now()
            state["revision"] += 1
            for following in STAGES[STAGES.index(stage) + 1:]:
                if following in state["records"]:
                    state["records"][following]["stale"] = True
            state["records"][stage] = {**payload, "complete": complete, "stale": False,
                                        "user_confirmed": user_confirmed, "revision": state["revision"],
                                        "updated_at": timestamp, "artifact_hashes": self._artifact_hashes(payload["artifacts"], exists=True)}
            state["updated_at"] = timestamp
            state["history"].append({"revision": state["revision"], "at": timestamp,
                                      "stage": stage, "previous_records": previous_records})
            result = self._view(state)
            self._write(state)
            return result

    def _write(self, state):
        self._guard_path()
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix="journey-", suffix=".tmp", dir=str(self.project.data))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._guard_path()
            os.replace(temporary, self.path)
        except OSError as exc:
            raise CompanionError("无法保存产品阶段记录；原文件保持不变") from exc
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

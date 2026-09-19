"""Explicitly authorized release commands and their local evidence (stdlib only)."""
import copy
import sys
import uuid
from pathlib import Path


def _kernel_path():
    """定位仓库根 packages/agents_kernel 并加入 sys.path（用 __file__ 相对定位）。

    兼容从仓库（<repo>/dev-companion/scripts）与从插件目录（scripts 与 packages 同根）
    两种布局。脱离仓库根的独立安装产物由 P2-05 的 vendor 构建提供
    （02_TARGET_ARCHITECTURE.md §3），此处不引入第二套机制。
    """
    for base in Path(__file__).resolve().parents:
        if (base / "packages" / "agents_kernel").is_dir():
            packages = str(base / "packages")
            if packages not in sys.path:
                sys.path.insert(0, packages)
            return
    raise ImportError("找不到 agents_kernel：需要仓库根 packages/agents_kernel（插件独立分发由 vendor 构建提供）")


_kernel_path()

from agents_kernel.atomicio import read_json, write_json
from agents_kernel.process import now, run_argv
from agents_kernel.validation import CompanionError, text

TIMEOUT_SECONDS = 120
ACTIVE = {"deploying", "verifying", "rolling_back"}
STATUSES = ACTIVE | {"prepared", "deployed_unverified", "deploy_failed", "verify_failed",
                     "local_verified", "staging_verified", "published", "rolled_back_unverified", "rollback_failed", "interrupted"}
VERIFIED = {"local": "local_verified", "staging": "staging_verified", "production": "published"}
FIELDS = {"version", "target", "environment", "summary", "rollback_plan", "verification_notes",
          "deploy_commands", "verify_commands", "rollback_commands"}


def validate_config(raw):
    if not isinstance(raw, dict) or set(raw) - FIELDS:
        raise CompanionError("发布输入只接受发布配置字段，不能填写状态或检查结果")
    config = {key: text(raw.get(key), key) for key in FIELDS if not key.endswith("_commands")}
    if config["environment"] not in VERIFIED:
        raise CompanionError("发布环境必须是 local、staging 或 production")
    for key in ("deploy_commands", "verify_commands", "rollback_commands"):
        commands = raw.get(key, [])
        if not isinstance(commands, list) or (key != "rollback_commands" and not commands):
            raise CompanionError(key + "必须是非空 argv 数组列表")
        for argv in commands:
            if (not isinstance(argv, list) or not argv or
                    any(not isinstance(arg, str) or not arg.strip() or "\0" in arg for arg in argv)):
                raise CompanionError(key + "中的每条命令必须是非空 argv 数组")
        config[key] = copy.deepcopy(commands)
    return config


class ReleaseStore:
    def __init__(self, project):
        self.project = project
        self.path = project.data / "release.json"
        self._check_paths()

    def _check_paths(self):
        if self.project.data.is_symlink() or self.path.is_symlink():
            raise CompanionError("发布记录路径不能是文件链接")
        if self.path.exists() and not self.path.is_file():
            raise CompanionError("发布记录必须是普通文件")

    def _load(self):
        self._check_paths()
        if not self.path.exists():
            return None
        record = read_json(self.path)
        if (not isinstance(record, dict) or type(record.get("schema_version")) is not int or record["schema_version"] != 1 or
                record.get("project") != str(self.project.root) or
                type(record.get("revision")) is not int or record["revision"] < 1 or
                not isinstance(record.get("status"), str) or record["status"] not in STATUSES or not isinstance(record.get("history"), list) or
                not isinstance(record.get("binding"), dict) or not isinstance(record.get("evidence"), dict)):
            raise CompanionError("发布记录不兼容或已损坏；请保留现场核对")
        config = validate_config(record.get("config"))
        binding = record["binding"]
        if (type(binding.get("scope_version")) is not int or binding["scope_version"] < 1 or
                not isinstance(binding.get("fingerprint"), str) or not binding["fingerprint"] or
                not isinstance(binding.get("check_ids"), dict) or not isinstance(binding.get("integration_ids"), dict) or
                "journey" not in binding):
            raise CompanionError("发布记录缺少有效的来源绑定")
        for action, evidence in record["evidence"].items():
            if (action not in {"deploy", "verify", "rollback"} or not isinstance(evidence, dict) or
                    type(evidence.get("passed")) is not bool or not evidence.get("id") or
                    not isinstance(evidence.get("results"), list) or not evidence["results"]):
                raise CompanionError("发布记录缺少有效命令证据")
            commands = config[action + "_commands"]
            for index, result in enumerate(evidence["results"]):
                if (not isinstance(result, dict) or index >= len(commands) or result.get("argv") != commands[index] or
                        type(result.get("executed")) is not bool or "exit_code" not in result or
                        (result.get("exit_code") is not None and type(result["exit_code"]) is not int) or
                        type(result.get("truncated")) is not bool or not isinstance(result.get("output"), str) or
                        not result.get("started_at") or not result.get("finished_at")):
                    raise CompanionError("发布命令证据不完整或与配置不一致")
            if evidence["passed"] and (len(evidence["results"]) != len(commands) or
                                       not all(r["executed"] and r["exit_code"] == 0 for r in evidence["results"])):
                raise CompanionError("发布成功状态缺少全部成功命令证据")
        required = {"deploy_failed": ("deploy", False), "deployed_unverified": ("deploy", True),
                    "verifying": ("deploy", True), "verify_failed": ("verify", False),
                    "rolled_back_unverified": ("rollback", True), "rollback_failed": ("rollback", False)}
        if record["status"] in VERIFIED.values():
            if record["status"] != VERIFIED[config["environment"]]:
                raise CompanionError("发布状态与目标环境不一致")
            required[record["status"]] = ("verify", True)
        if record["status"] in required:
            action, passed = required[record["status"]]
            if record["evidence"].get(action, {}).get("passed") is not passed:
                raise CompanionError("发布状态缺少对应的执行证据")
        if record["status"] in set(VERIFIED.values()) | {"verify_failed"} and not record["evidence"].get("deploy", {}).get("passed"):
            raise CompanionError("核验状态缺少成功部署证据")
        return record

    def _commit(self, record, event):
        self._check_paths()
        record["revision"] += 1
        record["updated_at"] = now()
        record["history"].append({"revision": record["revision"], "at": record["updated_at"],
                                  "version": record["config"]["version"], "status": record["status"], **event})
        # before_replace 保留原有的"写入后、替换前"再核查路径语义。
        write_json(self.path, record, prefix="release-", suffix=".tmp", before_replace=self._check_paths)

    def _current(self, view=None):
        from journey import Journey
        if view is None:
            snapshot = self.project.snapshot()
            view = self.project.status(include_release=False, snapshot=snapshot)
        fingerprint = view.get("source_fingerprint")
        if not fingerprint:
            if not self.project.state_path.exists() and not self.project.state_path.is_symlink():
                raise CompanionError("state.json 缺失，无法计算发布来源指纹；发布记录仍在，请先恢复 state.json 再操作发布")
            raise CompanionError("发布来源缺少项目文件指纹")
        context = Journey(self.project).context()
        binding = {"scope_version": view["scope_version"], "fingerprint": fingerprint,
                   "check_ids": {f["id"]: (f.get("verification") or {}).get("id") for f in view["features"]},
                   "integration_ids": {f["id"]: (f.get("integration") or {}).get("id") for f in view["features"]},
                   "journey": {key: context[key] for key in ("revision", "fingerprint")} if context else None}
        return binding, view.get("overall_percent") == 100 and view.get("confirmed") is True

    @staticmethod
    def _require_revision(record, revision):
        if type(revision) is not int or revision != (record["revision"] if record else 0):
            raise CompanionError("发布记录已更新；请重新读取当前发布 revision")

    @staticmethod
    def _require_known(record):
        if record and record["status"] in ACTIVE:
            raise CompanionError("上次发布操作结果未知；请先核对实际环境与进程，不能自动重放或重新准备")

    def prepare(self, raw, revision):
        config = validate_config(raw)
        self._check_paths()
        with self.project.locked():
            previous = self._load()
            self._require_revision(previous, revision)
            self._require_known(previous)
            self.project.require_idle(self.project.load())
            binding, accepted = self._current()
            if not accepted:
                raise CompanionError("当前范围必须全部验收且证据有效，才能准备发布")
            prior = {key: previous[key] for key in ("config", "binding", "status", "evidence")} if previous else None
            record = {"schema_version": 1, "project": str(self.project.root), "revision": previous["revision"] if previous else 0,
                      "history": previous["history"] if previous else [], "config": config, "binding": binding,
                      "status": "prepared", "evidence": {}}
            self._commit(record, {"kind": "prepared", "previous": prior})
            return self.status()

    def _execute(self, argv):
        return run_argv(argv, self.project.root, TIMEOUT_SECONDS,
                        "命令超时（%s秒）；必须核对目标环境和残留进程" % TIMEOUT_SECONDS)

    def run(self, action, revision, authorized=False):
        if action not in {"deploy", "verify", "rollback"}:
            raise CompanionError("发布操作必须是 deploy、verify 或 rollback")
        if authorized is not True:
            raise CompanionError("执行发布命令前需要用户对本次操作的明确授权")
        self._check_paths()
        with self.project.locked():
            record = self._load()
            if not record:
                raise CompanionError("尚未准备发布")
            self._require_revision(record, revision)
            self._require_known(record)
            self.project.require_idle(self.project.load())
            allowed = {"deploy": {"prepared", "deploy_failed"}, "verify": {"deployed_unverified", "verify_failed"},
                       "rollback": STATUSES - ACTIVE - {"prepared", "rolled_back_unverified"}}
            if record["status"] not in allowed[action]:
                raise CompanionError("当前发布状态不允许此操作；请先核对已有结果")
            if action != "rollback":
                binding, accepted = self._current()
                if not accepted or binding != record["binding"]:
                    raise CompanionError("发布来源或检查证据已变化；请完成验收并重新准备发布")
            commands = record["config"][action + "_commands"]
            if not commands:
                raise CompanionError("没有可执行的回退命令；请先明确回退方式")
            record["status"] = {"deploy": "deploying", "verify": "verifying", "rollback": "rolling_back"}[action]
            record["evidence"].pop(action, None)
            self._commit(record, {"kind": action + "_started"})
            results = []
            for argv in commands:
                results.append(self._execute(argv))
                if results[-1]["exit_code"] != 0:
                    break
            passed = len(results) == len(commands) and all(r["executed"] and r["exit_code"] == 0 for r in results)
            source_changed = False
            if action == "verify":
                try:
                    binding, accepted = self._current()
                    source_changed = binding != record["binding"] or not accepted
                except CompanionError:
                    source_changed = True
                passed = passed and not source_changed
            evidence = {"id": uuid.uuid4().hex, "at": now(), "passed": passed, "results": results,
                        "source_changed": source_changed}
            record["evidence"][action] = evidence
            success = {"deploy": "deployed_unverified", "verify": VERIFIED[record["config"]["environment"]],
                       "rollback": "rolled_back_unverified"}
            timed_out = any(result.get("timed_out") for result in results)
            record["status"] = ({"deploy": "deploying", "verify": "verifying", "rollback": "rolling_back"}[action]
                                if timed_out else success[action] if passed else action + "_failed")
            self._commit(record, {"kind": action + "_finished", "evidence": evidence})
            return {**self.status(), "passed": passed}

    def reconcile(self, revision, note, authorized=False):
        if authorized is not True:
            raise CompanionError("需明确授权：已核对现场并确认旧执行进程停止，才能解除未知执行状态")
        note = text(note, "现场核对说明")
        self._check_paths()
        with self.project.locked():
            record = self._load()
            self._require_revision(record, revision)
            if not record or record["status"] not in ACTIVE:
                raise CompanionError("只有结果未知的执行中记录可以核对中断状态")
            self.project.require_idle(self.project.load())
            previous_status = record["status"]
            record["status"] = "interrupted"
            self._commit(record, {"kind": "interruption_reconciled", "previous_status": previous_status, "note": note})
            return self.status()

    def status(self, current_view=None):
        record = self._load()
        if not record:
            return None
        binding, accepted = self._current(current_view)
        stale = binding != record["binding"] or not accepted
        message = "状态来自本地执行记录；源码变化不代表线上版本自动变化"
        if record["status"] in ACTIVE:
            message = "上次操作仍在执行或结果未知；必须核对实际环境与进程，不自动重放"
        elif record["status"] == "prepared":
            message = "发布方案已准备，尚未部署；核验命令应检查实际部署版本与核心流程"
        elif record["status"] == "rolled_back_unverified":
            message = "回退命令成功；回退后的实际版本与核心流程尚未核验"
        elif record["status"] == "interrupted":
            message = "已记录现场核对和执行中断；没有新增成功证据，可重新准备发布或明确执行回退"
        return {**record, "source_stale": stale, "message": message}

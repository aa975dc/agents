"""Project facts and verification for Dev Companion (Python 3.9+, stdlib only).

P2-01 起共享工具下沉 packages/agents_kernel，本模块保留原公共名作兼容 Facade
（re-export 或薄委托），并继续负责锁、状态机、快照、聚合与渲染。依赖方向：
core → kernel；core → archives（restore-pending 探测走其公共 API）；
core 可懒加载 journey/releases 做聚合——journey/releases 对 core 已无模块级依赖，环已解除。
"""
import contextlib
import html
import os
import stat
import subprocess  # noqa: F401 — re-export：tests 以 "core.subprocess.Popen" 为 patch 目标（模块单例）
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
from agents_kernel.digest import content_digest, digest, sha256_file
from agents_kernel.paths import EXCLUDED_DIRS, realpath, sensitive
from agents_kernel.process import now, redact_output, run_argv
from agents_kernel.validation import (CompanionError, relative_path, safe_file, strings, text,
                                      validate_scope)

from archives import ArchiveError, pending_restore


class Project:
    def __init__(self, path):
        self.root = realpath(path)
        if not self.root.is_dir():
            raise CompanionError("项目目录不存在")
        self.data = self.root / ".dev-companion"
        if self.data.is_symlink():
            raise CompanionError("项目记录目录不能是文件链接")
        self.state_path = self.data / "state.json"

    def recovery_status(self):
        # 探测走 archives 公共 API；内部布局（archives/restore-pending.json）属主是 archives.py。
        try:
            info = pending_restore(self.root)
        except ArchiveError as exc:
            raise CompanionError(str(exc)) from exc
        if info is None:
            return None
        return {"required": True, "safety_archive_id": info["safety_archive_id"],
                "message": "上次恢复未完成；请先核对保护存档和 restore-pending.json，暂停制作与验收"}

    def orphan_release_note(self):
        if (not self.state_path.exists() and not self.state_path.is_symlink()
                and (self.data / "release.json").is_file()):
            return ("项目状态 state.json 缺失，但发布记录 release.json 仍保留；不会自动重建状态，也不会把旧发布当作当前已验收。"
                    "请先从备份恢复 state.json；若确认放弃该发布历史，请人工移走 release.json 后再重新 init")
        return None

    def load(self):
        if self.state_path.is_symlink():
            raise CompanionError("项目记录不能是文件链接")
        if not self.state_path.exists():
            orphan = self.orphan_release_note()
            if orphan:
                raise CompanionError(orphan)
            raise CompanionError("尚未建立需求记录，请先整理需求并运行 init")
        state = read_json(self.state_path)
        if (not isinstance(state, dict) or state.get("schema_version") != 1 or
                state.get("project") != str(self.root) or type(state.get("revision")) is not int or
                state["revision"] < 1 or type(state.get("scope_version")) is not int or state["scope_version"] < 1 or
                type(state.get("confirmed")) is not bool or
                not isinstance(state.get("tasks"), dict) or not isinstance(state.get("events"), list)):
            raise CompanionError("项目记录不兼容或已损坏，请保留原文件后检查")
        scope = validate_scope(state.get("scope"))
        if set(state["tasks"]) != {f["id"] for f in scope["features"]}:
            raise CompanionError("功能清单与任务记录不一致")
        for feature in scope["features"]:
            task = state["tasks"][feature["id"]]
            if not isinstance(task, dict) or task.get("status") not in {
                    "pending", "running", "awaiting_review", "accepted", "blocked"}:
                raise CompanionError("无法识别任务状态")
            if task["status"] == "running" and (not isinstance(task.get("run"), dict) or
                    not isinstance(task["run"].get("baseline"), dict) or not task["run"].get("run_id")):
                raise CompanionError("正在制作的任务缺少执行记录")
            if task["status"] == "accepted":
                verification, acceptance = task.get("verification"), task.get("acceptance")
                if (not isinstance(verification, dict) or verification.get("passed") is not True or
                        not verification.get("id") or not isinstance(acceptance, dict) or
                        acceptance.get("verification_id") != verification["id"] or not acceptance.get("note") or
                        (feature["requires_user_acceptance"] and acceptance.get("user_confirmed") is not True)):
                    raise CompanionError("已验收状态缺少有效的检查与确认记录")
        return state

    @contextlib.contextmanager
    def locked(self):
        self.data.mkdir(exist_ok=True)
        lock = self.data / "write.lock"
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise CompanionError("另一次操作仍持有记录锁；请确认其结束后再处理 write.lock") from exc
        try:
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            recovery = self.recovery_status()
            if recovery:
                error = CompanionError(recovery["message"])
                error.safety_archive_id = recovery["safety_archive_id"]
                raise error
            yield
        finally:
            lock.unlink()

    def recover_lock(self, authorized=False):
        if authorized is not True:
            raise CompanionError("请先确认中断现场及全部写入进程已停止，再授权清理失效锁")
        if os.name != "posix":
            raise CompanionError("当前平台不支持自动核对锁进程，请先人工核查锁与进程")
        lock = self.data / "write.lock"
        if self.data.is_symlink() or lock.is_symlink():
            raise CompanionError("锁路径不能是文件链接")
        if not lock.exists():
            return {"cleared": False, "message": "没有需要清理的锁"}
        before = lock.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > 32:
            raise CompanionError("锁内容异常，请保留现场核查")
        content = lock.read_text(encoding="utf-8")
        if not content.isascii() or not content.isdigit() or int(content) < 1:
            raise CompanionError("无法识别锁的进程编号，请保留现场核查")
        pid = int(content)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except (PermissionError, OverflowError, OSError) as exc:
            raise CompanionError("无法确认锁进程已经退出，请保留现场核查") from exc
        else:
            raise CompanionError("锁所属进程仍存在，不能清理")
        after = lock.lstat()
        if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns, before.st_mode) != (
                after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns, after.st_mode):
            raise CompanionError("锁在核查期间发生变化，请重新核查")
        lock.unlink()
        return {"cleared": True, "previous_pid": pid,
                "message": "已清理退出进程的锁；未终止任何进程，也未改变任务或发布状态"}

    def commit(self, state, event):
        state["revision"] += 1
        state["updated_at"] = now()
        state["events"].append({"revision": state["revision"], "at": state["updated_at"], **event})
        write_json(self.state_path, state, prefix="state-", suffix=".tmp")

    def snapshot(self):
        files, excluded, total = {}, [], 0
        for directory, dirs, names in os.walk(self.root, followlinks=False):
            parent = Path(directory)
            kept = []
            for name in sorted(dirs):
                path = parent / name
                if name in EXCLUDED_DIRS or sensitive(name) or path.is_symlink():
                    excluded.append(path.relative_to(self.root).as_posix() + "/")
                else:
                    kept.append(name)
            dirs[:] = kept
            for name in sorted(names):
                path = parent / name
                relative = path.relative_to(self.root).as_posix()
                if name == ".DS_Store" or name.endswith(".pyc") or sensitive(name) or path.is_symlink():
                    excluded.append(relative)
                    continue
                if not path.is_file():
                    excluded.append(relative + "（特殊文件，未纳入项目检查）")
                    continue
                info = path.stat()
                total += info.st_size
                if info.st_size > 20 * 1024 * 1024 or total > 100 * 1024 * 1024 or len(files) >= 10000:
                    raise CompanionError("项目超出首版检查范围（单文件20MiB，总量100MiB，10000文件）")
                sha256_hex, size = sha256_file(path)
                after = path.stat()
                if (after.st_mtime_ns, after.st_ctime_ns, after.st_mode) != (info.st_mtime_ns, info.st_ctime_ns, info.st_mode) or size != info.st_size:
                    raise CompanionError("项目文件正在变化，请等待写入结束后刷新")
                files[relative] = content_digest(sha256_hex, stat.S_IMODE(info.st_mode))
        return {"files": files, "fingerprint": digest(files), "excluded": sorted(excluded)}

    def init(self, scope):
        scope = validate_scope(scope)
        with self.locked():
            orphan = self.orphan_release_note()
            if orphan:
                raise CompanionError(orphan)
            if self.state_path.exists() or self.state_path.is_symlink():
                raise CompanionError("已有项目记录；修改范围请使用 scope，避免覆盖历史")
            state = {"schema_version": 1, "project": str(self.root), "revision": 0,
                     "scope_version": 1, "confirmed": False, "scope": scope,
                     "tasks": {f["id"]: {"status": "pending"} for f in scope["features"]}, "events": []}
            self.commit(state, {"kind": "scope_drafted", "scope": scope})
            return {"revision": state["revision"], "scope_version": 1, "confirmed": False,
                    "message": "需求草案已记录；请向用户展示范围、验收条件和检查命令后确认"}

    def confirm(self, revision):
        with self.locked():
            state = self.load()
            self.require_revision(state, revision)
            self.require_plan(state)
            state["confirmed"] = True
            self.commit(state, {"kind": "scope_confirmed", "scope_version": state["scope_version"]})
            return {"revision": state["revision"], "scope_version": state["scope_version"], "confirmed": True}

    @staticmethod
    def require_revision(state, revision):
        if state["revision"] != revision:
            raise CompanionError("项目记录已更新，请重新阅读当前范围再确认")

    def revise(self, scope, revision):
        scope = validate_scope(scope)
        with self.locked():
            state = self.load()
            self.require_revision(state, revision)
            self.require_idle(state)
            previous = {f["id"]: f for f in state["scope"]["features"]}
            old = state["scope"]
            context_changed = any(old[key] != scope[key] for key in ("goal", "audience", "scenario", "assumptions", "out_of_scope"))
            tasks = {}
            for feature in scope["features"]:
                identifier = feature["id"]
                old_task = state["tasks"].get(identifier, {})
                task = old_task if not context_changed and previous.get(identifier) == feature else {
                    "status": "pending", "feedback": old_task.get("feedback", [])}
                for item in task.get("feedback", []):
                    if item["kind"] == "requirement":
                        if item.get("requirement_basis") == self.requirement_basis(scope, feature):
                            item.pop("resolved_in_scope", None)
                        else:
                            item["resolved_in_scope"] = state["scope_version"] + 1
                tasks[identifier] = task
            state["tasks"] = tasks
            state.update(scope=scope, scope_version=state["scope_version"] + 1, confirmed=False)
            self.commit(state, {"kind": "scope_changed", "previous_scope": old, "scope": scope})
            return {"revision": state["revision"], "scope_version": state["scope_version"],
                    "confirmed": False, "message": "首版范围已改变，待用户确认；旧范围保留在记录中"}

    @staticmethod
    def require_idle(state):
        if any(t["status"] == "running" for t in state["tasks"].values()):
            raise CompanionError("还有制作任务进行中；先实际停止写入并记录阻塞，再进行此操作")

    @staticmethod
    def feature(state, identifier):
        for feature in state["scope"]["features"]:
            if feature["id"] == identifier:
                return feature, state["tasks"][identifier]
        raise CompanionError("功能编号不存在：" + identifier)

    @staticmethod
    def require_confirmed(state):
        if not state["confirmed"]:
            raise CompanionError("首版范围尚未确认，暂不执行制作或检查")

    def planning_context(self):
        from journey import Journey
        return Journey(self).context()

    def require_plan(self, state):
        from journey import Journey
        Journey(self).require_ready(state["scope"])

    def planning_fingerprint(self):
        return (self.planning_context() or {}).get("fingerprint")

    @staticmethod
    def requirement_basis(scope, feature):
        return digest({"context": {k: scope[k] for k in ("goal", "audience", "scenario", "out_of_scope", "assumptions")},
                       "feature": {k: feature[k] for k in ("id", "title", "acceptance_criteria", "requires_user_acceptance")}})

    def packet(self, identifier):
        with self.locked():
            state = self.load()
            self.require_confirmed(state)
            self.require_idle(state)
            self.require_plan(state)
            feature, task = self.feature(state, identifier)
            feedback = task.get("feedback", [])
            if any(f["kind"] == "requirement" and not f.get("resolved_in_scope") for f in feedback):
                raise CompanionError("需求反馈尚未处理；请先更新并确认需求范围")
            for name in feature["allowed_paths"]:
                safe_file(self.root, name)
            baseline = self.snapshot()
            run = {"run_id": uuid.uuid4().hex, "scope_version": state["scope_version"],
                   "started_at": now(), "baseline": baseline["files"],
                   "planning_fingerprint": self.planning_fingerprint()}
            task.clear()
            task.update(status="running", run=run, feedback=feedback)
            self.commit(state, {"kind": "dispatched", "feature_id": identifier, "run_id": run["run_id"]})
            context = self.planning_context()
            interfaces = (context or {}).get("records", {}).get("technical", {}).get("interfaces", [])
            return {"feature_id": identifier, "run_id": run["run_id"], "scope_version": run["scope_version"],
                    "revision": state["revision"], "project": str(self.root), **feature,
                    "planning_context": context, "interfaces": [i for i in interfaces if i["feature_id"] == identifier],
                    "instruction": "仅修改 allowed_paths；执行者回报 implemented 仍需独立检查；不要自行改进度记录"}

    def receipt(self, raw):
        if not isinstance(raw, dict):
            raise CompanionError("执行回报必须是 JSON 对象")
        with self.locked():
            state = self.load()
            self.require_confirmed(state)
            identifier = text(raw.get("feature_id"), "功能编号")
            feature, task = self.feature(state, identifier)
            run = task.get("run", {})
            if (task["status"] != "running" or raw.get("run_id") != run.get("run_id") or
                    raw.get("scope_version") != state["scope_version"] or
                    run.get("planning_fingerprint") != self.planning_fingerprint()):
                raise CompanionError("执行回报重复、过期或不属于当前任务")
            if raw.get("status") not in {"implemented", "verified_existing", "blocked"}:
                raise CompanionError("执行回报仅接受 implemented、verified_existing 或 blocked")
            text(raw.get("summary"), "本次说明")
            changed = strings(raw.get("changed_files"), "修改文件")
            for name in changed:
                safe_file(self.root, name)
            current = self.snapshot()["files"]
            actual = {p for p in set(current) | set(run["baseline"])
                      if current.get(p) != run["baseline"].get(p)}
            if set(changed) != actual or not actual.issubset(feature["allowed_paths"]):
                raise CompanionError("实际修改与回报/允许范围不一致，请核查后重新回报")
            evidence_files = strings(raw.get("evidence_files", []), "证据文件")
            for name in evidence_files:
                safe_file(self.root, name, exists=True)
                if name not in feature["allowed_paths"]:
                    raise CompanionError("执行回报的证据文件不在本次约定文件范围")
            if raw["status"] == "implemented" and not actual:
                raise CompanionError("implemented 回报必须包含实际修改；复核既有实现请使用 verified_existing")
            if raw["status"] == "verified_existing" and (actual or not evidence_files):
                raise CompanionError("verified_existing 只适用于零修改复核，并且必须列出范围内证据文件")
            if raw["status"] == "blocked":
                text(raw.get("blocker"), "阻塞原因")
            task.update(status="blocked" if raw["status"] == "blocked" else "awaiting_review",
                        receipt=raw, reported_at=now())
            self.commit(state, {"kind": "worker_reported", "feature_id": identifier, "run_id": run["run_id"]})
            return {"revision": state["revision"], "status": task["status"],
                    "message": "执行回报已记录；尚未计入已验收"}

    def check(self, identifier, kind="feature"):
        if kind not in {"feature", "integration"}:
            raise CompanionError("检查类型必须为 feature 或 integration")
        with self.locked():
            state = self.load()
            self.require_confirmed(state)
            self.require_idle(state)
            self.require_plan(state)
            feature, task = self.feature(state, identifier)
            if task["status"] not in {"awaiting_review", "accepted"}:
                raise CompanionError("先完成当前制作回报，再进行检查")
            commands = feature["check_commands"]
            context = self.planning_context()
            if kind == "integration":
                interfaces = (context or {}).get("records", {}).get("technical", {}).get("interfaces", [])
                commands = [argv for interface in interfaces if interface["feature_id"] == identifier
                            for argv in interface["check_commands"]]
            if not commands:
                raise CompanionError("尚未约定可执行检查，不能标为通过；请完善需求范围后再确认")
            for name in feature["allowed_paths"]:
                safe_file(self.root, name)
            before = self.snapshot()
            planning_before = (context or {}).get("fingerprint")
            results = []
            for argv in commands:
                results.append(run_argv(argv, self.root, 120, "检查超时（120秒）"))
                if results[-1]["exit_code"] != 0:
                    break
            after = self.snapshot()
            passed = (len(results) == len(commands) and
                      all(r["executed"] and r["exit_code"] == 0 for r in results) and
                      before["fingerprint"] == after["fingerprint"] and
                      planning_before == self.planning_fingerprint())
            verification = {"id": uuid.uuid4().hex, "at": now(), "passed": passed,
                            "kind": kind, "planning_fingerprint": planning_before,
                            "fingerprint": after["fingerprint"], "content_changed": before["fingerprint"] != after["fingerprint"],
                            "results": results, "excluded_paths": after["excluded"]}
            task.update(status="awaiting_review")
            task["verification" if kind == "feature" else "integration"] = verification
            task.pop("acceptance", None)
            self.commit(state, {"kind": "checked", "feature_id": identifier,
                                "verification_id": verification["id"], "check_kind": kind, "passed": passed})
            return {"revision": state["revision"], **verification}

    def accept(self, identifier, note, user_confirmed=False):
        note = text(note, "验收说明")
        with self.locked():
            state = self.load()
            self.require_confirmed(state)
            self.require_idle(state)
            self.require_plan(state)
            feature, task = self.feature(state, identifier)
            verification = task.get("verification", {})
            fingerprint = self.snapshot()["fingerprint"]
            planning = self.planning_fingerprint()
            if (task["status"] != "awaiting_review" or not verification.get("passed") or
                    verification.get("fingerprint") != fingerprint or verification.get("planning_fingerprint") != planning):
                raise CompanionError("缺少当前内容的成功检查，请先运行 check")
            integration = task.get("integration", {})
            if planning is not None and (not integration.get("passed") or
                    integration.get("fingerprint") != fingerprint or integration.get("planning_fingerprint") != planning):
                raise CompanionError("缺少当前接口约定的成功联调，请运行 check --kind integration")
            if feature["requires_user_acceptance"] and not user_confirmed:
                raise CompanionError("此功能还需要用户实际试用并确认")
            task.update(status="accepted", acceptance={"at": now(), "note": note,
                        "user_confirmed": user_confirmed, "verification_id": verification["id"]})
            self.commit(state, {"kind": "accepted", "feature_id": identifier, "note": note})
            return {"revision": state["revision"], "status": "accepted"}

    def feedback(self, identifier, kind, note):
        if kind not in {"defect", "experience", "requirement", "environment"}:
            raise CompanionError("无法识别反馈类型")
        note = text(note, "反馈说明")
        with self.locked():
            state = self.load()
            self.require_idle(state)
            feature, task = self.feature(state, identifier)
            feedback = {"kind": kind, "note": note, "at": now(), "scope_version": state["scope_version"]}
            if kind == "requirement":
                feedback["requirement_basis"] = self.requirement_basis(state["scope"], feature)
            task.setdefault("feedback", []).append(feedback)
            task.update(status="blocked", blocker=note)
            for key in ("acceptance", "verification", "integration"):
                task.pop(key, None)
            self.commit(state, {"kind": "feedback_recorded", "feature_id": identifier, "feedback": feedback})
            return {"revision": state["revision"], "status": "blocked", "feedback": feedback}

    def block(self, identifier, reason):
        reason = text(reason, "阻塞原因")
        with self.locked():
            state = self.load()
            _, task = self.feature(state, identifier)
            task.update(status="blocked", blocker=reason)
            task.pop("acceptance", None)
            self.commit(state, {"kind": "blocked", "feature_id": identifier, "reason": reason})
            return {"revision": state["revision"], "status": "blocked",
                    "message": "已记录阻塞；此命令不会替你终止外部智能体，请确认实际写入已停止"}

    def status(self, include_release=True, snapshot=None):
        from journey import Journey
        orphan = self.orphan_release_note()
        if orphan:
            raise CompanionError(orphan)
        planning = Journey(self).status()
        if not self.state_path.exists() and not self.state_path.is_symlink() and planning:
            view = {"schema_version": 1, "title": "产品规划", "goal": "先明确产品，再形成可执行范围",
                    "scope": {"out_of_scope": [], "assumptions": []}, "revision": 0, "scope_version": 0,
                    "confirmed": False, "updated_at": planning.get("updated_at", "尚未建立技术范围"), "observed_at": now(),
                    "counts": {s: 0 for s in LABELS}, "total": 0, "overall_percent": None,
                    "next_step": planning["next_step"], "features": [], "excluded_paths": [],
                    "recovery": self.recovery_status(), "publication": "未核验发布状态", "history": []}
            return self.decorate_status(view, planning, include_release)
        state = self.load()
        recovery = self.recovery_status()
        snapshot = snapshot or self.snapshot()
        planning_fingerprint = self.planning_fingerprint()
        features, counts = [], {s: 0 for s in ("pending", "running", "awaiting_review", "accepted", "blocked")}
        for feature in state["scope"]["features"]:
            task = state["tasks"][feature["id"]]
            verification = task.get("verification", {})
            integration = task.get("integration", {})
            integration_stale = (not integration.get("passed") or integration.get("fingerprint") != snapshot["fingerprint"] or
                                 integration.get("planning_fingerprint") != planning_fingerprint)
            stale = (verification.get("fingerprint") != snapshot["fingerprint"] or
                     verification.get("planning_fingerprint") != planning_fingerprint or
                     (planning_fingerprint is not None and integration_stale))
            status = "awaiting_review" if task["status"] == "accepted" and stale else task["status"]
            counts[status] += 1
            features.append({**feature, "status": status, "evidence_stale": stale and "verification" in task,
                             "blocker": task.get("blocker") or task.get("receipt", {}).get("blocker"),
                             "verification": task.get("verification"), "integration": task.get("integration"),
                             "integration_stale": integration_stale,
                             "feedback": task.get("feedback", []), "acceptance": task.get("acceptance")})
        total = len(features)
        if recovery:
            next_step = recovery["message"]
        elif not state["confirmed"]:
            next_step = "请确认需求卡、首版范围、检查方式与可修改文件"
        elif counts["running"]:
            next_step = "等待当前制作任务回报；若已中止，请记录原因"
        elif counts["awaiting_review"]:
            next_step = "先检查待验收功能，再按验收条件试用"
        elif counts["pending"]:
            next_step = "选择下一项待开始功能，交给开发者"
        elif counts["blocked"]:
            next_step = "查看受阻原因，补充所需资料或决定"
        else:
            next_step = "本版功能已验收；查看使用说明与存档，发布另行决定"
        view = {"schema_version": 1, "title": state["scope"]["title"], "goal": state["scope"]["goal"],
                "scope": state["scope"], "revision": state["revision"], "scope_version": state["scope_version"],
                "confirmed": state["confirmed"], "updated_at": state["updated_at"], "observed_at": now(),
                "counts": counts, "total": total, "overall_percent": (counts["accepted"] * 100 // total) if state["confirmed"] and not recovery else None,
                "next_step": next_step, "features": features, "excluded_paths": snapshot["excluded"],
                "source_fingerprint": snapshot["fingerprint"],
                "recovery": recovery,
                "publication": "未核验发布状态", "history": state["events"][-10:]}
        return self.decorate_status(view, planning, include_release)

    def decorate_status(self, view, planning, include_release):
        release = None
        if include_release:
            from releases import ReleaseStore
            release = ReleaseStore(self).status(current_view=view)
        view.update(planning=planning, release=release)
        if planning and not planning["complete"]:
            stage = planning["current_stage"]
            view["next_step"] = planning["next_step"]
        elif not view["confirmed"]:
            stage = "technical"
        elif view["overall_percent"] == 100:
            stage = "release" if release and release["status"] != "prepared" else "release_preparation"
        elif view["counts"]["awaiting_review"]:
            stage = "integration" if planning and any(
                f["integration_stale"] for f in view["features"] if f["status"] == "awaiting_review") else "acceptance"
        else:
            stage = "implementation"
        if view.get("recovery"):
            view["next_step"] = view["recovery"]["message"]
        if release:
            view["publication"] = RELEASE_LABELS.get(release["status"], release["status"])
            if release.get("source_stale"):
                view["publication"] += "；当前源码或依据已改变，历史发布结果不代表当前版本"
            if view["overall_percent"] == 100 and not view.get("recovery"):
                view["next_step"] = release.get("next_step", "查看发布记录，按授权范围执行或核验")
        view.update(current_stage=stage, stage_label=STAGE_LABELS[stage])
        return view


LABELS = {"pending": "待开始", "running": "制作中", "awaiting_review": "待验收",
          "accepted": "已验收", "blocked": "受阻"}

STAGE_LABELS = dict(zip(("concept", "requirements", "product", "flow", "prototype", "technical", "implementation",
                        "integration", "acceptance", "release_preparation", "release"),
                       ("概念讲解", "需求问答", "产品方案", "产品流程", "原型与交互", "技术方案与任务", "编码实现",
                        "前后端联调", "测试与验收", "发布准备", "发布与核验")))
RELEASE_LABELS = {"prepared": "发布方案已准备", "deploying": "部署进行中，结果待核对", "deploy_failed": "部署失败",
                  "deployed_unverified": "已执行部署，尚未核验", "verifying": "发布核验进行中", "verify_failed": "发布核验失败",
                  "local_verified": "本地发布验证通过", "staging_verified": "测试环境验证通过", "published": "生产环境约定检查通过",
                  "rolling_back": "回退进行中", "rolled_back_unverified": "已执行回退，尚未核验", "rollback_failed": "回退失败"}
RELEASE_LABELS["interrupted"] = "已记录中断现场；发布结果仍未核验"


def render_markdown(view):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    progress = ("恢复未完成，暂不能计算" if view.get("recovery") else "范围待确认，暂不能计算") if view["overall_percent"] is None else "%s/%s 项已验收（%s%%）" % (
        view["counts"]["accepted"], view["total"], view["overall_percent"])
    lines = ["# " + cell(view["title"]), "", cell(view["goal"]), "", "**本版完成度：** " + progress,
             "**当前阶段：** " + view.get("stage_label", "编码实现"),
             "**推荐下一步：** " + view["next_step"], "", "| 功能 | 状态 | 提醒 |", "|---|---|---|"]
    for feature in view["features"]:
        notice = feature.get("blocker") or ("内容已变化，需要复查" if feature["evidence_stale"] else "")
        lines.append("| %s | %s | %s |" % (cell(feature["title"]), LABELS[feature["status"]], cell(notice)))
    lines += ["", "**首版不包含：** " + ("；".join(map(cell, view["scope"]["out_of_scope"])) or "尚未列出"),
              "**假设与待确认：** " + ("；".join(map(cell, view["scope"]["assumptions"])) or "当前未列出"),
              "**剩余工作：** %s 项尚未验收；其中 %s 项受阻。没有可靠依据时不预测工期。" % (
                  view["total"] - view["counts"]["accepted"], view["counts"]["blocked"]),
              "**发布状态：** " + view["publication"],
              "**记录时间：** %s；本次读取：%s；范围版本：%s" % (view["updated_at"], view["observed_at"], view["scope_version"]),
              "", "已存档、已验收与已上线是三个不同状态。文件快照范围请查看存档记录。"]
    return "\n".join(lines) + "\n"


def render_html(view):
    esc = lambda value: html.escape(str(value), quote=True)
    progress = ("恢复未完成" if view.get("recovery") else "范围待确认") if view["overall_percent"] is None else "%s / %s 项已验收" % (view["counts"]["accepted"], view["total"])
    cards = []
    for f in view["features"]:
        notes = f.get("blocker") or ("内容已变化，需要复查" if f["evidence_stale"] else "")
        cards.append('<article><span class="badge %s">%s</span><h3>%s</h3><p>%s</p><details><summary>怎样算完成</summary><ul>%s</ul></details></article>' % (
            f["status"], LABELS[f["status"]], esc(f["title"]), esc(notes),
            "".join("<li>%s</li>" % esc(c) for c in f["acceptance_criteria"])))
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s · 开发陪伴</title><style>
:root{color-scheme:light dark;--bg:#f5f6f8;--card:#fff;--text:#1f2430;--muted:#596574;--line:#d5dae1;--blue:#185fa5}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#161a21;--text:#e8eaed;--muted:#abb6c7;--line:#3a4356;--blue:#85b7eb}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.7 system-ui,sans-serif}main{max-width:980px;margin:auto;padding:40px 24px}h1{font-size:32px;margin:12px 0}h2{font-size:24px}p{color:var(--muted)}.eyebrow{color:var(--blue);letter-spacing:.08em}.summary,article{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px}.summary{margin:24px 0;border-left:5px solid var(--blue)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}.badge{display:inline-block;border:1px solid var(--line);padding:2px 10px;border-radius:20px;font-size:14px}.accepted{color:#3b7a28}.blocked{color:#b35428}summary{cursor:pointer;color:var(--blue)}footer{margin-top:28px;color:var(--muted);font-size:14px}ul{padding-left:24px}</style>
<main><div class="eyebrow">DEV COMPANION / 开发陪伴</div><h1>%s</h1><p>%s</p>
<section class="summary"><h2>%s</h2><p>当前阶段：%s</p><strong>下一步：%s</strong><p>这是本次读取的状态快照。重新运行进度命令可刷新；存档、验收与发布分别记录。</p></section>
<div class="grid">%s</div><section><h2>本版范围</h2><p>暂不包含：%s</p><p>假设与待确认：%s</p></section>
<footer>范围版本 %s · 记录更新 %s · 本次读取 %s<br>发布状态：%s</footer></main></html>''' % (
        esc(view["title"]), esc(view["title"]), esc(view["goal"]), esc(progress), esc(view.get("stage_label", "编码实现")), esc(view["next_step"]),
        "".join(cards), esc("；".join(view["scope"]["out_of_scope"]) or "尚未列出"),
        esc("；".join(view["scope"]["assumptions"]) or "当前未列出"), view["scope_version"],
        esc(view["updated_at"]), esc(view["observed_at"]), esc(view["publication"]))

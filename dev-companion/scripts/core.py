"""Project facts and verification for Dev Companion (Python 3.9+, stdlib only)."""
import contextlib
import datetime
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tempfile
import uuid


class CompanionError(ValueError):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CompanionError("无法读取有效的 JSON：%s" % path) from exc


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise CompanionError(label + "不能为空")
    return value.strip()


def strings(value, label, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise CompanionError(label + "必须是%s列表" % ("非空" if nonempty else ""))
    return [text(item, label) for item in value]


EXCLUDED_DIRS = {".git", ".dev-companion", "node_modules", ".venv", "venv", "__pycache__",
                 ".pytest_cache", ".mypy_cache", "dist", "build", ".next"}


def sensitive(name):
    name = name.lower()
    return (name == ".env" or name.startswith(".env.") or
            name in {"id_rsa", "id_ed25519", "credentials.json", ".npmrc", ".pypirc"} or
            name.endswith((".pem", ".key", ".p12", ".pfx")))


def relative_path(value):
    value = text(value, "文件路径")
    path = PurePosixPath(value)
    if (path.is_absolute() or "\\" in value or ":" in value or
            any(p in {"", ".", ".."} for p in value.split("/")) or
            any(p in EXCLUDED_DIRS for p in path.parts) or any(sensitive(p) for p in path.parts)):
        raise CompanionError("文件路径不在支持的普通项目文件范围：" + value)
    return value


def safe_file(project, name, exists=False):
    name = relative_path(name)
    path = project
    for part in PurePosixPath(name).parts:
        path = path / part
        if path.is_symlink():
            raise CompanionError("文件链接不在支持范围：" + name)
    if path.exists() and not path.is_file():
        raise CompanionError("需要普通文件：" + name)
    if exists and not path.is_file():
        raise CompanionError("找不到产物文件：" + name)
    return path


def validate_scope(raw):
    if not isinstance(raw, dict):
        raise CompanionError("需求必须是 JSON 对象")
    scope = {key: text(raw.get(key), key) for key in ("title", "goal", "audience", "scenario")}
    for key in ("out_of_scope", "assumptions"):
        scope[key] = strings(raw.get(key, []), key)
    features = raw.get("features")
    if not isinstance(features, list) or not features:
        raise CompanionError("首版至少需要一项功能")
    scope["features"] = []
    seen = set()
    for item in features:
        if not isinstance(item, dict):
            raise CompanionError("功能必须是对象")
        identifier = text(item.get("id"), "功能编号")
        if not all(c.isascii() and (c.isalnum() or c in "-_") for c in identifier) or identifier in seen:
            raise CompanionError("功能编号须唯一且仅含英文字母、数字、下划线或短横线")
        seen.add(identifier)
        paths = strings(item.get("allowed_paths"), "可修改文件", True)
        paths = list(dict.fromkeys(relative_path(p) for p in paths))
        commands = item.get("check_commands", [])
        if not isinstance(commands, list):
            raise CompanionError("检查命令必须为参数数组的列表")
        commands = [strings(c, "检查命令参数", True) for c in commands]
        needs_user = item.get("requires_user_acceptance", True)
        if type(needs_user) is not bool:
            raise CompanionError("requires_user_acceptance 必须为布尔值")
        scope["features"].append({"id": identifier, "title": text(item.get("title"), "功能名称"),
                                  "acceptance_criteria": strings(item.get("acceptance_criteria"), "验收条件", True),
                                  "allowed_paths": paths, "check_commands": commands,
                                  "requires_user_acceptance": needs_user})
    return scope


class Project:
    def __init__(self, path):
        self.root = Path(path).resolve()
        if not self.root.is_dir():
            raise CompanionError("项目目录不存在")
        self.data = self.root / ".dev-companion"
        if self.data.is_symlink():
            raise CompanionError("项目记录目录不能是文件链接")
        self.state_path = self.data / "state.json"

    def recovery_status(self):
        directory = self.data / "archives"
        pending = directory / "restore-pending.json"
        if directory.is_symlink() or pending.is_symlink():
            raise CompanionError("存档恢复记录路径异常，请先核查")
        if not pending.exists():
            return None
        record = read_json(pending)
        return {"required": True, "safety_archive_id": record.get("safety_archive_id") if isinstance(record, dict) else None,
                "message": "上次恢复未完成；请先核对保护存档和 restore-pending.json，暂停制作与验收"}

    def load(self):
        if self.state_path.is_symlink():
            raise CompanionError("项目记录不能是文件链接")
        if not self.state_path.exists():
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

    def commit(self, state, event):
        state["revision"] += 1
        state["updated_at"] = now()
        state["events"].append({"revision": state["revision"], "at": state["updated_at"], **event})
        fd, temporary = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=str(self.data))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

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
                    raise CompanionError("不支持特殊文件：" + relative)
                info = path.stat()
                total += info.st_size
                if info.st_size > 20 * 1024 * 1024 or total > 100 * 1024 * 1024 or len(files) >= 10000:
                    raise CompanionError("项目超出首版检查范围（单文件20MiB，总量100MiB，10000文件）")
                content = path.read_bytes()
                after = path.stat()
                if (after.st_mtime_ns, after.st_ctime_ns, after.st_mode) != (info.st_mtime_ns, info.st_ctime_ns, info.st_mode) or len(content) != info.st_size:
                    raise CompanionError("项目文件正在变化，请等待写入结束后刷新")
                files[relative] = digest({"sha256": hashlib.sha256(content).hexdigest(), "mode": stat.S_IMODE(info.st_mode)})
        return {"files": files, "fingerprint": digest(files), "excluded": sorted(excluded)}

    def init(self, scope):
        scope = validate_scope(scope)
        with self.locked():
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
            state["tasks"] = {f["id"]: state["tasks"][f["id"]] if not context_changed and previous.get(f["id"]) == f
                              else {"status": "pending"} for f in scope["features"]}
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

    def packet(self, identifier):
        with self.locked():
            state = self.load()
            self.require_confirmed(state)
            self.require_idle(state)
            feature, task = self.feature(state, identifier)
            for name in feature["allowed_paths"]:
                safe_file(self.root, name)
            baseline = self.snapshot()
            run = {"run_id": uuid.uuid4().hex, "scope_version": state["scope_version"],
                   "started_at": now(), "baseline": baseline["files"]}
            task.clear()
            task.update(status="running", run=run)
            self.commit(state, {"kind": "dispatched", "feature_id": identifier, "run_id": run["run_id"]})
            return {"feature_id": identifier, "run_id": run["run_id"], "scope_version": run["scope_version"],
                    "revision": state["revision"], "project": str(self.root), **feature,
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
                    raw.get("scope_version") != state["scope_version"]):
                raise CompanionError("执行回报重复、过期或不属于当前任务")
            if raw.get("status") not in {"implemented", "blocked"}:
                raise CompanionError("执行回报仅接受 implemented 或 blocked")
            text(raw.get("summary"), "本次说明")
            changed = strings(raw.get("changed_files"), "修改文件")
            for name in changed:
                safe_file(self.root, name)
            current = self.snapshot()["files"]
            actual = {p for p in set(current) | set(run["baseline"])
                      if current.get(p) != run["baseline"].get(p)}
            if set(changed) != actual or not actual.issubset(feature["allowed_paths"]):
                raise CompanionError("实际修改与回报/允许范围不一致，请核查后重新回报")
            for name in strings(raw.get("evidence_files", []), "证据文件"):
                safe_file(self.root, name, exists=True)
                if name not in feature["allowed_paths"]:
                    raise CompanionError("执行回报的证据文件不在本次约定文件范围")
            if raw["status"] == "blocked":
                text(raw.get("blocker"), "阻塞原因")
            task.update(status="blocked" if raw["status"] == "blocked" else "awaiting_review",
                        receipt=raw, reported_at=now())
            self.commit(state, {"kind": "worker_reported", "feature_id": identifier, "run_id": run["run_id"]})
            return {"revision": state["revision"], "status": task["status"],
                    "message": "执行回报已记录；尚未计入已验收"}

    def check(self, identifier):
        with self.locked():
            state = self.load()
            self.require_confirmed(state)
            self.require_idle(state)
            feature, task = self.feature(state, identifier)
            if task["status"] not in {"awaiting_review", "accepted"}:
                raise CompanionError("先完成当前制作回报，再进行检查")
            if not feature["check_commands"]:
                raise CompanionError("尚未约定可执行检查，不能标为通过；请完善需求范围后再确认")
            for name in feature["allowed_paths"]:
                safe_file(self.root, name)
            before = self.snapshot()
            results = []
            for argv in feature["check_commands"]:
                try:
                    with tempfile.TemporaryFile() as output:
                        completed = subprocess.run(argv, cwd=str(self.root), stdin=subprocess.DEVNULL,
                                                   stdout=output, stderr=subprocess.STDOUT, timeout=120, shell=False)
                        output.seek(0)
                        body = output.read(65537)
                    results.append({"argv": argv, "exit_code": completed.returncode,
                                    "output": body[:65536].decode("utf-8", errors="replace"),
                                    "truncated": len(body) > 65536, "executed": True})
                except subprocess.TimeoutExpired:
                    results.append({"argv": argv, "exit_code": None, "output": "检查超时（120秒）",
                                    "executed": True, "truncated": False})
                except OSError as exc:
                    results.append({"argv": argv, "exit_code": None, "output": str(exc),
                                    "executed": False, "truncated": False})
                if results[-1]["exit_code"] != 0:
                    break
            after = self.snapshot()
            passed = (len(results) == len(feature["check_commands"]) and
                      all(r["executed"] and r["exit_code"] == 0 for r in results) and
                      before["fingerprint"] == after["fingerprint"])
            verification = {"id": uuid.uuid4().hex, "at": now(), "passed": passed,
                            "fingerprint": after["fingerprint"], "content_changed": before["fingerprint"] != after["fingerprint"],
                            "results": results, "excluded_paths": after["excluded"]}
            task.update(status="awaiting_review", verification=verification)
            task.pop("acceptance", None)
            self.commit(state, {"kind": "checked", "feature_id": identifier,
                                "verification_id": verification["id"], "passed": passed})
            return {"revision": state["revision"], **verification}

    def accept(self, identifier, note, user_confirmed=False):
        note = text(note, "验收说明")
        with self.locked():
            state = self.load()
            self.require_confirmed(state)
            self.require_idle(state)
            feature, task = self.feature(state, identifier)
            verification = task.get("verification", {})
            if (task["status"] != "awaiting_review" or not verification.get("passed") or
                    verification.get("fingerprint") != self.snapshot()["fingerprint"]):
                raise CompanionError("缺少当前内容的成功检查，请先运行 check")
            if feature["requires_user_acceptance"] and not user_confirmed:
                raise CompanionError("此功能还需要用户实际试用并确认")
            task.update(status="accepted", acceptance={"at": now(), "note": note,
                        "user_confirmed": user_confirmed, "verification_id": verification["id"]})
            self.commit(state, {"kind": "accepted", "feature_id": identifier, "note": note})
            return {"revision": state["revision"], "status": "accepted"}

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

    def status(self):
        state = self.load()
        recovery = self.recovery_status()
        snapshot = self.snapshot()
        features, counts = [], {s: 0 for s in ("pending", "running", "awaiting_review", "accepted", "blocked")}
        for feature in state["scope"]["features"]:
            task = state["tasks"][feature["id"]]
            stale = (task.get("verification", {}).get("fingerprint") != snapshot["fingerprint"])
            status = "awaiting_review" if task["status"] == "accepted" and stale else task["status"]
            counts[status] += 1
            features.append({**feature, "status": status, "evidence_stale": stale and "verification" in task,
                             "blocker": task.get("blocker") or task.get("receipt", {}).get("blocker"),
                             "verification": task.get("verification"), "acceptance": task.get("acceptance")})
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
        return {"schema_version": 1, "title": state["scope"]["title"], "goal": state["scope"]["goal"],
                "scope": state["scope"], "revision": state["revision"], "scope_version": state["scope_version"],
                "confirmed": state["confirmed"], "updated_at": state["updated_at"], "observed_at": now(),
                "counts": counts, "total": total, "overall_percent": (counts["accepted"] * 100 // total) if state["confirmed"] and not recovery else None,
                "next_step": next_step, "features": features, "excluded_paths": snapshot["excluded"],
                "recovery": recovery,
                "publication": "未核验发布状态", "history": state["events"][-10:]}


LABELS = {"pending": "待开始", "running": "制作中", "awaiting_review": "待验收",
          "accepted": "已验收", "blocked": "受阻"}


def render_markdown(view):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    progress = ("恢复未完成，暂不能计算" if view.get("recovery") else "范围待确认，暂不能计算") if view["overall_percent"] is None else "%s/%s 项已验收（%s%%）" % (
        view["counts"]["accepted"], view["total"], view["overall_percent"])
    lines = ["# " + cell(view["title"]), "", cell(view["goal"]), "", "**本版完成度：** " + progress,
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
<section class="summary"><h2>%s</h2><strong>下一步：%s</strong><p>这是本次读取的状态快照。重新运行进度命令可刷新；存档、验收与发布分别记录。</p></section>
<div class="grid">%s</div><section><h2>本版范围</h2><p>暂不包含：%s</p><p>假设与待确认：%s</p></section>
<footer>范围版本 %s · 记录更新 %s · 本次读取 %s<br>发布状态：%s</footer></main></html>''' % (
        esc(view["title"]), esc(view["title"]), esc(view["goal"]), esc(progress), esc(view["next_step"]),
        "".join(cards), esc("；".join(view["scope"]["out_of_scope"]) or "尚未列出"),
        esc("；".join(view["scope"]["assumptions"]) or "当前未列出"), view["scope_version"],
        esc(view["updated_at"]), esc(view["observed_at"]), esc(view["publication"]))

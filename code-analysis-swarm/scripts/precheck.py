#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""code-analysis-swarm 确定性预检 helper（Z02/Z09，四根目录分离）。

只依赖 Python 3.9+ 标准库。子命令经 argv 传入；复杂输入经单个 JSON
（--json <字符串> 或 --json-file <路径>），路径从不经 shell 拼接。
stdout 恒为单行 JSON；错误输出只含路径与原因，不含密钥、环境变量或完整环境。

子命令与退出码：
  plan        计算四个根（source/host_workspace/run/team），校验目录关系、
              敏感路径与角色文件存在；不创建、不写任何东西。
  acquire     重复 plan 全部校验后，对 run_root 做排他创建（父目录允许补建，
              最后一级必须不存在），并写 run_root/precheck.json 回执。
  verify      校验 run_root 不含于 source_root、outputs 均在 run_root 内、
              敏感路径拒绝（复核用，不创建）。
  read-report 校验报告文件真实存在（普通文件、位于 within_root 内、大小合规），
              输出 size/sha256/正文/publish_relpath。

退出码：0 成功；2 参数/输入无效；3 目录关系冲突或排他创建失败；4 敏感路径。
"""

import hashlib
import json
import os
import secrets
import stat
import sys
from datetime import datetime, timezone

EXIT_OK = 0
EXIT_ARGUMENT = 2
EXIT_CONFLICT = 3
EXIT_SENSITIVE = 4

KIND_ARGUMENT = "argument"
KIND_CONFLICT = "conflict"
KIND_SENSITIVE = "sensitive"

MAX_REPORT_BYTES = 200_000
DEFAULT_RUNS_DIRNAME = ".code-analysis-swarm-runs"
RUN_ID_MAX = 80

ROLE_FILES = [
    "a1-scout.md",
    "a2-module-analyst.md",
    "a3-architect.md",
    "a4-dependency.md",
    "a5-build.md",
    "a6-verifier.md",
    "a7-reporter.md",
]

# 敏感目录：作为 source_root 或 run_root 的任一级祖先即拒绝。
SENSITIVE_NAMES = {".ssh", ".aws", ".gnupg", ".kube", ".netrc", ".git-credentials"}
SENSITIVE_PAIRS = {(".config", "gcloud")}


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(EXIT_OK)


def fail(code, kind, message):
    print(json.dumps({"ok": False, "kind": kind, "error": message}, ensure_ascii=False))
    sys.exit(code)


def require_string(payload, key):
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"{key} 必须是非空字符串")
    if "\x00" in value:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"{key} 含控制字符")
    return value


def valid_run_id(run_id):
    if not run_id or len(run_id) > RUN_ID_MAX:
        return False
    if not run_id[0].isalnum() or not run_id.isascii():
        return False
    return all(c.isalnum() or c in "-_" for c in run_id)


def is_inside(child, parent):
    """realpath 之后的包含判定；child == parent 视为包含。"""
    if child == parent:
        return True
    return child.startswith(parent.rstrip(os.sep) + os.sep)


def sensitive_component(realpath):
    segments = [s for s in realpath.split(os.sep) if s]
    for segment in segments:
        if segment in SENSITIVE_NAMES:
            return segment
    for i in range(len(segments) - 1):
        if (segments[i], segments[i + 1]) in SENSITIVE_PAIRS:
            return segments[i] + os.sep + segments[i + 1]
    return None


def relpath_within(path, root):
    """path 相对 root 的路径；不在 root 内返回 None。"""
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    except ValueError:
        return None
    return None if rel == os.pardir or rel.startswith(os.pardir + os.sep) else rel


def lstat_kind(path):
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return "missing"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "regular"
    return "other"


def resolve_source_root(payload):
    source_input = require_string(payload, "source_root")
    source_real = os.path.realpath(source_input)
    if not os.path.isdir(source_real):
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"source_root 不是现有目录：{source_input}")
    hit = sensitive_component(source_real)
    if hit:
        fail(EXIT_SENSITIVE, KIND_SENSITIVE, f"source_root 位于敏感目录 {hit} 内，拒绝审计")
    return source_input, source_real


def resolve_team_root(payload):
    team_input = payload.get("team_root")
    if team_input is None:
        return None, None
    if not isinstance(team_input, str) or not team_input.strip():
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, "team_root 提供时必须是非空字符串")
    team_real = os.path.realpath(team_input)
    for role in ROLE_FILES:
        role_path = os.path.join(team_real, "agents", role)
        if not os.path.isfile(role_path):
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"team_root 缺少角色文件 agents/{role}")
    return team_input, team_real


def compute_run_root(payload, workspace_root):
    run_id = payload.get("run_id")
    if run_id is not None:
        if not isinstance(run_id, str) or not valid_run_id(run_id):
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, "run_id 格式无效（仅限字母数字下划线连字符）")
    else:
        run_id = "run-" + secrets.token_hex(12)
    parent_input = payload.get("run_root_parent")
    if parent_input is None:
        run_parent = os.path.join(workspace_root, DEFAULT_RUNS_DIRNAME)
    else:
        if not isinstance(parent_input, str) or not parent_input.strip():
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, "run_root_parent 提供时必须是非空字符串")
        run_parent = os.path.realpath(parent_input)
    return run_id, run_parent, os.path.realpath(os.path.join(run_parent, run_id))


def check_roots(run_root_real, source_real, team_real):
    if is_inside(run_root_real, source_real):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"run_root 不得位于 source_root 内：{run_root_real}")
    if team_real and is_inside(run_root_real, team_real):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"run_root 不得位于插件安装目录内：{run_root_real}")
    hit = sensitive_component(run_root_real)
    if hit:
        fail(EXIT_SENSITIVE, KIND_SENSITIVE, f"run_root 位于敏感目录 {hit} 内，拒绝写入")


def roots_payload(source_input, source_real, team_real, workspace_root, run_root_real):
    return {
        "source_root": {"input": source_input, "realpath": source_real, "lstat_kind": lstat_kind(source_input)},
        "team_root": {"realpath": team_real} if team_real else None,
        "host_workspace_root": {"realpath": workspace_root},
        "run_root": {"realpath": run_root_real, "publish_relpath": relpath_within(run_root_real, workspace_root)},
    }


def cmd_plan(payload):
    source_input, source_real = resolve_source_root(payload)
    team_input, team_real = resolve_team_root(payload)
    workspace_root = os.path.realpath(os.getcwd())
    run_id, run_parent, run_root_real = compute_run_root(payload, workspace_root)
    check_roots(run_root_real, source_real, team_real)
    emit({
        "ok": True,
        "run_id": run_id,
        "run_root_exists": os.path.exists(run_root_real),
        "roots": roots_payload(source_input, source_real, team_real, workspace_root, run_root_real),
        "checks": {
            "source_root_is_dir": True,
            "run_root_outside_source_root": True,
            "run_root_outside_team_root": True,
            "sensitive_paths_clear": True,
            "team_role_files_present": team_real is not None,
        },
    })


def cmd_acquire(payload):
    source_input, source_real = resolve_source_root(payload)
    team_input, team_real = resolve_team_root(payload)
    workspace_root = os.path.realpath(os.getcwd())
    run_id, run_parent, run_root_real = compute_run_root(payload, workspace_root)
    check_roots(run_root_real, source_real, team_real)
    try:
        os.makedirs(run_parent, exist_ok=True)
    except OSError as exc:
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"无法准备运行目录父目录 {run_parent}：{exc.strerror or exc}")
    try:
        os.mkdir(run_root_real)
    except FileExistsError:
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"运行目录已存在，拒绝复用或覆盖：{run_root_real}")
    except OSError as exc:
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"运行目录排他创建失败 {run_root_real}：{exc.strerror or exc}")
    receipt = {
        "ok": True,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "mkdir_exclusive",
        "helper_version": 1,
        "roots": roots_payload(source_input, source_real, team_real, workspace_root, run_root_real),
    }
    receipt_path = os.path.join(run_root_real, "precheck.json")
    try:
        with open(receipt_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(receipt, ensure_ascii=False) + "\n")
    except OSError as exc:
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"回执写入失败 {receipt_path}：{exc.strerror or exc}")
    emit(receipt)


def cmd_verify(payload):
    source_input, source_real = resolve_source_root(payload)
    run_root_real = os.path.realpath(require_string(payload, "run_root"))
    check_roots(run_root_real, source_real, None)
    outputs = payload.get("outputs")
    if outputs is not None:
        if not isinstance(outputs, list) or not all(isinstance(o, str) and o.strip() for o in outputs):
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, "outputs 必须是非空字符串数组")
        for output in outputs:
            if not is_inside(os.path.realpath(output), run_root_real):
                fail(EXIT_CONFLICT, KIND_CONFLICT, f"输出路径不在 run_root 内：{output}")
    emit({
        "ok": True,
        "run_root_outside_source_root": True,
        "outputs_inside_run_root": True,
        "run_root_realpath": run_root_real,
        "source_root_realpath": source_real,
    })


def cmd_read_report(payload):
    path_input = require_string(payload, "path")
    within_root = os.path.realpath(require_string(payload, "within_root"))
    path_real = os.path.realpath(path_input)
    if not is_inside(path_real, within_root):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"报告路径不在 within_root 内：{path_input}")
    if not os.path.isfile(path_real):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"报告不是现有普通文件：{path_input}")
    try:
        with open(path_real, "rb") as fh:
            raw = fh.read(MAX_REPORT_BYTES + 1)
    except OSError as exc:
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"报告无法读取 {path_input}：{exc.strerror or exc}")
    if len(raw) > MAX_REPORT_BYTES:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"报告超过 {MAX_REPORT_BYTES} 字节上限：{path_input}")
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"报告不是有效 UTF-8 文本：{path_input}")
    emit({
        "ok": True,
        "path": path_input,
        "realpath": path_real,
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "body": body,
        "publish_relpath": relpath_within(path_real, os.getcwd()),
    })


COMMANDS = {
    "plan": cmd_plan,
    "acquire": cmd_acquire,
    "verify": cmd_verify,
    "read-report": cmd_read_report,
}


def load_payload(options):
    if "json" in options and "json-file" in options:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, "--json 与 --json-file 只能二选一")
    if "json-file" in options:
        try:
            with open(options["json-file"], "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"--json-file 无法读取：{exc.strerror or exc}")
    elif "json" in options:
        text = options["json"]
    else:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, "缺少 --json 或 --json-file 输入")
    try:
        payload = json.loads(text)
    except ValueError:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, "输入不是合法 JSON")
    if not isinstance(payload, dict):
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, "输入 JSON 必须是对象")
    return payload


def main(argv):
    if len(argv) < 1 or argv[0] not in COMMANDS:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, "用法：precheck.py plan|acquire|verify|read-report (--json <JSON>|--json-file <路径>)")
    options, i = {}, 1
    while i < len(argv):
        flag = argv[i]
        if flag not in ("--json", "--json-file") or i + 1 >= len(argv):
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"未知或残缺参数：{flag}")
        options[flag[2:]] = argv[i + 1]
        i += 2
    COMMANDS[argv[0]](load_payload(options))


if __name__ == "__main__":
    main(sys.argv[1:])

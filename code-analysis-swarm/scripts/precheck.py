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
  check-files 校验一批路径真实存在：均须位于 within_root 内（realpath 后判定，
              拒绝符号链接逃逸）、为普通文件且非空；全部合格 exit 0 并输出明细，
              否则 exit 3，错误回执含 missing 数组（Z11 制品存在性核验）。
  read-report 校验报告文件真实存在（普通文件、位于 within_root 内、大小合规），
              输出 size/sha256/正文/publish_relpath。
  claims-write校验送验结论清单（id/claim/source_role 非空、evidence_refs 非空数组、
              id 唯一）后写入 run_root/verification/claims.json（C10：全量 claims
              先落盘，供 A6 分页领取；协调者不把全部 claims stringify 进 prompt）。
  claims-page 从 run_root 内的 claims 分页文件有界读取一页：
              --json {within_root, claims_file, page, page_size} →
              {ok, total, pages, page, page_size, claims}；越界页/非法 page_size
              拒绝（exit 2），文件越界/缺失拒绝（exit 3），复用 read-report 校验风格。

SR-06 主链路容量桥接子命令（索引分页/有界派发/检查点续接/覆盖账）：
  index-count 纯标准库粗计数（os.scandir 增量，跳过 .git 与 symlink），仅供
              "小库直读 vs 大库索引"阈值分类，不是普查证据（真实普查归 A1 或
              index-scan）→ {ok, source_root, file_count}。
  以下子命令需要 agents_kernel（_ensure_kernel 引导：本目录 _kernel_vendor/
              优先，回退仓库 packages/；缺失 exit 2 明确报因）：
  index-scan  经 agents_kernel.indexing.scanner.IndexScanner 对 source_root 做
              流式普查，写入 run_root/index/facts.sqlite（世代语义与排除账全部
              复用内核，不复制逻辑）；完成后写 run_root/index/manifest.json
              （generation/file_count/source_anchor 指纹）→ 回执含两者路径。
  index-page  经 agents_kernel.indexing.reader.IndexReader 对索引 files 表做
              keyset 分页只读查询：{run_root, after?, limit?} →
              {ok, rows, cursor, generation, file_count, source_anchor}。
  chunks-write校验 A1 分块规划 schema（id/尺寸/邻块/跨块文件唯一）后原子写入
              run_root/index/chunks.json → {ok, chunks_file, chunk_count,
              file_count_total}。
  index-verify分页核对分块覆盖 = 索引全集：读 chunks.json 全部文件路径，经
              IndexReader keyset 分页拉取索引全集比对（协调者内存不经全集）；
              缺口/多余/重复 → exit 3 并附样本（≤20 条），闭合 → exit 0。
  chunks-page 从 chunks.json 分页领取"未完成"块（跳过 g2-progress 检查点已
              completed 的块）：{run_root, page, page_size, anchor}；anchor 与
              索引 manifest 的 source_anchor 不符 → exit 3（混代拒绝）。
              检查点即游标：消费方恒取 page 0（"下一批未完成块"），空批即终态
              （pending 随完成动态收缩，page 序号不稳定）；pending 非空时 page
              越界仍拒绝（exit 2，防失控循环）。
  g2-progress G2 批次进度检查点（run_root/checkpoints/g2-progress.json，原子
              写）：action=init（绑定 anchor/generation/chunk_order，anchor
              冲突拒绝）/mark（幂等标记单块完成）/read。
  resume-register经 agents_kernel.services.resume.ResumeLedger 登记 G2 活动
              （checkpoint_path 须在 run_root 内；source_anchor 绑定索引指纹）。
  resume-check经 ResumeLedger.resume_plan 核对活动：源锚变化 → condition=
              stale_anchor（拒绝混代续跑）；完成 → completed。
  coverage-record经 agents_kernel.services.coverage.CoverageLedger 记账三维度
              覆盖并原子写 run_root/coverage_account.json。

退出码：0 成功；2 参数/输入无效；3 目录关系冲突或排他创建失败；4 敏感路径。
"""

import hashlib
import json
import os
import re
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
MAX_CLAIMS_BYTES = 20_000_000
DEFAULT_CLAIM_PAGE_SIZE = 80
MAX_CLAIM_PAGE_SIZE = 500
DEFAULT_RUNS_DIRNAME = ".code-analysis-swarm-runs"
RUN_ID_MAX = 80

# —— SR-06 桥接常量（与 packages/agents_kernel 对应模块同源口径，不另造语义）——
INDEX_DIR = "index"
INDEX_DB_REL = INDEX_DIR + "/facts.sqlite"
INDEX_MANIFEST_REL = INDEX_DIR + "/manifest.json"
CHUNKS_REL = INDEX_DIR + "/chunks.json"
COVERAGE_REL = "coverage_account.json"
CHECKPOINTS_DIR = "checkpoints"
G2_PROGRESS_REL = CHECKPOINTS_DIR + "/g2-progress.json"
CHUNK_ID_PATTERN = re.compile(r"^chunk-[A-Za-z0-9_-]+$")
MAX_FILES_PER_CHUNK = 150          # 与 DWF G1 块尺寸上限一致
MAX_LOC_EST = 5000                 # 与 DWF G1 块 LOC 上限一致
G2_PROGRESS_VERSION = 1
INDEX_MANIFEST_VERSION = 1
COVERAGE_DIMENSIONS = ("index_files", "semantics_deep", "independent_review")

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


def fail(code, kind, message, extra=None):
    payload = {"ok": False, "kind": kind, "error": message}
    if extra:
        payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))
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


def cmd_check_files(payload):
    within_root = os.path.realpath(require_string(payload, "within_root"))
    paths = payload.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p.strip() for p in paths):
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, "paths 必须是非空字符串数组")
    files, missing = [], []
    for path in paths:
        real = os.path.realpath(path)
        inside = is_inside(real, within_root)
        kind = lstat_kind(path) if inside else "outside_root"
        size = None
        if kind == "regular":
            try:
                size = os.path.getsize(real)
            except OSError:
                size = None
        files.append({"path": path, "realpath": real, "inside_root": inside, "kind": kind, "size": size})
        if not inside or kind != "regular" or not size:
            missing.append(path)
    if missing:
        fail(EXIT_CONFLICT, KIND_CONFLICT,
             "以下制品不存在、越界或为空：" + "、".join(missing), {"missing": missing})
    emit({"ok": True, "files": files})


def validate_claims(claims):
    """C10 送验结论 schema：id/claim/source_role 非空、evidence_refs 非空字符串数组、id 唯一。"""
    if not isinstance(claims, list) or not claims:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, "claims 必须是非空数组")
    seen = set()
    for claim in claims:
        if not isinstance(claim, dict):
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, "claims 元素必须是对象")
        for key in ("id", "claim", "source_role"):
            value = claim.get(key)
            if not isinstance(value, str) or not value.strip():
                fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"结论缺少非空 {key}")
        refs = claim.get("evidence_refs")
        if not isinstance(refs, list) or not refs \
                or not all(isinstance(r, str) and r.strip() for r in refs):
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, "结论 evidence_refs 必须是非空字符串数组")
        if claim["id"] in seen:
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"结论 ID 重复：{claim['id']}")
        seen.add(claim["id"])
    return claims


def cmd_claims_write(payload):
    run_root = os.path.realpath(require_string(payload, "run_root"))
    claims = validate_claims(payload.get("claims"))
    if not os.path.isdir(run_root):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"run_root 不是现有目录：{run_root}")
    hit = sensitive_component(run_root)
    if hit:
        fail(EXIT_SENSITIVE, KIND_SENSITIVE, f"run_root 位于敏感目录 {hit} 内，拒绝写入")
    body = json.dumps(claims, ensure_ascii=False, indent=2) + "\n"
    if len(body.encode("utf-8")) > MAX_CLAIMS_BYTES:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"claims 超过 {MAX_CLAIMS_BYTES} 字节上限")
    claims_path = os.path.join(run_root, "verification", "claims.json")
    try:
        os.makedirs(os.path.dirname(claims_path), exist_ok=True)
        with open(claims_path, "w", encoding="utf-8") as fh:
            fh.write(body)
    except OSError as exc:
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"claims 写入失败 {claims_path}：{exc.strerror or exc}")
    emit({"ok": True, "claims_file": claims_path, "total": len(claims),
          "page_size_default": DEFAULT_CLAIM_PAGE_SIZE})


def cmd_claims_page(payload):
    run_root = os.path.realpath(require_string(payload, "within_root"))
    claims_file = os.path.realpath(require_string(payload, "claims_file"))
    if not is_inside(claims_file, run_root):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"claims 文件不在 run_root 内：{claims_file}")
    if not os.path.isfile(claims_file):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"claims 不是现有普通文件：{claims_file}")
    page = payload.get("page")
    if not isinstance(page, int) or isinstance(page, bool) or page < 0:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, "page 必须是非负整数")
    page_size = payload.get("page_size")
    if not isinstance(page_size, int) or isinstance(page_size, bool) \
            or not 1 <= page_size <= MAX_CLAIM_PAGE_SIZE:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"page_size 必须在 1..{MAX_CLAIM_PAGE_SIZE}")
    try:
        with open(claims_file, "rb") as fh:
            raw = fh.read(MAX_CLAIMS_BYTES + 1)
    except OSError as exc:
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"claims 无法读取 {claims_file}：{exc.strerror or exc}")
    if len(raw) > MAX_CLAIMS_BYTES:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"claims 超过 {MAX_CLAIMS_BYTES} 字节上限：{claims_file}")
    try:
        claims = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"claims 不是有效 UTF-8 JSON：{claims_file}")
    if not isinstance(claims, list):
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"claims 文件内容不是数组：{claims_file}")
    pages = (len(claims) + page_size - 1) // page_size
    if page >= pages:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT,
             f"page {page} 越界：共 {pages} 页 {len(claims)} 条",
             {"pages": pages, "total": len(claims)})
    start = page * page_size
    emit({"ok": True, "claims_file": claims_file, "total": len(claims), "pages": pages,
          "page": page, "page_size": page_size, "claims": claims[start:start + page_size]})


# —— SR-06 主链路容量桥接 ——
# 选型：经 tools/build_vendor.py 生成的 _kernel_vendor 私有副本 import agents_kernel
# （与 dev-companion/scripts/kernel_bootstrap.py 同模式），不复制索引/调度逻辑入本
# 文件（分发复制而非策略分叉：副本禁止手改，一律由 build_vendor 再生成）。引导代码
# 内嵌于本文件——两插件独立安装物理分离，无法 import 对方模块；本文件含 "agents_kernel"
# 字样即被 build_vendor.uses_kernel 检测，自动生成 swarm vendor。仅 index/g2/resume/
# coverage 子命令需要内核；plan/acquire/verify/check-files/read-report/claims-* 保持
# 纯标准库，无 vendor 的既有安装零回归。
_KERNEL_SOURCE = None


def _ensure_kernel():
    """使 import agents_kernel 可解析：vendor 优先，回退仓库布局；返回实际来源。"""
    global _KERNEL_SOURCE
    if _KERNEL_SOURCE is not None:
        return _KERNEL_SOURCE
    here = os.path.dirname(os.path.abspath(__file__))
    vendor = os.path.join(here, "_kernel_vendor")
    if os.path.isdir(os.path.join(vendor, "agents_kernel")):
        if vendor not in sys.path:
            sys.path.insert(0, vendor)
        _KERNEL_SOURCE = "vendor"
        return _KERNEL_SOURCE
    base = here
    while True:
        packages = os.path.join(base, "packages")
        if os.path.isdir(os.path.join(packages, "agents_kernel")):
            if packages not in sys.path:
                sys.path.insert(0, packages)
            _KERNEL_SOURCE = "packages"
            return _KERNEL_SOURCE
        parent = os.path.dirname(base)
        if parent == base:
            fail(EXIT_ARGUMENT, KIND_ARGUMENT,
                 "找不到 agents_kernel：优先本目录 _kernel_vendor/（tools/build_vendor.py 生成），"
                 "或回退仓库根 packages/agents_kernel")
        base = parent


def _require_int(value, label, minimum=0):
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"{label} 必须是不小于 {minimum} 的整数")
    return value


def _require_anchor(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, "anchor 必须是 64 位小写十六进制 sha256")
    return value


def _check_run_root(run_root):
    """index/g2/resume/coverage 子命令共用的 run_root 基本校验。"""
    if not os.path.isdir(run_root):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"run_root 不是现有目录：{run_root}")
    hit = sensitive_component(run_root)
    if hit:
        fail(EXIT_SENSITIVE, KIND_SENSITIVE, f"run_root 位于敏感目录 {hit} 内，拒绝写入")


def _load_json_file(path, label, max_bytes):
    try:
        with open(path, "rb") as fh:
            raw = fh.read(max_bytes + 1)
    except OSError as exc:
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"{label} 无法读取 {path}：{exc.strerror or exc}")
    if len(raw) > max_bytes:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"{label} 超过 {max_bytes} 字节上限：{path}")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"{label} 不是有效 UTF-8 JSON：{path}")


def _load_index_manifest(run_root):
    """读 run_root/index/manifest.json 并校验形状；缺失/损坏拒绝（不造空 manifest）。"""
    path = os.path.join(run_root, INDEX_MANIFEST_REL)
    if not os.path.isfile(path):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"索引 manifest 不存在（先运行 index-scan）：{path}")
    manifest = _load_json_file(path, "索引 manifest", MAX_REPORT_BYTES)
    if not isinstance(manifest, dict) or manifest.get("kind") != "index_manifest" \
            or not isinstance(manifest.get("generation"), int) or isinstance(manifest.get("generation"), bool) \
            or manifest["generation"] < 1 \
            or not isinstance(manifest.get("root"), str) \
            or not _is_int_like(manifest.get("file_count")) or manifest["file_count"] < 0 \
            or not isinstance(manifest.get("source_anchor"), str) \
            or not re.fullmatch(r"[0-9a-f]{64}", manifest.get("source_anchor", "")):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"索引 manifest 字段不完整或版本不符：{path}")
    return manifest


def _is_int_like(value):
    return isinstance(value, int) and not isinstance(value, bool)


def cmd_index_count(payload):
    """纯标准库粗计数：仅供阈值分类，不作为普查证据（详见子命令文档）。"""
    source_input, source_real = resolve_source_root(payload)
    count = 0
    pending = [source_real]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as children:
                for child in children:
                    try:
                        if child.is_symlink():
                            continue
                        if child.is_dir(follow_symlinks=False):
                            if child.name != ".git":
                                pending.append(child.path)
                        elif child.is_file(follow_symlinks=False):
                            count += 1
                    except OSError:
                        continue
        except OSError:
            if directory == source_real:
                fail(EXIT_CONFLICT, KIND_CONFLICT, f"source_root 不可读：{source_input}")
            continue
    emit({"ok": True, "source_root": source_input, "file_count": count})


def cmd_index_scan(payload):
    _ensure_kernel()
    from agents_kernel.atomicio import write_json
    from agents_kernel.indexing.scanner import IndexScanner
    from agents_kernel.storage import db
    source_input, source_real = resolve_source_root(payload)
    run_root = os.path.realpath(require_string(payload, "run_root"))
    _check_run_root(run_root)
    check_roots(run_root, source_real, None)
    store = db.Store(os.path.join(run_root, INDEX_DB_REL))
    store.open()
    try:
        writer = db.acquire_writer(store)
        try:
            result = IndexScanner(store).scan(source_real, writer)
        finally:
            writer.close()
        store.checkpoint()  # WAL 并回主库：manifest 指纹与回执对应的库为完整事实
    finally:
        store.close()
    # source_anchor：索引世代内容指纹（不含墙钟），续跑/领取据此拒绝混代。
    anchor = hashlib.sha256(json.dumps(
        {"generation": result.generation, "root": source_real,
         "mode": result.mode, "file_count": result.file_count},
        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    manifest_path = os.path.join(run_root, INDEX_MANIFEST_REL)
    manifest = {
        "version": INDEX_MANIFEST_VERSION, "kind": "index_manifest",
        "generation": result.generation, "root": source_input,
        "root_realpath": source_real, "mode": result.mode,
        "file_count": result.file_count, "excluded_count": result.excluded_count,
        "batch_count": result.batch_count, "source_anchor": anchor,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        write_json(manifest_path, manifest)
    except OSError as exc:
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"索引 manifest 写入失败 {manifest_path}：{exc}")
    emit({"ok": True, "index_db": os.path.join(run_root, INDEX_DB_REL),
          "manifest": manifest_path, "kernel_source": _KERNEL_SOURCE,
          "generation": result.generation, "mode": result.mode,
          "file_count": result.file_count, "excluded_count": result.excluded_count,
          "batch_count": result.batch_count, "source_anchor": anchor})


def cmd_index_page(payload):
    _ensure_kernel()
    from agents_kernel.indexing.reader import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, IndexReader
    from agents_kernel.storage import db
    run_root = os.path.realpath(require_string(payload, "run_root"))
    _check_run_root(run_root)
    manifest = _load_index_manifest(run_root)
    index_db = payload.get("index_db")
    if index_db is None:
        index_db = os.path.join(run_root, INDEX_DB_REL)
    index_db = os.path.realpath(index_db)
    if not is_inside(index_db, run_root):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"index_db 不在 run_root 内：{index_db}")
    if not os.path.isfile(index_db):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"索引库不是普通文件或不存在：{index_db}")
    after = payload.get("after")
    if after is not None and not isinstance(after, str):
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, "after 必须是字符串或省略")
    limit = payload.get("limit", DEFAULT_PAGE_SIZE)
    _require_int(limit, "limit", 1)
    if limit > MAX_PAGE_SIZE:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"limit 最大 {MAX_PAGE_SIZE}")
    store = db.Store(index_db)
    store.open_readonly()
    try:
        page = IndexReader(store).page_files(after=after, limit=limit,
                                             generation=manifest["generation"])
        rows = [dict(row) for row in page.rows]
    finally:
        store.close()
    emit({"ok": True, "after": after, "limit": limit,
          "generation": page.generation, "file_count": manifest["file_count"],
          "source_anchor": manifest["source_anchor"],
          "rows": rows, "cursor": page.cursor})


def _validate_chunks(chunks):
    """A1 分块规划 schema：id/尺寸/邻块/依据 + 跨块文件唯一（重复早失败）。"""
    if not isinstance(chunks, list) or not chunks:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, "chunks 必须是非空数组")
    ids, owner, duplicates = set(), {}, []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, "chunks 元素必须是对象")
        cid = chunk.get("id")
        if not isinstance(cid, str) or not CHUNK_ID_PATTERN.fullmatch(cid):
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"块 ID 无效：{cid!r}")
        if cid in ids:
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"块 ID 重复：{cid}")
        ids.add(cid)
        if not isinstance(chunk.get("rationale"), str) or not chunk["rationale"].strip():
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"块 {cid} 缺少分块依据")
        files = chunk.get("files")
        if not isinstance(files, list) or not files \
                or not all(isinstance(f, str) and f.strip() for f in files):
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"块 {cid} files 必须是非空字符串数组")
        if len(files) > MAX_FILES_PER_CHUNK:
            fail(EXIT_ARGUMENT, KIND_ARGUMENT,
                 f"块 {cid} 文件数 {len(files)} 超过 {MAX_FILES_PER_CHUNK}")
        if not _is_int_like(chunk.get("loc_est")) or not 0 <= chunk["loc_est"] <= MAX_LOC_EST:
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"块 {cid} loc_est 必须在 0..{MAX_LOC_EST}")
        neighbors = chunk.get("neighbors")
        if not isinstance(neighbors, list) \
                or not all(isinstance(n, str) and n.strip() for n in neighbors):
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"块 {cid} neighbors 必须是字符串数组")
        seen = set()
        for f in files:
            if f in seen:
                fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"块 {cid} 内文件重复：{f}")
            seen.add(f)
            if f in owner:
                duplicates.append(f)
            owner[f] = cid
    for chunk in chunks:
        for neighbor in chunk["neighbors"]:
            if neighbor == chunk["id"] or neighbor not in ids:
                fail(EXIT_ARGUMENT, KIND_ARGUMENT,
                     f"块 {chunk['id']} 邻块引用不存在或自引用：{neighbor}")
    return chunks, owner, sorted(set(duplicates))


def cmd_chunks_write(payload):
    _ensure_kernel()
    from agents_kernel.atomicio import write_json
    run_root = os.path.realpath(require_string(payload, "run_root"))
    _check_run_root(run_root)
    chunks, owner, duplicates = _validate_chunks(payload.get("chunks"))
    if duplicates:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT,
             "分块跨块文件重复：" + "、".join(duplicates[:20]),
             {"duplicate_sample": duplicates[:20], "duplicate_count": len(duplicates)})
    chunks_path = os.path.join(run_root, CHUNKS_REL)
    try:
        write_json(chunks_path, chunks)
    except OSError as exc:
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"chunks 写入失败 {chunks_path}：{exc}")
    emit({"ok": True, "chunks_file": chunks_path, "chunk_count": len(chunks),
          "file_count_total": len(owner)})


def _strip_root_prefix(path, prefixes):
    """绝对路径 → 索引相对路径：剥清单里登记的目标根前缀（root 与 realpath 双前缀，
    Windows 盘符大小写不敏感）。剥不掉的原样返回——闭合比对会按 extra/missing 拒绝。"""
    p = path.replace("\\", "/")
    for prefix in prefixes:
        if not isinstance(prefix, str) or not prefix:
            continue
        q = prefix.replace("\\", "/").rstrip("/")
        if p == q:
            return ""
        if p.lower().startswith(q.lower() + "/"):
            return p[len(q) + 1:]
    return p


def cmd_index_verify(payload):
    """分页核对分块覆盖 = 索引全集；比对在 python 侧，全集不进协调者上下文/回执。"""
    _ensure_kernel()
    from agents_kernel.indexing.reader import IndexReader
    from agents_kernel.storage import db
    run_root = os.path.realpath(require_string(payload, "run_root"))
    _check_run_root(run_root)
    manifest = _load_index_manifest(run_root)
    index_db = os.path.realpath(os.path.join(run_root, INDEX_DB_REL))
    if not os.path.isfile(index_db):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"索引库不是普通文件或不存在：{index_db}")
    chunks_path = os.path.join(run_root, CHUNKS_REL)
    if not os.path.isfile(chunks_path):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"分块规划不存在（先运行 chunks-write）：{chunks_path}")
    chunks = _load_json_file(chunks_path, "分块规划", MAX_CLAIMS_BYTES)
    if not isinstance(chunks, list) or not chunks:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"分块规划内容不是非空数组：{chunks_path}")
    prefixes = [manifest.get("root"), manifest.get("root_realpath")]
    claimed, duplicates = {}, set()
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        for f in chunk.get("files") or []:
            if not isinstance(f, str):
                continue
            rel = _strip_root_prefix(f, prefixes)
            if rel in claimed and claimed[rel] != chunk.get("id"):
                duplicates.add(rel)
            claimed.setdefault(rel, chunk.get("id"))
    claimed_set = set(claimed)
    store = db.Store(index_db)
    store.open_readonly()
    try:
        reader = IndexReader(store)
        index_set, after = set(), None
        while True:  # keyset 分页流式拉全集：协调者与回执都不一次性 stringify 全集
            page = reader.page_files(after=after, generation=manifest["generation"])
            index_set.update(row["path"] for row in page.rows)
            after = page.cursor
            if page.cursor is None:
                break
    finally:
        store.close()
    missing = sorted(index_set - claimed_set)
    extra = sorted(claimed_set - index_set)
    duplicate_list = sorted(duplicates)
    receipt = {"ok": not missing and not extra and not duplicate_list,
               "generation": manifest["generation"],
               "source_anchor": manifest["source_anchor"],
               "file_count": manifest["file_count"],
               "chunk_count": len(chunks), "covered": len(claimed_set & index_set),
               "missing_count": len(missing), "extra_count": len(extra),
               "duplicate_count": len(duplicate_list),
               "missing_sample": missing[:20], "extra_sample": extra[:20],
               "duplicate_sample": duplicate_list[:20]}
    if missing or extra or duplicate_list:
        fail(EXIT_CONFLICT, KIND_CONFLICT,
             f"分块覆盖与索引全集不闭合：索引有而分块无 {len(missing)} 项，"
             f"分块有而索引无 {len(extra)} 项，跨块重复 {len(duplicate_list)} 项", receipt)
    emit(receipt)


def _load_g2_progress(run_root):
    """读 G2 检查点；不存在返回 (None, path)。"""
    path = os.path.join(run_root, G2_PROGRESS_REL)
    if not os.path.isfile(path):
        return None, path
    data = _load_json_file(path, "G2 进度检查点", MAX_REPORT_BYTES)
    if not isinstance(data, dict) or data.get("version") != G2_PROGRESS_VERSION \
            or data.get("kind") != "g2_progress" \
            or not isinstance(data.get("chunk_order"), list) \
            or not isinstance(data.get("completed"), list) \
            or not isinstance(data.get("source_anchor"), str):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"G2 进度检查点损坏：{path}")
    return data, path


def cmd_chunks_page(payload):
    run_root = os.path.realpath(require_string(payload, "run_root"))
    _check_run_root(run_root)
    manifest = _load_index_manifest(run_root)
    _require_anchor(payload.get("anchor"))
    if payload["anchor"] != manifest["source_anchor"]:
        fail(EXIT_CONFLICT, KIND_CONFLICT,
             "anchor 与索引 manifest 不符（混代拒绝）："
             f"请求 {payload['anchor']}，索引世代 {manifest['generation']} 锚 "
             f"{manifest['source_anchor']}")
    chunks_path = os.path.join(run_root, CHUNKS_REL)
    if not os.path.isfile(chunks_path):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"分块规划不存在（先运行 chunks-write）：{chunks_path}")
    chunks = _load_json_file(chunks_path, "分块规划", MAX_CLAIMS_BYTES)
    if not isinstance(chunks, list) or not chunks:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"分块规划内容不是非空数组：{chunks_path}")
    progress, _ = _load_g2_progress(run_root)
    completed = set(progress["completed"]) if progress else set()
    pending = [c for c in chunks if isinstance(c, dict) and c.get("id") not in completed]
    page = _require_int(payload.get("page"), "page", 0)
    page_size = _require_int(payload.get("page_size"), "page_size", 1)
    if page_size > MAX_CLAIM_PAGE_SIZE:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"page_size 最大 {MAX_CLAIM_PAGE_SIZE}")
    if not pending:
        # 终态：全部块已完成（检查点续跑收尾）。空页对任意 page 都是合法空回执——
        # pending 随检查点完成动态收缩，page 不是稳定序号，消费方以"空批"终止。
        # 守护：pending 非空时 page 越界仍拒绝（捕获失控循环）。
        emit({"ok": True, "total": len(chunks), "completed": len(completed),
              "remaining": 0, "pages": 0, "page": page, "page_size": page_size,
              "source_anchor": manifest["source_anchor"], "chunks": []})
    pages = (len(pending) + page_size - 1) // page_size
    if page >= pages:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT,
             f"page {page} 越界：剩余 {len(pending)} 块共 {pages} 页",
             {"pages": pages, "remaining": len(pending),
              "completed": len(completed), "total": len(chunks)})
    start = page * page_size
    emit({"ok": True, "total": len(chunks), "completed": len(completed),
          "remaining": len(pending), "pages": pages, "page": page,
          "page_size": page_size, "source_anchor": manifest["source_anchor"],
          "chunks": pending[start:start + page_size]})


def cmd_g2_progress(payload):
    _ensure_kernel()
    from agents_kernel.atomicio import write_json
    run_root = os.path.realpath(require_string(payload, "run_root"))
    _check_run_root(run_root)
    action = require_string(payload, "action")
    path = os.path.join(run_root, G2_PROGRESS_REL)
    if action == "read":
        data, _ = _load_g2_progress(run_root)
        if data is None:
            emit({"ok": True, "exists": False, "checkpoint_path": path})
        emit({"ok": True, "exists": True, "checkpoint_path": path, "checkpoint": data})
    if action == "init":
        anchor = _require_anchor(payload.get("source_anchor"))
        generation = _require_int(payload.get("generation"), "generation", 1)
        order = payload.get("chunk_order")
        if not isinstance(order, list) or not order \
                or not all(isinstance(c, str) and c.strip() for c in order):
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, "chunk_order 必须是非空字符串数组")
        if len(set(order)) != len(order):
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, "chunk_order 含重复块 ID")
        existing, _ = _load_g2_progress(run_root)
        if existing is not None:
            if existing["source_anchor"] != anchor:
                fail(EXIT_CONFLICT, KIND_CONFLICT,
                     "检查点已存在且源锚不同（混代拒绝）："
                     f"既有 {existing['source_anchor']}，请求 {anchor}")
            emit({"ok": True, "idempotent": True, "checkpoint_path": path,
                  "completed": len(existing["completed"]), "total": len(existing["chunk_order"])})
        checkpoint = {"version": G2_PROGRESS_VERSION, "kind": "g2_progress",
                      "source_anchor": anchor, "generation": generation,
                      "chunk_order": order, "completed": []}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_json(path, checkpoint)
        emit({"ok": True, "idempotent": False, "checkpoint_path": path,
              "completed": 0, "total": len(order)})
    if action == "mark":
        chunk_id = require_string(payload, "chunk_id")
        progress, path_real = _load_g2_progress(run_root)
        if progress is None:
            fail(EXIT_CONFLICT, KIND_CONFLICT, f"检查点未初始化（先 init）：{path}")
        if chunk_id not in progress["chunk_order"]:
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"块 {chunk_id} 不在检查点 chunk_order 中")
        already = chunk_id in progress["completed"]
        if not already:
            progress["completed"].append(chunk_id)
            write_json(path_real, progress)
        emit({"ok": True, "chunk_id": chunk_id, "idempotent": already,
              "completed": len(progress["completed"]),
              "total": len(progress["chunk_order"])})
    fail(EXIT_ARGUMENT, KIND_ARGUMENT, "action 必须是 init/mark/read 之一")


def cmd_resume_register(payload):
    _ensure_kernel()
    from agents_kernel.services.resume import ResumeLedger
    run_root = os.path.realpath(require_string(payload, "run_root"))
    _check_run_root(run_root)
    kind = require_string(payload, "kind")
    activity_id = require_string(payload, "activity_id")
    checkpoint = os.path.realpath(require_string(payload, "checkpoint_path"))
    if not is_inside(checkpoint, run_root):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"checkpoint_path 不在 run_root 内：{checkpoint}")
    schema_version = _require_int(payload.get("schema_version"), "schema_version", 1)
    source_anchor = payload.get("source_anchor")
    if source_anchor is not None:
        _require_anchor(source_anchor)
    ledger = ResumeLedger.open(run_root, "precheck")
    row = ledger.register(kind, activity_id, checkpoint, schema_version, source_anchor)
    emit({"ok": True, "activity": row})


def cmd_resume_check(payload):
    _ensure_kernel()
    from agents_kernel.services.resume import ResumeLedger
    run_root = os.path.realpath(require_string(payload, "run_root"))
    _check_run_root(run_root)
    kind = require_string(payload, "kind")
    activity_id = require_string(payload, "activity_id")
    current_anchor = payload.get("current_anchor")
    if current_anchor is not None:
        _require_anchor(current_anchor)
    ledger = ResumeLedger.open(run_root, "precheck")
    known = [a for a in ledger.activities() if a["kind"] == kind and a["id"] == activity_id]
    if not known:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"活动未登记：{kind}/{activity_id}")
    if known[0]["status"] == "completed":
        emit({"ok": True, "condition": "completed", "activity": known[0]})
    item = next((i for i in ledger.resume_plan(current_anchor)
                 if i["kind"] == kind and i["id"] == activity_id), None)
    if item is None:
        fail(EXIT_CONFLICT, KIND_CONFLICT,
             f"活动 {kind}/{activity_id} 无法给出续接判定（见台账）")
    emit({"ok": True, **item})


def cmd_coverage_record(payload):
    _ensure_kernel()
    from agents_kernel.atomicio import write_json
    from agents_kernel.services.coverage import CoverageLedger
    run_root = os.path.realpath(require_string(payload, "run_root"))
    _check_run_root(run_root)
    run_id = require_string(payload, "run_id")
    generation = payload.get("generation")
    if generation is not None:
        generation = _require_int(generation, "generation", 0)
    dimensions = payload.get("dimensions")
    if not isinstance(dimensions, dict):
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, "dimensions 必须是对象")
    ledger = CoverageLedger(run_id=run_id, generation=generation)
    for name in COVERAGE_DIMENSIONS:
        spec = dimensions.get(name)
        if spec is None:
            continue
        if not isinstance(spec, dict):
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"维度 {name} 记账必须是对象")
        denominator = _require_int(spec.get("denominator"), f"{name} 分母", 0)
        covered = _require_int(spec.get("covered"), f"{name} covered", 0)
        if covered > denominator:
            fail(EXIT_ARGUMENT, KIND_ARGUMENT,
                 f"{name} 已覆盖 {covered} 超过分母 {denominator}（记账错误拒绝）")
        ledger.set_denominator(name, denominator)
        gaps = spec.get("gaps", [])
        if not isinstance(gaps, list):
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"维度 {name} gaps 必须是数组")
        ledger.record_covered(name, covered, gaps)
    account = ledger.to_account()
    account_path = os.path.join(run_root, COVERAGE_REL)
    try:
        write_json(account_path, account)
    except OSError as exc:
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"覆盖账写入失败 {account_path}：{exc}")
    emit({"ok": True, "account": account_path, "complete": account["complete"]})


COMMANDS = {
    "plan": cmd_plan,
    "acquire": cmd_acquire,
    "verify": cmd_verify,
    "check-files": cmd_check_files,
    "read-report": cmd_read_report,
    "claims-write": cmd_claims_write,
    "claims-page": cmd_claims_page,
    "index-count": cmd_index_count,
    "index-scan": cmd_index_scan,
    "index-page": cmd_index_page,
    "chunks-write": cmd_chunks_write,
    "index-verify": cmd_index_verify,
    "chunks-page": cmd_chunks_page,
    "g2-progress": cmd_g2_progress,
    "resume-register": cmd_resume_register,
    "resume-check": cmd_resume_check,
    "coverage-record": cmd_coverage_record,
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


def _kernel_error_class():
    """内核已加载时返回 CompanionError 类；未加载返回空元组（不捕获任何异常）。"""
    module = sys.modules.get("agents_kernel.validation")
    return module.CompanionError if module is not None else ()


def main(argv):
    if len(argv) < 1 or argv[0] not in COMMANDS:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT,
             "用法：precheck.py plan|acquire|verify|check-files|read-report|claims-write|claims-page"
             "|index-count|index-scan|index-page|chunks-write|index-verify|chunks-page"
             "|g2-progress|resume-register|resume-check|coverage-record"
             " (--json <JSON>|--json-file <路径>)")
    options, i = {}, 1
    while i < len(argv):
        flag = argv[i]
        if flag not in ("--json", "--json-file") or i + 1 >= len(argv):
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"未知或残缺参数：{flag}")
        options[flag[2:]] = argv[i + 1]
        i += 2
    try:
        COMMANDS[argv[0]](load_payload(options))
    except _kernel_error_class() as exc:
        # kernel 侧校验（清单/检查点/续接围栏等）拒绝：以参数级错误回执，不裸 traceback。
        # 内核未加载时返回空元组（不捕获任何异常），既有 stdlib 路径不受影响。
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, str(exc))


if __name__ == "__main__":
    main(sys.argv[1:])

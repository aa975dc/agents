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
              显式续接（resume=true）：run_root 已存在时核对既有回执——缺失/
              损坏/源不一致（或索引 manifest 的 root 不一致）→ 仍拒绝；一致
              → mode=adopt 接续既有制品（不删除、不覆盖、不重写首笔回执）。
              已存在但无 resume=true → 一律拒绝（新建排他保护不放松）。
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
  chunks-write分块规划落盘为"分块库"（有界两模式，产物相同）：
              {chunks:[...]} 既有 argv 模式（小规模/测试兼容）；或
              {plan_file, anchor} 有界导入模式——A1 已把规划（JSON 数组）写到
              run_root 内文件，本子命令流式校验导入，argv 只带路径+锚点。
              产物：index/chunks/<块id>.json（每块一文件，供分页领取）+
              index/chunks-summary.json（O(1) 汇总：anchor/块数/文件数）。
  index-verify分块覆盖 = 索引全集的流式核对：分页读索引 + 逐块读分块库，两侧
              各算（条数, Σsha256(path) mod 2^256, Σlen）交换律聚合指纹——
              常规路径不装任何全集集合（内存 O(单块)）；指纹不符才进第二轮
              精确比对（临时 sqlite 落盘做差，样本 ≤20 条，用后即删）。
  chunks-page 从分块库+完成标记分页领取"未完成"块（跳过 g2-progress 已完成
              块；每次只加载 ≤page_size 个分块条目，回执 loaded_entries 可证）：
              {run_root, page, page_size, anchor}；anchor 与索引 manifest 或
              标记元数据不符 → exit 3（混代拒绝）。检查点即游标：消费方恒取
              page 0（"下一批未完成块"），空批即终态；pending 非空时 page
              越界仍拒绝（exit 2，防失控循环）。
  g2-progress G2 完成标记（run_root/g2/<generation>/，per-chunk 唯一键文件，
              FIX05：不再用单体 JSON 读改写——两进程并发 mark 不同块互不覆
              盖，completed 列表=目录列举派生，N 个成功确认=N 个可恢复项）：
              init（登记 O(1) 元数据 meta.json：anchor/generation/total，块数
              取自分块库；不再持久化 chunk_order 全表——12 万块 init 与 12 块
              同界）/mark（原子写 <chunk_id>.done 单文件，幂等；result_path
              可选，给出即核验存在并记录 sha256）/read（恒有界：completed 只
              给计数）/list（完成 ID 分页领取）。旧 checkpoints/g2-progress.json
              单体检查点已删除，无兼容读。
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
import tempfile
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
# 分块库：每块一个 JSON 文件（index/chunks/<块id>.json）+ O(1) 汇总。
# FIX05：单体 chunks.json 无法分页读取（每次领取都要整文件加载），改按块分文件。
CHUNKS_DIR_REL = INDEX_DIR + "/chunks"
CHUNKS_SUMMARY_REL = INDEX_DIR + "/chunks-summary.json"
COVERAGE_REL = "coverage_account.json"
# G2 完成标记：run_root/g2/<generation>/<chunk_id>.done（per-chunk 唯一键文件）+
# O(1) 元数据 meta.json。并发 mark 不同块 = 不同文件的原子写，无共享读改写窗口。
G2_DIR = "g2"
G2_META_NAME = "meta.json"
CHUNK_MARKER_SUFFIX = ".done"
CHUNK_ID_PATTERN = re.compile(r"^chunk-[A-Za-z0-9_-]+$")
MAX_FILES_PER_CHUNK = 150          # 与 DWF G1 块尺寸上限一致
MAX_LOC_EST = 5000                 # 与 DWF G1 块 LOC 上限一致
G2_PROGRESS_VERSION = 1
CHUNK_PLAN_VERSION = 1
INDEX_MANIFEST_VERSION = 1
COVERAGE_DIMENSIONS = ("index_files", "semantics_deep", "independent_review")
MAX_CHUNKS = 100_000               # 分块规划条数上限（防御性；单块 ≤150 文件）
MAX_CHUNK_ENTRY_BYTES = 1_000_000  # 分块规划单条目字节上限
STREAM_READ_BYTES = 65_536         # 分块规划流式解析的读取缓冲
MODULUS = 1 << 256                 # 聚合指纹的模（sha256 摘要按大整数求和）

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
    resume = payload.get("resume", False)
    if resume not in (True, False):
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, "resume 必须是布尔（缺省 false）")
    try:
        os.makedirs(run_parent, exist_ok=True)
    except OSError as exc:
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"无法准备运行目录父目录 {run_parent}：{exc.strerror or exc}")
    try:
        os.mkdir(run_root_real)
    except FileExistsError:
        if not resume:
            fail(EXIT_CONFLICT, KIND_CONFLICT,
                 f"运行目录已存在，拒绝复用或覆盖（续接须显式 resume=true）：{run_root_real}")
        _acquire_adopt(run_root_real, source_real, source_input, team_real,
                       workspace_root, run_id)
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


def _acquire_adopt(run_root_real, source_real, source_input, team_real, workspace_root, run_id):
    """显式续接（resume=true 且 run_root 已存在）：核对既有事实后允许接续。

    - precheck.json 缺失/损坏/非回执 → 拒绝（"已存在但无有效 manifest 不 adopt"）；
    - 既有回执的 source realpath 与本次不一致 → 拒绝（异源混代）；
    - 索引 manifest 已存在时其 root_realpath 与本次不一致 → 拒绝（防误接异源目录）；
    - 全部一致 → mode=adopt 回执（created_at 沿用首次回执），不删除、不覆盖、
      不重写既有制品与首笔回执。
    边界（如实声明）：adopt 不提供并发互斥——同一 run_root 被两个工作流同时
    adopt 不在 helper 层排他；宿主层的调度/重放互斥另行验证（NOT_RUN）。
    """
    receipt_path = os.path.join(run_root_real, "precheck.json")
    prior = None
    if os.path.isfile(receipt_path):
        try:
            with open(receipt_path, "rb") as fh:
                raw = fh.read(MAX_REPORT_BYTES + 1)
            if len(raw) <= MAX_REPORT_BYTES:
                prior = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            prior = None
    if not isinstance(prior, dict) or prior.get("ok") is not True \
            or not isinstance(prior.get("created_at"), str) or not prior["created_at"].strip():
        fail(EXIT_CONFLICT, KIND_CONFLICT,
             f"运行目录已存在但首笔回执缺失或损坏，拒绝续接：{receipt_path}")
    prior_source = (prior.get("roots") or {}).get("source_root") or {}
    if prior_source.get("realpath") != source_real:
        fail(EXIT_CONFLICT, KIND_CONFLICT,
             f"运行目录属于其他源，拒绝混代续接：既有 {prior_source.get('realpath')}，本次 {source_real}")
    manifest_path = os.path.join(run_root_real, INDEX_MANIFEST_REL)
    if os.path.isfile(manifest_path):
        manifest = _load_index_manifest(run_root_real)
        if manifest.get("root_realpath") != source_real:
            fail(EXIT_CONFLICT, KIND_CONFLICT,
                 f"运行目录内的索引 manifest 属于其他源，拒绝混代续接："
                 f"既有 {manifest.get('root_realpath')}，本次 {source_real}")
    emit({
        "ok": True,
        "run_id": run_id,
        "created_at": prior["created_at"],
        "resumed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "adopt",
        "helper_version": 1,
        "roots": roots_payload(source_input, source_real, team_real, workspace_root, run_root_real),
    })


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
# 字样即被 build_vendor.uses_kernel 检测，自动生成 swarm vendor。仅 index-scan/index-page/
# index-verify/g2/resume/coverage 子命令需要内核；chunks-write（分块库为本地派生文件，
# 本地原子替换即可）与 plan/acquire/verify/check-files/read-report/claims-* 保持纯标准库，
# 无 vendor 的既有安装零回归。
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


def _validate_chunk_entry(chunk):
    """单个分块 schema：id/依据/files/尺寸/邻块类型；返回 (chunk_id, files)。
    跨块约束（ID 唯一/邻块存在/跨块文件唯一）由调用方按其掌握的数据核对。"""
    if not isinstance(chunk, dict):
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, "chunks 元素必须是对象")
    cid = chunk.get("id")
    if not isinstance(cid, str) or not CHUNK_ID_PATTERN.fullmatch(cid):
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"块 ID 无效：{cid!r}")
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
    return cid, files


def _validate_chunks(chunks):
    """argv 模式的 A1 分块规划校验：逐块 schema + ID 唯一 + 邻块存在 + 跨块文件
    唯一（argv 里全量在手，重复早失败）。"""
    if not isinstance(chunks, list) or not chunks:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, "chunks 必须是非空数组")
    ids, owner, duplicates = set(), {}, []
    for chunk in chunks:
        cid, files = _validate_chunk_entry(chunk)
        if cid in ids:
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"块 ID 重复：{cid}")
        ids.add(cid)
        for f in files:
            if f in owner:
                duplicates.append(f)
            owner[f] = cid
    for chunk in chunks:
        for neighbor in chunk["neighbors"]:
            if neighbor == chunk["id"] or neighbor not in ids:
                fail(EXIT_ARGUMENT, KIND_ARGUMENT,
                     f"块 {chunk['id']} 邻块引用不存在或自引用：{neighbor}")
    return chunks, owner, sorted(set(duplicates))


def _write_store_entry(path, obj):
    """分块库条目写：临时文件 + os.replace，不逐文件 fsync——分块库是可由
    chunk-plan.json 重导出重建的派生制品，逐文件 fsync 会让万块导入退化成
    分钟级；完整性由汇总文件与 index-verify 复核兜底。"""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def _write_chunks_summary(run_root, summary):
    _ensure_kernel()
    from agents_kernel.atomicio import write_json
    summary_path = os.path.join(run_root, CHUNKS_SUMMARY_REL)
    try:
        write_json(summary_path, summary)
    except OSError as exc:
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"分块汇总写入失败 {summary_path}：{exc}")
    return summary_path


def _load_chunks_summary(run_root):
    """读分块库汇总；缺失返回 None，损坏/形状不符拒绝（不造空汇总掩盖）。"""
    path = os.path.join(run_root, CHUNKS_SUMMARY_REL)
    if not os.path.isfile(path):
        return None
    data = _load_json_file(path, "分块库汇总", MAX_REPORT_BYTES)
    if not isinstance(data, dict) or data.get("version") != CHUNK_PLAN_VERSION \
            or data.get("kind") != "chunk_plan_summary" \
            or not _is_int_like(data.get("chunk_count")) or data["chunk_count"] < 1 \
            or not _is_int_like(data.get("file_count_total")) or data["file_count_total"] < 1 \
            or not isinstance(data.get("source_anchor"), str):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"分块库汇总损坏：{path}")
    return data


def _list_plan_ids(run_root):
    """分块库块 ID 清单（目录名列举，sorted 稳定序）；库不存在返回空表。"""
    store_dir = os.path.join(run_root, CHUNKS_DIR_REL)
    try:
        names = os.listdir(store_dir)
    except OSError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json") and not n.startswith("."))


def _load_plan_entry(run_root, chunk_id):
    """读单块分块条目（有界：单文件 ≤MAX_CHUNK_ENTRY_BYTES），重验 schema。"""
    path = os.path.join(run_root, CHUNKS_DIR_REL, chunk_id + ".json")
    entry = _load_json_file(path, f"分块条目 {chunk_id}", MAX_CHUNK_ENTRY_BYTES)
    cid, files = _validate_chunk_entry(entry)
    if cid != chunk_id:
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"分块条目 ID 与文件名不符：{path}")
    return entry, files


def _iter_plan_file(path):
    """流式产出分块规划（JSON 数组）的元素：缓冲区只保留未解析尾部，单条过大/
    条数超限即拒。从不整文件载入内存——12 万条规划的导入与 12 条同一路径、
    同一内存画像（FIX05 有界入口）。"""
    decoder = json.JSONDecoder()

    def fill():
        return fh.read(STREAM_READ_BYTES)

    with open(path, "r", encoding="utf-8") as fh:
        buf, i = fill(), 0
        while True:  # 跳过前导空白
            while i < len(buf) and buf[i].isspace():
                i += 1
            if i < len(buf):
                break
            buf, i = fill(), 0
            if not buf:
                fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"分块规划是空文件：{path}")
        if buf[i] != "[":
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"分块规划必须是 JSON 数组：{path}")
        i += 1
        count = 0
        while True:
            while True:  # 跳过元素间空白与逗号
                while i < len(buf) and buf[i].isspace():
                    i += 1
                if i >= len(buf):
                    buf, i = fill(), 0
                    if not buf:
                        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"分块规划 JSON 截断：{path}")
                    continue
                break
            ch = buf[i]
            if ch == "]":
                return
            if ch == ",":
                i += 1
                continue
            while True:  # 解析一个元素；缓冲不足续读（只保留未解析尾部）
                try:
                    obj, end = decoder.raw_decode(buf, i)
                    break
                except ValueError:
                    more = fill()
                    if not more:
                        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"分块规划不是有效 JSON：{path}")
                    buf, i = buf[i:] + more, 0
                    if len(buf) > MAX_CHUNK_ENTRY_BYTES:
                        fail(EXIT_ARGUMENT, KIND_ARGUMENT,
                             f"分块规划单条目超过 {MAX_CHUNK_ENTRY_BYTES} 字节上限：{path}")
            count += 1
            if count > MAX_CHUNKS:
                fail(EXIT_ARGUMENT, KIND_ARGUMENT,
                     f"分块规划超过 {MAX_CHUNKS} 块上限：{path}")
            yield obj
            i = end
            if i >= STREAM_READ_BYTES:  # 丢弃已解析前缀，缓冲不随文件增长
                buf, i = buf[i:], 0


def cmd_chunks_write(payload):
    """分块规划落盘为分块库（有界两模式，产物相同）：
    - {chunks:[...]} 既有 argv 模式：小规模/测试兼容，全量校验（含跨块重复）后落盘；
    - {plan_file, anchor} 有界导入模式：A1 已把规划（JSON 数组）写到 run_root 内
      文件，本子命令流式校验导入——argv 只带路径+锚点，协调者不经手全部文件路径。
    产物：index/chunks/<块id>.json（每块一文件）+ index/chunks-summary.json
    （O(1)：anchor/chunk_count/file_count_total）。跨块文件重复在导入模式不做
    前置检查（流式无全集），由 index-verify 聚合指纹精确拒绝。"""
    run_root = os.path.realpath(require_string(payload, "run_root"))
    _check_run_root(run_root)
    store_dir = os.path.join(run_root, CHUNKS_DIR_REL)
    manifest_path = os.path.join(run_root, INDEX_MANIFEST_REL)
    manifest = _load_index_manifest(run_root) if os.path.isfile(manifest_path) else None
    if payload.get("plan_file") is not None:
        if payload.get("chunks") is not None:
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, "chunks 与 plan_file 只能二选一")
        anchor = _require_anchor(payload.get("anchor"))
        plan_file = os.path.realpath(require_string(payload, "plan_file"))
        if not is_inside(plan_file, run_root):
            fail(EXIT_CONFLICT, KIND_CONFLICT, f"分块规划文件不在 run_root 内：{plan_file}")
        if not os.path.isfile(plan_file):
            fail(EXIT_CONFLICT, KIND_CONFLICT, f"分块规划文件不存在：{plan_file}")
        if manifest is not None and manifest["source_anchor"] != anchor:
            fail(EXIT_CONFLICT, KIND_CONFLICT,
                 "anchor 与索引 manifest 不符（混代拒绝）："
                 f"请求 {anchor}，索引世代 {manifest['generation']} 锚 {manifest['source_anchor']}")
        existing = _load_chunks_summary(run_root)
        if existing is not None and existing["source_anchor"] not in (None, anchor):
            fail(EXIT_CONFLICT, KIND_CONFLICT,
                 f"分块库已绑定其他源锚，拒绝覆盖导入：既有 {existing['source_anchor']}，请求 {anchor}")
        ids, file_total = set(), 0
        try:
            for chunk in _iter_plan_file(plan_file):
                cid, files = _validate_chunk_entry(chunk)
                if cid in ids:
                    fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"块 ID 重复：{cid}")
                ids.add(cid)
                file_total += len(files)
                _write_store_entry(os.path.join(store_dir, cid + ".json"), chunk)
        except OSError as exc:
            fail(EXIT_CONFLICT, KIND_CONFLICT, f"分块库写入失败 {store_dir}：{exc}")
        if not ids:
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"分块规划内容是空数组：{plan_file}")
        summary_path = _write_chunks_summary(run_root, {
            "version": CHUNK_PLAN_VERSION, "kind": "chunk_plan_summary",
            "source_anchor": anchor, "chunk_count": len(ids),
            "file_count_total": file_total,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        emit({"ok": True, "mode": "plan_import", "chunks_dir": store_dir,
              "summary": summary_path, "chunk_count": len(ids),
              "file_count_total": file_total})
    chunks, owner, duplicates = _validate_chunks(payload.get("chunks"))
    if duplicates:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT,
             "分块跨块文件重复：" + "、".join(duplicates[:20]),
             {"duplicate_sample": duplicates[:20], "duplicate_count": len(duplicates)})
    anchor = manifest["source_anchor"] if manifest is not None else None
    existing = _load_chunks_summary(run_root)
    if existing is not None and anchor is not None \
            and existing["source_anchor"] not in (None, anchor):
        fail(EXIT_CONFLICT, KIND_CONFLICT,
             f"分块库已绑定其他源锚，拒绝覆盖写入：既有 {existing['source_anchor']}")
    try:
        for chunk in chunks:
            _write_store_entry(os.path.join(store_dir, chunk["id"] + ".json"), chunk)
    except OSError as exc:
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"分块库写入失败 {store_dir}：{exc}")
    summary_path = _write_chunks_summary(run_root, {
        "version": CHUNK_PLAN_VERSION, "kind": "chunk_plan_summary",
        "source_anchor": anchor, "chunk_count": len(chunks),
        "file_count_total": len(owner),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    emit({"ok": True, "mode": "argv", "chunks_dir": store_dir,
          "summary": summary_path, "chunk_count": len(chunks),
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


def _agg_update(count, sha_sum, len_sum, rel_path):
    """交换律聚合指纹的单步：条数 + Σsha256(rel) mod 2^256 + Σlen。
    说明：分块库按块分文件、块内路径与全局排序无涉，拿不到两侧同序的
    "排序路径流"，故用交换律聚合替代有序滚动 sha256——同一 (条数, Σsha, Σlen)
    的不同多重集需构造 sha256 碰撞，工程上等价；外加计数与长度两路独立混合。"""
    raw = rel_path.encode("utf-8")
    return (count + 1,
            (sha_sum + int.from_bytes(hashlib.sha256(raw).digest(), "big")) % MODULUS,
            len_sum + len(raw))


def cmd_index_verify(payload):
    """分块覆盖 = 索引全集的流式核对：分页读索引 + 逐块读分块库，两侧各算聚合
    指纹——常规路径不装任何全集集合（内存 O(单块)，FIX05 有界入口）；指纹不符
    才进第二轮精确比对（临时 sqlite 落盘做差，精确条数+样本 ≤20 条，用后即删）。"""
    _ensure_kernel()
    from agents_kernel.indexing.reader import IndexReader
    from agents_kernel.storage import db
    run_root = os.path.realpath(require_string(payload, "run_root"))
    _check_run_root(run_root)
    manifest = _load_index_manifest(run_root)
    index_db = os.path.realpath(os.path.join(run_root, INDEX_DB_REL))
    if not os.path.isfile(index_db):
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"索引库不是普通文件或不存在：{index_db}")
    plan_ids = _list_plan_ids(run_root)
    if not plan_ids:
        fail(EXIT_CONFLICT, KIND_CONFLICT,
             f"分块库不存在或为空（先运行 chunks-write）：{os.path.join(run_root, CHUNKS_DIR_REL)}")
    prefixes = [manifest.get("root"), manifest.get("root_realpath")]
    generation = manifest["generation"]
    store = db.Store(index_db)
    store.open_readonly()
    try:
        reader = IndexReader(store)
        idx_count = idx_sha = idx_len = 0
        after = None
        while True:  # 索引侧：keyset 分页流式聚合，全集不进内存
            page = reader.page_files(after=after, generation=generation)
            for row in page.rows:
                idx_count, idx_sha, idx_len = _agg_update(idx_count, idx_sha, idx_len, row["path"])
            after = page.cursor
            if page.cursor is None:
                break
        plan_count = plan_sha = plan_len = 0
        for cid in plan_ids:  # 分块侧：逐块读（单块 ≤150 路径，内存 O(单块)）
            _, files = _load_plan_entry(run_root, cid)
            for f in files:
                rel = _strip_root_prefix(f, prefixes)
                plan_count, plan_sha, plan_len = _agg_update(plan_count, plan_sha, plan_len, rel)
    finally:
        store.close()
    closed = (plan_count, plan_sha, plan_len) == (idx_count, idx_sha, idx_len)
    missing_sample, extra_sample, duplicate_sample = [], [], []
    missing_count = extra_count = duplicate_count = 0
    covered = plan_count if closed else 0
    if not closed:
        # 第二轮：临时 sqlite 做精确差集（磁盘承载全集，进程内存仍 O(单块)），用后即删。
        import sqlite3
        tmp_db = os.path.join(run_root, INDEX_DIR, ".verify-diff.sqlite")
        for stale in (tmp_db, tmp_db + "-journal", tmp_db + "-wal", tmp_db + "-shm"):
            if os.path.exists(stale):
                os.unlink(stale)
        conn = sqlite3.connect(tmp_db)
        try:
            conn.execute("CREATE TABLE p(path TEXT PRIMARY KEY)")
            conn.execute("ATTACH DATABASE ? AS idx", (index_db,))
            streamed = 0
            for cid in plan_ids:
                _, files = _load_plan_entry(run_root, cid)
                for f in files:
                    rel = _strip_root_prefix(f, prefixes)
                    streamed += 1
                    try:
                        conn.execute("INSERT INTO p(path) VALUES (?)", (rel,))
                    except sqlite3.IntegrityError:
                        duplicate_count += 1
                        if len(duplicate_sample) < 20:
                            duplicate_sample.append(rel)
            conn.commit()
            plan_rows = conn.execute("SELECT COUNT(*) FROM p").fetchone()[0]
            extra_count = conn.execute(
                "SELECT COUNT(*) FROM p WHERE NOT EXISTS (SELECT 1 FROM idx.files f"
                " WHERE f.generation = ? AND f.path = p.path)", (generation,)).fetchone()[0]
            extra_sample = [r[0] for r in conn.execute(
                "SELECT p.path FROM p WHERE NOT EXISTS (SELECT 1 FROM idx.files f"
                " WHERE f.generation = ? AND f.path = p.path) LIMIT 20", (generation,))]
            missing_count = conn.execute(
                "SELECT COUNT(*) FROM idx.files f WHERE f.generation = ?"
                " AND NOT EXISTS (SELECT 1 FROM p WHERE p.path = f.path)", (generation,)).fetchone()[0]
            missing_sample = [r[0] for r in conn.execute(
                "SELECT f.path FROM idx.files f WHERE f.generation = ?"
                " AND NOT EXISTS (SELECT 1 FROM p WHERE p.path = f.path) LIMIT 20", (generation,))]
            covered = plan_rows - extra_count
        finally:
            conn.close()
            for stale in (tmp_db, tmp_db + "-journal", tmp_db + "-wal", tmp_db + "-shm"):
                if os.path.exists(stale):
                    os.unlink(stale)
    receipt = {"ok": not (missing_count or extra_count or duplicate_count or not closed),
               "generation": generation,
               "source_anchor": manifest["source_anchor"],
               "file_count": manifest["file_count"],
               "chunk_count": len(plan_ids), "covered": covered,
               "missing_count": missing_count, "extra_count": extra_count,
               "duplicate_count": duplicate_count,
               "missing_sample": missing_sample[:20], "extra_sample": extra_sample[:20],
               "duplicate_sample": duplicate_sample[:20]}
    if not receipt["ok"]:
        fail(EXIT_CONFLICT, KIND_CONFLICT,
             f"分块覆盖与索引全集不闭合：索引有而分块无 {missing_count} 项，"
             f"分块有而索引无 {extra_count} 项，跨块重复 {duplicate_count} 项", receipt)
    emit(receipt)


def _g2_paths(run_root, generation):
    """G2 标记层路径：目录 run_root/g2/<generation>/ 与其 O(1) 元数据文件。"""
    markers_dir = os.path.join(run_root, G2_DIR, str(generation))
    return markers_dir, os.path.join(markers_dir, G2_META_NAME)


def _load_g2_meta(run_root, generation):
    """读 G2 标记元数据；缺失返回 (None, path)，损坏/形状不符拒绝。"""
    _, meta_path = _g2_paths(run_root, generation)
    if not os.path.isfile(meta_path):
        return None, meta_path
    meta = _load_json_file(meta_path, "G2 标记元数据", MAX_REPORT_BYTES)
    if not isinstance(meta, dict) or meta.get("version") != G2_PROGRESS_VERSION \
            or meta.get("kind") != "g2_markers" \
            or not _is_int_like(meta.get("generation")) or meta["generation"] != generation \
            or not isinstance(meta.get("source_anchor"), str) \
            or not re.fullmatch(r"[0-9a-f]{64}", meta.get("source_anchor", "")) \
            or not _is_int_like(meta.get("total")) or meta["total"] < 1:
        fail(EXIT_CONFLICT, KIND_CONFLICT, f"G2 标记元数据损坏：{meta_path}")
    return meta, meta_path


def _list_markers(run_root, generation):
    """已完成块 ID = 标记目录列举派生（N 个 .done 文件 = N 个成功确认）。"""
    markers_dir, _ = _g2_paths(run_root, generation)
    try:
        names = os.listdir(markers_dir)
    except OSError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(CHUNK_MARKER_SUFFIX))


def cmd_chunks_page(payload):
    """从分块库 + 完成标记分页领取"未完成"块：每次只加载 ≤page_size 个分块条目
    （回执 loaded_entries 可证），名称列举只取目录名、不读内容。"""
    run_root = os.path.realpath(require_string(payload, "run_root"))
    _check_run_root(run_root)
    manifest = _load_index_manifest(run_root)
    _require_anchor(payload.get("anchor"))
    if payload["anchor"] != manifest["source_anchor"]:
        fail(EXIT_CONFLICT, KIND_CONFLICT,
             "anchor 与索引 manifest 不符（混代拒绝）："
             f"请求 {payload['anchor']}，索引世代 {manifest['generation']} 锚 "
             f"{manifest['source_anchor']}")
    generation = manifest["generation"]
    plan_ids = _list_plan_ids(run_root)
    if not plan_ids:
        fail(EXIT_CONFLICT, KIND_CONFLICT,
             f"分块库不存在或为空（先运行 chunks-write）：{os.path.join(run_root, CHUNKS_DIR_REL)}")
    meta, _ = _load_g2_meta(run_root, generation)
    if meta is not None and meta["source_anchor"] != payload["anchor"]:
        fail(EXIT_CONFLICT, KIND_CONFLICT,
             "anchor 与 G2 标记元数据不符（混代拒绝）："
             f"请求 {payload['anchor']}，标记锚 {meta['source_anchor']}")
    markers_dir, _ = _g2_paths(run_root, generation)
    completed_ids = set(_list_markers(run_root, generation))
    if meta is None and completed_ids:
        fail(EXIT_CONFLICT, KIND_CONFLICT,
             f"完成标记存在而元数据缺失，拒绝领取（先 g2-progress init）：{markers_dir}")
    plan_set = set(plan_ids)
    completed = len(completed_ids & plan_set)
    pending = [cid for cid in plan_ids if cid not in completed_ids]
    page = _require_int(payload.get("page"), "page", 0)
    page_size = _require_int(payload.get("page_size"), "page_size", 1)
    if page_size > MAX_CLAIM_PAGE_SIZE:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"page_size 最大 {MAX_CLAIM_PAGE_SIZE}")
    if not pending:
        # 终态：全部块已完成（检查点续跑收尾）。空页对任意 page 都是合法空回执——
        # pending 随完成标记动态收缩，page 不是稳定序号，消费方以"空批"终止。
        # 守护：pending 非空时 page 越界仍拒绝（捕获失控循环）。
        emit({"ok": True, "total": len(plan_ids), "completed": completed,
              "remaining": 0, "pages": 0, "page": page, "page_size": page_size,
              "loaded_entries": 0, "markers_dir": markers_dir,
              "source_anchor": manifest["source_anchor"], "chunks": []})
    pages = (len(pending) + page_size - 1) // page_size
    if page >= pages:
        fail(EXIT_ARGUMENT, KIND_ARGUMENT,
             f"page {page} 越界：剩余 {len(pending)} 块共 {pages} 页",
             {"pages": pages, "remaining": len(pending),
              "completed": completed, "total": len(plan_ids)})
    start = page * page_size
    out_chunks, loaded = [], 0
    for cid in pending[start:start + page_size]:
        entry, _ = _load_plan_entry(run_root, cid)
        out_chunks.append(entry)
        loaded += 1
    emit({"ok": True, "total": len(plan_ids), "completed": completed,
          "remaining": len(pending), "pages": pages, "page": page,
          "page_size": page_size, "loaded_entries": loaded,
          "markers_dir": markers_dir, "source_anchor": manifest["source_anchor"],
          "chunks": out_chunks})


def cmd_g2_progress(payload):
    """G2 完成标记（FIX05）：per-chunk 唯一键文件，无共享读改写。
    - init：登记 O(1) 元数据（anchor/generation/total，total 取自分块库块数）；
      不持久化 chunk_order 全表——12 万块 init 与 12 块同界，read 恒有界。
    - mark：原子写 <chunk_id>.done 单文件（幂等：已存在直接返回 ok）；块须在
      分块库中（membership 用点查，不载全量）；result_path 给出即核验存在并
      记录 sha256，完成确认与制品绑定。
    - read：completed 只给计数（恒有界）；ID 清单经 list 分页领取。
    旧 checkpoints/g2-progress.json 单体检查点已删除（读改写互覆 + init/read
    边界不一致的根因），无兼容读。"""
    _ensure_kernel()
    from agents_kernel.atomicio import write_json
    run_root = os.path.realpath(require_string(payload, "run_root"))
    _check_run_root(run_root)
    action = require_string(payload, "action")
    if action == "init":
        anchor = _require_anchor(payload.get("source_anchor"))
        generation = _require_int(payload.get("generation"), "generation", 1)
        plan_ids = _list_plan_ids(run_root)
        if not plan_ids:
            fail(EXIT_CONFLICT, KIND_CONFLICT,
                 f"分块库不存在或为空（先运行 chunks-write）：{os.path.join(run_root, CHUNKS_DIR_REL)}")
        markers_dir, meta_path = _g2_paths(run_root, generation)
        meta, _ = _load_g2_meta(run_root, generation)
        if meta is not None:
            if meta["source_anchor"] != anchor:
                fail(EXIT_CONFLICT, KIND_CONFLICT,
                     "G2 标记已存在且源锚不同（混代拒绝）："
                     f"既有 {meta['source_anchor']}，请求 {anchor}")
            completed = _list_markers(run_root, generation)
            emit({"ok": True, "idempotent": True, "markers_dir": markers_dir,
                  "meta_path": meta_path, "completed": len(completed),
                  "total": meta["total"]})
        meta = {"version": G2_PROGRESS_VERSION, "kind": "g2_markers",
                "source_anchor": anchor, "generation": generation,
                "total": len(plan_ids),
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            os.makedirs(markers_dir, exist_ok=True)
            write_json(meta_path, meta)  # 完成记录的锚基石：原子写 + fsync
        except OSError as exc:
            fail(EXIT_CONFLICT, KIND_CONFLICT, f"G2 标记元数据写入失败 {meta_path}：{exc}")
        emit({"ok": True, "idempotent": False, "markers_dir": markers_dir,
              "meta_path": meta_path, "completed": 0, "total": meta["total"]})
    if action == "mark":
        chunk_id = require_string(payload, "chunk_id")
        if not CHUNK_ID_PATTERN.fullmatch(chunk_id):
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"块 ID 无效：{chunk_id!r}")
        generation = _require_int(payload.get("generation"), "generation", 1)
        meta, _ = _load_g2_meta(run_root, generation)
        if meta is None:
            _, meta_path = _g2_paths(run_root, generation)
            fail(EXIT_CONFLICT, KIND_CONFLICT, f"标记未初始化（先 init）：{meta_path}")
        plan_entry_path = os.path.join(run_root, CHUNKS_DIR_REL, chunk_id + ".json")
        if not os.path.isfile(plan_entry_path):
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"块 {chunk_id} 不在分块库中：{plan_entry_path}")
        result_path = payload.get("result_path")
        result_sha = None
        if result_path is not None:
            result_path = os.path.realpath(require_string(payload, "result_path"))
            if not is_inside(result_path, run_root):
                fail(EXIT_CONFLICT, KIND_CONFLICT, f"result_path 不在 run_root 内：{result_path}")
            try:
                with open(result_path, "rb") as fh:
                    raw = fh.read(MAX_CLAIMS_BYTES + 1)
            except OSError as exc:
                fail(EXIT_CONFLICT, KIND_CONFLICT, f"块结果无法读取 {result_path}：{exc.strerror or exc}")
            if len(raw) > MAX_CLAIMS_BYTES:
                fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"块结果超过 {MAX_CLAIMS_BYTES} 字节上限：{result_path}")
            result_sha = hashlib.sha256(raw).hexdigest()
        markers_dir, _ = _g2_paths(run_root, generation)
        marker_path = os.path.join(markers_dir, chunk_id + CHUNK_MARKER_SUFFIX)
        if os.path.exists(marker_path):
            completed = _list_markers(run_root, generation)
            emit({"ok": True, "chunk_id": chunk_id, "idempotent": True,
                  "marker_path": marker_path, "completed": len(completed),
                  "total": meta["total"]})
        marker = {"version": G2_PROGRESS_VERSION, "kind": "g2_chunk_marker",
                  "chunk_id": chunk_id, "source_anchor": meta["source_anchor"],
                  "generation": generation, "result_path": result_path,
                  "result_sha256": result_sha,
                  "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            write_json(marker_path, marker)  # 完成记录：单文件原子写 + fsync
        except OSError as exc:
            fail(EXIT_CONFLICT, KIND_CONFLICT, f"完成标记写入失败 {marker_path}：{exc}")
        completed = _list_markers(run_root, generation)
        emit({"ok": True, "chunk_id": chunk_id, "idempotent": False,
              "marker_path": marker_path, "completed": len(completed),
              "total": meta["total"]})
    if action == "read":
        generation = _require_int(payload.get("generation"), "generation", 1)
        meta, meta_path = _load_g2_meta(run_root, generation)
        markers_dir, _ = _g2_paths(run_root, generation)
        if meta is None:
            emit({"ok": True, "exists": False, "markers_dir": markers_dir,
                  "meta_path": meta_path})
        completed = _list_markers(run_root, generation)
        # 恒有界：completed 只给计数；ID 清单经 action=list 分页领取。
        emit({"ok": True, "exists": True, "markers_dir": markers_dir,
              "meta_path": meta_path, "meta": meta,
              "completed_count": len(completed), "total": meta["total"]})
    if action == "list":
        generation = _require_int(payload.get("generation"), "generation", 1)
        meta, _ = _load_g2_meta(run_root, generation)
        if meta is None:
            _, meta_path = _g2_paths(run_root, generation)
            fail(EXIT_CONFLICT, KIND_CONFLICT, f"标记未初始化（先 init）：{meta_path}")
        ids = _list_markers(run_root, generation)
        page = _require_int(payload.get("page"), "page", 0)
        page_size = _require_int(payload.get("page_size"), "page_size", 1)
        if page_size > MAX_CLAIM_PAGE_SIZE:
            fail(EXIT_ARGUMENT, KIND_ARGUMENT, f"page_size 最大 {MAX_CLAIM_PAGE_SIZE}")
        pages = (len(ids) + page_size - 1) // page_size
        if ids and page >= pages:
            fail(EXIT_ARGUMENT, KIND_ARGUMENT,
                 f"page {page} 越界：共 {pages} 页 {len(ids)} 条",
                 {"pages": pages, "total": len(ids)})
        start = page * page_size
        emit({"ok": True, "total": len(ids), "pages": pages, "page": page,
              "page_size": page_size, "ids": ids[start:start + page_size]})
    fail(EXIT_ARGUMENT, KIND_ARGUMENT, "action 必须是 init/mark/read/list 之一")


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

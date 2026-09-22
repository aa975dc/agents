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

FIX-04（SR-01）：team-task 不再是任意状态 upsert——状态词白名单只是词法检查，
堵不住"从未执行的任务直接写 done"。现在公共入口收敛为唯一的团队协调动作模型
（CLI 形状保持）：创建（team-task-add）、派发（ready）、开始执行（running，强制
建 attempt）、回报（team-report，固定 artifact sha）、独立审查（team-approve，
复用 ReviewBoard：实现者自审拒绝、结论绑定 subject sha）、完成（done 前置门：
未完结 attempt + succeeded 回报 + review_required 时的有效独立批准 + 完成证据
与 done 事件同一 SQLite 事务提交）、blocked/cancelled 必须 --reason（Z24）、
集成（team-integrate，复用 IntegrationBoard：候选批准门→版本幂等→两级回归→
completed）。前置不满足即拒绝并明确缺什么，不写任何事件。done→ready 等非法
转换经 domain.tasks 白名单拒绝。门禁前置从事件回放（TaskBoard/ReviewBoard/
IntegrationBoard 真实重建）查询，历史事实经 TaskBoard.adopt_task 原样重建、
不做白名单重审（门禁只管新写入）。

FIX04-followup（2026-09 复核 §4：门禁与实际文件版本没有闭环）：attempt 显式绑定
真实工作区产物——team-report 对 changed_files 逐条核对 ⊆ 任务 allowed_paths（越界
即拒），并在报告时点真实读取每个改动文件计算内容 sha256（file_shas，摘要由采集
产生而非仅收字符串）；done 前与 team-integrate 入列前对同一批文件复核"当前内容
sha == 报告时 sha"，漂移/缺失即拒绝（ReviewBoard 的 sha 绑定使旧批准对新回报自动
失效，须重新 report→approve→done）；集成把"回归运行于哪个 sha 集合"记进版本级
回归与完成证据（regressed_on）。修复前落库的历史回报无 file_shas，按"门禁只管
新写入"口径跳过产物复核，与 adopt_task 同一原则。
"""
import json
import os
import shlex
import sqlite3
import subprocess
import uuid
from pathlib import Path

from agents_kernel import digest
from agents_kernel.atomicio import write_json
from agents_kernel.domain import views
from agents_kernel.domain.review_record import ReviewRecord, attempt_id
from agents_kernel.domain.tasks import TASK_KINDS, TASK_STATUSES, TaskBoard
from agents_kernel.paths import inside, realpath
from agents_kernel.storage import db, events, migration
from agents_kernel.services.integration import IntegrationBoard
from agents_kernel.services.review_board import ReviewBoard
from agents_kernel.validation import (CompanionError, feature_id, relative_path,
                                      strings, text)

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
    if path.exists() and not path.is_file():
        # FIX-01：目录/fifo/设备等非普通文件在打开前拒绝（stat 检查，不 open——
        # 打开 fifo 会阻塞，sqlite 对目录报错含糊）。
        raise CompanionError("team 事实库不是普通文件，拒绝打开（目录/管道/设备等）：%s" % path)
    return path


# 只读打开时必须齐全的表（schema 校验的最小集，与 db._SCHEMA_V1 对齐）。
_REQUIRED_TABLES = ("store_meta", "events", "feature_view", "task_view",
                    "evidence_view", "release_view")


def _open_readonly(path):
    """只读打开既有 team 事实库（FIX-01）：查询绝不初始化或迁移 schema。

    库不存在 → 引导 team-init（不静默建空库）；损坏/未知 schema → 结构化报错；
    全程 mode=ro/immutable 连接，不留 journal/WAL/锁文件。
    """
    if not path.exists():
        raise CompanionError("team 事实库不存在：%s；请先运行 team-init" % path)
    store = db.Store(path)
    try:
        try:
            store.open_readonly()
            version_row = store.query_one("SELECT MAX(version) AS version FROM schema_version")
        except sqlite3.Error as exc:
            raise CompanionError(
                "team 事实库损坏或不是有效的 SQLite 库：%s（%s）" % (path, exc)) from exc
        version = version_row["version"] if version_row is not None else None
        if version is None:
            raise CompanionError("team 事实库缺少 schema 版本记录（不是 team 库或已损坏）：%s" % path)
        if version > db.SCHEMA_VERSION:
            raise CompanionError(
                "team 事实库 schema 版本 %s 高于当前工具支持的 %d，请升级工具后再读：%s"
                % (version, db.SCHEMA_VERSION, path))
        try:
            missing = [name for name in _REQUIRED_TABLES if store.query_one(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (name,)) is None]
        except sqlite3.Error as exc:
            raise CompanionError("team 事实库损坏：%s（%s）" % (path, exc)) from exc
        if missing:
            raise CompanionError("team 事实库缺少表（%s），schema 不完整或已损坏：%s"
                                 % (", ".join(missing), path))
        return store
    except BaseException:
        store.close()
        raise


def _open_existing(path):
    """打开既有 team 事实库；不存在时报错引导 team-init，绝不静默建空库。"""
    if not path.exists():
        raise CompanionError("team 事实库不存在：%s；请先运行 team-init" % path)
    store = db.Store(path)
    store.open()
    return store


def set_task_status(project_dir, task_id, feature_id_value, status, *, expect_seq=None,
                    reason=None, blocked_by=(), workspace=None):
    """team-task：经门禁的状态转换（FIX-04/SR-01：白名单只是词法，门禁才是语义）。

    - ready/running：任务必须已存在（team-task-add），转换经 domain.tasks 白名单；
      running 强制创建 attempt（attempt_no 递增 + started_at，写入事件）。
    - done：前置门——(a) 未完结 attempt；(b) 该 attempt 有 succeeded 回报；
      (c) feature review_required 时的有效独立审查批准（ReviewBoard 语义）；
      (d) done 事件与完成证据（evidence_view 行）同一 SQLite 事务提交，缺一整体回滚；
      (e) FIX04-followup：回报的每个改动文件当前内容 sha 必须仍等于报告时采集的
      sha，且 changed_files ⊆ allowed_paths（产物漂移=拒绝，指出文件与新旧哈希）。
      workspace 指定产物核验目录（缺省项目根；实现在独立 worktree 时传其路径）。
      缺任一前置即拒绝并逐条列出缺什么，不写任何事件。
    - blocked/cancelled：必须 --reason（Z24 统一入口语义，来源状态白名单校验）。
    - failed：须有进行中 attempt（running→failed），关闭该 attempt 并要求 --reason。
    - pending：不接受直接置位——任务创建必须经 team-task-add（带类型/依赖/文件边界）。
    expect_seq = 调用方从 team-status 读到的 generation：与当前视图 head 不符即拒。
    """
    tid = text(task_id, "任务编号")
    fid = text(feature_id_value, "功能编号")
    if status not in TASK_STATUSES:
        raise CompanionError("未知任务状态：%s（允许：%s）" % (status, "/".join(TASK_STATUSES)))
    if status == "pending":
        raise CompanionError("任务创建必须经 team-task-add（含类型/依赖/文件边界），"
                             "不接受直接置 pending")
    if expect_seq is not None and (isinstance(expect_seq, bool) or not isinstance(expect_seq, int)
                                   or expect_seq < 0):
        raise CompanionError("expect_seq 必须是非负整数")
    path = team_db_path(project_dir)
    store = _open_existing(path)
    try:
        facts = _replay(store)
        if fid not in facts.features:
            raise CompanionError("功能 %s 未在 team 事实库登记；请先 team-init" % fid)
        entry = facts.tasks.get(tid)
        if entry is None:
            raise CompanionError("任务 %s 不存在；请先 team-task-add 创建"
                                 "（门禁不接受任意状态 upsert，2026-09 复核 SR-01）" % tid)
        if entry["feature_id"] != fid:
            raise CompanionError("任务 %s 属于功能 %s，与 --feature %s 不符"
                                 % (tid, entry["feature_id"], fid))
        if status == "ready":
            return _action_ready(facts, store, path, tid, fid, expect_seq)
        if status == "running":
            return _action_running(facts, store, path, tid, fid, expect_seq)
        if status == "done":
            return _action_done(facts, store, path, tid, fid, expect_seq,
                                _resolve_workspace(project_dir, workspace))
        if status == "failed":
            return _action_failed(facts, store, path, tid, fid, expect_seq, reason)
        if status == "blocked":
            return _action_blocked(facts, store, path, tid, fid, expect_seq, reason,
                                   blocked_by, target="blocked", verb="block")
        return _action_blocked(facts, store, path, tid, fid, expect_seq, reason,
                               (), target="cancelled", verb="cancel")
    finally:
        store.close()


def add_task(project_dir, task_id, feature_id_value, kind, depends_on=(), priority=0,
             allowed_paths=(), *, expect_seq=None):
    """team-task-add：任务创建动作（SR-01：创建/派发/回报/审查/集成各有前置）。

    任务必须显式创建后才可派发——类型、依赖（DAG）、优先级与文件级 allowed_paths
    经 TaskBoard.add_task 校验后随 pending 事件一次落库；创建是状态机的起点，
    不再允许 team-task 从任意状态凭空造出任务（复核探针 never-executed 即此洞）。
    """
    tid = text(task_id, "任务编号")
    fid = text(feature_id_value, "功能编号")
    if kind not in TASK_KINDS:
        raise CompanionError("未知任务类型：%s（允许：%s）" % (kind, "/".join(TASK_KINDS)))
    if expect_seq is not None and (isinstance(expect_seq, bool) or not isinstance(expect_seq, int)
                                   or expect_seq < 0):
        raise CompanionError("expect_seq 必须是非负整数")
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise CompanionError("优先级必须是整数")
    deps = strings([dep.strip() for dep in list(depends_on) if dep and dep.strip()],
                   "依赖任务")
    paths = [relative_path(p) for p in list(allowed_paths)]
    path = team_db_path(project_dir)
    store = _open_existing(path)
    try:
        facts = _replay(store)
        if fid not in facts.features:
            raise CompanionError("功能 %s 未在 team 事实库登记；请先 team-init" % fid)
        facts.board.add_task(tid, fid, kind, depends_on=deps, priority=priority,
                             allowed_paths=paths)
        payload = {"feature_id": fid, "status": "pending", "kind": kind,
                   "depends_on": deps, "priority": priority,
                   "allowed_paths": list(dict.fromkeys(paths))}
        writer = db.acquire_writer(store)
        try:
            result = _append_batch(store, writer.epoch, [(
                views.EVENT_TASK_STATUS, tid, payload, "team-task-add:%s" % tid)],
                expect_seq=expect_seq)[0]
        finally:
            writer.close()
        return {"store": str(path), "task_id": tid, "feature_id": fid, "kind": kind,
                "status": "pending", "seq": result["seq"], "applied": result["applied"],
                "deduped": result["deduped"], "generation": events.view_head(store)}
    finally:
        store.close()


def report(project_dir, task_id, outcome, summary, changed_files=(), artifact_sha256=None,
           *, expect_seq=None, workspace=None):
    """team-report：attempt 回报动作（done 前置门 (b) 的事实来源）。

    回报绑定当前未完结 attempt（task_id#attempt_no），携带 outcome/summary/
    changed_files 与产出固定 artifact_sha256；回报即实现者以该固定版本提交审查
    （ReviewBoard.submit_for_review 校验，team-approve 只能审这个 sha）。
    FIX04-followup：changed_files 逐条核对 ⊆ 任务 allowed_paths（越界即拒并指出
    越界文件），且每个改动文件在报告时点从工作区（workspace，缺省项目根）真实
    读取并计算内容 sha256 记入 file_shas——报告摘要由真实采集产生，done/integrate
    据此绑定实际产物版本。没有进行中 attempt（未 running 或已关闭）即拒绝。
    outcome=failed 的回报不关 attempt——关闭用 team-task --status failed --reason
    （TK08：失败重试开新 attempt）。
    """
    tid = text(task_id, "任务编号")
    if outcome not in ("succeeded", "failed"):
        raise CompanionError("回报 outcome 只能是 succeeded/failed：%s" % outcome)
    summary = text(summary, "回报摘要")
    sha = _check_sha256(artifact_sha256)
    changes = [relative_path(p) for p in list(changed_files)]
    root = _resolve_workspace(project_dir, workspace)
    path = team_db_path(project_dir)
    store = _open_existing(path)
    try:
        facts = _replay(store)
        entry = facts.tasks.get(tid)
        if entry is None:
            raise CompanionError("任务 %s 不存在；请先 team-task-add 创建" % tid)
        attempts = facts.board.attempts(tid)
        if not attempts or attempts[-1]["outcome"] != "pending":
            raise CompanionError("任务 %s 没有进行中的 attempt（先 team-task --status running）"
                                 % tid)
        no = attempts[-1]["attempt_no"]
        ref = attempt_id(tid, no)
        file_shas = _collect_file_shas(root, entry["allowed_paths"], changes)
        facts.review_board.submit_for_review(attempts[-1], sha)  # 最新 attempt 才可提交
        payload = {"kind": "check", "role": "report", "subject_id": ref,
                   "result": outcome, "source": "team-gate",
                   "detail": "attempt %s 回报 outcome=%s：%s" % (ref, outcome, summary),
                   "fact": {"task_id": tid, "attempt_no": no, "outcome": outcome,
                            "summary": summary,
                            "changed_files": list(dict.fromkeys(changes)),
                            "artifact_sha256": sha, "file_shas": file_shas}}
        writer = db.acquire_writer(store)
        try:
            # 幂等键含固定版本 sha：同一版本重报去重；合法重报（内容变化→新 sha）
            # 必须落为新事实——否则漂移后重走 report→approve→done 的链被旧键吞掉
            #（FIX04-followup 正向对照：合法新版本经重新报告/批准后仍可完成）。
            result = _append_batch(store, writer.epoch, [(
                views.EVENT_EVIDENCE_REGISTERED, "report:" + ref, payload,
                "team-report:%s:%s" % (ref, sha))], expect_seq=expect_seq)[0]
        finally:
            writer.close()
        return {"store": str(path), "task_id": tid, "attempt": ref, "outcome": outcome,
                "artifact_sha256": sha, "changed_files": list(dict.fromkeys(changes)),
                "file_shas": file_shas,
                "seq": result["seq"], "applied": result["applied"],
                "deduped": result["deduped"], "generation": events.view_head(store)}
    finally:
        store.close()


def approve(project_dir, task_id, reviewer, verdict, blockers=(), review_id=None,
            *, expect_seq=None):
    """team-approve：独立审查动作（SR-01 (c)：复用 ReviewBoard 语义）。

    结论绑定被审 attempt 回报的固定 artifact sha（调用方不能传 sha——防止审的与
    批的不是同一版本）；实现者自审（reviewer 身份 = 实现 attempt 引用）拒绝；
    未回报先审拒绝。verdict=changes_requested 必须逐条 --blockers。review_id 缺省
    自动生成：同一 subject 多条结论按落账顺序取最新（ReviewBoard 既有语义）。
    """
    tid = text(task_id, "任务编号")
    reviewer = text(reviewer, "审查者身份")
    if verdict not in ("approved", "changes_requested"):
        raise CompanionError("verdict 只能是 approved/changes_requested：%s" % verdict)
    blocks = strings([b.strip() for b in list(blockers) if b and b.strip()], "阻塞项")
    if verdict == "approved" and blocks:
        raise CompanionError("approved 不得携带阻塞项")
    if verdict == "changes_requested" and not blocks:
        raise CompanionError("changes_requested 必须逐条写明阻塞项（--blockers）")
    path = team_db_path(project_dir)
    store = _open_existing(path)
    try:
        facts = _replay(store)
        entry = facts.tasks.get(tid)
        if entry is None:
            raise CompanionError("任务 %s 不存在；请先 team-task-add 创建" % tid)
        attempts = facts.board.attempts(tid)
        if not attempts or attempts[-1]["outcome"] != "pending":
            raise CompanionError("任务 %s 没有进行中的 attempt，无可审对象" % tid)
        no = attempts[-1]["attempt_no"]
        ref = attempt_id(tid, no)
        report = facts.reports.get(ref)
        if report is None:
            raise CompanionError("attempt %s 尚无回报（先 team-report 提交固定版本），拒绝审查" % ref)
        rid = text(review_id, "审查记录编号") if review_id else "rev:%s:%s" % (ref, uuid.uuid4().hex[:12])
        record = ReviewRecord(rid, "attempt", ref, report["artifact_sha256"],
                              {"role": reviewer, "attempt_id": reviewer}, verdict,
                              blocks, db.utcnow()).to_dict()
        facts.review_board.record(record)  # 独立性 + sha 绑定 + 请求存在性（真实校验）
        payload = {"kind": "check", "role": "review_record", "subject_id": ref,
                   "result": verdict, "source": "team-gate",
                   "detail": "%s 对 %s 的审查结论：%s" % (reviewer, ref, verdict),
                   "fact": record}
        writer = db.acquire_writer(store)
        try:
            result = _append_batch(store, writer.epoch, [(
                views.EVENT_EVIDENCE_REGISTERED, record["review_id"], payload,
                "team-approve:%s" % rid)], expect_seq=expect_seq)[0]
        finally:
            writer.close()
        return {"store": str(path), "task_id": tid, "attempt": ref, "review_id": rid,
                "verdict": verdict, "reviewer": reviewer,
                "subject_sha256": report["artifact_sha256"],
                "seq": result["seq"], "applied": result["applied"],
                "deduped": result["deduped"], "generation": events.view_head(store)}
    finally:
        store.close()


def integrate(project_dir, candidate_id, task_ids, check_argv, cwd=None, timeout=300,
              *, expect_seq=None):
    """team-integrate：集成动作（SR-01：复用 IntegrationBoard 语义与检查点）。

    候选（各任务 done + succeeded 回报的固定 sha；review_required 时必须有有效独立
    批准且 sha 一致）→ 版本（内容哈希幂等）→ 两级回归登记：功能级如实记 unknown
    （team 模式未接功能级新鲜度评估；只记录不拦截），版本级 = 真实 subprocess 运行
    check_argv（shlex 切分、无 shell；退出码即通过与否）→ 通过才允许 complete。
    FIX04-followup：入列前对每个候选任务重执行 done 门的产物核验——回归目录
    （cwd，缺省项目根）里各改动文件的当前内容 sha 必须仍等于报告时采集值，任一
    漂移即整体拒绝（候选失效，须重报重审）——保证被测内容就是批准/报告的版本；
    并把每个任务的报告 sha 集合（artifact + 逐文件）作为 regressed_on 记入版本级
    回归与完成证据（feature_level 仍如实标 unknown，不虚标已验证）。
    回归失败：候选/版本/失败回归照常落库（事实如实），status 保持 candidate，
    结果 passed=False（CLI 退出 3）。候选、版本、回归、完成证据同一事务提交。
    """
    cid = text(candidate_id, "候选编号")
    tids = strings([t.strip() for t in list(task_ids) if t and t.strip()], "集成任务")
    if not tids:
        raise CompanionError("team-integrate 至少需要一个任务编号（--tasks）")
    argv = shlex.split(check_argv if isinstance(check_argv, str) else text(check_argv, "检查命令"))
    if not argv:
        raise CompanionError("版本级回归必须提供真实检查命令（--check-cmd），不接受占位")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 < timeout <= 3600:
        raise CompanionError("timeout 必须是 1..3600 的秒数")
    work_dir = realpath(Path(cwd)) if cwd is not None else realpath(Path(project_dir))
    if not work_dir.is_dir():
        raise CompanionError("回归运行目录不存在：%s" % work_dir)
    path = team_db_path(project_dir)
    store = _open_existing(path)
    try:
        facts = _replay(store)
        artifacts = {}
        regressed_on = {}   # FIX04-followup：回归实际运行于的报告 sha 集合（进 manifest 证据）
        for tid in tids:
            entry = facts.tasks.get(tid)
            if entry is None:
                raise CompanionError("任务 %s 不存在；请先 team-task-add 创建" % tid)
            attempts = facts.board.attempts(tid)
            if entry["status"] != "done" or not attempts \
                    or attempts[-1]["outcome"] != "succeeded":
                raise CompanionError("任务 %s 尚未 done（当前 %s），不能入列集成候选"
                                     % (tid, entry["status"]))
            report = facts.reports.get(attempt_id(tid, attempts[-1]["attempt_no"]))
            if report is None or report.get("outcome") != "succeeded":
                raise CompanionError("任务 %s 缺少 succeeded 回报，集成对象无固定版本" % tid)
            file_shas = _verify_reported_files(work_dir, tid, report, action="team-integrate")
            _verify_report_scope(tid, entry["allowed_paths"], report)
            artifacts[tid] = report["artifact_sha256"]
            regressed_on[tid] = {"artifact_sha256": report["artifact_sha256"],
                                 "files": dict(file_shas) if file_shas else {}}
        facts.integration.add_candidate(cid, artifacts)  # 批准/sha 一致门（真实校验）
        manifest = facts.integration.build_integration_version([cid])
        vno, sha = manifest["integration_version"], manifest["manifest_sha256"]
        fids = sorted({facts.board.task(t)["feature_id"] for t in tids})
        feature_level = [{"feature_id": f, "status": "unknown", "conservative": True,
                          "stale_files": [], "hit_modules": [], "transitive": False}
                         for f in fids]
        facts.integration.record_feature_level(vno, feature_level)
        try:
            proc = subprocess.run(argv, cwd=str(work_dir), capture_output=True,
                                  text=True, timeout=timeout)
            exit_code, stderr_tail = proc.returncode, (proc.stderr or "")[-400:]
        except subprocess.TimeoutExpired as exc:
            exit_code = -1
            stderr_tail = "检查超时（%d 秒）" % timeout
            del exc
        passed = exit_code == 0
        detail_cmd = " ".join(argv)
        facts.integration.record_version_level(
            vno, passed, detail="%s（cwd=%s，exit=%s）" % (detail_cmd, work_dir, exit_code))
        specs = [
            (views.EVENT_EVIDENCE_REGISTERED, "integration-candidate:" + cid,
             {"kind": "check", "role": "integration_candidate", "subject_id": cid,
              "result": "registered", "source": "team-gate",
              "detail": "候选 %s 登记：%s" % (cid, "、".join(sorted(artifacts))),
              "fact": {"tasks": {t: artifacts[t] for t in sorted(artifacts)}}},
             "team-integrate:candidate:%s:%s" % (cid, sha)),
            (views.EVENT_EVIDENCE_REGISTERED, "integration-version:%d" % vno,
             {"kind": "check", "role": "integration_version", "subject_id": "integration-version:%d" % vno,
              "result": "candidate", "source": "team-gate",
              "detail": "集成版本 %d candidate manifest=%s" % (vno, sha),
              "fact": {"integration_version": vno, "manifest_sha256": sha,
                       "candidates": [cid]}},
             "team-integrate:version:%d:%s" % (vno, sha)),
            (views.EVENT_EVIDENCE_REGISTERED, "integration-version:%d:feature-level" % vno,
             {"kind": "check", "role": "integration_feature_level",
              "subject_id": "integration-version:%d" % vno, "result": "recorded",
              "source": "team-gate",
              "detail": "集成版本 %d 功能级回归登记（unknown=未接新鲜度评估）" % vno,
              "fact": {"integration_version": vno, "records": feature_level}},
             "team-integrate:feature-level:%d:%s" % (vno, sha)),
            (views.EVENT_EVIDENCE_REGISTERED, "integration-version:%d:version-level" % vno,
             {"kind": "check", "role": "integration_version_level",
              "subject_id": "integration-version:%d" % vno,
              "result": "passed" if passed else "failed", "source": "team-gate",
              "detail": "集成版本 %d 版本级回归 %s（exit=%s）" % (vno, detail_cmd, exit_code),
              "fact": {"integration_version": vno, "command": detail_cmd,
                       "exit_code": exit_code, "cwd": str(work_dir),
                       "regressed_on": regressed_on}},
             # 回归重跑是新的覆盖事实（IntegrationBoard：再次记录覆盖），键按次唯一，
             # 不随候选内容 sha 去重——同版本先失败后通过的回归不能被旧失败去重吞掉。
             "team-integrate:version-level:%d:%s" % (vno, uuid.uuid4().hex[:12])),
        ]
        status = "candidate"
        if passed:
            facts.integration.complete(vno)  # G-INTEGRATION：版本级回归通过才 completed
            specs.append((
                views.EVENT_EVIDENCE_REGISTERED, "integration-version:%d" % vno,
                {"kind": "acceptance", "role": "integration_completed",
                 "subject_id": "integration-version:%d" % vno, "result": "completed",
                 "source": "team-gate",
                 "detail": "集成版本 %d 完成 manifest=%s" % (vno, sha),
                 "fact": {"integration_version": vno, "manifest_sha256": sha,
                          "regressed_on": regressed_on}},
                "team-integrate:complete:%d:%s" % (vno, sha)))
            status = "completed"
        version_id = "integration-version:%d" % vno

        def verify(conn):
            row = conn.execute("SELECT result FROM evidence_view WHERE evidence_id = ?",
                               (version_id,)).fetchone()
            if row is None:
                raise CompanionError("集成证据未随同事务折叠（evidence_view 缺行）；整体回滚")
            if status == "completed" and row["result"] != "completed":
                raise CompanionError("集成完成证据未随同事务折叠；整体回滚")

        writer = db.acquire_writer(store)
        try:
            results = _append_batch(store, writer.epoch, specs, expect_seq=expect_seq,
                                    verify=verify)
        finally:
            writer.close()
        return {"store": str(path), "candidate_id": cid, "tasks": tids,
                "integration_version": vno, "manifest_sha256": sha, "status": status,
                "passed": passed, "regressed_on": regressed_on,
                "version_level": {"command": detail_cmd,
                                  "exit_code": exit_code,
                                  "passed": passed},
                "feature_level": feature_level,
                "applied": sum(1 for r in results if r["applied"]),
                "deduped": sum(1 for r in results if r["deduped"]),
                "generation": events.view_head(store)}
    finally:
        store.close()


def team_accept(project_dir, feature_id_value, note, user_confirmed, *, expect_seq=None):
    """team 模式 accept 入口（Step-8 验收路由）：用户验收动作（五前置门，缺一拒绝并指名）。

    复核 §4：team-only 项目的 accept 此前误入 legacy Project.accept（exit 2
    "尚未建立需求记录"）——本入口补上团队链路的验收集成，复用既有集成/证据模型，
    不直接改库、不创建 legacy state.json：
    (a) 功能已在 team 事实库登记；
    (b) 该功能全部 impl/review 任务 status=done 且最新 attempt 有 succeeded 回报；
    (c) 存在已完成的集成版本（status=completed 且版本级回归通过）且其候选任务
        覆盖该功能的全部 done 任务；
    (d) 产物未漂移：集成 manifest regressed_on 的每个文件当前内容 sha256 仍等于
        集成时采集值（漂移即拒绝并指出文件与新旧哈希——须重走
        team-report → team-approve → team-integrate）；
    (e) user_confirmed 为真且 note 非空——没有用户真实试用确认不产生验收
        （缺省一律拒绝，不把"无确认"当默认同意）。
    通过 → acceptance 证据事件（绑定集成候选/版本/manifest_sha256/regressed_on/
    note/user_confirmed/accepted_at，evidence_view 可查）与 feature 状态 accepted
    事件同一事务提交（折叠核验失败整体回滚）。
    """
    fid = text(feature_id_value, "功能编号")
    note = text(note, "验收说明")
    path = team_db_path(project_dir)
    store = _open_existing(path)
    try:
        facts = _replay(store)
        # (a) 功能存在
        if fid not in facts.features:
            raise CompanionError("功能 %s 未在 team 事实库登记；请先 team-init" % fid)
        # (b) 该功能全部 impl/review 任务 done 且有 succeeded 回报
        done_tids, missing = [], []
        for tid in sorted(facts.tasks):
            entry = facts.tasks[tid]
            if entry["feature_id"] != fid or entry["kind"] not in ("impl", "review"):
                continue
            if entry["status"] != "done":
                missing.append("任务 %s 状态 %s（须 done）" % (tid, entry["status"]))
                continue
            attempts = facts.board.attempts(tid)
            report = facts.reports.get(attempt_id(tid, attempts[-1]["attempt_no"])) \
                if attempts else None
            if not attempts or attempts[-1]["outcome"] != "succeeded" or report is None:
                missing.append("任务 %s 缺最新 attempt 的 succeeded 回报" % tid)
            done_tids.append(tid)
        if missing:
            raise CompanionError("功能 %s 不能验收，任务门未过：%s" % (fid, "；".join(missing)))
        # (c) 已完成的集成版本 + 候选任务覆盖全部 done 任务
        completed = [m for m in facts.integration_versions
                     if m["status"] == "completed" and m.get("version_level_passed") is True]
        if not completed:
            raise CompanionError(
                "功能 %s 不能验收：没有已完成的集成版本"
                "（team-integrate 版本级回归通过后才是 completed）" % fid)
        vno = max(m["integration_version"] for m in completed)
        manifest = facts.integration.version(vno)
        sha = manifest["manifest_sha256"]
        candidate_tids = {task["task_id"]: task["subject_sha256"]
                          for candidate in manifest["candidates"] for task in candidate["tasks"]}
        uncovered = sorted(set(done_tids) - set(candidate_tids))
        if uncovered:
            raise CompanionError(
                "功能 %s 不能验收：已完成集成版本 %d 的候选任务未覆盖其全部 done 任务（缺 %s）"
                % (fid, vno, "、".join(uncovered)))
        # (d) 产物未漂移：regressed_on 每个文件当前 sha 仍等于集成回归时采集值
        root = realpath(Path(project_dir))
        regressed_on = (facts.version_bindings.get(vno) or {}).get("regressed_on") or {}
        drifted = []
        for tid in sorted(regressed_on):
            for rel, recorded in sorted((regressed_on.get(tid) or {}).get("files", {}).items()):
                try:
                    current = digest.sha256_file(_workspace_file(root, rel))[0]
                except CompanionError:
                    current = None
                if current != recorded:
                    drifted.append("%s（任务 %s）：集成时 sha=%s，当前 %s"
                                   % (rel, tid, recorded,
                                      "sha=%s" % current if current else "不存在或不可核"))
        if drifted:
            raise CompanionError(
                "功能 %s 验收被拒：集成版本 %d 的产物已漂移（批准与回归只对集成时固定版本有效，"
                "须重走 team-report → team-approve → team-integrate）：%s"
                % (fid, vno, "；".join(drifted)))
        # (e) 用户真实确认（最后核验：复核 §4 指出旧路径在核验同意之前就进 legacy）
        if user_confirmed is not True:
            raise CompanionError("需要用户真实试用反馈后验收：必须显式 --user-confirmed，"
                                 "不接受把缺省当作已确认")
        accepted_at = db.utcnow()
        cids = sorted(candidate["candidate_id"] for candidate in manifest["candidates"])
        evidence_id = "feature-acceptance:" + fid
        key = "team-accept:%s:%d:%s" % (fid, vno, uuid.uuid4().hex[:12])
        feature = facts.features[fid]
        specs = [
            (views.EVENT_EVIDENCE_REGISTERED, evidence_id,
             {"kind": "acceptance", "role": "feature_accepted", "subject_id": fid,
              "result": "accepted", "source": "team-gate",
              "detail": "功能 %s 用户验收：集成版本 %d（manifest=%s，候选 %s）：%s"
                        % (fid, vno, sha, "、".join(cids), note),
              "fact": {"feature_id": fid, "note": note, "user_confirmed": True,
                       "accepted_at": accepted_at,
                       "integration": {"integration_version": vno, "manifest_sha256": sha,
                                       "candidates": cids,
                                       "candidate_tasks": candidate_tids,
                                       "regressed_on": regressed_on}}},
             key),
            (views.EVENT_FEATURE_STATUS, fid,
             {"title": feature["title"], "status": "accepted",
              "review_required": feature["review_required"],
              "allowed_paths": feature["allowed_paths"]},
             key + ":status"),
        ]

        def verify(conn):
            row = conn.execute("SELECT result FROM evidence_view WHERE evidence_id = ?",
                               (evidence_id,)).fetchone()
            if row is None or row["result"] != "accepted":
                raise CompanionError("验收证据未随同事务折叠（evidence_view 缺行）；整体回滚")
            frow = conn.execute("SELECT status FROM feature_view WHERE feature_id = ?",
                                (fid,)).fetchone()
            if frow is None or frow["status"] != "accepted":
                raise CompanionError("accepted 状态未随同事务折叠进功能视图；整体回滚")

        writer = db.acquire_writer(store)
        try:
            results = _append_batch(store, writer.epoch, specs, expect_seq=expect_seq,
                                    verify=verify)
        finally:
            writer.close()
        return {"store": str(path), "feature_id": fid, "status": "accepted",
                "note": note, "user_confirmed": True, "accepted_at": accepted_at,
                "integration": {"integration_version": vno, "manifest_sha256": sha,
                                "candidates": cids,
                                "candidate_tasks": dict(sorted(candidate_tids.items())),
                                "regressed_on": regressed_on},
                "seq": results[-1]["seq"], "applied": all(r["applied"] for r in results),
                "deduped": any(r["deduped"] for r in results),
                "generation": events.view_head(store)}
    finally:
        store.close()


def gate_check(project_dir, feature_id_value, kind="feature"):
    """team 模式 check 入口：只读门禁审计（SR-05：正常 check 入口接同一事实库）。

    逐任务核对状态与证据一致性：done 必须有完成证据行 + succeeded attempt + 回报
    （review_required 时另有有效独立批准）；running 必须有未完结 attempt；blocked
    必须有原因。kind=integration 时加查集成版本：completed 必须有通过版本级回归，
    candidate 不得冒充完成。只读，不写任何事实；违例逐条列出（passed=False → CLI 3）。
    FIX04-followup 语义澄清：本入口是台账审计（audit_only=true）——核对的是库内
    状态与证据的一致性，不等同于功能验证/回归；实际产物版本绑定在 report/done/
    integrate 门禁里核验，验收不得以本审计代替真实检查。
    """
    fid = text(feature_id_value, "功能编号")
    if kind not in ("feature", "integration"):
        raise CompanionError("检查类型必须为 feature 或 integration")
    path = team_db_path(project_dir)
    store = _open_readonly(path)
    try:
        facts = _replay(store)
        if fid not in facts.features:
            raise CompanionError("功能 %s 未在 team 事实库登记；请先 team-init" % fid)
        violations = []
        tasks = sorted((t for t in facts.tasks.values() if t["feature_id"] == fid),
                       key=lambda t: t["id"])
        if not tasks:
            violations.append("功能 %s 下没有登记任务（team-task-add）" % fid)
        for entry in tasks:
            tid = entry["id"]
            status = entry["status"]
            attempts = facts.board.attempts(tid)
            open_attempt = attempts and attempts[-1]["outcome"] == "pending"
            if status == "done":
                if store.query_one("SELECT 1 FROM evidence_view WHERE evidence_id = ?",
                                   ("task:" + tid,)) is None:
                    violations.append("任务 %s 状态 done 但 evidence_view 无完成证据行" % tid)
                if not attempts or attempts[-1]["outcome"] != "succeeded":
                    violations.append("任务 %s 状态 done 但无 succeeded attempt" % tid)
                elif facts.reports.get(attempt_id(tid, attempts[-1]["attempt_no"])) is None:
                    violations.append("任务 %s 状态 done 但 attempt 缺回报" % tid)
                if facts.features[fid]["review_required"]:
                    verdict = facts.review_board.current_approval(tid)
                    if verdict["status"] != "approved":
                        violations.append("任务 %s 缺有效独立审查批准：%s"
                                          % (tid, facts._verdict_reason(verdict)))
            elif status == "running" and not open_attempt:
                violations.append("任务 %s 状态 running 但没有未完结 attempt" % tid)
            elif status == "blocked" and not entry["block_reason"]:
                violations.append("任务 %s 状态 blocked 但无原因" % tid)
        if kind == "integration":
            if not facts.integration_versions:
                violations.append("没有任何集成版本（team-integrate）")
            for manifest in facts.integration_versions:
                if manifest["status"] != "completed":
                    violations.append("集成版本 %d 尚未完成（当前 %s；版本级回归通过才 completed）"
                                      % (manifest["integration_version"], manifest["status"]))
                elif manifest.get("version_level_passed") is not True:
                    violations.append("集成版本 %d 标记 completed 但版本级回归未通过"
                                      % manifest["integration_version"])
        result = {"feature_id": fid, "kind": kind, "passed": not violations,
                  "audit_only": True, "violations": violations,
                  "tasks": [{"task_id": t["id"], "status": t["status"],
                             "kind": t["kind"]} for t in tasks],
                  "store": str(path), "generation": events.view_head(store)}
        return result
    finally:
        store.close()


def resume_view(project_dir):
    """team 模式 resume 入口：持久事实 + 可执行续接清单（SR-05：恢复不靠聊天记忆）。

    从事件回放还原全部事实（features/tasks/attempts/回报/审查/集成），并给出每个
    未闭合事实的门禁下一步（派发/回报/审查/done 门/集成）；completed 事实不列。
    只读；ResumeLedger 的接管与登记由调用方（companion.py resume 入口）组合。
    """
    path = team_db_path(project_dir)
    store = _open_readonly(path)
    try:
        facts = _replay(store)
        plan = []
        tasks_by_feature = {}
        for entry in facts.tasks.values():
            tasks_by_feature.setdefault(entry["feature_id"], []).append(entry["id"])
        for fid in sorted(facts.features):
            if not tasks_by_feature.get(fid):
                plan.append({"kind": "feature", "id": fid, "status":
                             facts.features[fid]["status"], "feature_id": fid,
                             "action": "team-task-add --set <任务编号> --feature %s "
                                       "--kind impl --allowed-paths <文件清单>" % fid,
                             "detail": "功能下还没有任务（review_required=%s）"
                                       % facts.features[fid]["review_required"]})
        for tid in sorted(facts.tasks):
            entry = facts.tasks[tid]
            status = entry["status"]
            item = {"kind": "task", "id": tid, "status": status,
                    "feature_id": entry["feature_id"], "action": None, "detail": None}
            attempts = facts.board.attempts(tid)
            if status == "pending":
                item["action"] = "team-task --set %s --status ready" % tid
                item["detail"] = "任务待派发"
            elif status == "ready":
                item["action"] = "team-task --set %s --status running" % tid
                item["detail"] = "任务已就绪待执行"
            elif status == "running" and attempts and attempts[-1]["outcome"] == "pending":
                ref = attempt_id(tid, attempts[-1]["attempt_no"])
                report = facts.reports.get(ref)
                if report is None:
                    item["action"] = ("team-report --task %s --outcome succeeded "
                                      "--summary ... --artifact-sha256 ..." % tid)
                    item["detail"] = "attempt %s 执行中，待回报" % ref
                else:
                    needs_review = facts.features[entry["feature_id"]]["review_required"]
                    approved = needs_review and facts.review_board.current_approval(
                        tid)["status"] == "approved"
                    if needs_review and not approved:
                        item["action"] = ("team-approve --task %s --reviewer <独立审查者> "
                                          "--verdict approved" % tid)
                        item["detail"] = "attempt %s 已回报，待独立审查" % ref
                    else:
                        item["action"] = "team-task --set %s --status done" % tid
                        item["detail"] = "attempt %s 已回报%s，done 门可过"
                        item["detail"] = item["detail"] % (ref, "且批准" if approved else "")
            elif status == "blocked":
                item["detail"] = "阻断原因：%s" % entry["block_reason"]
            elif status == "failed":
                item["action"] = "team-task --set %s --status ready（重试开新 attempt）" % tid
                item["detail"] = "上次失败：%s" % (attempts[-1]["failure_reason"]
                                                  if attempts else None)
            if item["action"] or item["detail"]:
                plan.append(item)
        for manifest in facts.integration_versions:
            if manifest["status"] != "completed":
                plan.append({"kind": "integration", "id": "integration-version:%d"
                             % manifest["integration_version"], "status": manifest["status"],
                             "feature_id": None,
                             "action": "team-integrate --candidate <候选> --tasks ... "
                                       "--check-cmd <真实回归命令>",
                             "detail": "集成版本未完成（版本级回归通过后才 completed）"})
        return {"mode": "team", "store": str(path), "generation": events.view_head(store),
                "features": views.list_features(store, offset=0, limit=_MAX_LIMIT),
                "tasks": views.list_tasks(store, offset=0, limit=_MAX_LIMIT),
                "activity": _activity(facts), "resume_plan": plan,
                "next": plan[0] if plan else None}
    finally:
        store.close()


def init_feature(project_dir, feature_id_value, title, *, review_required=False,
                 allowed_paths=()):
    """team-init：建库（首次）+ 登记 feature（feature_status 事件，epoch 注册写者）。

    同 feature 重复 init 以幂等键去重（applied=False），事实不变。
    review_required（FIX-04）：feature 级审查 policy——开启后该功能下 impl/review
    任务的 done 门要求有效独立审查批准（复用 ReviewBoard 语义），缺省 False。
    """
    fid = feature_id(text(feature_id_value, "功能编号"), set())
    title = text(title, "功能标题")
    if not isinstance(review_required, bool):
        raise CompanionError("review_required 必须是布尔")
    paths = [relative_path(p) for p in list(allowed_paths)]
    path = team_db_path(project_dir)
    store = db.Store(path)
    store.open()
    try:
        payload = {"title": title, "status": "draft", "review_required": review_required,
                   "allowed_paths": list(dict.fromkeys(paths))}
        writer = db.acquire_writer(store)
        try:
            result = events.append_event(
                store, writer.epoch, event_type=views.EVENT_FEATURE_STATUS,
                entity_id=fid, payload=payload,
                idempotency_key="team-init:" + fid)
        finally:
            writer.close()
        return {"store": str(path), "feature_id": fid, "title": title,
                "status": "draft", "review_required": review_required,
                "seq": result["seq"], "applied": result["applied"],
                "generation": events.view_head(store)}
    finally:
        store.close()


# ---- 门禁动作内核（FIX-04/SR-01）：同事务多事件边界 + 事件回放 + 各动作前置 ----

# team 事实证据的来源标记：回放只解释带此标记的证据事件；迁移导入/手工登记的
# 其他 evidence_registered 事件（subject 形状各异）计入事实但不参与门禁重建。
_TEAM_SOURCE = "team-gate"


def _check_sha256(value):
    if not isinstance(value, str) or len(value) != 64 \
            or any(ch not in "0123456789abcdef" for ch in value):
        raise CompanionError("artifact_sha256 必须是 64 位小写十六进制：%r" % (value,))
    return value


# ---- FIX04-followup：attempt 与真实产物版本绑定（报告时采集 + 消费前复核） ----

def _resolve_workspace(project_dir, workspace):
    """产物采集/核验目录：workspace 优先，缺省项目根；先 realpath 归一（与库路径同口径）。"""
    root = realpath(Path(workspace)) if workspace else realpath(Path(project_dir))
    if not root.is_dir():
        raise CompanionError("工作区目录不存在：%s" % root)
    return root


def _workspace_file(root, rel):
    """改动文件的真实路径：逐段拒绝符号链接（与 core.safe_file 同口径），必须是普通文件。"""
    target = Path(root)
    for part in rel.split("/"):  # relative_path 已保证 POSIX 相对、无 ""/./.. 段
        target = target / part
        if target.is_symlink():
            raise CompanionError("文件链接不在支持范围：" + rel)
    if not target.is_file():
        raise CompanionError("改动文件在 %s 下不存在（无法采集/核对内容）：%s" % (root, rel))
    return target


def _collect_file_shas(root, allowed_paths, changed_files):
    """report 前置：changed_files 逐条核对 ⊆ allowed_paths（精确路径集合，越界即拒并
    指出越界文件）；每个改动文件在报告时点真实读取并计算内容 sha256（复用
    digest.sha256_file，报告摘要由真实采集产生，不只收调用方字符串）。"""
    if changed_files and not set(allowed_paths):
        raise CompanionError(
            "回报被拒：任务未声明文件边界（allowed_paths 为空），不能回报改动文件；"
            "请先经 team-task-add --allowed-paths 声明精确边界")
    outside = sorted(p for p in changed_files if p not in set(allowed_paths))
    if outside:
        raise CompanionError(
            "回报被拒：改动文件超出任务边界 allowed_paths：%s（任务边界：%s）"
            % (", ".join(outside), ", ".join(sorted(allowed_paths))))
    return {rel: digest.sha256_file(_workspace_file(root, rel))[0] for rel in changed_files}


def _verify_reported_files(root, tid, report, action):
    """done/integrate 前置：报告的每个改动文件当前内容 sha 必须仍等于报告时采集值
    （内容漂移/缺失=拒绝，指出具体文件与新旧哈希；等长同 mtime 的篡改因此无效）。
    file_shas 缺失的是本修复前落库的历史回报——按"门禁只管新写入"口径跳过
    （与 TaskBoard.adopt_task 同一原则），返回其 file_shas（历史回报为 None）。"""
    file_shas = report.get("file_shas")
    if not isinstance(file_shas, dict):
        return None
    drifted = []
    for rel, recorded in sorted(file_shas.items()):
        try:
            target = _workspace_file(root, rel)  # 与报告时同一路径策略（逐段拒链接）
        except CompanionError:
            drifted.append("%s：当前不存在或不可核（报告时 sha=%s）" % (rel, recorded))
            continue
        current = digest.sha256_file(target)[0]
        if current != recorded:
            drifted.append("%s：报告时 sha=%s，当前 sha=%s" % (rel, recorded, current))
    if drifted:
        raise CompanionError(
            "任务 %s %s 被拒：实际产物已偏离报告版本（批准只对报告时固定版本有效，"
            "须重新 team-report → 重新批准）：%s" % (tid, action, "；".join(drifted)))
    return file_shas


def _verify_report_scope(tid, allowed_paths, report):
    """done/integrate 前置复核：回报 changed_files ⊆ allowed_paths（report 时已校验，
    消费点再核一遍；同样只约束带 file_shas 采集数据的新回报）。"""
    changed = report.get("changed_files")
    if not isinstance(changed, list) or not isinstance(report.get("file_shas"), dict):
        return
    outside = sorted(p for p in changed if p not in set(allowed_paths))
    if outside:
        raise CompanionError(
            "任务 %s 回报的改动文件超出 allowed_paths：%s（任务边界：%s）"
            % (tid, ", ".join(outside), ", ".join(sorted(allowed_paths)) or "(空)"))


def _append_batch(store, epoch, specs, *, expect_seq=None, verify=None):
    """同一 SQLite 事务内追加多条事件（events.append_event 的批内版本）。

    events.append_event 每条自开事务且不可嵌套；门禁动作的"动作事件 + 其证据"
    必须同界提交（FIX-04 §3），因此在此复用其逐条步骤（幂等键去重 → epoch 门 →
    CAS → 插入 → 视图折叠 → head 前移），任一异常整体回滚。verify 在提交前于
    同一事务内核对物化视图（如 evidence_view 行必须已折叠），失败即回滚。
    """
    results = []
    with store.transaction() as conn:
        head = int(db.get_meta(conn, db.HEAD_SEQ_KEY, "0"))
        if expect_seq is not None and expect_seq != head:
            raise CompanionError("CAS 冲突：期望 head=%r，实际 head=%d" % (expect_seq, head))
        for event_type, entity_id, payload, idempotency_key in specs:
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            existing = conn.execute(
                "SELECT seq, event_id FROM events WHERE idempotency_key = ?",
                (idempotency_key,)).fetchone()
            if existing is not None:
                results.append({"seq": existing[0], "event_id": existing[1],
                                "applied": False, "deduped": True})
                continue
            db.require_epoch(conn, epoch)
            event_id = uuid.uuid4().hex
            cursor = conn.execute(
                """INSERT INTO events
                   (event_id, entity_type, entity_id, event_type, payload,
                    idempotency_key, writer_epoch, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, views.entity_type_for(event_type), entity_id, event_type,
                 serialized, idempotency_key, int(epoch), db.utcnow()))
            seq = cursor.lastrowid
            views.apply_event(conn, seq, event_type, entity_id, payload)
            db.set_meta(conn, db.HEAD_SEQ_KEY, seq)
            results.append({"seq": seq, "event_id": event_id, "applied": True,
                            "deduped": False})
        if verify is not None:
            verify(conn)
    return results


class _Facts:
    """一次门禁动作的回放快照：真实 TaskBoard/ReviewBoard/IntegrationBoard +
    事件证据索引。只存活于单次动作内，动作经真实板的既有校验执行后落事件。"""

    def __init__(self):
        self.features = {}      # fid -> {review_required, allowed_paths, status, title}
        self.tasks = {}         # tid -> task dict（board.task 的最新副本）
        self.reports = {}       # "tid#no" -> report detail dict
        self.integration_versions = []  # [{integration_version, status, version_level_passed}]
        # 验收绑定事实（team_accept 专用；_activity 视图形状不变）：vno →
        # {"manifest_sha256", "candidates", "regressed_on"}——来自集成证据事件。
        self.version_bindings = {}
        self.board = TaskBoard()
        self.review_board = ReviewBoard(self.board)
        self.board.review_guard = self.review_board.require_approval
        self.integration = IntegrationBoard(self.board, self.review_board)

    def _verdict_reason(self, verdict):
        if verdict["status"] == "changes_requested":
            return "changes_requested：%s" % "；".join(verdict["blockers"])
        return verdict.get("invalid_reason", "无有效批准")


def _replay(store):
    """从事件日志回放团队事实（只读）。

    门禁前置必须查持久事实而不是内存板（复核报告 §4："内存模块的审查门测试通过，
    不能证明公共入口不能绕过它"）。历史事件按 seq 顺序重放进真实板：
    - 任务创建事件带 kind/deps/allowed_paths；迁移导入的历史任务无这些扩展字段
      → kind 缺省 impl，状态经 TaskBoard.adopt_task 原样重建（不做白名单重审）；
    - done/failed 回放走 finish_attempt（review_required 功能的真实审查门随之
      复验——合法历史必然通过，被篡改历史在此暴露）；
    - 证据事件只解释带 source=team-gate 标记的：report → 提交审查请求，
      review_record → 落审查结论，integration_* → 重建候选/版本/回归。
    """
    facts = _Facts()
    for row in store.query_all(
            "SELECT seq, event_type, entity_id, payload FROM events ORDER BY seq"):
        payload = json.loads(row["payload"])
        event_type, entity_id = row["event_type"], row["entity_id"]
        if event_type == views.EVENT_FEATURE_STATUS:
            if entity_id not in facts.features:
                facts.board.add_feature(entity_id, payload.get("title", ""),
                                        allowed_paths=payload.get("allowed_paths", ()),
                                        review_required=bool(payload.get("review_required", False)))
            facts.features[entity_id] = {
                "title": payload.get("title", ""), "status": payload.get("status", ""),
                "review_required": bool(payload.get("review_required", False)),
                "allowed_paths": list(payload.get("allowed_paths", ()))}
        elif event_type == views.EVENT_TASK_STATUS:
            _replay_task_event(facts, entity_id, payload)
        elif event_type == views.EVENT_EVIDENCE_REGISTERED:
            if payload.get("source") == _TEAM_SOURCE:
                _replay_evidence(facts, entity_id, payload)
    return facts


def _replay_task_event(facts, tid, payload):
    status = payload.get("status")
    if tid not in facts.tasks:
        kind = payload.get("kind")
        facts.board.adopt_task(
            tid, payload.get("feature_id", ""),
            kind if kind in TASK_KINDS else "impl",
            depends_on=payload.get("depends_on", ()),
            priority=payload.get("priority", 0),
            allowed_paths=payload.get("allowed_paths", ()),
            status=status if status in TASK_STATUSES else "pending",
            block_reason=payload.get("reason"), blocked_by=payload.get("blocked_by", ()))
        facts.tasks[tid] = facts.board.task(tid)
        status = facts.tasks[tid]["status"]  # 后续 attempt 重建按历史终态
        if status == "running":
            facts.board.start_attempt(tid, payload.get("started_at", ""))
        return
    current = facts.tasks[tid]["status"]
    if status == current:
        return
    if status == "ready":
        facts.board.transition_task(tid, "ready")
    elif status == "running":
        facts.board.transition_task(tid, "running")
        facts.board.start_attempt(tid, payload.get("started_at", ""))
    elif status == "done":
        attempts = facts.board.attempts(tid)
        if attempts and attempts[-1]["outcome"] == "pending":
            facts.board.finish_attempt(tid, "succeeded")  # review_required 时过真实审查门
    elif status == "failed":
        attempts = facts.board.attempts(tid)
        if attempts and attempts[-1]["outcome"] == "pending":
            reason = payload.get("failure_reason") or payload.get("reason") or "(未记录原因)"
            facts.board.finish_attempt(tid, "failed", reason)
    elif status == "blocked":
        facts.board.block(tid, payload.get("reason") or "(未记录原因)",
                          payload.get("blocked_by", ()))
    elif status == "cancelled":
        facts.board.cancel(tid, payload.get("reason") or "(未记录原因)")
    facts.tasks[tid] = facts.board.task(tid)


def _replay_evidence(facts, entity_id, payload):
    role, detail = payload.get("role"), payload.get("fact")
    if role == "report" and isinstance(detail, dict):
        ref = detail.get("task_id"), detail.get("attempt_no")
        if isinstance(ref[0], str) and isinstance(ref[1], int) and not isinstance(ref[1], bool):
            facts.reports["%s#%d" % ref] = detail
            attempts = facts.board.attempts(ref[0])
            if attempts and attempts[-1]["outcome"] == "pending" \
                    and attempts[-1]["attempt_no"] == ref[1]:
                # 回报即实现者以固定版本提交审查（与 team-report 动作同一语义）
                facts.review_board.submit_for_review(attempts[-1], detail["artifact_sha256"])
    elif role == "review_record":
        try:
            facts.review_board.record(ReviewRecord.coerce(detail).to_dict())
        except CompanionError:
            raise CompanionError("team 事实库中的审查记录损坏（事件 %s）：%s" % (entity_id, detail))
    elif role == "integration_candidate" and isinstance(detail, dict):
        facts.integration.add_candidate(entity_id[len("integration-candidate:"):],
                                        detail.get("tasks", {}))
    elif role == "integration_version" and isinstance(detail, dict):
        facts.integration.build_integration_version(detail.get("candidates", []))
        facts.integration_versions.append({"integration_version": detail.get("integration_version"),
                                           "status": "candidate",
                                           "version_level_passed": None})
        facts.version_bindings[detail.get("integration_version")] = {
            "manifest_sha256": detail.get("manifest_sha256"),
            "candidates": list(detail.get("candidates", [])),
            "regressed_on": None}
    elif role == "integration_version_level" and isinstance(detail, dict):
        for manifest in reversed(facts.integration_versions):
            if manifest["integration_version"] == detail.get("integration_version"):
                manifest["version_level_passed"] = detail.get("exit_code") == 0
                break
        binding = facts.version_bindings.get(detail.get("integration_version"))
        if binding is not None and binding["regressed_on"] is None:
            binding["regressed_on"] = detail.get("regressed_on")
    elif role == "integration_completed":
        for manifest in reversed(facts.integration_versions):
            if manifest["integration_version"] == (detail or {}).get("integration_version"):
                manifest["status"] = "completed"
                break
        binding = facts.version_bindings.get((detail or {}).get("integration_version"))
        if binding is not None:
            binding["manifest_sha256"] = detail.get("manifest_sha256")
            if detail.get("regressed_on"):
                binding["regressed_on"] = detail.get("regressed_on")


def _require_open_attempt(facts, tid, action):
    attempts = facts.board.attempts(tid)
    if not attempts or attempts[-1]["outcome"] != "pending":
        raise CompanionError("任务 %s 没有 %s 所需的进行中 attempt"
                             "（先 team-task --status running）" % (tid, action))
    return attempts[-1]


def _action_ready(facts, store, path, tid, fid, expect_seq):
    # 白名单在此拒绝 done→ready 等非法转换（复核探针 done_to_ready 的负向断言）
    facts.board.transition_task(tid, "ready")
    return _append_status(store, path, tid, fid, "ready", {}, expect_seq,
                          key_suffix=":ready")


def _action_running(facts, store, path, tid, fid, expect_seq):
    facts.board.transition_task(tid, "running")
    attempt_no = len(facts.board.attempts(tid)) + 1
    started_at = db.utcnow()
    facts.board.start_attempt(tid, started_at)  # running 必须建 attempt（TK08 计数）
    return _append_status(store, path, tid, fid, "running",
                          {"attempt_no": attempt_no, "started_at": started_at},
                          expect_seq, key_suffix=":running")


def _action_done(facts, store, path, tid, fid, expect_seq, root):
    missing = []
    attempts = facts.board.attempts(tid)
    open_attempt = attempts and attempts[-1]["outcome"] == "pending"
    attempt_no = attempts[-1]["attempt_no"] if attempts else None
    report = facts.reports.get("%s#%s" % (tid, attempt_no)) if open_attempt else None
    if not open_attempt:
        missing.append("没有未完结 attempt（先 team-task --status running 开始执行）")
    elif report is None:
        missing.append("attempt #%s 缺少回报（team-report --outcome succeeded ...）" % attempt_no)
    elif report.get("outcome") != "succeeded":
        missing.append("attempt #%s 回报 outcome=%s，不能 done" % (attempt_no, report.get("outcome")))
    verified_files = None
    if report is not None and report.get("outcome") == "succeeded":
        # FIX04-followup (e)：done 绑定实际产物——先核内容漂移与范围，漂移即拒绝
        #（ReviewBoard 的 sha 绑定使重报后的旧批准自动失效，重走 report→approve 链）。
        verified_files = _verify_reported_files(root, tid, report, action="done 门")
        _verify_report_scope(tid, facts.tasks[tid]["allowed_paths"], report)
    approval = None
    if not missing:
        feature = facts.features[fid]
        if feature["review_required"]:
            verdict = facts.review_board.current_approval(tid)
            if verdict["status"] != "approved":
                missing.append("功能 %s 要求独立审查（G-REVIEW）：%s"
                               % (fid, facts._verdict_reason(verdict)))
            else:
                approval = {"reviewer": verdict["reviewer"],
                            "subject_sha256": verdict["subject_sha256"],
                            "reviewed_at": verdict["reviewed_at"]}
    if missing:
        raise CompanionError("任务 %s 不能 done，缺：%s" % (tid, "；".join(missing)))
    done_key = "team-task:%s:done@%s" % (tid, "seq" if expect_seq is None else expect_seq)
    done_fact = {"attempt_no": attempt_no,
                 "artifact_sha256": report["artifact_sha256"],
                 "approval": approval}
    if isinstance(verified_files, dict):
        done_fact["file_shas"] = verified_files  # done 时复核通过的报告时 sha 集合
    specs = [
        (views.EVENT_TASK_STATUS, tid,
         {"feature_id": fid, "status": "done", "attempt_no": attempt_no},
         done_key),
        (views.EVENT_EVIDENCE_REGISTERED, "task:" + tid,
         {"kind": "acceptance", "role": "task_done", "subject_id": tid,
          "result": "done", "source": "team-gate",
          "detail": "任务 %s 完成（attempt #%s，artifact=%s）"
                    % (tid, attempt_no, report["artifact_sha256"]),
          "fact": done_fact},
         done_key + ":evidence")]

    def verify(conn):
        row = conn.execute("SELECT result FROM evidence_view WHERE evidence_id = ?",
                           ("task:" + tid,)).fetchone()
        if row is None:
            raise CompanionError("完成证据未随同事务折叠（evidence_view 缺行）；整体回滚")
        task_row = conn.execute("SELECT status FROM task_view WHERE task_id = ?",
                                (tid,)).fetchone()
        if task_row is None or task_row["status"] != "done":
            raise CompanionError("done 事件未随同事务折叠进任务视图；整体回滚")

    writer = db.acquire_writer(store)
    try:
        results = _append_batch(store, writer.epoch, specs, expect_seq=expect_seq,
                                verify=verify)
    finally:
        writer.close()
    return {"store": str(path), "task_id": tid, "feature_id": fid, "status": "done",
            "attempt": "%s#%s" % (tid, attempt_no),
            "artifact_sha256": report["artifact_sha256"],
            "seq": results[-1]["seq"], "applied": all(r["applied"] for r in results),
            "deduped": any(r["deduped"] for r in results),
            "generation": events.view_head(store)}


def _action_failed(facts, store, path, tid, fid, expect_seq, reason):
    if not reason or not str(reason).strip():
        raise CompanionError("failed 必须 --reason 说明失败原因")
    attempt = _require_open_attempt(facts, tid, "failed")
    facts.board.finish_attempt(tid, "failed", str(reason))
    return _append_status(store, path, tid, fid, "failed",
                          {"attempt_no": attempt["attempt_no"], "failure_reason": str(reason)},
                          expect_seq, key_suffix=":failed")


def _action_blocked(facts, store, path, tid, fid, expect_seq, reason, blocked_by,
                    target, verb):
    if not reason or not str(reason).strip():
        raise CompanionError("进入 %s 必须给出原因（--reason）；无因阻断已被拒绝" % target)
    chain = strings([b.strip() for b in list(blocked_by) if b and b.strip()], "阻断链")
    if target == "blocked":
        facts.board.block(tid, str(reason), chain)      # 来源状态白名单（终态拒绝）
    else:
        facts.board.cancel(tid, str(reason))
    return _append_status(store, path, tid, fid, target,
                          {"reason": str(reason), "blocked_by": chain},
                          expect_seq, key_suffix=":" + target)


def _append_status(store, path, tid, fid, status, extra, expect_seq, *, key_suffix):
    payload = dict({"feature_id": fid, "status": status}, **extra)
    key = None if expect_seq is None else "team-task:%s%s@%d" % (tid, key_suffix, expect_seq)
    writer = db.acquire_writer(store)
    try:
        result = _append_batch(store, writer.epoch, [(
            views.EVENT_TASK_STATUS, tid, payload, key)], expect_seq=expect_seq)[0]
    finally:
        writer.close()
    return {"store": str(path), "task_id": tid, "feature_id": fid, "status": status,
            "seq": result["seq"], "applied": result["applied"],
            "deduped": result["deduped"], "generation": events.view_head(store)}


def _activity(facts):
    """门禁活动摘要（status/resume 聚合视图）：attempt/回报/批准/集成版本。"""
    tasks = {}
    for tid in sorted(facts.tasks):
        attempts = facts.board.attempts(tid)
        open_attempt = bool(attempts) and attempts[-1]["outcome"] == "pending"
        report = approval = None
        if attempts:
            report = facts.reports.get("%s#%d" % (tid, attempts[-1]["attempt_no"]))
            if report is not None:
                verdict = facts.review_board.current_approval(tid)
                approval = None if verdict["status"] == "none" else verdict["status"]
        tasks[tid] = {"kind": facts.tasks[tid]["kind"],
                      "attempt_count": len(attempts),
                      "open_attempt": open_attempt,
                      "reported": report is not None,
                      "approval": approval}
    return {"tasks": tasks, "integrations": list(facts.integration_versions)}


def read_status(project_dir, *, offset=0, limit=50):
    """team-status：物化视图分页只读（features/tasks 同一游标），含 generation 与事实源路径。

    FIX-01：经只读连接打开——状态查询不建库、不迁移 schema、不写 journal/WAL；
    损坏/未知 schema 结构化报错，而不是被打开路径顺手"修好"。
    FIX-04：附 activity 门禁活动摘要（attempt/回报/批准/集成版本）——聚合视图
    一次性给出"哪些任务可过 done 门、哪些缺什么"，正常入口路由（SR-05）复用。
    """
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise CompanionError("offset 必须是非负整数")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_LIMIT:
        raise CompanionError("limit 必须在 1..%d" % _MAX_LIMIT)
    path = team_db_path(project_dir)
    store = _open_readonly(path)
    try:
        facts = _replay(store)
        return {"store": str(path), "generation": events.view_head(store),
                "offset": offset, "limit": limit,
                "features": views.list_features(store, offset=offset, limit=limit),
                "tasks": views.list_tasks(store, offset=offset, limit=limit),
                "activity": _activity(facts)}
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
    """回退前导出全量事实（FIX-03）：旧 schema 草稿 + 不可映射事实 sidecar + 导出
    manifest（migration.fallback_export，含迁移后新增事实的全量折叠），另附
    team_events.json 全量事件日志（append-only 事实的完整保真记录）。逐项校验：
    草稿/sidecar/manifest 事件计数与库内总数一致、manifest 读回核对 head，任何
    短写即拒绝回退，team.db 保留。"""
    out_dir = Path(out_dir)
    if inside(realpath(out_dir), realpath(db_path.parent)):
        raise CompanionError("导出目录不得在项目记录目录内（避免覆盖 legacy 原件）：" + str(out_dir))
    exported = {"out_dir": str(out_dir), "drafts": {}, "events_log": str(out_dir / "team_events.json")}
    if manifest is not None:
        report = migration.fallback_export(project_dir, out_dir, db_path=db_path)
        exported["drafts"] = report["files"]
        exported["sidecar"] = report.get("sidecar")
        exported["warnings"] = report["warnings"]
        exported["counts"] = report["counts"]
        # manifest 完整性核对：读回落盘的 export_manifest.json，计数与 head 必须与库内一致
        written_manifest = json.loads(
            Path(report["manifest"]).read_text(encoding="utf-8"))
        if (written_manifest.get("counts", {}).get("events_total") != total or
                written_manifest.get("head_seq") != events.view_head(store)):
            raise CompanionError(
                "导出 manifest 校验失败：manifest 记录与库内事实不一致；回退中止，team.db 保留")
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
    # FIX-01：核对阶段只读打开——回退删除前不得对库做任何初始化/迁移写入
    #（含"顺手建表"：若这其实是一个外部/无关库，删库前必须先验证它是有效 team 库）。
    store = _open_readonly(path)
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

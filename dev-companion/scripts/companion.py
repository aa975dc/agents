#!/usr/bin/env python3
"""ZCode-facing CLI. Commands print JSON except the explicitly formatted board."""
import argparse
import json
import os
import shutil
import sys
import tempfile

import kernel_bootstrap  # noqa: F401 — P2-05 单处引导：优先本目录 _kernel_vendor，回退仓库 packages/

from core import CompanionError, Project, read_json, render_html, render_markdown

from agents_kernel.paths import absolute, inside, realpath


def parser():
    root = argparse.ArgumentParser(description="开发陪伴：产品规划、实现联调、可信验收与发布")
    root.add_argument("--project", default=".", help="项目目录")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("recover-lock").add_argument("--authorized", action="store_true")
    from journey import STAGES
    child = sub.add_parser("plan")
    child.add_argument("--stage", choices=STAGES, required=True)
    child.add_argument("--input", required=True)
    child.add_argument("--revision", required=True, type=int)
    child.add_argument("--complete", action="store_true")
    child.add_argument("--user-confirmed", action="store_true")
    sub.add_parser("planning-status")
    sub.add_parser("release-status")
    child = sub.add_parser("release-prepare")
    child.add_argument("--input", required=True)
    child.add_argument("--revision", required=True, type=int)
    child = sub.add_parser("release-run")
    child.add_argument("--action", choices=("deploy", "verify", "rollback"), required=True)
    child.add_argument("--revision", required=True, type=int)
    child.add_argument("--authorized", action="store_true", help="已明确授权本版本、目标环境及命令后使用")
    child = sub.add_parser("release-reconcile")
    child.add_argument("--revision", required=True, type=int)
    child.add_argument("--note", required=True)
    child.add_argument("--authorized", action="store_true", help="已核对中断现场且相关进程停止后使用")
    for name in ("init", "scope", "receipt"):
        child = sub.add_parser(name)
        child.add_argument("--input", required=True)
        if name == "scope":
            child.add_argument("--revision", required=True, type=int)
    sub.add_parser("confirm").add_argument("--revision", required=True, type=int)
    child = sub.add_parser("status")
    child.add_argument("--format", choices=("markdown", "json", "html"), default="markdown")
    child.add_argument("--out")
    for name in ("packet", "check", "accept", "block"):
        child = sub.add_parser(name)
        child.add_argument("--feature", required=True)
        if name == "check":
            child.add_argument("--kind", choices=("feature", "integration"), default="feature")
        if name == "accept":
            child.add_argument("--note", required=True)
            child.add_argument("--user-confirmed", action="store_true")
        if name == "block":
            child.add_argument("--reason", required=True)
    child = sub.add_parser("feedback")
    child.add_argument("--feature", required=True)
    child.add_argument("--kind", choices=("defect", "experience", "requirement", "environment"), required=True)
    child.add_argument("--note", required=True)
    child = sub.add_parser("save")
    child.add_argument("--paths", nargs="+", required=True)
    child.add_argument("--summary", required=True)
    sub.add_parser("history")
    for name in ("preview-restore", "restore"):
        child = sub.add_parser(name)
        child.add_argument("--archive", required=True)
        if name == "restore":
            child.add_argument("--token", required=True)
    # R02：team 模式事实库入口（SQLite 事件库）；legacy 命令完全不感知 team.db。
    child = sub.add_parser("team-init")
    child.add_argument("--feature", required=True)
    child.add_argument("--review-required", action="store_true", dest="review_required",
                       help="该功能 impl/review 任务 done 前必须过独立审查门（G-REVIEW）")
    child.add_argument("--allowed-paths", dest="allowed_paths", default="",
                       help="逗号分隔的功能级可修改文件清单（可选）")
    child.add_argument("title")
    child = sub.add_parser("team-status")
    child.add_argument("--offset", type=int, default=0)
    child.add_argument("--limit", type=int, default=50)
    child = sub.add_parser("team-task")
    child.add_argument("--set", required=True, dest="task_id")
    child.add_argument("--feature", required=True)
    child.add_argument("--status", required=True)
    child.add_argument("--reason", default=None,
                       help="blocked/cancelled/failed 必须给出原因（Z24）")
    child.add_argument("--blocked-by", dest="blocked_by", default="",
                       help="逗号分隔的阻断链（可选）")
    child.add_argument("--workspace", default=None,
                       help="done 门产物核验目录（缺省项目根；实现在独立 worktree 时传其路径）")
    child.add_argument("--expect-seq", type=int, dest="expect_seq")
    # FIX-04/SR-01：门禁动作——创建/回报/独立审查/集成各有前置，状态 upsert 不再通用。
    child = sub.add_parser("team-task-add")
    child.add_argument("--set", required=True, dest="task_id")
    child.add_argument("--feature", required=True)
    child.add_argument("--kind", default="impl",
                       help="任务类型：design/impl/review/integration/release（缺省 impl）")
    child.add_argument("--depends-on", dest="depends_on", default="",
                       help="逗号分隔的依赖任务编号")
    child.add_argument("--priority", type=int, default=0)
    child.add_argument("--allowed-paths", dest="allowed_paths", default="",
                       help="逗号分隔的文件级可修改清单（精确文件，不用通配）")
    child.add_argument("--expect-seq", type=int, dest="expect_seq")
    child = sub.add_parser("team-report")
    child.add_argument("--task", required=True)
    child.add_argument("--outcome", choices=("succeeded", "failed"), required=True)
    child.add_argument("--summary", required=True)
    child.add_argument("--changed-files", dest="changed_files", default="")
    child.add_argument("--artifact-sha256", dest="artifact_sha256", required=True,
                       help="attempt 产出的固定版本 sha256（审查与集成都绑它）")
    child.add_argument("--workspace", default=None,
                       help="改动文件采集目录（缺省项目根；实现在独立工作区时传该工作区路径）")
    child.add_argument("--expect-seq", type=int, dest="expect_seq")
    child = sub.add_parser("team-approve")
    child.add_argument("--task", required=True)
    child.add_argument("--reviewer", required=True,
                       help="独立审查者身份（role/id）；不得是实现 attempt 引用")
    child.add_argument("--verdict", choices=("approved", "changes_requested"), required=True)
    child.add_argument("--blockers", default="", help="逗号分隔阻塞项（changes_requested 必填）")
    child.add_argument("--review-id", dest="review_id", default="")
    child.add_argument("--expect-seq", type=int, dest="expect_seq")
    child = sub.add_parser("team-integrate")
    child.add_argument("--candidate", required=True)
    child.add_argument("--tasks", required=True, help="逗号分隔的集成任务编号（均须 done）")
    child.add_argument("--check-cmd", dest="check_cmd", required=True,
                       help="版本级回归命令（真实 subprocess，shlex 切分，无 shell）")
    child.add_argument("--cwd", default=None, help="回归运行目录（缺省项目根）")
    child.add_argument("--timeout", type=int, default=300)
    child.add_argument("--expect-seq", type=int, dest="expect_seq")
    child = sub.add_parser("team-migrate")
    child.add_argument("--from-json", action="store_true", dest="from_json",
                       help="从本项目旧三 JSON（state/journey/release）只读导入")
    child.add_argument("--dry-run", action="store_true")
    child = sub.add_parser("team-rollback")
    child.add_argument("--export-first", dest="export_first", metavar="DIR",
                       help="回退前先把全量事实导出到该目录（存在迁移后新写入时必填）")
    # FIX-04/SR-05：正常续接入口——team 模式返回持久事实 + 门禁续接清单，legacy 原样。
    sub.add_parser("resume")
    return root


def _split_list(value):
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _team_mode(project):
    """team 模式检测（FIX-04/SR-05）：项目只有 team.db、没有 legacy state.json。

    迁移项目（team.db 与三 JSON 并存）继续走 legacy 入口——legacy 命令对 team.db
    零感知的既有契约不变（tests NoDualMaster 逐字节锁定）；team-* 命令仍可用于
    其团队事实。只有 team.db 的新团队项目由正常入口直接服务，不再误报"请先 init"。
    """
    return (project.data / "team.db").is_file() and not project.state_path.exists()


def _render_team_markdown(view):
    """team 聚合视图的 Markdown 渲染（status --format markdown，progress 入口即此）。"""
    lines = ["# 团队状态（team 事实库）", "",
             "- 事实源：%s" % view["store"],
             "- generation：%d（读取时点，写入可用 --expect-seq 做乐观并发）" % view["generation"],
             ""]
    lines += ["## 功能", "", "| 功能 | 状态 |", "|---|---|"]
    lines += ["| %s | %s |" % (f["feature_id"], f["status"])
              for f in view["features"]["items"]]
    lines += ["", "## 任务", "", "| 任务 | 类型 | 状态 | attempt | 已回报 | 审查 |", "|---|---|---|---|---|---|"]
    activity = view.get("activity", {}).get("tasks", {})
    lines += ["| %s | %s | %s | %s | %s | %s |" % (
        tid, activity.get(tid, {}).get("kind", "-"), t["status"],
        activity.get(tid, {}).get("attempt_count", 0),
        "是" if activity.get(tid, {}).get("reported") else "否",
        activity.get(tid, {}).get("approval") or "-")
        for t in view["tasks"]["items"]]
    integrations = view.get("activity", {}).get("integrations", [])
    if integrations:
        lines += ["", "## 集成版本", "", "| 版本 | 状态 | 版本级回归 |", "|---|---|---|"]
        lines += ["| %d | %s | %s |" % (m["integration_version"], m["status"],
                                        "通过" if m.get("version_level_passed") else "未通过/未记录")
                  for m in integrations]
    lines += ["", "以本命令的当前输出为事实源；禁止手写状态或用旧输出估算进度。"]
    return "\n".join(lines)


def _write_status_out(project, out_arg, output):
    """status --out 的既有落盘约束（从 legacy 分支原样提取，legacy 行为逐字节不变）。"""
    path = absolute(out_arg)
    resolved = realpath(path)
    internal = realpath(project.data)
    if path.is_symlink() or (inside(resolved, internal) and resolved != internal / "board.html"):
        raise CompanionError("不能输出到项目事实记录目录；内部仅允许 board.html")
    if path.exists() and resolved != internal / "board.html":
        raise CompanionError("输出文件已经存在；请使用一个新的文件名以保留之前的状态快照")
    fd, temporary = tempfile.mkstemp(prefix=".board-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(output)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def run(args):
    project = Project(args.project)
    name = args.command
    if name == "doctor":
        return {"python": sys.version.split()[0], "project": str(project.root),
                "state_exists": project.state_path.is_file(), "git_available": bool(shutil.which("git")),
                "planning_exists": (project.data / "journey.json").is_file(),
                "release_exists": (project.data / "release.json").is_file(),
                "archive_backend": "explicit-file-snapshots",
                "message": "本地运行环境可用；此结果不代表宿主已加载插件或模型调用已成功"}
    if name == "recover-lock":
        return project.recover_lock(args.authorized)
    if name in {"plan", "planning-status"}:
        from journey import Journey
        journey = Journey(project)
        if name == "planning-status":
            return {"record": journey.status()}
        return journey.save(args.stage, read_json(args.input), args.revision, args.complete, args.user_confirmed)
    if name.startswith("release-"):
        from releases import ReleaseStore
        store = ReleaseStore(project)
        if name == "release-status":
            return {"record": store.status()}
        if name == "release-prepare":
            return store.prepare(read_json(args.input), args.revision)
        if name == "release-reconcile":
            return store.reconcile(args.revision, args.note, args.authorized)
        return store.run(args.action, args.revision, args.authorized)
    if name == "init":
        return project.init(read_json(args.input))
    if name == "scope":
        return project.revise(read_json(args.input), args.revision)
    if name == "confirm":
        return project.confirm(args.revision)
    if name == "receipt":
        return project.receipt(read_json(args.input))
    if name == "packet":
        return project.packet(args.feature)
    if name == "check":
        if _team_mode(project):
            # FIX-04/SR-05：team 模式的检查入口走同一事实库的门禁审计（只读）；
            # 通过与否来自 attempt/回报/审查/集成证据的一致性，不来自任务包状态机。
            from agents_kernel.storage import team
            return team.gate_check(args.project, args.feature, kind=args.kind)
        return project.check(args.feature, args.kind)
    if name == "feedback":
        return project.feedback(args.feature, args.kind, args.note)
    if name == "accept":
        if _team_mode(project):
            # Step-8 验收路由：team-only 项目由正常验收入口走团队事实库的五前置门
            #（任务 done + succeeded 回报、completed 集成覆盖、产物未漂移、真实确认），
            # 产出 acceptance 证据与 feature accepted 事件；不再误入 legacy 的
            # "尚未建立需求记录"。legacy 分支（无 team.db 或迁移并存项目）原样不动。
            from agents_kernel.storage import team
            return team.team_accept(args.project, args.feature, args.note, args.user_confirmed)
        return project.accept(args.feature, args.note, args.user_confirmed)
    if name == "block":
        return project.block(args.feature, args.reason)
    if name == "status":
        if _team_mode(project):
            # FIX-04/SR-05：team-only 项目由正常状态入口返回团队聚合视图（复核探针
            # normal_chat_status_on_team_only_project 的负向断言：exit 0，含 generation
            # 与事实源路径）。progress 入口（无独立 CLI）即 status——聚合视图即进度。
            from agents_kernel.storage import team
            view = team.read_status(args.project, offset=0, limit=team._MAX_LIMIT)
            if args.format == "json":
                return view
            output = _render_team_markdown(view)
        else:
            view = project.status()
            output = (json.dumps(view, ensure_ascii=False, indent=2) if args.format == "json" else
                      render_html(view) if args.format == "html" else render_markdown(view))
        if args.out:
            path = _write_status_out(project, args.out, output)
            return {"output": str(path), "revision": view["revision"]}
        print(output)
        return None
    if name == "resume":
        # FIX-04/SR-05：续接入口的模式路由。team 模式：持久事实 + 门禁续接清单
        # （存在 ResumeLedger 时按 FIX-02 的接管语义打开，resume_plan 并列呈现）；
        # 无 team.db 的项目保持 legacy 事实视图。
        if _team_mode(project):
            from agents_kernel.storage import team
            result = team.resume_view(args.project)
            ledger_path = project.root / ".code-analysis-resume.json"
            if ledger_path.is_file():
                from agents_kernel.services.resume import ResumeLedger
                ledger = ResumeLedger.open(project.root, "companion-resume")
                result["ledger"] = {"path": str(ledger_path), "writer_epoch": ledger.epoch,
                                    "owner_id": ledger.owner_id,
                                    "activities": ledger.activities(),
                                    "resume_plan": ledger.resume_plan()}
            return result
        return {"mode": "legacy", "status": project.status()}
    if name.startswith("team-"):
        from agents_kernel.storage import team
        if name == "team-init":
            return team.init_feature(args.project, args.feature, args.title,
                                     review_required=args.review_required,
                                     allowed_paths=_split_list(args.allowed_paths))
        if name == "team-status":
            return team.read_status(args.project, offset=args.offset, limit=args.limit)
        if name == "team-task":
            return team.set_task_status(args.project, args.task_id, args.feature,
                                        args.status, expect_seq=args.expect_seq,
                                        reason=args.reason,
                                        blocked_by=_split_list(args.blocked_by),
                                        workspace=args.workspace)
        if name == "team-task-add":
            return team.add_task(args.project, args.task_id, args.feature, args.kind,
                                 depends_on=_split_list(args.depends_on),
                                 priority=args.priority,
                                 allowed_paths=_split_list(args.allowed_paths),
                                 expect_seq=args.expect_seq)
        if name == "team-report":
            return team.report(args.project, args.task, args.outcome, args.summary,
                               changed_files=_split_list(args.changed_files),
                               artifact_sha256=args.artifact_sha256,
                               expect_seq=args.expect_seq,
                               workspace=args.workspace)
        if name == "team-approve":
            return team.approve(args.project, args.task, args.reviewer, args.verdict,
                                blockers=_split_list(args.blockers),
                                review_id=args.review_id or None,
                                expect_seq=args.expect_seq)
        if name == "team-integrate":
            return team.integrate(args.project, args.candidate,
                                  _split_list(args.tasks), args.check_cmd,
                                  cwd=args.cwd, timeout=args.timeout,
                                  expect_seq=args.expect_seq)
        if name == "team-rollback":
            return team.rollback_store(args.project, export_first=args.export_first)
        if not args.from_json:
            raise CompanionError("team-migrate 需要 --from-json 指定从旧三 JSON 导入")
        return team.migrate_from_json(args.project, dry_run=args.dry_run)
    from archives import ArchiveStore
    store = ArchiveStore(project.root)
    if name == "history":
        return store.history()
    with project.locked():
        if project.state_path.exists():
            project.require_idle(project.load())
        if name == "preview-restore":
            return store.preview_restore(args.archive)
        if name == "save":
            return store.save(args.paths, args.summary)
        return store.restore(args.archive, args.token)


def _utf8_stdio():
    """Z22：非 UTF-8 终端（如 Windows GBK 代码页）下打印中文/特殊字符不再触发
    UnicodeEncodeError 二次 traceback；输出统一为 UTF-8，无法编码的字符以替换符兜底，
    保证错误路径始终输出可解析的单行 JSON。py<3.7 或流不可重配时静默保持现状。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv=None):
    _utf8_stdio()
    args = parser().parse_args(argv)
    try:
        result = run(args)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if isinstance(result, dict) and result.get("passed") is False:
                return 3
        return 0
    except (CompanionError, ValueError, OSError) as exc:
        result = {"success": False, "error": str(exc), "state": "unknown_or_unchanged"}
        if getattr(exc, "safety_archive_id", None):
            result["safety_archive_id"] = exc.safety_archive_id
            result["state"] = "restore_incomplete"
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

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
    child.add_argument("title")
    child = sub.add_parser("team-status")
    child.add_argument("--offset", type=int, default=0)
    child.add_argument("--limit", type=int, default=50)
    child = sub.add_parser("team-task")
    child.add_argument("--set", required=True, dest="task_id")
    child.add_argument("--feature", required=True)
    child.add_argument("--status", required=True)
    child.add_argument("--expect-seq", type=int, dest="expect_seq")
    child = sub.add_parser("team-migrate")
    child.add_argument("--from-json", action="store_true", dest="from_json",
                       help="从本项目旧三 JSON（state/journey/release）只读导入")
    child.add_argument("--dry-run", action="store_true")
    child = sub.add_parser("team-rollback")
    child.add_argument("--export-first", dest="export_first", metavar="DIR",
                       help="回退前先把全量事实导出到该目录（存在迁移后新写入时必填）")
    return root


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
        return project.check(args.feature, args.kind)
    if name == "feedback":
        return project.feedback(args.feature, args.kind, args.note)
    if name == "accept":
        return project.accept(args.feature, args.note, args.user_confirmed)
    if name == "block":
        return project.block(args.feature, args.reason)
    if name == "status":
        view = project.status()
        output = (json.dumps(view, ensure_ascii=False, indent=2) if args.format == "json" else
                  render_html(view) if args.format == "html" else render_markdown(view))
        if args.out:
            path = absolute(args.out)
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
            return {"output": str(path), "revision": view["revision"]}
        print(output)
        return None
    if name.startswith("team-"):
        from agents_kernel.storage import team
        if name == "team-init":
            return team.init_feature(args.project, args.feature, args.title)
        if name == "team-status":
            return team.read_status(args.project, offset=args.offset, limit=args.limit)
        if name == "team-task":
            return team.set_task_status(args.project, args.task_id, args.feature,
                                        args.status, expect_seq=args.expect_seq)
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

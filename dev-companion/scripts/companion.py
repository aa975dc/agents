#!/usr/bin/env python3
"""ZCode-facing CLI. Commands print JSON except the explicitly formatted board."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from core import CompanionError, Project, read_json, render_html, render_markdown


def parser():
    root = argparse.ArgumentParser(description="开发陪伴：需求、可信进度和明确范围的本地存档")
    root.add_argument("--project", default=".", help="项目目录")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
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
        if name == "accept":
            child.add_argument("--note", required=True)
            child.add_argument("--user-confirmed", action="store_true")
        if name == "block":
            child.add_argument("--reason", required=True)
    child = sub.add_parser("save")
    child.add_argument("--paths", nargs="+", required=True)
    child.add_argument("--summary", required=True)
    sub.add_parser("history")
    for name in ("preview-restore", "restore"):
        child = sub.add_parser(name)
        child.add_argument("--archive", required=True)
        if name == "restore":
            child.add_argument("--token", required=True)
    return root


def run(args):
    project = Project(args.project)
    name = args.command
    if name == "doctor":
        return {"python": sys.version.split()[0], "project": str(project.root),
                "state_exists": project.state_path.is_file(), "git_available": bool(shutil.which("git")),
                "archive_backend": "explicit-file-snapshots",
                "message": "本地运行环境可用；此结果不代表宿主已加载插件或模型调用已成功"}
    if name == "init":
        return project.init(read_json(args.input))
    if name == "scope":
        return project.revise(read_json(args.input), args.revision)
    if name == "confirm":
        return project.confirm(args.revision)
    if name == "receipt":
        return project.receipt(read_json(args.input))
    if name in {"packet", "check"}:
        return getattr(project, name)(args.feature)
    if name == "accept":
        return project.accept(args.feature, args.note, args.user_confirmed)
    if name == "block":
        return project.block(args.feature, args.reason)
    if name == "status":
        view = project.status()
        output = (json.dumps(view, ensure_ascii=False, indent=2) if args.format == "json" else
                  render_html(view) if args.format == "html" else render_markdown(view))
        if args.out:
            path = Path(args.out).absolute()
            if path.is_symlink() or path.resolve() == project.state_path.resolve():
                raise CompanionError("不能覆盖项目状态或通过文件链接输出")
            if path.exists() and path != project.data / "board.html":
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


def main(argv=None):
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

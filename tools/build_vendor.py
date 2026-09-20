#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建插件私有 vendor 副本（P2-05：共享内核 × 独立安装）。

单一源码 packages/agents_kernel/ 复制到各依赖插件的 <plugin>/scripts/_kernel_vendor/agents_kernel，
使插件离开仓库布局（安装产物只有插件目录）仍可 import 共享内核。这是分发复制，
不是业务策略分叉：副本禁止手改，一律由本脚本再生成（02_TARGET_ARCHITECTURE.md §3）。

行为：
  1. 版本门禁：marketplace.json 与两个 plugin.json 一致性检查（对齐 .github/workflows/ci.yml
     的 packaging 作业）；不一致 exit 1 并逐条列出。
  2. 依赖判定：扫描插件 .py 是否引用 agents_kernel；无依赖则跳过其 vendor 并说明。
     （SR-06：code-analysis-swarm/scripts/precheck.py 的 index/g2/resume/coverage 子命令
     经 _ensure_kernel 引导 agents_kernel，文本含内核名即被本判定命中，自动生成
     swarm 的 vendor——既有规则补触发，无需额外开关。）
  3. 复制：排除 __pycache__/ 与 *.pyc；目标与源不符的文件覆盖并报告 diff 清单，
     源里已删除的目标残留文件清理并报告。
  4. 校验：源与副本逐字节（sha256+字节数）核对，不一致即失败。
  5. 清单：vendor/MANIFEST.json 记录源 commit、文件清单+sha256、生成时间、目标插件。

幂等：对同一源重复运行，内核文件字节不变（仅 generated_at 随运行刷新）。
纯标准库，Python 3.9+。退出码：0 成功；1 版本门禁拒绝；2 环境/校验错误。
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

KERNEL = "agents_kernel"
SOURCE_REL = "packages/" + KERNEL
VENDOR_DIRNAME = "_kernel_vendor"
MANIFEST_NAME = "MANIFEST.json"
PLUGIN_DIRS = ("dev-companion", "code-analysis-swarm")
EXCLUDED_DIRS = {"__pycache__"}
EXCLUDED_SUFFIXES = (".pyc",)


class BuildError(Exception):
    pass


def sha256_file(path):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError("无法读取有效的 JSON：%s（%s）" % (path, exc)) from exc


def check_versions(root):
    """marketplace.json vs 两 plugin.json；与 CI packaging 作业同一套判定。"""
    entries = {p.get("name"): p for p in load_json(root / "marketplace.json")["plugins"]}
    failures = []
    for plugin_dir in PLUGIN_DIRS:
        pj_path = root / plugin_dir / ".zcode-plugin" / "plugin.json"
        if not pj_path.is_file():
            failures.append("%s: 缺少 .zcode-plugin/plugin.json" % plugin_dir)
            continue
        pj = load_json(pj_path)
        entry = entries.get(pj.get("name"))
        if entry is None:
            failures.append("%s: marketplace 缺少 %s" % (plugin_dir, pj.get("name")))
            continue
        if entry.get("version") != pj.get("version"):
            failures.append("%s: 版本不一致 marketplace=%s plugin.json=%s"
                            % (plugin_dir, entry.get("version"), pj.get("version")))
        source = root / entry["source"].lstrip("./")
        if not (source / ".zcode-plugin" / "plugin.json").is_file():
            failures.append("%s: source 路径无效 %s" % (plugin_dir, entry["source"]))
    return failures


def uses_kernel(plugin_dir):
    """插件 .py 是否引用 agents_kernel（vendor 副本自身除外）。"""
    for py in sorted(plugin_dir.rglob("*.py")):
        if VENDOR_DIRNAME in py.relative_to(plugin_dir).parts:
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if KERNEL in text:
            return True
    return False


def git_commit(root):
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                                capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def build_vendor(root, plugin_dir, commit):
    """同步一份内核副本并写 MANIFEST；返回 (变更文件, 清理文件, 文件数)。"""
    source = root / SOURCE_REL
    vendor_dir = root / plugin_dir / "scripts" / VENDOR_DIRNAME
    dest_root = vendor_dir / KERNEL

    entries = []
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if not path.is_file() or EXCLUDED_DIRS.intersection(rel.parts) or path.suffix in EXCLUDED_SUFFIXES:
            continue
        digest, size = sha256_file(path)
        entries.append({"path": KERNEL + "/" + rel.as_posix(), "sha256": digest, "bytes": size})

    changed, removed = [], []
    if dest_root.is_dir():
        for path in sorted(dest_root.rglob("*"), reverse=True):
            rel = path.relative_to(dest_root).as_posix()
            known = any(entry["path"] == KERNEL + "/" + rel for entry in entries)
            if path.is_dir():
                if not any(path.iterdir()):
                    path.rmdir()
                continue
            if not known:
                path.unlink()
                removed.append(KERNEL + "/" + rel)
    for entry in entries:
        dest = dest_root.parent / entry["path"]
        if dest.is_file() and sha256_file(dest)[0] == entry["sha256"]:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source.parent / entry["path"], dest)
        changed.append(entry["path"])

    # 逐字节校验：副本必须与源一致，否则构建失败。
    for entry in entries:
        digest, size = sha256_file(dest_root.parent / entry["path"])
        if digest != entry["sha256"] or size != entry["bytes"]:
            raise BuildError("校验失败：%s %s 与源不一致" % (plugin_dir, entry["path"]))

    manifest = {
        "manifest_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"path": SOURCE_REL, "commit": commit},
        "target": {"plugin": plugin_dir,
                   "vendor_dir": (plugin_dir + "/scripts/" + VENDOR_DIRNAME)},
        "files": entries,
    }
    vendor_dir.mkdir(parents=True, exist_ok=True)
    (vendor_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed, removed, len(entries)


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成插件私有 _kernel_vendor 副本（P2-05）")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                        help="仓库根（默认取本脚本上两级）")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        failures = check_versions(root)
        if failures:
            print("版本/清单不一致，拒绝构建：", file=sys.stderr)
            for line in failures:
                print("  " + line, file=sys.stderr)
            return 1
        if not (root / SOURCE_REL).is_dir():
            print("缺少源目录：%s" % (root / SOURCE_REL), file=sys.stderr)
            return 2
        commit = git_commit(root)
        for plugin_dir in PLUGIN_DIRS:
            if not (root / plugin_dir).is_dir():
                print("跳过 %s：插件目录不存在" % plugin_dir)
                continue
            if not uses_kernel(root / plugin_dir):
                print("跳过 %s：未发现 agents_kernel 依赖，不生成 vendor" % plugin_dir)
                continue
            changed, removed, total = build_vendor(root, plugin_dir, commit)
            print("%s: vendor 就绪 %s/scripts/%s（%d 个文件，源 commit %s）"
                  % (plugin_dir, plugin_dir, VENDOR_DIRNAME, total, commit or "未知（非 git 仓库）"))
            if changed:
                print("  与源不一致，已覆盖：%s" % ", ".join(changed))
            if removed:
                print("  源已删除，已清理：%s" % ", ".join(removed))
            if not changed and not removed:
                print("  与源一致，无变更")
    except BuildError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

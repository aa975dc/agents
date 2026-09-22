#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单一命令注册表生成器（Z18/P7-02）。

同一套命令说明此前在根 README（中/英）、cli-contract.md、commands/*.md 多处重复
维护，一致性靠人肉。本工具从各插件 `.zcode-plugin/plugin.json` 显式声明的命令
目录自动收集聊天命令，生成 docs/command-registry.md（每命令一行：名称 / 插件 /
源文件 / 一句话说明 / 退出码引用），作为唯一注册表；命令说明的修改只改命令文件
frontmatter，再重新生成本表。

- 不改变任何命令入口：目录名沿用 manifest 显式声明（swarm 为 `command/` 单数、
  companion 为 `commands/` 复数，Z16 结论：显式配置合法，不是命名缺陷）。
- dev-companion 的 22 个 companion.py CLI 子命令与退出码约定只在
  dev-companion/references/cli-contract.md 单点维护，本表只引用不复制。
- 幂等：输出不含时间戳，重复生成字节一致。`--check` 比对生成结果与现文件，
  不一致（或文件缺失）exit 1，CI 可直接调用。

纯标准库，Python 3.9+。退出码：0 一致（或已写入）；1 --check 发现漂移；2 环境/参数错误。
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_REL = "docs/command-registry.md"
PLUGINS = ("code-analysis-swarm", "dev-companion")

# 退出码引用列：companion 命令最终走 scripts/companion.py（0/2/3，单点见 cli-contract）；
# swarm-analyze 是聊天入口，没有 CLI 退出码。
EXIT_REF = {
    "dev-companion": "`0`/`2`/`3`，见 [cli-contract.md](../dev-companion/references/cli-contract.md)",
    "code-analysis-swarm": "—（聊天入口，无 CLI 退出码）",
}


def frontmatter_description(text):
    """取命令 markdown 头部 frontmatter 的 description 一行；无则返回空串。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("description:"):
            return line[len("description:"):].strip()
    return ""


def collect_rows():
    """从各 plugin.json 声明的命令目录收集命令行；返回 (rows, problems)。"""
    rows, problems = [], []
    for plugin in PLUGINS:
        manifest_path = REPO_ROOT / plugin / ".zcode-plugin" / "plugin.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append("读取 %s 失败：%s" % (manifest_path, exc))
            continue
        commands_dir = manifest.get("commands")
        version = manifest.get("version", "?")
        if not commands_dir:
            problems.append("%s: plugin.json 未声明 commands 目录" % plugin)
            continue
        base = REPO_ROOT / plugin / commands_dir
        if not base.is_dir():
            problems.append("%s: 命令目录不存在 %s" % (plugin, base))
            continue
        for path in sorted(base.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                problems.append("读取 %s 失败：%s" % (path, exc))
                continue
            description = frontmatter_description(text)
            if not description:
                problems.append("%s: frontmatter 缺 description" % path)
            rows.append({
                "name": path.stem,
                "plugin": "%s %s" % (plugin, version),
                "source": "%s/%s" % (commands_dir, path.name),
                "description": description,
                "exit_ref": EXIT_REF.get(plugin, "—"),
                "commands_dir": commands_dir,
            })
    return rows, problems


def render(rows):
    """渲染注册表 markdown；无时间戳等易变内容，保证幂等。"""
    out = [
        "# 命令注册表 / Command Registry",
        "",
        "> 本文件由 `tools/command_registry.py` 从各插件 `.zcode-plugin/plugin.json` 显式声明的",
        "> 命令目录自动生成（Z18：单一注册表）。**勿手改**：修改命令 frontmatter 后运行",
        "> `python3 tools/command_registry.py` 重新生成；CI / 本地用 `--check` 校验一致性。",
        "",
        "## 命令目录名说明（Z16）",
        "",
        "`code-analysis-swarm` 用 `command/`（单数）、`dev-companion` 用 `commands/`（复数）：",
        "两者都是各自 `plugin.json` 中 `commands` 字段的**显式声明**，宿主按 manifest 解析，",
        "属合法自定义配置而非命名缺陷；如需统一须先测宿主兼容性，不在文档层擅自\"纠正\"。",
        "",
        "## 插件聊天命令（%d 个）" % len(rows),
        "",
        "| 命令 | 插件 | 源文件 | 说明 | 退出码 |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        out.append("| `/{name}` | {plugin} | `{source}` | {description} | {exit_ref} |".format(**row))
    out += [
        "",
        "## dev-companion CLI 子命令",
        "",
        "`scripts/companion.py` 的 22 个子命令全表与退出码约定（`0` 成功；`2` 参数 / 状态 /",
        "权限错误；`3` 实际检查或发布命令失败）只在 [cli-contract.md](../dev-companion/references/cli-contract.md)",
        "单点维护，本表不重复（Z18）。各聊天命令文档见 `dev-companion/commands/*.md`。",
        "",
    ]
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成 / 校验 docs/command-registry.md")
    parser.add_argument("--check", action="store_true",
                        help="不写入，只校验生成结果与现文件一致（一致 exit 0，漂移或缺失 exit 1）")
    parser.add_argument("--output", default=OUTPUT_REL,
                        help="输出路径（仓库相对，默认 %(default)s；测试用）")
    args = parser.parse_args(argv)

    rows, problems = collect_rows()
    if problems:
        for problem in problems:
            print("registry: %s" % problem, file=sys.stderr)
        return 2
    content = render(rows)
    target = Path(args.output)
    if not target.is_absolute():
        target = REPO_ROOT / target
    if args.check:
        try:
            current = target.read_text(encoding="utf-8")
        except OSError:
            print("registry --check: %s 不存在，请先运行 python3 tools/command_registry.py" % args.output)
            return 1
        if current != content:
            print("registry --check: %s 与生成结果不一致，请运行 python3 tools/command_registry.py" % args.output)
            return 1
        print("registry --check: OK（%d 条命令）" % len(rows))
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    print("registry: 已写入 %s（%d 条命令）" % (args.output, len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

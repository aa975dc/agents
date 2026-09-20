# 命令注册表 / Command Registry

> 本文件由 `tools/command_registry.py` 从各插件 `.zcode-plugin/plugin.json` 显式声明的
> 命令目录自动生成（Z18：单一注册表）。**勿手改**：修改命令 frontmatter 后运行
> `python3 tools/command_registry.py` 重新生成；CI / 本地用 `--check` 校验一致性。

## 命令目录名说明（Z16）

`code-analysis-swarm` 用 `command/`（单数）、`dev-companion` 用 `commands/`（复数）：
两者都是各自 `plugin.json` 中 `commands` 字段的**显式声明**，宿主按 manifest 解析，
属合法自定义配置而非命名缺陷；如需统一须先测宿主兼容性，不在文档层擅自"纠正"。

## 插件聊天命令（8 个）

| 命令 | 插件 | 源文件 | 说明 | 退出码 |
|---|---|---|---|---|
| `/swarm-analyze` | code-analysis-swarm 0.2.1 | `command/swarm-analyze.md` | 只读分析目标软件的结构、模块、架构、依赖与构建，交付带证据和覆盖声明的报告 | —（聊天入口，无 CLI 退出码） |
| `/companion-archive` | dev-companion 0.2.0 | `commands/companion-archive.md` | 保存选定项目文件、查看历史，或预览并确认恢复 | `0`/`2`/`3`，见 [cli-contract.md](../dev-companion/references/cli-contract.md) |
| `/companion-check` | dev-companion 0.2.0 | `commands/companion-check.md` | 独立检查功能与真实联调，处理试用反馈并重新验收 | `0`/`2`/`3`，见 [cli-contract.md](../dev-companion/references/cli-contract.md) |
| `/companion-progress` | dev-companion 0.2.0 | `commands/companion-progress.md` | 分别查看当前阶段、真实功能验收、发布状态与下一步 | `0`/`2`/`3`，见 [cli-contract.md](../dev-companion/references/cli-contract.md) |
| `/companion-release` | dev-companion 0.2.0 | `commands/companion-release.md` | 准备具体发布计划，按授权部署、验证或回退并记录真实结果 | `0`/`2`/`3`，见 [cli-contract.md](../dev-companion/references/cli-contract.md) |
| `/companion-resume` | dev-companion 0.2.0 | `commands/companion-resume.md` | 读取规划决定、功能检查、发布记录和未决事项，从当前阶段继续 | `0`/`2`/`3`，见 [cli-contract.md](../dev-companion/references/cli-contract.md) |
| `/companion-start` | dev-companion 0.2.0 | `commands/companion-start.md` | 从一句想法讲清概念，经问答、产品流程和原型形成可执行方案 | `0`/`2`/`3`，见 [cli-contract.md](../dev-companion/references/cli-contract.md) |
| `/companion-work` | dev-companion 0.2.0 | `commands/companion-work.md` | 按当前方案串行实现功能，连接真实接口并安排独立检查 | `0`/`2`/`3`，见 [cli-contract.md](../dev-companion/references/cli-contract.md) |

## dev-companion CLI 子命令

`scripts/companion.py` 的 22 个子命令全表与退出码约定（`0` 成功；`2` 参数 / 状态 /
权限错误；`3` 实际检查或发布命令失败）只在 [cli-contract.md](../dev-companion/references/cli-contract.md)
单点维护，本表不重复（Z18）。各聊天命令文档见 `dev-companion/commands/*.md`。

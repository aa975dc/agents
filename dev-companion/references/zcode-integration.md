# ZCode 接入约定

依据 2026-09-20 阅读的 [Plugin 文档](https://zcode.z.ai/cn/docs/plugin)、[子智能体文档](https://zcode.z.ai/cn/docs/subagents)及 [Command 文档](https://zcode.z.ai/cn/docs/commands)。目标机器仍需要实际安装验证。

- 插件清单位于 `.zcode-plugin/plugin.json`，声明 `commands`、`skills` 和 `agents` 三个目录。
- 命令通过 `$ARGUMENTS` 接收用户文字。用户输入是数据，不能直接拼进 shell 命令执行。
- 主会话调度开发者和独立检查者。子智能体不再派发子智能体，模型默认继承用户当前配置。
- 显式命令配合聊天中的 Markdown 信息卡即可使用。没有 Hook、后台监控、原生侧边栏或必装 MCP；静态 HTML 看板需要再次生成才更新。
- 聊天入口统一以 `companion-` 开头；`resume` 是 ZCode 内置命令，不能用它注册插件续接入口。用户自己的同名命令优先级高于插件，因此代码分析入口使用 `swarm-analyze`，保留用户原有 `my-team` 不变。

## 路径与运行方式

先从宿主提供的插件/技能文件位置解析插件根目录：`SKILL.md` 位于 `<插件根>/skills/dev-companion/SKILL.md`，命令位于 `<插件根>/commands/`。验证根目录中存在 `.zcode-plugin/plugin.json` 且 `name` 为 `dev-companion`、`scripts/companion.py` 可读。

不要把当前项目目录当作插件目录，不写死作者机器路径。若宿主没有提供位置，先查插件安装信息；仍不能确定时说明缺少插件路径。文档中的 `<插件根>`、`<项目>` 是待解析的参数，不是要原样执行的字符串。

调用格式：

```text
python3 "<插件根>/scripts/companion.py" --project "<项目绝对路径>" <子命令>
```

Windows 可使用已经核实版本的 `py -3` 或 `python`，需要 Python 3.9 及以上。优先使用工具的 argv 参数；仅有 shell 时，路径必须按当前 shell 正确转义。结构数据写 JSON 文件后传入 `--input`；不要把用户原话展开为命令片段。

第一次先运行 `doctor`。Python 不可用或宿主禁止执行时，仍可整理需求草案，但明确说明“执行与真实进度记录尚不可用”。不更换工具绕过权限限制，不把当前模式叫作已接入。

## 与代码分析智能团协作

`code-analysis-swarm` 是同仓库的独立只读分析插件，可单独安装。它提供现状、结构与风险证据；报告不能自动使开发功能变成已验收。

- 新建小项目：按需要做简短读取，不启动完整分析团。
- 已有项目中的小改动：限定目录或功能做针对性分析。
- 陌生、较大的现有项目：先使用已安装分析团的 `swarm-analyze` 入口，要求报告当前版本、覆盖边界和证据。
- 分析团未安装：使用宿主已有读取/Explore 能力做限定分析，明确覆盖范围。Dev Companion 可独立使用。

分工固定为：分析团负责看清现状；`companion-developer` 按确认范围实现；`companion-checker` 独立检查；主会话维护唯一状态入口并向新手解释。角色不可用但 `Agent` 工具可用时，读取角色文件全文，交给 `general-purpose` 执行同一职责。独立 Agent 工具不可用时，不宣称独立验收已完成。

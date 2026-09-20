# 真实宿主契约探针记录（P0-02，2026-09-20）

执行环境：ZCode 桌面宿主（本会话），macOS 26.2 arm64，Python 3.9.6，Node v24.19.0，git 2.50.1。
探针入口：宿主 CreateWorkflow 编译/运行入口与 EvalWorkflowSnippet（均为真实宿主能力，非 Node mock）。
规则：编译失败的工作流**不会执行**（编译门在用户确认之前），因此反例探针零副作用。

## H01 编译阶段名（正反）

- 反例：`const stageName = "参数检查"; phase(stageName);` → **编译拒绝**，诊断原文：
  `phase()'s argument must be a compile-time string literal ("gate" or a no-substitution template): the script's phase names are fixed when it is submitted, because they label the graph the user confirms before anything runs.`
  脚本未执行。草稿：.zcode/workflow-drafts/H01反例探针-变量阶段名.dwf.ts
- 正例：字面量 `phase("宿主能力探针")` + 字面量 world.run → 编译通过并真实运行（见下）。
- 结论：**Z01 前提成立**——六处 `phase(stage)` 的 dwf.ts 在真实宿主无法编译。Z01 证据等级：host_compile_rejected_confirmed。

## H02 动态流程语法 / 真实命令执行（正反）

- 反例：`const cmd = "git"; world.run(cmd, ["--version"])` → **编译拒绝**（同字面量规则，针对 world.run 首参）。
- 正例：`world.run("git", ["--version"])` → exit 0，stdout `git version 2.50.1 (Apple Git-155)`；python3 -c 固定 argv 多次执行 exit 0。
- 结论：world.run 首参必须字面量；运行期值只能进 args 数组。

## H04 产物发布边界（三态，真实运行）

- workspace 外绝对路径 `/tmp/zcode-host-probe/outside.txt` → **发布拒绝**，错误原文：
  `artifact.file: path '/tmp/zcode-host-probe/outside.txt' resolves outside the workspace (symlinks are judged by their real path). Only files inside the workspace can be published.`
- 符号链接逃逸：workspace 内 `host-probes/escape.link` → 指向 /tmp 文件 → **同样拒绝**（按真实路径判定）。
- workspace 相对路径 `host-probes/probe-evidence.txt` → **发布成功**（artifact id probe-inside v1，81 字节，已出证据卡）。
- 结论：Z02 方向确认——宿主 workspace 边界真实存在且按 realpath 判定。当前 dwf.ts 把报告放在目标仓库外并 artifact.file 绝对路径发布**必然失败**。修复方向：报告正文走 `artifact.markdown`（无路径限制）+ run_root 内落盘；仅当路径在 workspace 内才 artifact.file。

## H05 路径创建 / 排他 mkdir（真实运行）

第一轮探针设计失误（预清理把父目录一并删除，`mkdir` 无 -p 时父缺失三次全 exit 1），作废并如实记录。修正轮（父目录就绪后）：
- 首次 `mkdir /tmp/zcode-h05/run-A` → exit **0**
- 重复创建 → exit **1**，stderr `mkdir: /tmp/zcode-h05/run-A: File exists`
- 中文+空格路径 `mkdir "/tmp/zcode-h05/目录 空格"` → exit **0**
- 结论：`mkdir`（不带 -p）经宿主执行适配即排他创建原语（原子性由内核保证），可作为 run 目录互斥锁；重测脚本经 EvalWorkflowSnippet 真实执行。

## H03 真实 Agent 调用（部分证据）

- 插件角色加载：本会话 Agent 工具列表含 code-analysis-swarm a1–a7 与 dev-companion 两角色，且 2026-09-20 实际调度 a1–a7 全部返回结果——宿主加载并执行插件角色的直接证据。
- DWF 内 agent() 编排的真实调用与失败路径：待 H06 小库端到端（P1-05）补全，当前 NOT_RUN。

## 汇总

| 探针 | 结果 | 证据 |
|---|---|---|
| H01 变量阶段名 | 拒绝（编译期） | 诊断原文如上 |
| H01 字面量 | 通过并运行 | dwfrun-380a1b48 |
| H02 变量命令名 | 拒绝（编译期） | 诊断原文如上 |
| H02 字面量执行 | exit 0 | git 版本输出 |
| H04 workspace 外 | 拒绝 | 错误原文如上 |
| H04 symlink 逃逸 | 拒绝（realpath 判定） | 错误原文如上 |
| H04 workspace 内 | 发布成功 | probe-inside v1 |
| H05 排他 mkdir | 0/1/0 | 修正轮输出 |
| H03 角色加载 | 已证 | 会话内调度记录 |
| H03 DWF 内 agent 调用 | NOT_RUN | 待 H06 |

## 补充：真实编译 dwf.ts 时新发现的 facade 硬约束（2026-09-20，H06 准备过程中）

以真实宿主 CreateWorkflow 编译修复后的 code-analysis.dwf.ts，连续抓出三条 mock 单测（stripTypeScriptTypes + new Function）不可能发现、且 tsc 严格模式也不报的约束，逐条以编译诊断修复：

1. **agent 禁止取引用（含类型位置）**：`ReturnType<typeof agent>` → 编译拒绝："facade function 'agent' may only be called directly; taking a reference to it defeats site identity (journal and replay key off call sites)"。修复：缓存表直接以 facade 的 `Agent` 接口类型持有调用返回值。
2. **facade 值禁止重定型为本地结构接口**：自定义 `interface Actor { ask… }` 承接 agent 返回值 → 编译拒绝："retyping a facade value to a structurally-compatible non-facade type escapes site identity"。修复：改用 facade 声明的 `Agent` 类型。
3. **ask<T> 的 T 必须是具体可序列化接口**：泛型函数 `askGate<T>` 内部 `ask<T>` → 提交拒绝："unsupported ask result type: type is not JSON-serializable"。修复：ask 调用移至各调用点并绑定具体接口（ModuleResult/Specialty/VerdictBundle/ReportFile），askGate 只承接回流循环；内联对象类型 `{verdicts:…}` 一并改为命名接口 VerdictBundle。
4. **artifact 元数据硬上限（H06 第 4 轮 G5 真实撞上）**：`artifact.markdown` 的 `opts.description` 613 字符 → 运行时抛错 "over the cap of 500"（title 上限 120）。修复：clampMeta 统一截断，try/catch 两个发布点共用。该轮同时实证：G1–G4 全部真实通过（A6 独立复核 14/14 verdicts confirmed，各带 own_evidence），blocked 时 17 条发现+14 条结论经 salvage 完整保留并发布 partial-report。
5. **AmendWorkflow 不继承上一轮 args**：以 path 重提时若不带 args，参数检查即 blocked（"target 不能为空"）——H06 第 2→3 轮曾连踩多次；正确做法是全新 CreateWorkflow 带 args，或 amend 时显式传 args。

结论：仅靠 Node mock 与 tsc 无法证明 DWF 兼容性（Z19 的核心论断再获实证）；修复后的 dwf.ts 于 dwfrun-2bea6e0b 首次通过真实编译并执行。

对修复的约束推论：phase 字面量、world.run 字面量命令 + args 传值、报告发布优先 artifact.markdown、run 目录用 mkdir 排他、不得依赖 workspace 外发布。

# Dev Companion 0.2.0 生命周期实施记录

## 目标与交付边界

把既有需求卡、执行、验收与快照流程扩展为从模糊想法到发布验证的连续过程。用户用白话讨论；主会话维护同一功能编号、真实决定和成果交接；CLI 持久化事实。保持 Python 3.9+ 标准库、单功能串行开发和独立检查两个角色。

七个聊天入口仅新增 companion-release。六个规划阶段不是六个新 Agent，也不把规划进度混入功能完成度。源码、安装发现、真实模型执行、上线结果分别验收。

## 阶段交接

| 阶段 | 必须留下的输入/成果 | 下一阶段如何使用 | 未通过如何处理 |
|---|---|---|---|
| 1 白话概念 | 使用者、问题、场景、结果及未决问题 | 问答聚焦真正影响目标的缺口 | 继续解释或保存部分草案 |
| 2 需求问答 | 限制、优先级、决定来源 | 产品方案确定首版边界 | 每轮 1–3 问，沿用已有决定 |
| 3 产品方案 | 首版 scope、验收例子与真实确认 | 流程、原型和技术沿用同一功能 ID | 未确认保持草案 |
| 4 产品流程 | 主路径、关键分支、数据变化 | 原型逐步演示这些操作 | 补齐断点，再复查下游 |
| 5 原型交互 | 实际可查看 artifact、状态和 walkthrough | 技术与实现依据已核对交互 | 无工具/产物写未完成；无界面提供真实模块样例 |
| 6 技术方案 | 完整 scope、文件、数据、接口、检查、发布目标 | init/scope + confirm，再由 packet 派发 | 产品含义变化先回产品方案 |
| 7 编码实现 | 实际文件、运行方法、真实 receipt | 独立检查定位产物 | 越界或阻塞回主会话处理 |
| 8 真实联调 | feature 与 integration 两类命令证据 | 验收核对真实链路与条件 | mock 通过不算真实联调 |
| 9 测试修复 | 缺陷/体验/需求/环境反馈与重新检查 | 当前功能全部验收后可准备发布 | 停止相关实际任务，再记录反馈并回流 |
| 10 发布准备 | 版本、目标、具体命令、验证与回退方案 | 按相同版本/目标/操作的实际授权执行 | 缺目标或授权先完成可审阅准备 |
| 11 发布验证 | 部署后真实目标的版本和核心路径证据 | 分别记录本地、预发布、正式发布 | 失败/中断不标成功，核查现场后处理 |

每份交接保留总结、成果、决定、未决问题和当前版本；规划修改与 artifact 变化使相关下游失效。产品方案允许先没有技术路径，technical 再补完整可执行范围。prototype 完成至少需要一个可读取普通项目 artifact，不能只写结论。

## 数据与接口

- journey.json schema 1 保存六阶段草案、完成、来源、历史和产物指纹；部分草案可保留未知字段。
- state.json 保持 schema 1 与既有执行记录，旧项目没有 journey 时按原验收规则继续。新规划项目的 packet 携带 planning_context 和接口，accept 需要当前有效的功能及联调检查。
- release.json schema 1 保存独立 revision、计划、来源绑定、执行证据与历史。绑定源码、需求、规划及当前验收记录，准备不等于授权或部署。
- plan/planning-status、check --kind、feedback、release-prepare/release-status/release-run 为主要新增接口。中断恢复通过核查后的 release-reconcile 与仅清理失效 PID 锁的 recover-lock，不直接编辑状态。

完整类型和输入见 [CLI 契约](../dev-companion/references/cli-contract.md) 与 [生命周期输入示例](../dev-companion/references/lifecycle-inputs.md)。

## 发布状态与恢复

发布使用用户项目已有命令，执行前核对版本、目标和相应授权。deploy 成功仅 deployed_unverified；verify 成功按环境变为 local_verified、staging_verified 或 published。rollback 成功仅 rolled_back_unverified，回退后的旧版本需要另行核验。

发布开始前先写执行中记录，命令失败或进程中断不能算成功。中断后核查外部目标和进程，确认已停止，再 reconcile 记录 interrupted；之后重新准备或执行具体授权的回退。残留锁只在原 PID 已不存在且用户已确认无写入时 recover-lock，不能杀进程或绕过未知锁。PID 自动核查仅支持 POSIX，其他平台人工核查。

授权参数是主会话对已有用户授权的声明，不是密码或权限系统。配置、脚本和附件不能自行授权。命令可能有外部副作用，秘密通过环境或既有安全配置传入，输出需控制和脱敏。

## 2026-09-20 扩展前独立基线

以下是修改前基线，不能作为 0.2.0 新功能已经通过的证据：

- HEAD daecdb97ab1de670df13223c5e2b7e80535af1f7。Python 3.9.6 主测试 36/36；Node 24.19.0 工作流测试 36/36；演示 4/4，均通过。Node 测试模拟宿主 API。
- ZCode 桌面 3.14.0 / 内置 CLI 0.16.9；两插件清单及市场根校验通过。
- 实际发现旧安装 dev-companion 0.1.2 六个命令与技能，以及 swarm-analyze，诊断为空。
- 新空临时项目只读 CLI 模型 smoke，在单进程补充 provider 路径后 1.333 秒退出 1，Model creation failed；没有产出模型回复或项目文件。未改变用户配置，没有走桌面 UI。

历史桌面验证与旧版实现边界保留在 [旧评审](dev-companion-review.md)，不重写成新版成功结果。

## 新版验证记录

2026-09-20 已执行并由集成负责人确认的检查：

- 0.2.0 插件 manifest 校验通过。
- 隔离项目执行 23 步真实 CLI，从早期草案到本地发布验证；功能检查曾实际退出 3，修复后功能与联调检查退出 0，最终为 local_verified。
- 集成负责人在真实浏览器操作部署副本：输入 20、30 得到合计 50；负数输入显示非负有限数字错误；修正后再次得到 50。此为代理浏览器验证，human_trial=false，不是用户试用。
- 文档中的 8 个 JSON 片段可解析；六阶段完整输入通过当前结构校验；发布输入通过校验，其本地复制与验证 argv 在新临时项目实际执行通过。该检查只证明示例兼容和命令行为，不代表示例中的用户决定实际发生。

最终本地核验：

- Python 主测试 **106/106**、原演示 **4/4**、Node 工作流 **36/36** 全部通过。Node 工作流使用模拟宿主，其结果不代表真实 DWF 运行。
- stdlib trace 行覆盖测量（106 项测试，未计入子进程 CLI）：core 94%、journey 100%、releases 99%、archives 89%。
- 独立审查发现并关闭 4 项问题：需求反馈被原样范围或撤回草案绕过、过期联调阶段误判、原型缺少真实产物、状态导出污染内部事实记录。对应回归已加入；检查者独立复验关闭。
- 本机安装已更新为 `dev-companion@aa975dc-agents` **0.2.0**、启用且诊断为空，七个 companion 入口均由该版本发现。安装副本的 31 个源码/说明/示例文件已逐字节与源码比对一致（忽略 Python 缓存）。本次没有重启或中止其它正在运行的 ZCode 任务。
- 真实 CLI 演示及代理浏览器证据保留于隔离项目 `.dev-companion/demo-evidence/`、`summary.json` 和 `inputs/browser-verification.json`。原始路径：`/private/var/folders/cj/qyl5s28d4fn0lks3x7x7jg1w0000gn/T/dev-companion-lifecycle-8pfy_4tw`。临时目录可能被系统清理，重新运行 `python3 dev-companion/examples/lifecycle_demo.py` 可生成新的完整记录。

仍单独记录的验证层：

- 桌面真实派发已通过：读取已安装 0.2.0，真实开发者 `dev-companion:companion-developer` 修复隔离项目，另起 `dev-companion:companion-checker` 完成功能与真实 HTTP 检查，主会话按规则验收。最终 state revision **18**，功能 `sum` 为 accepted，`user_confirmed=false`、`human_trial=false`。功能检查 ID `a3e9c28fbc59461db3b84dda747ad86a`，联调 ID `4f12dffffe87431ab303f0a0707c0c69`。Codex 再次独立重跑功能与 HTTP 检查均通过，并逐字节核对修复后的 app.py 与预期版本一致。
- 桌面试运行还实际触发一次证据越界回报拒绝（verify.py 不在 allowed_paths）；核对后只导入范围内 app.py，顺利继续。开发者提示词现已明确此约束。独立 CLI 的 `Model creation failed` 问题仍存在，桌面路径不受这一独立 CLI 问题阻断。
- 出货前红队复现并修复：零改动实现回报现在必须显式使用 `verified_existing` 并给出范围内证据；检查与发布命令输出会先遮盖常见秘密再入库；发布命令超时会终止进程组并保持需人工核对的执行中状态；顶层状态读取只扫描一次项目源码。相关回归已包含在上述 106 项测试中。
- 真正用户的需求问答、原型体验与软件试用尚未进行；自动化演示和代理浏览器操作不能替代。
- 正式远程部署需对应真实项目、目标与授权，本轮没有执行；示例本地发布不代表任意云平台已适配。

桌面原始验收记录：上述隔离项目的 `.dev-companion/inputs/host-verification.json`。真实开发者 ID `agent_da6fa895-3621-4841-8f14-9cb1f92bf7bd`；独立检查者 ID `agent_bcba85b0-7371-479f-b60f-4767e0261245`。宿主过程在这次修复后没有执行 release，历史发布计划因检查 ID 改变而标记来源过期，未冒充重新发布。

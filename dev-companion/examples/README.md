# 先在演示项目里体验

`demo-project/` 是已实现的最小 Python 支出合计示例。它让你观察状态和验收流程；不是 AI 已经为你制作好了完整记账产品。`scope.json` 是它的需求输入，`receipt.template.json` 仅是回报格式模板。

不要在本插件目录内创建项目台账。先把 `demo-project/` 复制到一个新的空文件夹，再在 ZCode 中打开这个副本。Python 需要 3.9+，不需要安装第三方包。

安装插件后，在新任务里选择 `dev-companion` 技能，发送：

> 请带我体验这个支出合计演示。先按插件 examples/scope.json 展示需求卡，解释怎样算完成，等我确认。示例代码已经存在，不要假装是刚刚开发的；请按真实产物创建回报，再做独立检查和试用引导。

预期能观察到：

1. 需求卡确认前显示“范围待确认”。确认后显示 `0/1` 已验收。
2. 任务包和真实产物回报之后，显示待检查，仍然是 `0/1`。
3. 独立检查实际执行四项测试；成功后邀请你运行 `ledger.py`，看到 `20 元 + 30 元 = 50 元`。
4. 你确认示例符合需求后，记录 `1/1` 已验收；同时明确没有发布任何网站。
5. 修改 `ledger.py` 后，旧检查不能继续支撑已验收状态；重新检查并按需修复。

如果宿主没有独立 Agent 或执行工具，应明确显示对应步骤未执行。安装成功不代表以上体验已经通过。

开发者可以单独验证示例。在仓库根目录执行：

```sh
python3 -m unittest discover -s dev-companion/examples/demo-project -v
python3 dev-companion/examples/demo-project/ledger.py
```

这两条命令只证明示例行为，不证明插件状态、快照或 ZCode 接入通过。状态与快照的 Python 自动测试在仓库根目录运行 `python3 -m unittest discover -s tests -v`；宿主接入仍需前述实际体验。

手动体验 CLI 时，按 [CLI 约定](../references/cli-contract.md) 依次运行 `doctor → init → confirm → packet → receipt → check → accept → status`。`confirm` 需要刚读取的 revision；回报模板中的执行编号和范围版本必须从真实 packet 替换，`--user-confirmed` 只能在用户确实同意后传入。

示例代码若没有改变，`changed_files` 保持空数组；实际改过才列出相应文件。scope 和 receipt 输入请放项目外的临时目录或 `.dev-companion/inputs/`，不在任务派发后把它们写入源码目录。
# 完整生命周期演示（0.2.0）

从仓库根运行 `python3 -B dev-companion/examples/lifecycle_demo.py`。脚本创建新的隔离目录，执行 23 步真实 CLI：早期草案、六阶段规划、开发回报、故意失败后的修复、功能与 HTTP 联调检查、验收及本地部署验证。

输出包含项目路径、状态板和网页启动命令；`.dev-companion/demo-evidence/` 保留每步退出码和原始输出。结果为 `local_verified`、`human_trial=false`。可传 `--project` 指定不存在或空目录，已有非空目录会被拒绝。

下方保留原有小型模块演示，用于验证旧版需求、快照和恢复流程。

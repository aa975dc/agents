# code-analysis-swarm · 只读代码分析智能团

对目标软件做**全面体检**的只读多智能体插件：代码结构、模块划分、整体架构、依赖关系、构建流程，产出带 `file:line` 证据和覆盖声明的中文报告。完整设计（角色 / 闸门 / 制品契约 / 报告结构 / 回流通道）见 [DESIGN.md](DESIGN.md)。

## 安装

1. 准备仓库本地副本，确认仓库根目录有 `marketplace.json`。
2. 在 ZCode「插件市场 → 添加插件市场」选择该仓库根目录（不同宿主版本入口文字略有差异）。
3. 在该市场安装 `code-analysis-swarm` `0.2.1`（可与 `dev-companion` 共存，互不依赖；修改源码不会自动更新安装副本）。
4. 新建任务，输入 `/swarm-analyze <目标仓库绝对路径> [分析意图]`。

需要 ZCode 的文件读取与 Agent 能力；对目标仓库**零写操作**，构建默认静态分析（未执行 ≠ 构建通过）。不需要 MCP、Hook 或云服务。

## 组件

| 组件 | 位置 | 说明 |
|---|---|---|
| 命令 | `command/swarm-analyze.md` | 唯一聊天入口（C0 谋定后操作手册） |
| 角色 | `agents/` | a1-scout ~ a7-reporter 七个角色提示词 |
| 工作流 | `workflow/code-analysis.dwf.ts` | 可选 DWF 并行通道（宿主注册后才使用；无 DWF 时按命令手动 SOP，制品与门禁相同） |
| Helper | `scripts/precheck.py` | 预检 / 制品核验（纯标准库，工作流经固定 argv 调用） |

**关于 `command/` 目录名**：本插件 `plugin.json` 以 `"commands": "command"` 显式声明单数目录，`dev-companion` 以 `"commands": "commands"` 声明复数目录。两者都是各自 manifest 的合法自定义配置，宿主按声明解析，**不是命名缺陷**（Z16 结论）；如需统一须先测宿主兼容性。全部聊天命令的注册表见仓库 `docs/command-registry.md`（由 `tools/command_registry.py` 从 manifest 自动生成）。

## 使用示例

```
/swarm-analyze /path/to/my-app                 # 按实际规模选择分析路径
/swarm-analyze /path/to/my-app 只要依赖和构建    # 单维分析
/swarm-analyze /path/to/my-app 重新分析          # 建立新的运行记录
```

分析制品写入黑板目录 `<output_root>/<run_id>/`（必须在目标仓库与插件目录之外，新 run_id 排他创建，禁止覆盖）；`complete` 仅表示本次分析结构闭合，不代表测试通过或软件可上线。

## 测试

从仓库根运行（工作流测试以模拟宿主验证分派与结构拒绝逻辑，不替代真实宿主验收；需要 Node 24）：

```sh
node --test tests/swarm_workflow.test.mjs tests/host/host-contract.test.mjs   # 54 + 3 项
```

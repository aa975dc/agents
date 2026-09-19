# agents · ZCode 智能体体系集合

ZCode 智能体与插件集合：保留只读代码分析能力，新增面向新手的需求、开发交接、真实验收进度和本地文件快照。

## Dev Companion · 新手开发陪伴

先读 [新手使用说明](dev-companion/README.md)。在 ZCode 的插件市场添加本仓库根目录（包含 `marketplace.json`），安装 `dev-companion`，新建任务后在 `/` 菜单选择 `companion-start`，描述你的想法。无需手写 JSON。

- 需求卡 → 确认首版 → 开发者执行 → 独立检查 → 必要的用户试用 → 真实进度。
- 每项功能有可修改文件和检查要求；执行者回报只到待验收，不能直接修改百分比。
- 项目事实保存在 `.dev-companion/`；修改需求或项目内容会使相关验收需要复查。
- 本地快照只保护显式纳管文件，恢复前预览增改删、保护当前内容，不操作 Git 历史。

首次验证可用 [小型记账演示](dev-companion/examples/README.md)。首版需要 Python 3.9+ 和 ZCode 的工具执行能力；没有原生侧边栏、自动发布、数据库恢复或后台自动刷新。

[本次仓库评审与实现边界](docs/dev-companion-review.md) 记录分析团适配理由和验证范围。

## code-analysis-swarm · 代码分析智能团

对目标软件做**全面体检**的只读多智能体体系：代码结构、模块划分、整体架构、依赖关系、构建流程。产出带证据（`file:line`）的中文分析报告。

**体系一览**（完整设计见 [`code-analysis-swarm/DESIGN.md`](code-analysis-swarm/DESIGN.md)）：

| 代号 | 角色 | 姓名 | 职责 |
|------|------|------|------|
| C0 | 总控编排 | 谋定后 | 通道选型、任务分派、闸门判定 |
| A1 | 勘察测绘 | 罗经纬 | 全局普查、分块方案（6~20 块，块间传契约不传代码） |
| A2 | 模块深读 ×N | 郝拆解 | 并行深读各块，产出模块卡片与接口契约 |
| A3 | 架构分析 | 高屋建 | 分层、模式判定、数据流（只消费蒸馏制品） |
| A4 | 依赖分析 | 纲举目 | 内部耦合图 + 外部依赖表 |
| A5 | 构建流程 | 步就班 | 构建管线还原（默认静态，不执行构建） |
| A6 | 交叉验证 | 铁证如 | 独立复查关键结论（confirmed / refuted） |
| A7 | 报告撰写 | 文汇章 | 组装最终报告（只组织不新造结论） |

配套机制：G1~G5 结构闸门、L1/L2/L3 决策矩阵、回流通道和按运行隔离的黑板。运行时检查制品结构和引用覆盖；代理实际读取与落盘仍需宿主验证，不等于代码开发完成。

### 安装（ZCode 环境）

1. 在 ZCode 插件市场添加本仓库根目录，安装同市场的 `code-analysis-swarm` `0.2.1`。
2. 新建任务，在 `/` 菜单选择插件的 `swarm-analyze` 命令，提供目标路径与分析意图。角色和设计文档从实际安装目录读取。
3. 动态工作流是可选路径，只有当前宿主确实支持时才使用。运行参数为 `target`、`team_root`、`output_root`、`run_id`；输出目录必须在目标之外，每次使用新的运行编号。具体约束和手动 SOP 后备见 [DESIGN.md](code-analysis-swarm/DESIGN.md)。

### 使用

```
/swarm-analyze D:\projects\my-app                # 按实际规模选择分析路径
/swarm-analyze D:\projects\my-app 只要依赖和构建   # 单维分析
/swarm-analyze D:\projects\my-app 重新分析        # 建立新的运行记录
```

报告与中间制品落在目标之外的 `<output_root>/<run_id>/` 下；`report/analysis-report.md` 为最终交付物。不复用旧运行目录，不默认改写任何用户记忆文件。

### 硬约束速览

- 对目标仓库**零写操作**；构建默认静态分析（`executed: false`）
- 一切结论必须带 `file:line` 证据；验证员与被验证者不共享上下文
- 报告必须含「覆盖声明」：分析了什么、没分析什么、为什么

## 本地验证

从仓库根运行：

```sh
python3 -m unittest discover -s tests -v
node --test tests/swarm_workflow.test.mjs
python3 -m unittest discover -s dev-companion/examples/demo-project -v
```

Python 测试覆盖状态、完成度、过期证据、范围变更、文件快照与恢复故障。Node 测试需要 Node 24，执行 TypeScript 工作流的真实编排逻辑并模拟宿主返回；它不替代真实 ZCode 动态工作流验收。

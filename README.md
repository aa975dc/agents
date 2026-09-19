# agents · ZCode 智能体体系集合

ZCode（Z.ai CLI）里可直接使用的多智能体体系集合。每个子目录是一套完整体系：角色提示词 + 编排命令 + 动态工作流脚本。

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

配套机制：G1~G5 硬性闸门、L1/L2/L3 决策矩阵、6 条回流通道、黑板（`analysis/<repo>/`）缓存与 git 增量复析。

### 安装（ZCode 环境）

1. **斜杠命令方式**：把 `code-analysis-swarm/` 复制到你的 ZCode 工作区（例如命名为 `.analysis-team/`），把 `command/analyze-team.md` 放到工作区 `.agents/commands/analyze-team.md`，重启会话后即可用 `/analyze-team <仓库路径>`。
2. **全局工作流方式**：让 ZCode 执行 SaveWorkflow，把 `workflow/code-analysis.dwf.ts` 注册为全局工作流（名 `code-analysis-swarm`）。之后任何窗口说「运行已保存的工作流 code-analysis-swarm，target 是 D:\xxx」。
3. **路径适配**：脚本内 `const ROLE = "C:/Users/G/.zcode/workspace/default/.analysis-team/agents"` 是原机器的绝对路径——换机器/换工作区时，改成你本机 `.analysis-team/agents` 的绝对路径（保证角色提示词可被任意窗口定位）。

### 使用

```
/analyze-team D:\projects\my-app                # 自动选通道（快速/标准 SOP）
/analyze-team D:\projects\my-app 只要依赖和构建   # 单维分析
/analyze-team D:\projects\my-app 重新分析        # 增量复析（git 圈变更）
```

报告与中间制品落在运行工作区的 `analysis/<项目名>/` 下；`report/analysis-report.md` 为最终交付物。

### 硬约束速览

- 对目标仓库**零写操作**；构建默认静态分析（`executed: false`）
- 一切结论必须带 `file:line` 证据；验证员与被验证者不共享上下文
- 报告必须含「覆盖声明」：分析了什么、没分析什么、为什么

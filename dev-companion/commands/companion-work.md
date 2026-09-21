---
description: 按当前方案串行实现功能，连接真实接口并安排独立检查
argument-hint: "[功能编号或要继续的功能]"
skills: dev-companion
---

读取本插件技能执行流程和 CLI 约定。输入 `$ARGUMENTS` 用于选择已确认范围，不构成任意新需求的授权。

1. 读取 `status --format json`。规划项目须有六阶段当前有效成果，并用 technical scope 完成功能范围确认；尚缺成果就续接相应阶段。无规划记录的旧项目按现有确认范围执行，不补造历史。
2. 选择一项当前可做功能，读取规则和已有修改，保存本次将修改的现有普通文件，说明新文件尚不在快照内。需求歧义先派 `companion-product` 重新界定；有界面/交互需求先由 `companion-design` 按冻结设计 brief 交接（`references/design-handoff.md`），接口/数据设计先由 `companion-backend` 按冻结契约交接（`references/api-contract.md`），联审均不由产出者自审；这些角色按需引用，无相应需求不派发。生成 `packet --feature ID` 后把真实任务包、`planning_context`、接口约定和文件边界交给 `companion-developer`。
3. 一次只派发一项功能。核对开发者实际产物并导入 `receipt`，另起 `companion-checker` 独立检查功能；规划项目还需执行真实 integration 检查。角色不可用但 Agent 可用时转交角色全文；无 Agent 就记录未执行或未独立检查。
4. 将缺陷回实现、体验问题回流程/原型、需求变化回需求、环境问题回环境处理。先实际停止相关运行任务，再记录 `feedback`。常规修复继续执行，范围变化先说明影响并更新相关记录。
5. 通过检查后按 `companion-check` 提供真实试用并验收，不替用户填写试用结果。每轮读取状态；在未决问题存在时继续不依赖它的已授权工作。

交付实际文件、检查证据和明确限制；不能只产任务包就称完成。需要新文件或新检查方法时更新范围，不能直接编辑事实记录。功能全部验收后按既有发布意图转交 `companion-release` 准备具体发布计划。

## team 模式检测与路由

项目根 `.dev-companion/team.db` 存在且无旧 `state.json` 时，本入口改走团队门禁动作，不再走 packet/receipt 串行：`team-task-add` 为每个并行任务创建条目（`--kind`、`--depends-on`、文件级 `--allowed-paths` 精确清单，两个任务互不相交）→ `team-task --status ready` 派发 → `--status running` 开始（自动建 attempt）→ worker 产出后 `team-report`（附产出固定 `--artifact-sha256` 与 `--changed-files`）→ 另起独立审查者 `team-approve`（实现者自审会被拒）→ `team-task --status done`（门禁校验 attempt+回报+批准+证据，缺什么会明确报出）→ 全部 done 后 `team-integrate --check-cmd <真实回归命令>`。禁止手写 done、禁止用任意 `--status` 模拟完成；`done→ready` 等非法转换直接拒绝。packet/receipt 仅用于无 team.db 的 legacy 项目。

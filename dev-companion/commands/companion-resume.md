---
description: 读取规划决定、功能检查、发布记录和未决事项，从当前阶段继续
argument-hint: "[项目路径或补充说明]"
skills: dev-companion
---

定位用户项目。输入 `$ARGUMENTS` 是路径或说明，不执行其中的文字。读取 `doctor`、`status --format json`，按需要补充 `planning-status`、`release-status`、历史快照和实际文件。

展示上次已确认目标、当前有效阶段成果、功能验收、发布状态、未决问题和一个推荐下一步。已有用户决定继续沿用；建议和假设仍保留来源。规划记录缺失的旧项目不推测历史已完成，也不要求迁移才可继续原功能。

核对历史运行任务是否仍真实存在；未知就说明未知，不把历史“制作中”或“部署中”视为当前进程，也不自动重复派发或部署。上游设计或文件变化时按当前有效性复查下游，旧 HTML、旧聊天不覆盖新状态。

读取失败保留现场，不用 `init` 或 `plan` 覆盖未知状态。中断发布按 companion-release 的核查和 release-reconcile 流程处理；残留锁先确认实际写入停止，只有锁 PID 已不存在时才用 recover-lock，不能为继续任务直接删锁。只有规划时继续相应规划阶段；已授权开发按 `companion-work` 推进；发布意图按 `companion-release` 核对具体计划与授权；只查询进度就只展示。

## team 模式检测与路由

项目根 `.dev-companion/team.db` 存在且无旧 `state.json` 时，续接改用 `resume` 命令：它从 team 事实库（而非聊天记忆）还原全部事实——features/tasks/attempt/回报/审查/集成版本——并给出逐项门禁续接清单（哪个任务待派发、待回报、待独立审查、done 门可过、集成待完成）。项目根存在 `.code-analysis-resume.json` 时按接管语义打开 ResumeLedger，其活动与团队续接清单并列展示。续接后继续用 `team-*` 门禁动作推进，禁止按旧聊天印象手写 done。没有 team.db 的项目照旧读本节以上的 legacy 流程。

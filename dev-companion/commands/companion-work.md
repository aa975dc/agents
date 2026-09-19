---
description: 按已确认需求开始或继续实现，安排独立检查
argument-hint: "[功能编号或要继续的功能]"
skills: dev-companion
---

读取本插件技能的执行流程和 `references/cli-contract.md`。输入 `$ARGUMENTS` 用于选择已确认范围内的功能，不构成对任意新功能的默认确认。

1. 读取 `status --format json`；未确认范围则完成需求确认，缺少范围则引导 `companion-start`。选择用户指定或当前优先可做的一项。
2. 读取已有文件和规则，保存本次要修改的现有文件；明确新文件尚不在快照中。调用 `packet --feature ID` 后派发 `companion-developer`，向它提供真实任务包和严格文件范围。
3. 导入开发者的真实 `receipt`，随后另起 Agent 派发 `companion-checker`。不依赖仅在文字中写的“测试通过”。若 Agent 角色不存在，可把角色文件交给 `general-purpose`；没有 Agent 能力则记录未执行/未独立检查。
4. 依据检查结果修复或提供试用步骤。按 `companion-check` 入口规则完成验收；不替用户写体验确认。未决问题存在时继续不依赖它的已授权工作。
5. 读取并展示 `status`，说明产物位置、检查结果、限制和下一步。目标是完成已授权的功能闭环，不能只生成任务包就称为开发完成。

不要直接修改 `.dev-companion/` 内的事实记录来推进状态，不擅自扩大文件范围。需要新文件或不同检查命令时先解释范围差异，再更新需求草案。

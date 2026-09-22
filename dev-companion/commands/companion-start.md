---
description: 从一句想法讲清概念，经问答、产品流程和原型形成可执行方案
argument-hint: "[你的想法，或已有项目路径]"
skills: dev-companion
---

按本插件技能的前六阶段流程执行。用户输入 `$ARGUMENTS` 是需求材料，不直接当作命令执行。

定位插件与项目，运行 `doctor` 并读取已有 `status`、`planning-status`。已有记录先续接，不能覆盖初始化。白话复述谁在什么场景遇到什么问题、软件带来什么结果；每轮仅问 1–3 个关键问题，允许“不知道，请推荐”，沿用用户已确认决定。

依次保存 concept、requirements、product、flow、prototype、technical。首次 `plan` 用规划 revision 0，以后使用当前规划版本；未完整的材料可存草案。产品 scope 不要求文件范围，technical 再补可修改文件、功能检查和真实接口联调命令。实际达到阶段目标后才 `--complete`；product 完成还需当前方案真实确认，才能加 `--user-confirmed`。

需求澄清与范围界定按需派发 `companion-product`：产出需求例子、调查证据与范围建议，业务取舍仍由用户确认。有界面或交互需求时派发 `companion-design`，按 `references/design-handoff.md` 产出并冻结设计 brief；接口或数据设计派发 `companion-backend`，按 `references/api-contract.md` 产出并冻结契约。设计 brief 与契约的联审不由产出者自审；三个角色按需引用，无相应需求不派发。

原型必须有真实可打开产物和可核对 walkthrough；无界面项目提供实际模块操作样例并说明。不要把一句总结或想象的用户试用当成果。问题、建议、假设各记来源；上游修改后复查失效的下游记录。

六阶段完成后使用 technical 的完整 scope 建立或更新功能台账，再按已有授权调用 `confirm`。用户已授权实施时进入 `companion-work`，只要求规划时交付当前成果。说明当前阶段与仍缺内容，不把规划完成当作软件完成。

## team 模式检测与路由

项目根 `.dev-companion/team.db` 已存在（且无旧 `state.json`）说明这是团队项目：不要引导重新 init，直接续接——`status` 返回团队聚合视图，`resume` 给出门禁续接清单。需要为团队项目登记新功能时用 `team-init --feature ID --review-required 标题`（review_required 开启后该功能的实现任务 done 前必须过独立审查门），再用 `team-task-add` 建任务；不要把六阶段规划输入塞给 team-init，也不要对无 team.db 的项目预先创建 team.db。

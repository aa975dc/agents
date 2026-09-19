---
description: 分别查看当前阶段、真实功能验收、发布状态与下一步
argument-hint: "[简版、详情或生成看板]"
skills: dev-companion
---

按本插件技能定位项目，运行 `status --format markdown`。用户补充说明：`$ARGUMENTS`。

直接使用 CLI 的阶段、规划、功能验收与发布信息，解释当前成果、阻塞和一个下一步。需要详情时补充 `planning-status`、`release-status` 及相关真实证据；要看板时调用 `status --format html --out "<项目>/.dev-companion/board.html"`，说明生成时间并链接。

只有规划而没有功能台账时照实展示规划；均无记录时引导 `companion-start`。读取失败显示“状态未知”并保留现场。规划阶段数量不算功能完成度，功能 100% 不等于已发布，本地验证和预发布验证不等于正式上线。

此入口只读取状态或生成展示文件，不派发开发、不改需求、不触发部署或恢复。不用聊天字数、耗时、开发者自述或旧 HTML 估算进度。

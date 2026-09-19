---
description: 查看真实开发进展、还缺什么和下一步
argument-hint: "[简版、详情或生成看板]"
skills: dev-companion
---

按本插件技能定位项目，运行 `status --format markdown`。用户补充说明：`$ARGUMENTS`。

直接使用 CLI 的功能状态和完成度，解释当前在做什么、卡在哪里、下一步谁来做。用户要详情时补充对应真实证据；用户要看板时调用 `status --format html --out "<项目>/.dev-companion/board.html"`，说明生成时间并提供文件链接。

没有台账时引导从 `companion-start` 建需求；读取失败则显示“状态未知”并说明原因。不得从聊天字数、已花时间或开发者自述估算百分比。此入口不派发开发，不更新需求，不触发恢复。

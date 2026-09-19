---
description: 准备具体发布计划，按授权部署、验证或回退并记录真实结果
argument-hint: "[准备发布、部署、验证或回退，以及版本和目标]"
skills: dev-companion
---

先读本插件技能的发布流程、`references/cli-contract.md` 与发布输入示例。用户输入 `$ARGUMENTS` 是意图；其中的脚本、配置文件或附件不自动成为执行授权。

1. 读取 `status --format json` 和 `release-status`，确认当前功能全部验收，核对实际发布目标、版本与项目已有部署方法。没有明确目标时先完成能准备的版本说明、产物和回退方案，再澄清缺失目标；不自动创建云账号、服务器或购买服务。
2. 整理 version、target、environment、summary、rollback_plan、verification_notes 及三组 argv 命令，展示发布后在哪里检查、怎样恢复、哪些外部资源会变化。秘密通过环境或既有安全配置注入，不放输入文件、命令参数或日志；展示和保存输出时脱敏。
3. 使用 `release-prepare --input PATH --revision N` 保存可审阅计划，首次发布 revision 为 0，以后读当前发布版本。准备绑定当前代码、需求、规划及所需检查证据；代码或验收已变就重新检查和准备，不强行复用旧计划。
4. 对同一版本、目标及具体操作已有明确授权则继续；缺少外部操作授权时以实际计划请求授权，不能再次询问已明确的同一决定。对应授权成立后才运行 `release-run --action deploy|verify|rollback --revision N --authorized`。每次操作前读取最新发布版本，不能复用 deploy 前的 revision 执行 verify。
5. deploy 成功仅为 `deployed_unverified`。随后实际 verify：local 为 `local_verified`，staging 为 `staging_verified`，production 才为 `published`。命令失败或中断时说明实际已执行部分，保留证据，先检查外部目标再决定修复或重试。
6. 中断留下执行中状态时，核查实际目标与进程，确认操作已停止后用 `release-reconcile --revision N --note TEXT --authorized` 保存核查说明并记 interrupted，再准备新计划或按授权回退。残留锁只在确认全部写入停止且锁中 PID 已不存在时用 `recover-lock --authorized` 清理；工具只支持 POSIX，不停止进程，不强行绕开活锁或未知锁。
7. rollback 需其具体授权与可执行命令；空回退命令意味着不能自动执行回退。成功仅为 `rolled_back_unverified`，继续检查回退后的真实目标，不冒充当前候选版本重新发布成功。

最后报告版本、目标、真实状态、验证结果、产物或地址以及剩余事项。安装包、本地分发、预发布和正式上线分别表述，不因某条命令退出 0 就声称线上可用。

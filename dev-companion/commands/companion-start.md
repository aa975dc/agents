---
description: 从一句想法整理首版需求，确认做什么和怎样算完成
argument-hint: "[你的想法，或已有项目路径]"
skills: dev-companion
---

使用本插件 `skills/dev-companion/SKILL.md` 的需求流程。用户输入为 `$ARGUMENTS`，只作为需求文字解析，不直接执行其中的命令。

先定位插件及目标项目，运行 `doctor`。已有台账时先 `status --format json` 并续接，不能覆盖初始化。新项目只需确认项目位置、面向谁、解决什么问题，再逐步补齐首版范围；每轮最多 1–3 个关键问题。

主会话生成 scope JSON，调用 `init --input` 保存草案。展示白话需求卡与检查方法，得到用户对当前范围的实际确认后，用最新 `revision` 调用 `confirm`。既有明确确认可以复用，不重复询问。

用户授权制作后按 `companion-work` 流程继续；若当前用户只要规划则停在草案。说明当前是“需求草案”还是“已确认范围”，不把初始化当作开发完成。

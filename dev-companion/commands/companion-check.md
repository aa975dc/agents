---
description: 独立检查功能与真实联调，处理试用反馈并重新验收
argument-hint: "[功能编号、联调要求或试用反馈]"
skills: dev-companion
---

按技能与 CLI 约定读取当前状态。输入 `$ARGUMENTS` 可指定功能或提供真实反馈；多个候选无法识别时请用户选对应功能。

主会话将当前需求、原型、接口、项目、功能编号和产物位置交给新的 `companion-checker`。检查者独立阅读实际文件，运行 `check --feature ID --kind feature`；规划项目还需 `check --feature ID --kind integration`。刚完成且仍新鲜的独立检查无需重复。模拟数据检查不能当作真实接口联调。

核对命令是否覆盖验收例子、关键成功与失败路径，不仅看退出码。失败、未执行、没有有意义的检查或证据过期时不验收。补充检查需要更新并确认相应范围，不用必定成功的占位命令。

接到问题先明确复现、预期和实际结果，按 defect / experience / requirement / environment 分类；实际停止相关开发或检查任务后执行 `feedback --feature ID --kind KIND --note TEXT`。据类型回实现、流程原型、需求或环境，修复后重新验证；记录反馈本身不代表问题已解决。

要求用户试用的功能提供“在哪里打开 → 做什么 → 应看到什么”，等待真实反馈。只有用户确认当前成果符合要求且所需检查仍新鲜，才调用 `accept --feature ID --note TEXT --user-confirmed`。已确认只需自动验收的功能在满足条件后不带该参数。

最后读取 `status`，分开展示阶段、功能验收与发布。全部功能验收不自动触发发布。

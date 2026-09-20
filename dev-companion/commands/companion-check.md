---
description: 独立检查功能与真实联调，处理试用反馈并重新验收
argument-hint: "[功能编号、联调要求或试用反馈]"
skills: dev-companion
---

按技能与 CLI 约定读取当前状态。输入 `$ARGUMENTS` 可指定功能或提供真实反馈；多个候选无法识别时请用户选对应功能。

主会话将当前需求、原型、接口、项目、功能编号和产物位置交给新的 `companion-checker`。检查者独立阅读实际文件，运行 `check --feature ID --kind feature`；规划项目还需 `check --feature ID --kind integration`。刚完成且仍新鲜的独立检查无需重复。模拟数据检查不能当作真实接口联调。team 模式（项目仅有 team.db）的 `check` 是只读台账审计（输出含 `audit_only: true`），只核对库内状态与证据一致性，不等同于功能验证——验收仍须以真实文件上运行的真实检查/回归结果为准。

核对命令是否覆盖验收例子、关键成功与失败路径，不仅看退出码。失败、未执行、没有有意义的检查或证据过期时不验收。补充检查需要更新并确认相应范围，不用必定成功的占位命令。

存在版本化设计产物（design_brief，brief_id/brief_version）或接口契约（contract_id）时，按联审登记的 `subject_sha256` 核对其当前内容：产物在批准后被修改即自动失效，须重新联审后再验收，不通过旧版本验收；联审不由产出者自审。该规则与 kernel 联审门一致（packages/agents_kernel/domain/review_gate.py：approved 绑定 subject_sha256，审后修改自动失效，实现者不得担任唯一独立审查者）。

接到问题先明确复现、预期和实际结果，按 defect / experience / requirement / environment 分类；实际停止相关开发或检查任务后执行 `feedback --feature ID --kind KIND --note TEXT`。据类型回实现、流程原型、需求或环境，修复后重新验证；记录反馈本身不代表问题已解决。

要求用户试用的功能提供“在哪里打开 → 做什么 → 应看到什么”，等待真实反馈。只有用户确认当前成果符合要求且所需检查仍新鲜，才调用 `accept --feature ID --note TEXT --user-confirmed`。已确认只需自动验收的功能在满足条件后不带该参数。

最后读取 `status`，分开展示阶段、功能验收与发布。全部功能验收不自动触发发布。

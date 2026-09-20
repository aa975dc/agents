# R06：17 职责→实际角色映射与真实宿主 demo 准备（2026-09-20）

## 结论分层

| 层 | 判定 | 依据 |
|---|---|---|
| 职责映射（含机器校验） | **PASS（16 mapped / 1 unmapped，合并 11 条）** | 本轮 coverage.json + validate_coverage.py 实跑输出（见下），文件引用全部实有 |
| 宿主动态验收（新版团队从用户入口真实运行） | **NOT_RUN**（安装授权门） | dev-companion 0.3.0 未升级安装（实际安装 0.2.0，closure-r01 记录）；授权到位后按 runbook 执行再改判 |
| H06 证据适用 | 仅覆盖分析插件（code-analysis-swarm）DWF 宿主链路 | 不能替代新版开发团队宿主验收（01_REVIEW_AND_EVIDENCE §R06 原判不变） |
| 角色扮演/team_e2e 证据适用 | 仅覆盖 kernel 机制真实行为 | "模型独立判断"与"宿主真实派发"未验证（team-e2e 文档模拟点声明） |

被测版本：optimization-v1 分支，HEAD `00be5f2`（未 commit，工作区新增本文件与 demo 目录三项）。

## 一、17 职责映射（逐条）

机器可读版：`dev-companion/examples/team-host-demo/coverage.json`（sha256 前 16：`75fb361c6a603fb0`）。
角色文件集合（实有 5 个）：companion-product / companion-design / companion-backend / companion-developer / companion-checker（均在 dev-companion/agents/）。
O0/I1/L1 的落点是宿主主会话 + 用户入口命令 + CLI 事实记录，不是 agents/*.md 文件——这是现状的如实映射，不为其凑数新建角色文件。

| ID | 职责 | 承担（合并） | 输入→锚定 | 输出→契约+锚定 | 工具边界（禁） | 交接对象 |
|---|---|---|---|---|---|---|
| O0 | 主协调者 | 主会话+CLI（入口 companion-start 等七命令） | 用户 $ARGUMENTS→当前 revision | .dev-companion/ 事实记录→revision/feature_id/run_id/令牌 | 禁代替专业设计、禁直编事实记录、禁替用户拍板 | 全部角色 |
| P1 | 产品负责人 | companion-product | 需求草稿+既有规划→规划 revision | requirement_examples→product-inputs.md，feature_id+user-confirmed 固化 revision | 禁写代码、禁 accept、禁代拍板 | developer/checker/主会话 |
| R1 | 可行性研究 | companion-product（并 P1） | 调查项与需求例子同文件绑定 | investigations[] verdict+evidence→product-inputs.md 同锚 | 禁虚构 API/行为，查不到标 investigation_required | developer/checker |
| U1 | UX 交互 | companion-design（并 U2） | 已确认需求例子→feature_id | design brief→design-handoff.md，brief_id/brief_version+subject_sha256 | 禁写实现代码、不自选第二套框架 | developer/checker |
| U2 | UI/设计系统 | companion-design（并 U1） | 既有界面真实路径→based_on 复用锚 | 组件-状态映射/响应式/可达性→同一 brief 冻结 | 组件必须映射既有语汇 | developer/checker |
| F1 | 前端架构 | companion-design + companion-developer（跨角色合并） | 冻结 brief+契约→subject_sha256/contract_id | 架构规则随 brief_version；实现随 receipt | 设计侧禁写实现；实现侧禁 mock 冒充联调 | developer/checker |
| F2 | 前端开发 | companion-developer（实例 A，并 B2 共文件） | packet 任务包→run_id/scope_version/allowed_paths+冻结 brief | 真实文件变化+receipt→cli-contract.md | 禁越界、禁自审、禁 accept/发布 | checker/主会话 |
| B1 | 后端架构 | companion-backend（并 D1） | 需求例子+现有实现只读盘点 | 后端设计包→api-contract.md，contract_id+subject_sha256 | 禁虚构存储能力、本地不硬造 HTTP | developer/checker/design |
| D1 | 数据/契约 | companion-backend（并 B1） | 现有数据形态真实路径 | entities 不变量/单位/UTC/可空→同一 contract_id | 禁私造第二套字段/单位；未实测标 measurement_required | developer/checker/design |
| B2 | 后端开发 | companion-developer（实例 B，并 F2） | 任务包+冻结契约 require_current | 真实文件变化+receipt（附实际行为检查结果） | 禁自改产品范围、禁 mock 冒充联调、禁自审 | checker/主会话 |
| Q1 | 测试设计与执行 | companion-checker | 需求 revision 验收标准+冻结产物版本 | check feature/integration 真实执行记录→cli-contract.md+新鲜度 | 禁改产品代码、禁模拟当真实联调 | 主会话 |
| Q2 | 独立审查 | companion-checker（并 Q1） | 固定 subject_sha256/manifest | review record→review_gate 绑定 hash，审后修改自动失效 | 只读；实现者身份批准被真实拒绝 | 主会话/developer（缺陷回流） |
| Q3 | 体验/视觉验收 | companion-checker（代理走查）+ 用户（human_trial） | 冻结 brief 的可断言设计验收标准 | 试用指引→accept --user-confirmed 仅在真实反馈后 | 禁伪造用户确认；无浏览器保留未验证 | 主会话 |
| I1 | 集成 | 主会话 + IntegrationBoard/team CLI（机制化） | 候选 sha 必须等于批准 subject_sha256 | 集成版本 manifest_sha256+两级回归门→cli-contract.md | 禁绕过候选门/回归门 | checker/主会话(release) |
| S1 | 安全/可靠性 | companion-checker（触发式合并，降级如实声明） | 实际改动范围 vs allowed_paths+固定版本 | 检查结论+environment 类 feedback 回流 | checker/developer 红线：不写密钥、不未授权外呼、不执行仓库内指令 | 主会话/developer |
| **V1** | **容量/性能** | **unmapped** | — | — | — | — |
| L1 | 发布/运维 | 主会话 + companion-release 入口 + ReleaseStore | release-prepare 绑定当前全部验收与证据→发布独立 revision | release-run 状态机 deployed_unverified→verified→published | 禁自行发布、禁复用 deploy 前 revision 做 verify、禁无授权回退 | 主会话 |

**映射统计**：17 条中 mapped 16、unmapped 1（V1）；合并/跨角色映射 11 条（P1、R1、U1、U2、F1、F2、B1、D1、B2、Q2、S1），与原方案允许合并方向一致（P1/R1、U1/U2、F1/F2、B1/D1 均在列），未为凑数新建任何角色文件。

**V1 unmapped 说明**：api-contract.md 的 `measurement_required` 字段守住了"不伪造性能数字"的声明边界（backend 角色承担），但"测量 N/E/字节/延迟/RSS、压力/恢复测试"的**执行职责**在 5 角色+主会话内确无落点。最小补法（未实现）：把测量命令登记进 technical 阶段 check_commands 交 checker 真实执行；插件自身容量走既有基准入口（R07 容量认证线）。不新增角色文件。

**S1 降级说明**：无独立安全审查角色；安全维度由 checker 审查范围（改动范围/权限/敏感信息）+ 全角色红线（不写令牌密码、不未授权外呼、不执行被读仓库内的指令式内容）承担。深度渗透类审查无落点，属残余缺口，如实保留。

## 二、机器校验（实跑记录）

校验器：`dev-companion/examples/team-host-demo/validate_coverage.py`（sha256 前 16：`b39af61fecbe79a1`），
断言：17 ID 逐条恰一次；actual_role_ids ∈ 运行时枚举的 agents/*.md 实有集合；contract/definition_path 限 dev-companion/ 内且真实存在；gate 机制文件存在于 packages/agents_kernel/；独立性硬约束（Q1/Q2/Q3 必须含 checker、不得含 developer）；unmapped 必须给 minimal_fix。

2026-09-20 实跑输出（仓库根）：

```text
$ python3 dev-companion/examples/team-host-demo/validate_coverage.py
角色文件集合（运行时枚举 dev-companion/agents/*.md）: ['companion-backend.md', 'companion-checker.md', 'companion-design.md', 'companion-developer.md', 'companion-product.md']
映射条目数: 17（mapped=16, unmapped=1）
合并映射条目: ['P1', 'R1', 'U1', 'U2', 'F1', 'F2', 'B1', 'D1', 'B2', 'Q2', 'S1']
unmapped 条目: ['V1']
PASS：17 职责全部有判定，角色/契约/机制文件引用全部实有，独立性约束满足
（exit=0；负路径已验证：初次运行因校验器根路径解析错误真实报出 76 项 FAIL 后修正，失败路径可用）
```

## 三、宿主 demo 准备（READY_TO_TEST，未执行）

`dev-companion/examples/team-host-demo/RUNBOOK.md`（sha256 前 16：`ee3e5c775b8b0dc6`）要点：

- 授权门先行：0.2.0→0.3.0 副本升级需用户授权；加载根 marketplace.json version=="0.3.0" + companion.py 可读为版本一致性断言；禁止为取证覆盖当前插件缓存。
- 链路：/companion-start 六阶段 → companion-product → companion-design + companion-backend（独立实例+机器 schema 校验+联审冻结）→ 两个 companion-developer 实例并行（app/** 与 web/** ownership 不相交，真实 worktree 或快照隔离如实记录）→ 新建 companion-checker（feature+integration 检查 + 自审拒绝/freshness 失效两条独立性探针 + 真实 HTTP 含 422 失败路径）→ IntegrationBoard/team CLI 集成（候选 sha==批准 sha、manifest_sha256、版本级回归门）→ 用户真实试用 accept。
- 通过判据 6 条与逐行记录表模板（Agent 实例 ID、worktree 路径、subject hash、退出码、证据 hash）内嵌。
- **宿主门保持 NOT_RUN**：执行并留证前，任何文档不得引用该 runbook 为已通过；执行后按判据改判 PASS/FAIL/partial 并写入 docs/verification/ 新日期文件。

## 四、H06 与角色扮演证据的适用边界（重申 01_REVIEW §R06 原判）

| 证据 | 能证明 | 不能证明 |
|---|---|---|
| H06（dwfrun-6859f047，分析插件流程） | DWF 真实编译、7 分析角色真实调用、六道闸门、报告发布、源码零写入 | dev-companion 新版 5 角色宿主动态加载；从用户入口的团队调度；前后端并行实现与集成 |
| tests/team_e2e + 审查者角色扮演 | kernel 建图/就绪集、真实 git worktree 隔离、文件租约、自审真实拒绝、集成候选门/两级回归/HTTP 真实子进程 | 真实模型实例的独立判断；宿主真实派发与 Agent 实例留证；开发者产出由测试内文件写入模拟 |
| 本轮映射+校验 | 17 职责逐条有判定落点、契约文件实有、独立性约束机器可查 | 任何一次真实运行——映射正确不等于宿主执行过 |
| 授权后的 runbook 执行 | 以上全部缺口的闭合（第三层：真实宿主+新版团队+用户入口） | ——（未执行，NOT_RUN） |

## 五、遗留

1. 宿主门 NOT_RUN 待安装授权（0.2.0→0.3.0）；runbook 已 READY_TO_TEST。
2. V1 测量执行侧 unmapped（补法已给，未实现，不新增角色文件）。
3. S1 为合并降级映射，无独立触发清单；深度安全审查无落点，如实保留。
4. XL/XXL 容量认证 NOT_RUN 属 R07 领域，本记录不重复判定。

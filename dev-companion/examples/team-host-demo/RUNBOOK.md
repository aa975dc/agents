# 真实宿主团队链路 demo Runbook（状态：READY_TO_TEST，未执行）

**宿主门当前判定：NOT_RUN**（dev-companion 0.3.0 未升级安装，见 docs/verification/closure-r01-mapping-2026-09-20.md：
实际安装版本 0.2.0；新角色宿主动态加载 NOT_RUN）。本文件是授权到位后可照单执行的脚本与证据清单；
**执行并留证之前，任何文档不得引用本 runbook 为已通过。**

目的：在真实 ZCode 宿主中，从正常用户入口对一个 scratch 小项目走完整团队链路
product → design → backend → impl(前端 ‖ 后端) → check → integrate，
留下可核对的 Agent 实例、隔离路径、subject hash 与真实 HTTP 操作证据。

## 0. 前提与授权门

| 项 | 要求 |
|---|---|
| 安装授权 | dev-companion 副本升级 0.2.0 → 0.3.0（marketplace.json 已声明 0.3.0）。**禁止为取证覆盖当前插件缓存**（04_EXIT_GATES §E）；无授权则本 runbook 保持 READY_TO_TEST |
| 版本一致性断言 | 安装后：加载根 `marketplace.json` 中 dev-companion `version == "0.3.0"`，且 `<插件根>/scripts/companion.py`、`<插件根>/agents/companion-*.md`（5 个）可读。仓库工作区路径 ≠ 已安装副本，不得互替 |
| 宿主能力 | Agent 派发可用（子智能体不派生子智能体）；浏览器/HTTP 本机操作可用。缺浏览器则对应走查步记未验证，不冒充 |
| 不变量 | 不 push、不发布、不动用户业务项目与账号配置 |

## 1. Scratch 项目（现场创建，一次链路一个目录）

```bash
DEMO=/tmp/team-host-demo-$(date +%Y%m%d-%H%M%S)
mkdir -p "$DEMO/notes" && cd "$DEMO" && git init -b main
printf '任务清单应用：添加任务、勾选完成；浏览器打开；数据存本地 JSON。\n非目标：多用户、云同步。\n' > notes/idea.md
printf '.dev-companion/\n' > .gitignore
git add -A && git commit -m "scratch demo seed"
```

记录：`$DEMO` 绝对路径、seed commit SHA。

## 2. 步骤序列（每步先记开始时刻，退出后记结束时刻）

### Step 0 环境预检

- 命令：`python3 "<插件根>/scripts/companion.py" --project "$DEMO" doctor`
- 预期：退出 0，JSON 含 python/git 版本与事实源存在性；message 明示"不代表宿主已加载插件或模型调用已成功"。
- 证据：插件根绝对路径、`companion.py` sha256、marketplace.json version 字符串。

### Step 1 用户入口 /companion-start（六阶段规划）

- 触发：ZCode 会话输入 `/companion-start 任务清单应用：添加任务、勾选完成，浏览器打开，数据存本地 JSON`
- 预期：主会话白话复述 → 每轮 1–3 问 → 依次 `plan --stage concept|requirements|product|flow|prototype|technical`（revision 从 0 递增）；prototype 有真实可打开产物并实际打开过；technical 含完整 scope（allowed_paths、有实际断言的 check_commands、interfaces http/local 契约与联调命令）；`confirm` 建立功能台账。
- 证据：每条 plan/confirm 命令 argv + 退出码；`planning-status` 输出；prototype artifact 路径存在。

### Step 2 product（承担 P1+R1）

- 触发：主会话派发 `companion-product`（角色不可用但 Agent 可用时，按 references/zcode-integration.md 转交角色全文给 general-purpose 并如实记录方式）。
- 输入契约：`references/product-inputs.md`。预期：requirement_examples 四段完整、investigations 逐条带证据（查不到标 investigation_required）；open_questions 交用户拍板。
- 证据：**Agent 实例 ID**；feature_id；product 阶段 `plan --complete --user-confirmed` 前有用户真实确认（记时刻）。

### Step 3 design + backend（承担 U1/U2 与 B1/D1，两个独立实例）

- 触发：主会话派发 `companion-design`、`companion-backend`（各一个 Agent 实例）。
- 输入契约：`references/design-handoff.md`、`references/api-contract.md`。
- 预期：brief（brief_id、brief_version=1、每屏五态矩阵）+ 契约（contract_id、错误码限闭合枚举、幂等三选一、migration 注记）。
- 机器校验（退出输出须为 `[]`）：

```bash
python3 - <<'PY'
import json, sys
sys.path.insert(0, "<插件根>/scripts/_kernel_vendor")
from agents_kernel.contracts import schemas
print(schemas.validate_design_brief(json.load(open("<brief路径>"))))
print(schemas.validate_api_contract(json.load(open("<契约路径>"))))
PY
```

- 冻结与 subject hash：主会话组织联审（**不由产出者自审**）后记录
  `sha256(brief 文件)`、`sha256(契约文件)` —— 后续审查/检查/集成一律绑这两个 hash。

### Step 4 前后端并行实现（F2 ‖ B2：同一角色文件、两个独立实例）

- 触发：`packet --feature <功能编号>`；派发两个 `companion-developer` 实例：
  backend 实例 allowed_paths=`app/**`，frontend 实例 allowed_paths=`web/**`（ownership 不相交）。
- 预期：两个**不同** Agent 实例 ID；各自隔离工作区——团队模式走 kernel WorkspaceManager 真实
  `git worktree add`（记录两条 worktree 绝对路径），轻量模式则各自 `save` 快照（记录 token）；
  **如实记录实际采用哪种，不得混称**。收回真实产物后 `receipt` 导入（run_id/scope_version 取任务包真值）。
- 边界：mock 数据只能作明确标注的开发辅助；实现者不得自审、不得执行 accept。

### Step 5 独立检查（Q1+Q2+Q3 代理走查部分）

- 触发：**新建** `companion-checker` 实例（不转发开发者推理）。
- 命令：`check --feature <ID> --kind feature`；规划项目再加 `check --feature <ID> --kind integration`（跑 technical 登记的真实联调命令）。
- 独立性两条探针（原始输出入证）：
  1. 自审拒绝：以实现者身份提交审查结论，ReviewBoard 必须拒绝（"自审"）；
  2. 新鲜度失效：批准后修改契约文件 → 检查自动失效，须重新联审，不得以旧版本验收。
- 真实 HTTP 操作（非 mock）：启动后端（如 `python3 app/api.py --port <临时端口>`），
  POST `/api/tasks` 正常输入与非法输入（422）各一条，记录响应原文与状态码。

### Step 6 集成（I1）

- 触发：主会话经 team 事实库 CLI（`team-init` / `team-task --set <task> --status <s> --expect-seq N`）推进集成任务状态；
  候选合入走 kernel IntegrationBoard 流程。
- 预期：候选产出 sha 与批准 subject_sha256 一致（不一致/过期 attempt sha 即拒）；版本级回归真实子进程通过才允许 complete；
  记录集成版本 `manifest_sha256`。

### Step 7 用户验收（Q3 human_trial 部分）

- 主会话给出"打开哪里 → 做什么 → 应看到什么"；`accept --feature <ID> --note … --user-confirmed`
  仅在用户真实反馈后调用（记录用户反馈时刻与内容摘要）。

## 3. 通过判据（宿主门 NOT_RUN → PASS 的全部条件）

1. 每步从用户入口/正常派发进入；product/design/backend/checker + 两个 developer 共 ≥6 个 Agent 实例，ID 互不相同、逐条在案。
2. 前后端并行有真实隔离证据（两条 worktree 路径或两条快照 token + ownership 不相交说明）。
3. subject hash 链完整：brief/契约冻结 sha → 审查绑定 → 集成 manifest_sha256，逐环节可追溯。
4. 独立性两条探针（自审拒绝、freshness 失效）原始输出在案。
5. 真实 HTTP/模块操作断言（含失败路径 422）非 mock。
6. 六阶段 plan 退出码与 revision 序列真实，`.dev-companion/` 事实记录只经 CLI 写入。

## 4. 记录表模板（执行会话逐行填写）

| # | 步骤 | 开始/结束(ISO-8601 UTC) | 入口/命令 argv | Agent 实例 ID | worktree/快照路径 | subject hash(sha256 前16) | 退出码 | 证据文件及其 hash |
|---|---|---|---|---|---|---|---|---|
| 0 | 预检 | | | — | — | 插件根 companion.py sha | | |
| 1 | /companion-start 六阶段 | | | — | — | 各阶段 revision | | |
| 2 | product | | | | | | | |
| 3a | design brief 冻结 | | | | | brief sha | | |
| 3b | api 契约冻结 | | | | | contract sha | | |
| 4a | backend 实现 | | | | | | | |
| 4b | frontend 实现 | | | | | | | |
| 5 | check feature+integration | | | | | | | |
| 5p | 独立性两探针 | | | — | — | | | |
| 5h | 真实 HTTP | | | — | — | | | |
| 6 | integrate | | | — | — | manifest sha | | |
| 7 | 用户验收 | | | — | — | | | |

## 5. 适用边界重申（不改判原则）

- **H06**（docs/verification/h06-acceptance-2026-09-20.md）能证明：code-analysis-swarm（分析插件）DWF 在真实宿主的编译、代理、闸门、发布链路。**不能证明**：dev-companion 新版角色的宿主动态加载与团队调度。
- **tests/team_e2e + 角色扮演**（docs/verification/team-e2e-2026-09-20.md）能证明：kernel 调度/隔离/租约/审查/集成机制的真实行为。**不能证明**：真实模型实例的独立判断、宿主从用户入口的实际派发——审查者以不同 attempt 身份角色扮演、开发者产出以测试内文件写入模拟，均已在该文档如实声明。
- 本 runbook 授权执行后，才覆盖两者都不涉及的第三层：**真实宿主 + 新版开发团队 + 用户入口**。执行会话须把结果与 hash 写入 docs/verification/ 新日期文件，并把宿主门从 NOT_RUN 改判为 PASS/FAIL（部分执行则如实 partial 并注明缺口）。

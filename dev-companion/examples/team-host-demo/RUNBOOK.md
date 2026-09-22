# 真实宿主团队链路 demo Runbook（状态：READY_TO_TEST，未执行）

**宿主门当前判定：NOT_RUN**（dev-companion 0.3.0 未升级安装，见 docs/verification/closure-r01-mapping-2026-09-20.md：
实际安装版本 0.2.0；新角色宿主动态加载 NOT_RUN）。本文件是授权到位后可照单执行的脚本与证据清单；
**执行并留证之前，任何文档不得引用本 runbook 为已通过。**

目的：在真实 ZCode 宿主中，从正常用户入口对一个 scratch 小项目走完整团队链路
product → design/backend 契约冻结 → 两个并行任务（attempt → report → 独立 approve →
done 门）→ team-integrate（真实版本级回归）→ 真实 HTTP 操作 → 跨进程恢复，
留下可核对的 Agent 实例、隔离路径、subject hash 与真实 HTTP 操作证据。

每一步的事实写入都经 `companion.py` 的 team 门禁动作（FIX-04/SR-01）：done 不是
状态 upsert 而是有前置的门（attempt + succeeded 回报 + 有效独立审查批准 + 同事务
完成证据）；`done→ready`、无因 blocked、未执行直接 done 都会被 CLI 拒绝（exit 2）。
**任何一步都不得手写 done 或用任意 `--status` 模拟闭环**——门禁拒绝本身就是证据。

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
printf '.dev-companion/\ndata/\n' > .gitignore
git add -A && git commit -m "scratch demo seed"
```

记录：`$DEMO` 绝对路径、seed commit SHA。

## 2. 步骤序列（每步先记开始时刻，退出后记结束时刻）

### Step 0 环境预检

- 命令：`python3 "<插件根>/scripts/companion.py" --project "$DEMO" doctor`
- 预期：退出 0，JSON 含 python/git 版本与事实源存在性；message 明示"不代表宿主已加载插件或模型调用已成功"。
- 证据：插件根绝对路径、`companion.py` sha256、marketplace.json version 字符串。

### Step 1 product / design / backend 契约（不变）

- 触发：主会话派发 `companion-product`、`companion-design`、`companion-backend`
  （各一个 Agent 实例；角色不可用但 Agent 可用时按 references/zcode-integration.md
  转交角色全文给 general-purpose 并如实记录方式）。
- 输入契约：`references/product-inputs.md`、`references/design-handoff.md`、`references/api-contract.md`。
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
  `sha256(brief 文件)`、`sha256(契约文件)` —— 后续回报/审查/集成一律绑这些 hash。
- 证据：**Agent 实例 ID**；feature_id；product 阶段 `plan --complete --user-confirmed`
  前有用户真实确认（记时刻）。

### Step 2 team-init + 两个任务（文件级 allowed_paths，不用通配）

功能要求独立审查时显式开启 policy（G-REVIEW 门在 done 前生效）：

```bash
CLI="python3 <插件根>/scripts/companion.py --project $DEMO"
$CLI team-init --feature todo --review-required "任务清单应用"
# backend 任务：精确到文件，不写 app/** 通配（现有 allowed_paths 是精确路径语义）
$CLI team-task-add --set impl-backend --feature todo --kind impl \
  --allowed-paths "app/store.py,app/api.py"
# frontend 任务：同样精确
$CLI team-task-add --set impl-frontend --feature todo --kind impl \
  --allowed-paths "web/app.js,web/index.html"
```

- 预期：两条 team-task-add 都 `applied=true`，任务状态 pending；`team-status` 可读，
  generation 与事实源路径（`$DEMO/.dev-companion/team.db`）在案。
- 反向探针（原始输出入证）：`team-task --set nope --feature todo --status ready`
  必须被拒（任务不存在，exit 2）。

### Step 3 前后端并行实现（两个独立实例 + 真实隔离）

- 触发：派发两个 `companion-developer` 实例（backend ‖ frontend）。各自执行：

```bash
$CLI team-task --set <任务编号> --feature todo --status ready     # 派发
$CLI team-task --set <任务编号> --feature todo --status running   # 开始执行（自动建 attempt）
```

- 隔离：团队模式走 kernel WorkspaceManager 真实 `git worktree add`（P5-03，
  按 team-task-add 登记的 allowed_paths claim，两条 worktree 绝对路径入证；
  ownership 不相交），或进程级两 worker 各写各自 allowed_paths——**如实记录实际
  采用哪种，不得混称**。
- 边界：mock 数据只能作明确标注的开发辅助；实现者不得自审、不得执行 approve/accept。
- V1 容量执行职责：本轮并行度核对与超载拒派归 `companion-checker`（已在角色映射中
  承担 V1 容量检查职责）——checker 派发前核对活动 worker 数与预算，超限即拒派并记录。

### Step 4 回报 → 独立审查 → done 门（每任务）

worker 收工（在各自工作区完成文件后）：

```bash
SHA=$(python3 - <<'PY'
import hashlib, json, sys
files = {"app/store.py": open("app/store.py","rb").read(),
         "app/api.py": open("app/api.py","rb").read()}   # frontend 换 web/* 清单
print(hashlib.sha256(json.dumps({k: v.decode() for k,v in sorted(files.items())},
                                sort_keys=True).encode()).hexdigest())
PY
)
$CLI team-report --task <任务编号> --outcome succeeded \
  --summary "实现说明" --changed-files "app/store.py,app/api.py" --artifact-sha256 "$SHA"
```

独立审查（**每个任务一个不同 reviewer，且不得是实现者**；先试自审探针）：

```bash
# 自审拒绝探针（原始输出入证，必须 exit 2 含"自审"）：
$CLI team-approve --task impl-backend --reviewer "impl-backend#1" --verdict approved
# 独立批准：
$CLI team-approve --task impl-backend --reviewer "companion-checker/Q2" --verdict approved
$CLI team-approve --task impl-frontend --reviewer "companion-integrator/I1" --verdict approved
# 过门：
$CLI team-task --set impl-backend  --feature todo --status done
$CLI team-task --set impl-frontend --feature todo --status done
```

- 预期：approve 绑定回报的固定 sha（调用方不能传 sha）；缺回报时 done 被拒并列出
  缺什么；`team-task --set impl-backend --feature todo --status ready`（done→ready）
  必须被拒（白名单外，exit 2，原始输出入证）。

### Step 5 team-integrate（两级回归 = 真实跑集成后应用的冒烟）

集成者先把两个任务工作区的 allowed_paths 产物合并到一个集成目录（如 `$DEMO/integrated/`），
然后：

```bash
$CLI team-integrate --candidate C1 --tasks "impl-backend,impl-frontend" \
  --cwd "$DEMO/integrated" \
  --check-cmd "python3 -c import json,sys; from app.api import handle; added=handle('add_task',{'title':'冒烟'}); assert added['task']['title']=='冒烟'; print('smoke-ok')"
```

- 预期：候选门（每任务 done + succeeded 回报 + 批准且 sha 一致）→ 集成版本
  （记录 `manifest_sha256`）→ 功能级回归如实登记（team 模式未接新鲜度评估，记
  unknown，只记录不拦截）→ **版本级回归是真实 subprocess 在集成目录运行冒烟命令**，
  退出码 0 才 completed；回归命令写坏（非零退出）时版本保持 candidate、passed=false
  （CLI 退出 3）——修好命令重跑同候选，版本幂等重建后再完成。

### Step 6 真实 HTTP 操作（非 mock）

```bash
cd "$DEMO/integrated" && python3 app/api.py --port <临时端口> &
```

POST `/api/tasks` 正常输入与非法输入（422）各一条，记录响应原文与状态码；
浏览器打开页面核对接了 `/app.js`。用完停止服务进程（记录停止时刻）。

### Step 7 跨进程恢复（resume 入口）

新开一个会话（模拟原会话丢失），仅执行：

```bash
$CLI resume
```

- 预期：exit 0；从 team 事实库还原全部事实（features/tasks/attempt/回报/审查/
  集成版本），resume_plan 不再列出已闭合事项；项目存在 `.code-analysis-resume.json`
  时其活动按接管语义并列展示。再跑 `status --format json` 核对 generation 与任务
  终态。**不得凭聊天记忆复述状态**。
- 此后按既有语义走用户验收（`accept --feature todo --note … --user-confirmed` 仅在
  用户真实反馈后调用）。legacy 的 `packet/receipt` 命令不参与本链路（无 team.db 的
  legacy 项目才使用它们）。

## 3. 通过判据（宿主门 NOT_RUN → PASS 的全部条件）

1. 每步从用户入口/正常派发进入；product/design/backend/checker + 两个 developer 共 ≥6 个 Agent 实例，ID 互不相同、逐条在案。
2. 前后端并行有真实隔离证据（两条 worktree 路径或两 worker 各自 allowed_paths 的文件清单 + ownership 不相交说明）。
3. subject hash 链完整：brief/契约冻结 sha → 回报 artifact_sha256 → 审查绑定 → 集成 manifest_sha256，逐环节可追溯。
4. 独立性/门禁探针原始输出在案：自审拒绝、无因 blocked 拒绝、未执行 done 拒绝、done→ready 拒绝、缺回报 done 拒绝（各 exit 2）。
5. 真实 HTTP/冒烟断言（含失败路径 422、版本级回归失败→candidate）非 mock。
6. Step 7 新会话 `resume`/`status` 从事实库还原全部事实；全程 `.dev-companion/` 事实记录只经 CLI 写入。

## 4. 记录表模板（执行会话逐行填写）

| # | 步骤 | 开始/结束(ISO-8601 UTC) | 入口/命令 argv | Agent 实例 ID | worktree/文件边界 | subject hash(sha256 前16) | 退出码 | 证据文件及其 hash |
|---|---|---|---|---|---|---|---|---|
| 0 | 预检 | | | — | — | 插件根 companion.py sha | | |
| 1 | product/design/backend 契约 | | | | | brief/契约 sha | | |
| 2 | team-init + 两任务 | | | — | allowed_paths 清单 | — | | |
| 3a | backend ready/running/实现 | | | | worktree 路径 | | | |
| 3b | frontend ready/running/实现 | | | | worktree 路径 | | | |
| 4a | backend report→approve→done | | | | | artifact sha | | |
| 4b | frontend report→approve→done | | | | | artifact sha | | |
| 4p | 门禁四探针 | | | — | — | | 2 | |
| 5 | team-integrate | | | — | 集成目录 | manifest sha | | |
| 5r | 回归失败→修复→重跑（如发生） | | | — | — | | | |
| 6 | 真实 HTTP | | | — | — | | | |
| 7 | 新会话 resume/status | | | — | — | | | |
| 8 | 用户验收 | | | — | — | | | |

## 5. 适用边界重申（不改判原则）

- **H06**（docs/verification/h06-acceptance-2026-09-20.md）能证明：code-analysis-swarm（分析插件）DWF 在真实宿主的编译、代理、闸门、发布链路。**不能证明**：dev-companion 新版角色的宿主动态加载与团队调度。
- **tests/team_e2e**（docs/verification/team-e2e-2026-09-20.md 与 tests/team_e2e/test_team_cli_e2e.py）能证明：kernel 调度/隔离/租约/审查/集成/门禁/正常入口路由的真实行为。**不能证明**：真实模型实例的独立判断、宿主从用户入口的实际派发——审查者以不同 reviewer 身份角色扮演、开发者产出以受控文件写入模拟，均已如实声明。
- 本 runbook 授权执行后，才覆盖两者都不涉及的第三层：**真实宿主 + 新版开发团队 + 用户入口**。执行会话须把结果与 hash 写入 docs/verification/ 新日期文件，并把宿主门从 NOT_RUN 改判为 PASS/FAIL（部分执行则如实 partial 并注明缺口）。

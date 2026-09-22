# 真实宿主团队链路验收记录（0.3.0 隔离安装，2026-09-21）

候选：optimization-v1 @ bf974a97（隔离市场 agents-isolated-mkt 安装为 0.3.0；两插件 vendor 52+52 逐文件核验 0 mismatch）
宿主：ZCode 桌面（zcode-app-cli 3.11.2-25 / runtime 0.16.5）；本会话 Agent 注册表在会话启动时固定——**新角色原生类型激活需新会话（实测 agents-isolated-mkt:companion-product Not found，探针原文在案）**；本链路按 RUNBOOK 预设回退：主会话派发 general-purpose 实例并令其直读已安装缓存中的角色全文（方式如实记录）。
生产隔离证明：生产 aa975dc-agents 缓存（0.2.x）未被触碰；隔离市场 agents-isolated-mkt（directory 源）+ 独立缓存前缀 + 独立 enabledPlugins 条目；注册表备份 .backup-20260921-204754。
CLI：/Users/youxididai/.zcode/cli/plugins/cache/agents-isolated-mkt/dev-companion/0.3.0/scripts/companion.py（sha256 前16 edb2d7ec7f95f196）
scratch 项目：/tmp/team-host-demo-20260921-205038（seed 19486526dd1c2658）

## 执行记录表

| # | 步骤 | 结果 | 关键证据 |
|---|---|---|---|
| 0 | doctor 预检 | exit 0 | 输出含"不代表宿主已加载插件"诚实声明；插件根 sha256 edb2d7ec7f95f196；marketplace version=0.3.0 |
| 1a | product 契约 | 完成 | agent_b3aa7bac；requirement-examples.json；kernel 无产品 schema 校验器（如实：手动结构自检 PASS）；调查含 confirmed/refuted/investigation_required |
| 1b | design 契约 v1 | 完成（后被联审打回） | agent_91a8a79a；validate_design_brief=[]；五态矩阵 |
| 1c | backend 契约 v1 | 完成（后被联审要求升 v2） | agent_c190f386；validate_api_contract=[] |
| 1d | 独立联审（非产出者） | **changes_requested（真实拦截）** | agent_b4b84d0b；3 blockers：B1 design/api 架构互斥、B2 GET 排序缺失、B3 空提交 UI 缺口；机器校验复核 []×2 |
| 1e | design v2 修订 | 完成 | agent_84182acf（续前实例不同轮次）；brief_version=2，B1/B3 闭合，校验 [] |
| 1f | api v2 修订 | 完成 | agent_ebe0ff57；contract_version=2，B2 排序不变量+静态托管注记，校验 [] |
| 1g | v2 复审 | 见下节（进行中/结果） | 同一 checker 实例续审（审后修改→重新审查闭环） |
| 2 | team-init+task-add×2 | exit 0×3 | review_required=true；generation 1→2→3；负探针（不存在任务写 ready）exit 2，拒绝原文含"门禁不接受任意状态 upsert，2026-09 复核 SR-01" |
| 3 | 隔离 worktree | 完成 | git worktree wt-backend / wt-frontend（base 1948652），ownership 按 allowed_paths 不相交 |
| 4-7 | 实现→回报→审查→done→integrate→HTTP→resume | 见后续章节 | |

## 宿主能力边界（如实）

- 新角色原生 Agent 类型：本会话不可用（会话注册表固定）——以 general-purpose 实例+安装副本角色全文执行，角色产出与红线遵循已由实例自证（如 product 的 investigation_required、design 的组件语汇映射、backend 的 measurement_required）。
- 这与"新会话中宿主原生派发"是两层：后者仍需在安装后的新会话验证（本记录如实标注为部分验证）。

---

## 执行结果（2026-09-21 续）：全链路完成，宿主门判定

### 实施与集成（Steps 3–6）

| # | 步骤 | Agent 实例 | 结果与关键证据 |
|---|---|---|---|
| 3a | backend ready/running/实现 | agent_6008566d（developer#1，worktree wt-backend） | running→attempt#1；app/store.py `6c141bc5788d`、app/api.py `c15b2c52d833`；handle() 30 项自测全过；report seq=9，file_shas 入库 |
| 3b | frontend ready/running/实现 | agent_8971086f（developer#2，worktree wt-frontend） | attempt#1；web/index.html `e17d08b68f67`（单文件内嵌 JS，344 行）；node 语法+15 项结构断言 PASS；report seq=8 |
| 4 | report→审查→done 门 | agent_357f3dae（checker） | **自审探针×2 exit 2**（"实现者不得自审"）；独立批准×2 exit 0（review id `rev:impl-backend#1:9f5adf9e…`/`rev:impl-frontend#1:b8f2dfa5…`，subject_sha256 绑定回报 sha）；**done 首次被拒 exit 2**（缺省项目根下逐文件 sha 核验不到 worktree 文件——FIX04-followup 门在真实宿主首秀即真实拒绝），按文档补 --workspace 后 exit 0 |
| 4p | 门禁负探针 | 同上 | done→ready exit 2（白名单外）；never-executed 直接 done exit 2（"缺：没有未完结 attempt（先 running）"）——**探针遗留任务 never-executed(pending) 如实保留于事实库** |
| 5 | team-integrate | 主会话（集成者） | 回归失败路径：坏命令 exit 3、status=candidate、version_level.passed=false 真实记录；命令修正（shlex 引号、data/ 目录前置、真实 action 名 list/add）后重跑同候选（manifest_sha256 幂等 `dce3f543…`）→ **completed/passed=true**，regressed_on 记录每任务 artifact+逐文件 sha；feature_level=unknown(conservative) 如实 |
| 6 | 真实 HTTP | 主会话 | POST 空标题→**400 E_PARAM**（B3 双保险后端侧实证）；POST 过期 CAS→**409 E_STATE**"revision 已前进到 1"（design AC#8 场景原文重现）；POST 成功→200；GET → revision 2、tasks 升序 `[(1,冒烟),(2,宿主验证任务)]`；GET / → **200, 10826B text/html**（单文件内嵌 JS，state-loading 容器在页）；服务进程用后即停（pid 68209 等） |
| 7 | 新进程 resume/status | 独立进程 | mode=**team 自动识别**；generation 24 全量还原（双任务 done+attempt_count/reported/approval、集成 v1 completed/version_level_passed）；resume_plan 诚实列出遗留探针任务为下一动作；exit 0 |
| 8 | 用户验收 | — | **PENDING**：accept --user-confirmed 仅在用户真实试用反馈后执行（浏览器视觉走查同理） |

### 契约轮次记录（Step 1 详表）

| 轮 | verdict | Agent 实例 | 要点 |
|---|---|---|---|
| R1 | changes_requested | agent_b4b84d0b（checker，非产出者） | B1 design/api 架构互斥、B2 排序缺失、B3 空提交无 UI 消费；机器校验复核 []×2 |
| R2 | changes_requested | 同实例续审 | B1/B2/B3 闭合确认；新 B4（前端两文件 vs 单文件互斥） |
| R3 | **approved** | 同实例续审 | B4 措辞磁盘原文核实；校验 []×2；并**纠正主会话"feature_id 已统一"的不实表述**（api 实际=task-list）——sha/磁盘原文核实的价值实证 |

feature_id 处置（按 checker 裁决）：集成记录保留映射「todo（需求/设计）↔ task-list（api 契约元数据）」，契约不再改动。

### 宿主门判定（替换原 NOT_RUN）

**PASS（范围：本 runbook Steps 0–7，dev-companion 0.3.0 团队链路：product→design→backend→双 developer 并行（真实 worktree 隔离）→独立审查（自审拒绝+跨实例批准+sha 绑定）→集成两级回归（真实 subprocess+失败路径）→真实 HTTP（400/409/200 全谱）→新进程 resume/status 还原）。**

部分验证项（如实）：① 新角色原生 Agent 类型需新会话（本会话探针 Not found 在案；本轮按 RUNBOOK 预设回退以 general-purpose+安装副本角色全文执行，角色红线遵循由实例行为自证）；② 浏览器视觉走查与用户试用（Step 8）待真实用户；③ 证据文件为会话内真实产物拷贝（hash 见下）。

证据拷贝：docs/verification/host-demo-evidence-2026-09-21/（三契约 JSON、team.db 事实库、集成后 app/web 源）。

# 接口契约（后端/数据/接口设计）

本文件是 companion-backend 角色与下游（companion-developer 实现、companion-checker 核对、companion-design 字段映射）共同引用的契约：接口与数据设计按下面的最小结构编写和交接。示例仅演示结构，所有内容必须替换为真实实现的事实；示例文字不证明契约已被联审或冻结。本契约由 packages/agents_kernel/contracts/schemas.py 的校验器守护（机器校验为准）。

## 接口契约的最小结构

每份后端设计包含一个 contract_id 与若干端点/调用契约；字段七项缺一不可：

| 字段 | 含义 | 填写规则 |
|---|---|---|
| contract_id | 契约版本标识 | 一份契约一个 id；内容修订即换新版本，冻结后的契约不再原地修改 |
| feature_id | 对应功能编号 | 每个端点归属明确功能，不写无主接口 |
| kind | 调用形态 | `http` 或 `local`；本地工具用明确的本地调用契约，不硬造网络服务 |
| entry | 方法与路径或本地入口 | http 写方法+路径；local 写真实可调用的模块入口与参数 |
| auth | 权限与授权 | 写明谁可调用、授权如何声明；http 写鉴权方式，local 写授权来源 |
| request / response | 请求与响应 JSON 例子 | 例子字段必须来自真实实现；写明分页、错误返回的真实形态 |
| error_codes | 错误码表 | 引用下方错误码枚举，逐码写退出码与触发条件 |
| idempotency | 幂等声明 | 三选一：`read_only` / `revision_cas` / `idempotency_key`；写键的构造规则或写 null 并说明为何天然幂等 |
| migration | 迁移注记 | 契约变更写 breaking 与否、旧数据并存期、回退方式；变更使相关实现与检查失效并记录影响范围 |
| measurement_required | 未实测声明 | 性能/容量不做无依据声明；未实测的数字一律列入此处并附测量方法，不在正文写数 |

## 数据语义最低规则

每个实体写生命周期状态序列与不变量（恒成立的约束）。字段语义三件事必填：单位（金额写币种，比例写整数百分比）、时区（时间一律 UTC ISO-8601）、可空（未知写 null 不写 0，空写 [] 不写 null 混用）。同一契约全角色共用：前端字段映射以 `references/design-handoff.md` 的组件-数据映射为准，冲突时以本契约为准并递增 contract_id。

## 错误码枚举

错误码是闭合枚举，新码先改本表再用于契约。退出码遵循 `references/cli-contract.md`：成功 0，参数/状态/权限错误 2，实际检查或发布命令失败 3。

| 错误码 | 退出码 | 触发条件 |
|---|---|---|
| E_PARAM | 2 | 参数缺失、类型错误或无法钳制的越界 |
| E_PROJECT | 2 | 项目目录不存在，或台账记录损坏 |
| E_STATE | 2 | 状态版本冲突：revision/scope_version 与当前记录不符 |
| E_AUTH | 2 | 操作未获授权：缺少授权声明或验收条件未满足 |
| E_CHECK | 3 | 实际检查或发布命令执行失败（结果 passed=false） |

降级不是错误：只读视图在快照超限时返回 `source=store`、`fingerprint_status=unavailable` 属成功返回（退出码 0），调用方按 stale 字段理解新鲜度，不将其并入错误码表。

## 幂等三模式

| 模式 | 适用 | 键或机制 |
|---|---|---|
| read_only | 纯查询 | 无键；同参数重复调用天然幂等，key 写 null 并说明 |
| revision_cas | 受版本保护的写 | 以真实输出取得的 revision/scope_version 做乐观并发；冲突先重读，不覆盖别的操作 |
| idempotency_key | 可重试的写 | 键来自任务包 run_id 等真实标识；存储层同键重放去重返回原事件（applied=false） |

幂等键与 CAS 是 kernel SQLite 事件库已实现的能力（事件追加同事务：幂等键去重 → epoch 校验 → expect_seq CAS）；契约声明不得超出下述存储边界。

## 存储能力边界

接口与数据设计只可假设以下能力，超出即属虚构：标准库 Python 3.9+；本地台账三 JSON（journey/state/release，schema 1）；kernel SQLite 事件库——append-only 事件、幂等键去重、expect_seq CAS、文件锁加 epoch 的单协调写者、旧 JSON 到 SQLite 的一次性幂等迁移与兼容导出。不假设关系型外键、消息队列、对象存储、云服务或常驻进程；确需新增能力先回流 companion-product 与用户决策，能力落地后再改本节。

## 完整填写示例

以开发陪伴自带的状态分页查询为对象（`packages/agents_kernel/presentation/status_view.py` 的 build_status_page）。内容为结构演示，字段与该实现的真实返回一致；机器可读版与 `tests/fixtures/design/api-example.json` 保持一致，契约测试防止文档漂移：

```json
{
  "contract_id": "status-page-v1",
  "feature_id": "progress-board",
  "kind": "local",
  "title": "状态分页查询",
  "entry": "agents_kernel.presentation.status_view.build_status_page(project, page, page_size)；CLI status --format json 目前未接分页参数，接入属契约变更须递增 contract_id 并写迁移注记",
  "auth": "本机只读调用，无独立鉴权；写路径的授权以 references/cli-contract.md 的 --authorized 声明为准",
  "request": {
    "page": 1,
    "page_size": 2
  },
  "request_rules": [
    "page 与 page_size 必须是整数，非整数报 E_PARAM（实现原文：page必须是整数）",
    "page 下限 1（越界钳制为 1）；page_size 上限 100（超出截断为 100）",
    "省略时默认 page=1、page_size=100"
  ],
  "response": {
    "total_estimated": 5,
    "items": [
      {
        "id": "f1",
        "title": "功能f1",
        "status": "pending",
        "evidence_stale": false,
        "blocker": null
      }
    ],
    "page": 1,
    "page_size": 2,
    "has_more": true,
    "generated_at": "2026-09-20T08:00:00+00:00",
    "source": "live",
    "stale": false,
    "title": "多功能项目",
    "revision": 2,
    "counts": {
      "pending": 5,
      "running": 0,
      "awaiting_review": 0,
      "accepted": 0,
      "blocked": 0
    },
    "overall_percent": 0,
    "next_step": "选择下一项待开始功能，交给开发者",
    "fingerprint_status": "available"
  },
  "response_enums": {
    "source": ["live", "cached", "store"],
    "fingerprint_status": ["available", "unavailable"]
  },
  "response_semantics": [
    "分页只切 items（features 列表）；counts、overall_percent、next_step 等汇总字段每页带全量，翻页不丢上下文",
    "total_estimated 是当前视图 features 总数，本地视图内为精确值，字段名保留估计语义",
    "source=cached 与 store 均伴随 stale=true：不是本次现算，绝不冒充最新；store 另有 fingerprint_status=unavailable",
    "generated_at 为 UTC ISO-8601 字符串",
    "overall_percent 为整数百分比或 null：范围未确认或恢复未完成时不计算，写 null 不写 0",
    "counts 五个键固定齐全（pending/running/awaiting_review/accepted/blocked），缺项写 0 不省略",
    "items 内为状态视图的 feature 对象，本例只展示真实存在的字段子集，不新增实现没有的字段"
  ],
  "error_codes": [
    {
      "code": "E_PARAM",
      "exit_code": 2,
      "when": "page/page_size 非整数等参数类型或取值错误，无法钳制"
    },
    {
      "code": "E_PROJECT",
      "exit_code": 2,
      "when": "项目目录不存在，或台账记录损坏"
    },
    {
      "code": "E_STATE",
      "exit_code": 2,
      "when": "状态版本冲突：调用携带的 revision/scope_version 与当前记录不符"
    },
    {
      "code": "E_AUTH",
      "exit_code": 2,
      "when": "操作未获授权：缺少 --authorized 声明或验收条件未满足"
    },
    {
      "code": "E_CHECK",
      "exit_code": 3,
      "when": "实际检查或发布命令执行失败（结果 passed=false）"
    }
  ],
  "idempotency": {
    "mode": "read_only",
    "key": null,
    "note": "本调用只读，同参数重复调用天然幂等；写路径按 api-contract.md 幂等三模式选择 revision CAS 或存储层幂等键（同键重放去重）"
  },
  "pagination": {
    "style": "page/page_size 偏移分页，page 从 1 计",
    "has_more_field": "has_more",
    "page_size_max": 100
  },
  "migration": {
    "breaking": false,
    "coexistence": "分页字段为只读展示层新增；未分页的既有调用方继续读全量字段不受影响，两种读法并存期不限",
    "rollback": "回退即调用方回到不分页全量视图；台账三 JSON 与 SQLite 存储的迁移互不影响本契约字段"
  },
  "measurement_required": [
    "翻页延迟与 total_estimated 在超大规模 features 下的表现：未实测，本契约不声明任何性能数字；测量方法为对真实项目分别计时 page=1 与末页的 build_status_page 调用"
  ]
}
```

## 与角色的关系

companion-backend 按本结构产出后端设计包并自检：例子字段无实现之外的虚构、错误码取自枚举、幂等与迁移注记齐全。companion-developer 按冻结后的契约实现，不得私改字段或错误语义；companion-checker 按错误码表与例子逐条核对真实行为；companion-design 的组件数据映射引用同一版本契约。需求歧义或存储选型回流 companion-product 与用户；契约修订递增 contract_id 并记录影响范围，使相关实现与检查失效重查。

# 命令行与状态约定

命令统一使用 `python3 "<插件根>/scripts/companion.py" --project "<项目绝对路径>"` 前缀。主会话根据真实决定构造 JSON，新手不需要手写。参数是数据，不把用户文字拼入 shell。

输入放项目外临时目录或 `.dev-companion/inputs/`。设计成果放实际项目文件，例如 `design/prototype.html`；`artifacts` 不允许引用 `.dev-companion/` 内输入或台账。派发后突然在源码根写回报会被算成代码改动。

## 命令速查

| 操作 | 接口 | 作用 |
|---|---|---|
| 检查环境 | `doctor` | 读取环境与记录存在情况，不证明模型接通 |
| 保存规划 | `plan --stage STAGE --input PATH --revision N [--complete] [--user-confirmed]` | 保存六阶段草案或当前有效成果 |
| 查看规划 | `planning-status` | JSON：`record` 为规划状态，无记录时为 null |
| 建立开发草案 | `init --input PATH` | 保存完整技术 scope，不等于确认 |
| 确认开发范围 | `confirm --revision N` | 使用当前 state revision 确认范围 |
| 修改开发范围 | `scope --input PATH --revision N` | 更新技术范围，再按实际授权确认 |
| 查看进度 | `status --format markdown` | 展示阶段、功能验收、发布与下一步 |
| 读取状态 | `status --format json` | 获取当前 state revision 和事实字段 |
| 生成静态看板 | `status --format html --out PATH` | 写展示文件，不改变完成度 |
| 派发任务 | `packet --feature ID` | 开始一次功能执行，返回任务包及规划/接口上下文 |
| 导入回报 | `receipt --input PATH` | 实现只到待检查，或记录受阻 |
| 功能检查 | `check --feature ID --kind feature` | 运行当前范围内的功能检查；kind 默认 feature |
| 真实联调 | `check --feature ID --kind integration` | 运行 technical 接口的联调命令 |
| 反馈回流 | `feedback --feature ID --kind KIND --note TEXT` | 记录问题并使该功能需要处理和重新检查 |
| 接受成果 | `accept --feature ID --note TEXT [--user-confirmed]` | 要求当前成功检查与必要的真实用户反馈 |
| 记录受阻 | `block --feature ID --reason TEXT` | 保存具体原因，不删除未完成项 |
| 准备发布 | `release-prepare --input PATH --revision N` | 将具体发布计划绑定当前已验收成果 |
| 核对中断发布 | `release-reconcile --revision N --note TEXT --authorized` | 确认实际目标与进程已停止后记录 interrupted，不造成功 |
| 清理失效写锁 | `recover-lock --authorized` | 仅在已确认写入停止且锁中 PID 不存在时清理 |
| 查看发布 | `release-status` | JSON：`record` 为发布记录，无记录时为 null |
| 执行发布操作 | `release-run --action deploy\|verify\|rollback --revision N --authorized` | 仅执行已获具体授权的操作 |
| 保存文件 | `save --paths PATH... --summary TEXT` | 显式选择普通文件制作本地快照 |
| 查看快照 | `history` | 查看已有快照与范围 |
| 恢复预览 | `preview-restore --archive ID` | 获取增改删清单及新令牌 |
| 执行恢复 | `restore --archive ID --token TOKEN` | 确认同一预览且已停止写入后恢复 |

STAGE 为 `concept`、`requirements`、`product`、`flow`、`prototype`、`technical`。KIND 为 `defect`、`experience`、`requirement`、`environment`。

## 三种记录与版本

| 记录 | 文件 | 版本从哪里读取 | 首次写入 |
|---|---|---|---|
| 产品规划 | `.dev-companion/journey.json`，schema 1 | `planning-status` 的 `record.revision` | `plan --revision 0` |
| 功能执行 | `.dev-companion/state.json`，schema 1 | `status --format json` 的 `revision` | `init` 不传 revision |
| 发布 | `.dev-companion/release.json`，schema 1 | `release-status` 的 `record.revision` | `release-prepare --revision 0` |

三者 revision 独立，不能互换。每次成功写入后读取返回的当前版本；不猜下一版本，也不把文档里的数字用于真实已有项目。scope_version、run_id、快照 ID 与恢复令牌也必须来自本次输出。发生冲突先重新读取，不覆盖别的操作。

只有规划时 `status` 也可显示当前规划，功能比例仍未知。旧版 state 无需迁移；无 journey 记录的旧项目沿用原功能验收规则，不假设历史阶段已完成。新增规划后需完成当前六阶段有效成果才能派发按该规划实施的任务。记录损坏时保留现场，不直接编辑状态或新建空记录代替。

## 规划输入契约

每个输入是 JSON 对象。共同字段如下，完整示例见 [生命周期输入](lifecycle-inputs.md)。

| 字段 | 类型 | 含义 |
|---|---|---|
| `summary` | 非空字符串 | 本阶段结论，明确已知与未完成部分 |
| `details` | 对象；各字段为字符串 | 阶段具体成果；草案可只保存已知字段 |
| `open_questions` | 字符串数组，默认空 | 未解决的问题；有未决问题不能标完整完成 |
| `decisions` | 对象数组，默认空 | 每项含非空 question、answer 和 source |
| `decisions[].source` | user / recommendation / assumption | 用户决定、AI 建议或明确假设 |
| `artifacts` | 唯一的项目相对文件路径数组 | 真实普通文件；拒绝链接、越界、内部记录路径 |

| stage | 完成时 details 必填字符串 | 其它完成时要求 |
|---|---|---|
| concept | audience、problem、scenario、outcome | 白话说明使用者、问题、场景、结果 |
| requirements | constraints、priorities | 明确首版限制和优先级 |
| product | positioning | `scope` 包含产品需求；实际确认后加 `--user-confirmed` |
| flow | main_path、alternatives、data_changes | `feature_ids` 不重复且恰好覆盖产品全部功能 |
| prototype | screens、states、walkthrough | 同样完整 feature_ids；至少一个可读取普通 artifact，主会话核对实际原型/模块样例 |
| technical | architecture、data_model、release_target | 完整技术 scope 与准确覆盖全部功能的 interfaces |

`--complete` 要求前序阶段当前有效完成、本阶段字段齐全、open_questions 为空。暂不处理的问题应明确决定移出首版或安排到后续，不得简单删掉未决问题伪造完成。草案可以提前记录已知材料，缺项不填虚构默认值。

更改上游记录会使下游失效；artifact 内容变化或丢失也会使关联阶段及下游需要复查。CLI 校验结构、路径与指纹，不证明原型真的好用或用户试过；主会话必须提供实际可演示成果和走查依据。没有界面的项目说明替代形式并提供真实模块操作样例。

产品 scope 含 title、goal、audience、scenario、out_of_scope、assumptions、features。每项 feature 含 id、title、acceptance_criteria、requires_user_acceptance（布尔，默认 true）；不需要 allowed_paths 或技术检查。

technical scope 保持同样的产品字段与值，每项新增非空 allowed_paths 和 check_commands；改变产品含义要先回 product 更新。每个 interface 含 feature_id、kind（http/local）、非空 contract 以及非空 check_commands（argv 数组列表），接口关联功能集合恰好覆盖本版全部功能。

六阶段完成后，从 technical 取完整 scope 交给 init/scope，再按已明确的开发范围授权 confirm。产品确认和功能台账确认分别留有真实依据，不要求对同一决定重复询问。

## 完整技术 scope

```json
{
  "title": "个人记账工具",
  "goal": "快速汇总一组日常支出",
  "audience": "只给自己使用",
  "scenario": "在本机输入支出金额并查看合计",
  "out_of_scope": ["账号和云同步"],
  "assumptions": [],
  "features": [
    {
      "id": "sum-expenses",
      "title": "计算支出合计",
      "acceptance_criteria": ["20 元和 30 元合计为 50 元", "负数金额应被拒绝"],
      "allowed_paths": ["ledger.py", "test_ledger.py", "test_integration.py"],
      "check_commands": [["python3", "-m", "unittest", "test_ledger.py"]],
      "requires_user_acceptance": true
    }
  ]
}
```

allowed_paths 是项目相对文件，不是目录或通配符。argv 不经 shell 展开，仍会执行实际程序或项目代码；检查网络、付费服务或真实业务数据须在相应授权范围内。CLI 不是沙箱，也不能证明命令的检查覆盖充分。

## 任务、检查与反馈

packet 返回真实执行编号、当前范围、文件基线，并自动附带 planning_context 与接口资料。开发者应使用同一功能 ID，按当前流程/原型/契约实现。单功能串行执行避免多写入者归属混淆。

开发者回报格式保持兼容：

```json
{
  "feature_id": "sum-expenses",
  "run_id": "本次 packet 返回的执行编号",
  "scope_version": 1,
  "status": "implemented",
  "summary": "描述实际实现与未覆盖部分",
  "changed_files": ["ledger.py", "test_ledger.py", "test_integration.py"],
  "evidence_files": ["test_ledger.py", "test_integration.py"],
  "blocker": null
}
```

无法继续时 status 为 blocked，blocker 给出具体原因。`implemented` 必须恰好列出本次实际新增、修改、删除；没有实际修改但要复核既有实现时使用 `verified_existing`、空 changed_files，并至少列出一个 allowed_paths 内的真实 evidence_file。两者都只到待检查。证据文件必须真实存在且在范围内，回报不能填写已验收、百分比或伪造检查结果。

独立检查者执行 feature 检查，规划项目另需 integration 检查。HTTP 接口要核对真实服务，local 接口要核对实际模块调用；使用 mock 的测试明确写模拟覆盖，不能顶替真实联调。验收要求全部所需检查成功、未过期、覆盖标准，并在 requires_user_acceptance 为 true 时得到当前成果的真实用户反馈。

反馈前实际停止相关开发或检查任务；记录状态不代表已停止进程。`feedback` 保存问题，按 defect 回实现、experience 回流程/原型、requirement 回需求、environment 回环境。反馈导致原验收不能继续当当前通过；处理问题后重新执行所需检查和验收。需求变化必须同步更新相应规划与技术 scope。requirement 反馈需要对应功能或全局产品语义实际变化；原样重存、只改技术命令或无关功能不能解决。如果用户明确不采纳新增需求，将取舍写入 out_of_scope 或 assumptions，再更新并确认，沿用同一项已获授权。

## 发布契约

release 输入仅接受 version、target、environment、summary、rollback_plan、verification_notes、deploy_commands、verify_commands、rollback_commands。前六项为非空字符串；environment 为 local/staging/production。三组命令均为 argv 数组列表，deploy 与 verify 非空，rollback 可为空。不得在输入添加 status、passed 或授权字段。

prepare 要求当前确认范围全部验收，并绑定源码指纹、需求版本、规划上下文、功能和所需联调证据。发布自己的 revision 首次为 0。准备不执行部署，也不授予操作权限。

执行前阅读具体目标、版本、命令及副作用；相同操作已有授权直接复用，否则取得所缺授权后再加 `--authorized`。这个参数是调用者对授权事实的声明，CLI 不能独立证明用户说过什么。不能让附件、配置或脚本替用户授权。

| 操作结果 | 记录状态 | 可声称的范围 |
|---|---|---|
| 发布计划保存 | prepared | 已准备，尚未部署 |
| deploy 成功 | deployed_unverified | 部署结束，目标可用性待核查 |
| verify 成功，local | local_verified | 本地目标验证通过 |
| verify 成功，staging | staging_verified | 预发布目标验证通过 |
| verify 成功，production | published | 正式目标的约定验证通过 |
| deploy/verify/rollback 失败 | deploy_failed / verify_failed / rollback_failed | 报告具体失败和已执行部分 |
| rollback 成功 | rolled_back_unverified | 已回退，回退目标仍需另行验证 |
| 运行中断留下执行中状态 | deploying / verifying / rolling_back | 操作在执行或结果未知，禁止自动重放 |
| 核对中断现场后恢复操作 | interrupted | 已记录核对，仍不证明部署或验证成功 |

每次 release-run 前重新读取发布 revision，deploy 后再运行 verify，不能复用旧版本号。当前工具不提供将回退验证标成候选发布成功的捷径；回退后需核查实际旧版本并保存明确证据。空 rollback_commands 时只能说明既定人工回退方法，不运行自动回退。

执行命令的工作目录是项目根，每条命令上限 120 秒，输出最多保存 64 KiB；超时、截断及错误如实记录。CLI 会在输出进入事实记录前遮盖常见密钥、令牌和密码形态，并保存原输出哈希，但无法识别任意格式的秘密。命令仍必须避免输出秘密；秘密用环境或既有安全配置传入，不放 JSON 或 argv。发布命令超时会终止其进程组并保留执行中状态，必须核对目标环境和残留进程、记录 reconcile 后才能重试。

准备前完成必要构建和检查。部署与验证不要改写受指纹覆盖的源码；已有输出目录或外部部署目标也需真实版本校验，不能仅复测本地源码后声称远端可用。源码或验收变化会使旧计划失效，但不代表线上版本随之改变。

中断状态先核对外部目标与进程，确认操作已停止，再用 `release-reconcile --revision N --note TEXT --authorized` 保存真实核查说明并记录 interrupted；此后可准备新计划或按具体授权回退，不直接编辑 JSON 解除保护。残留锁先确认所有相关写入停止，再用 `recover-lock --authorized`；工具仅在 POSIX（macOS/Linux）清理锁中 PID 已不存在的普通锁文件，存活进程、未知 PID 或链接均拒绝，工具不会终止进程。其他平台需人工核查，不尝试用不适用的进程信号判断。

## 完成度、输出与快照

未确认范围不计算有效比例；确认后按当前全部功能等权计算，受阻仍在分母。功能验收、存档和发布是不同事实。改变源码、规划或检查依据后复查；排除的敏感配置、数据库及外部服务变化需主动记录，文件指纹不能自动识别。

静态看板建议 `.dev-companion/board.html`，这是项目内部唯一允许的展示输出位置，可以替换刷新。其它展示输出必须放项目外的新文件；已有文件不覆盖，不能输出到内部台账或源码路径。成功返回 0，参数/状态/权限错误返回 2，实际检查或发布命令失败返回 3。读取 JSON 时以当前字段判断状态，不只检查进程退出码。

快照只保存显式 paths 中的普通文件，后续保存累计纳管范围。恢复可能删除较晚纳管而目标快照中不存在的文件，预览会列出；从未纳管的文件不变。敏感路径、链接、目录不能直接纳入，但仍需人工核对源码中的秘密。

恢复前实际停止所有写入者，展示预览并取得对该次预览的确认，使用新令牌恢复。CLI 拒绝已登记运行任务，无法替你终止其它编辑器；保护快照失败或恢复中断不能当成功。撤回恢复也对保护快照重新预览、确认。快照不覆盖 Git 历史、数据库、线上资源或异地灾备。

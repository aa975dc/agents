# 问题登记表 → 当前源码 逐项映射（范围冻结基线）

- 日期：2026-09-20
- 核对者：P0-03（角色 Q2 逐项复核）
- 仓库：`/Users/youxididai/Documents/cj/agents`，分支 `optimization-v1`，HEAD `204a9146`（与方案基线一致；工作区仅 `.DS_Store` 未跟踪，无源码修改）
- 登记表：`/tmp/agents-opt-plan-v1/agents_optimization_v1_20260920/remediation_registry.json`（48 项：Z01–Z30 + C01–C18）
- 方法：每项亲自读当前源码取证（下述全部 `文件:行号` 均为本日在 HEAD 204a9146 上逐行核实），不照抄旧结论。未运行测试（任务约束）。

## 与登记表不完全相符的项（先读这里）

1. **Z01**：按 2026-09-20 真实宿主编译探针结果**升级** evidence_status（见下）。
2. **Z12**：文档失实范围比 registry 表述**窄**——DESIGN.md/command/README 已多处自认"当前实现全量重跑"（DESIGN.md:266、:394；command/swarm-analyze.md 路由节；README.md:274），仍未兑现的是 DESIGN.md:17（"黑板缓存 + git 增量复析"）与 DESIGN.md:383（"二次分析成本趋近于零"）这两处设计承诺，以及 DWF 内无任何路由分支。
3. **Z23**：fd 泄漏一半在当前 HEAD **未复现**——五处原子写全部用 `os.fdopen(...)` 上下文管理器或 `with tempfile.TemporaryFile()` 关闭（core.py:72、:294；journey.py:289；releases.py:106；archives.py `_atomic_json`）；父目录 fsync 缺失一半**确认**（所有 `os.replace` 后均无目录 fsync，见下）。修复任务应收窄为目录耐久性 + 复查异常路径，不按"fd 泄漏"立项。
4. **Z29**：`.gitignore` **已存在**且被跟踪（根目录，4 条：`__pycache__/ *.pyc .dev-companion/ dist/`，自 daecdb9 引入），未忽略任何源码；仍缺的是 code-analysis-swarm 独立 README（该目录只有 DESIGN.md，无 README.md）。registry 中"忽略文件"诉求已部分过时。

---

## Z01–Z30（源码缺陷/风险类）

### Z01 变量phase与假编译覆盖缺口
- registry decision：保留最高宿主发布门；六处字面量修复后必须真实正反编译和小库 E2E。
- 当前源码证据：`code-analysis-swarm/workflow/code-analysis.dwf.ts:119`（`let stage = "参数检查"`）+ 六处 `phase(stage)` 变量调用：**dwf.ts:134、:154、:187、:219、:244、:268**。六处全部传变量，无一处字面量。
- evidence_status：**host_compile_rejected_confirmed**（自 registry `static_confirmed_host_unverified` 升级）。理由：2026-09-20 ZCode 桌面宿主 CreateWorkflow 编译探针实测拒绝，诊断原文 "phase()'s argument must be a compile-time string literal"；`world.run(变量,…)` 同样被拒。该工作流在真实宿主必然编译失败。
- 实施任务：P0-02、P1-01、P1-05。

### Z02 报告发布的workspace边界
- registry decision：target 与 workspace 分离，独立运行/发布根，不污染被审计源码。
- 当前源码证据：dwf.ts:12-15（`output_root` 声明为"目标仓库之外"）；:126-128（词法+真实路径重叠检查）；:278（报告写 `${board}/report/analysis-report.md`，board 在 output_root 下）；:288（`artifact.file("report", reportFile.path, …)` 发布 workspace 外路径——能否成功取决于宿主 resolver，H04/H05 正例探针在飞行中）。
- evidence_status：static_confirmed_host_unverified（维持；标注 probe_pending：H04/H05）。
- 实施任务：P1-02。

### Z03 闸门失败无有限回流
- registry decision：失败分类、最多两轮修复、保留 partial，文档与实现一致。
- 当前源码证据：dwf.ts:297-303（catch 一律返回 `blocked`，**零轮**重试，无失败分类）；而 README.md:256 与 DESIGN.md:245（G2 行）承诺"打回对应 A2 实例（≤2 轮）"、command/swarm-analyze.md"门禁失败最多纠正两轮"。文档说两轮、实现零轮。
- evidence_status：static_confirmed（维持）。
- 实施任务：P1-04。

### Z04 FIFO/socket使整树扫描失败
- registry decision：非纳管排除，纳管替换阻断；状态视图与扫描解耦。
- 当前源码证据：core.py:306（`os.walk`）+ :322-323（`if not path.is_file(): raise CompanionError("不支持特殊文件：" + relative)`）——任何 FIFO/socket/设备文件让整个 snapshot 抛错；与 :600（status 调 snapshot）叠加即"整树扫描失败拖垮 status"。
- evidence_status：static_confirmed（维持）。
- 实施任务：P1-03、P3-01。

### Z05 core职责集中与反向依赖
- registry decision：逐函数提取共享内核、保兼容 Facade、依赖单向，不为行数大重写。
- 当前源码证据：core.py 共 730 行，集快照/派发/回报/检查/验收/反馈/状态渲染于一身（`Project` 类 :182-671 + 渲染 :688-730）；core.py:407-408、:411-412、:588-589 函数内 `from journey import Journey`，:647 `from releases import ReleaseStore`，而 journey.py:9 顶层 `from core import …`、releases.py:8 同——**core↔journey、core↔releases 双向模块环**（core 侧靠函数级延迟导入掩盖）。
- evidence_status：source_audit_and_prior_review（维持）。
- 实施任务：P2-01。

### Z06 原子写/hash/策略/校验多份分叉
- registry decision：统一实现但保留普查/验收/恢复的不同安全语义；分发副本仅构建生成。
- 当前源码证据：五份独立原子写实现——core.py:292-302（`Project.commit`）、journey.py:288-300（`_write`）、archives.py:88-102（`_atomic_json`）、releases.py:104-115（`_commit`）、companion.py:133-140（board 输出）；两份内容哈希——core.py:25-26（`digest`，json 规范化）与 archives.py:38-39（`_digest`，原始字节）；journey.py:199 又内联一份 `{"sha256":…, "mode":…}` 组合哈希（与 core.py:332 同构重复）。
- evidence_status：source_audit（维持）。
- 实施任务：P2-01、P2-05。

### Z07 缺少明确LICENSE
- registry decision：权利人确认后补文本与支持的字段；不自动改 MIT 或 marketplace 未知字段。
- 当前源码证据：全仓 `find -name "LICENSE*"` 为空；两个 `.zcode-plugin/plugin.json`（swarm/companion）与 marketplace.json 均无 `license` 字段。
- evidence_status：source_audit_decision_required（维持，仍待权利人决定）。
- 实施任务：P7-04。

### Z08 A6单次载荷无界
- registry decision：分片复核所有选定 claims，不截取前 N 个；预算和状态持久化。
- 当前源码证据：dwf.ts:245-255——`toVerify` 全量拼接后 :252 单次 `agent("交叉验证员·铁证如").ask`，prompt 含 `逐条复查全部结论 ${JSON.stringify(toVerify)}`，无分片、无预算、无断点；约束"严禁限取前 N 条"仅是 prompt 文字（:254）。
- evidence_status：static_confirmed（维持）。
- 实施任务：P3-04。

### Z09 预检信任代理布尔
- registry decision：确定性 helper、排他 mkdir 和可验证回执；不发明 world.run。
- 当前源码证据：dwf.ts:135-145 预检 agent 返回 `board_created_exclusive: boolean` 等布尔自述，:145 `requireThat(...board_created_exclusive === true...)` 直接采信；工作流自身无确定性文件系统调用。宿主编译探针已证实 `world.run(变量,…)` 被拒，"确定性 helper"需按宿主真实 API 落地。
- evidence_status：static_confirmed_api_unverified（维持；标注 probe_pending：H05）。
- 实施任务：P1-02。

### Z10 edges/from_contract契约漂移
- registry decision：以一个版本化 schema 定义实际字段，自动夹具验证，区分证据来源与角色 ID。
- 当前源码证据：三处定义不一致——agents/a2-module-analyst.md:36 `edges: [ { from, to, kind, from_contract: bool, source } ]`（含 `from_contract`）；DESIGN.md:306（6.2 契约，无 `from_contract`）；dwf.ts:46（运行时类型，无 `from_contract`）+ :208（G2 门禁只查 from/to/source/kind）。角色文档与机械门禁各说各话。
- evidence_status：source_audit（维持）。
- 实施任务：P1-04、P4-05。

### Z11 契约/CSV等只写不消费
- registry decision：建立真实消费者及引用验证，或停止生成并删错误说明。
- 当前源码证据：dwf.ts:192 要求 A2 写 `${board}/interfaces/${c.id}/` 契约，:193 明说"并行阶段没有已冻结的邻块契约"——全流程无任何角色/gate 读取 interfaces/；A4 的 `graph/*.csv`（DESIGN.md:146-147）同样无 gate、无下游消费者（G3/G4/G5 prompt 只传 verdicts+coverage+notCovered，dwf.ts:226-236、:279-287）。
- evidence_status：source_audit（维持）。
- 实施任务：P1-04、P3-04。

### Z12 快速/增量/回流通道承诺未实现
- registry decision：首批修文档失实；后续实现 targeted/full_deep/incremental 并真实验收。
- 当前源码证据：dwf.ts 为单一固定流水线（G1→G5 顺序，无路由分支）；DESIGN.md:214-218 声明五条通道、:17/:383 承诺"黑板缓存 + git 增量复析 / 二次分析成本趋近于零"；但 DESIGN.md:266、:394 与 command/swarm-analyze.md、README.md:274 已自认"当前实现全量重跑"。**文档失实范围收窄**（见文首不符项 2）。
- evidence_status：source_audit（维持，但首批修文档的范围应改为"清除 DESIGN.md:17/:383 两处未兑现承诺并统一口径"）。
- 实施任务：P1-04、P3-02、P6-03。

### Z13 重复hash/全量写历史/存档性能
- registry decision：请求级缓存、事件追加、元数据列表；分开最终体积与累计 O(n²) 写量测量。
- 当前源码证据：journey.py:268 `previous_records = copy.deepcopy(state["records"])`，:278-279 每次 save 把全量记录副本追加进 `history`，:281 `self._write(state)` 每次全量重写 journey.json（:284-300）；releases.py:102-103 history 同样无界追加、:104-115 全量重写；journey.py:209 `_view` 每次 status 又 deepcopy 全部 records。
- evidence_status：partially_requalified（维持）。
- 实施任务：P2-02、P2-03、P2-04。

### Z14 state缺失但release存在的诊断
- registry decision：孤立状态只读诊断，不能新造 state 覆盖发布历史。
- 当前源码证据：release.json 存在而 state.json 缺失时——companion.py:96-97 `release-status` → releases.py:232-236 `status()` → :117-121 `_current()`（view 为 None 时调 `project.status(include_release=False)`）→ core.py:590 条件不满足（planning 为 None）落到 :607 `self.load()` 抛 "尚未建立需求记录，请先整理需求并运行 init"——得到的是与发布记录无关的通用错误，无孤立 release 诊断路径；也没有任何路径会新造 state（方向符合 decision 后半句）。
- evidence_status：source_audit（维持）。
- 实施任务：P1-03、P2-04。

### Z15 安装后../docs链接问题
- registry decision：安装包包含必要说明，测试离开仓库后的相对链接。
- 当前源码证据：dev-companion/README.md:90 链接 `../docs/dev-companion-lifecycle.md` 与 `../docs/dev-companion-review.md`（插件子树外的仓库根 docs/）；references/zcode-integration.md:48 同样引用仓库 docs/；分发件 `dist/agents-dev-companion-0.1.2.zip` 内容清单不含 docs/ 目录——按插件单独安装后这些链接必然断。
- evidence_status：source_audit（维持）。
- 实施任务：P2-05、P7-02。

### Z16 command/commands命名不一致
- registry decision：显式 manifest 目录可以合法；统一前先测兼容，不把命名风格称故障。
- 当前源码证据：code-analysis-swarm/.zcode-plugin/plugin.json `"commands": "command"`（单数目录）vs dev-companion/.zcode-plugin/plugin.json `"commands": "commands"`（复数目录）；tests/swarm_workflow.test.mjs:186 断言 swarm 侧为 `'command'`。两侧均显式声明、各自自洽。
- evidence_status：severity_requalified（维持）。
- 实施任务：P7-02。

### Z17 缺CI与格式检查
- registry decision：分层 CI、schema/文档/包验证，宿主缺失不能伪造绿灯。
- 当前源码证据：仓库无 `.github/` 目录（find 为空）、无任何 lint/format 配置文件；README.md:352-362"本地验证"仅三条例行命令（python unittest ×2 + node --test），全靠手工执行。
- evidence_status：source_audit（维持）。
- 实施任务：P1-05、P7-05。

### Z18 多处命令说明和前缀分发
- registry decision：单一命令注册表生成帮助与一致性校验；保留既有入口。
- 当前源码证据：同一套 22 个 CLI 子命令在 ≥3 处重复描述——README.md:90-126（中文速查表）与 :452-488（英文表）、dev-companion/references/cli-contract.md（全表，:165 含退出码约定）、dev-companion/commands/*.md（7 个聊天入口各带一份）；前缀约定散见 README.md:25（"resume 是内置命令，故统一 companion- 前缀"）。一致性靠人肉维护，无生成/校验机制。
- evidence_status：source_audit（维持）。
- 实施任务：P7-02。

### Z19 DWF真实编译/跨块/看板测试缺口
- registry decision：真实宿主、稳定 ID、边端点和看板结构断言；不依赖 prompt 中文 split。
- 当前源码证据：tests/swarm_workflow.test.mjs:10-11（`stripTypeScriptTypes` + `new Function(...)` 假宿主）、:64（`phase` 传空函数）；:53 测试用 `prompt.split('逐条复查全部结论 ')[1].split('。\n')[0]` 解析 A6 prompt——断言逻辑耦合中文 prompt 文案；:24/:64-65 `cards` 只收集从不断言（看板结构零覆盖）。真实宿主编译已由 Z01 探针证明失败。
- evidence_status：static_confirmed（维持，且被 Z01 宿主探针间接强化）。
- 实施任务：P1-01、P1-04、P1-05。

### Z20 Node最低版本表述
- registry decision：22.13 提供所需 API 但全套最低版本由 CI 实测确认，保留已测 24 版本说明。
- 当前源码证据：tests/swarm_workflow.test.mjs:4 `import { stripTypeScriptTypes } from 'node:module'`（该 API 22.13 起提供）；README.md:362 与 :724 表述为"Node 测试需要 Node 24"——把已测版本写成了需求版本，无版本矩阵 CI 佐证。
- evidence_status：partially_requalified（维持）。
- 实施任务：P7-05。

### Z21 100MiB/1万文件导致status不可用
- registry decision：状态视图不扫描，流式索引取代固定小容量硬门；容量按等级实测。
- 当前源码证据：core.py:326-327 硬门 `if info.st_size > 20*1024*1024 or total > 100*1024*1024 or len(files) >= 10000: raise CompanionError("项目超出首版检查范围…")`；core.py:600 `snapshot = snapshot or self.snapshot()`——status 每次全树扫描，超限项目连状态页都打不开。archives.py 另有一套更小上限（:19-22 MAX_FILES=1000 等）。
- evidence_status：static_confirmed（维持）。
- 实施任务：P2-03、P3-01、P6-05。

### Z22 Windows中文编码及二次异常
- registry decision：UTF-8 输出或安全 fallback，错误路径仍可解析。
- 当前源码证据：companion.py:163 `print(json.dumps(result, ensure_ascii=False, indent=2))` 直接写 stdout，无 `sys.stdout.reconfigure(encoding="utf-8")` 之类处理——Windows 默认控制台编码（如 cp936）下中文 JSON 可抛 `UnicodeEncodeError`；且 `UnicodeEncodeError` 是 `ValueError` 子类，会落入 :167 `except (CompanionError, ValueError, OSError)` 后在 :172 再次 `print(..., ensure_ascii=False)` 到 stderr——错误处理路径本身可二次同因失败。文件输出侧无此问题（:135 显式 `encoding="utf-8"`）。
- evidence_status：source_audit（维持，静态推演；无 Windows 实机验证）。
- 实施任务：P7-03。

### Z23 fd泄漏和父目录fsync
- registry decision：统一原子写/异常清理；按平台验证耐久性，不假定支持。
- 当前源码证据：**父目录 fsync 缺失确认**——六处 `os.replace` 后均无目录 fsync：core.py:299、journey.py:295、archives.py:101/:274/:352、releases.py:112、companion.py:137（崩溃后 rename 可能不持久）。**fd 泄漏未复现**——各写路径均用 `os.fdopen(...)` with 块（core.py:294、journey.py:289、releases.py:106）或 `with tempfile.TemporaryFile()`（core.py:72）关闭（见文首不符项 3）。
- evidence_status：source_audit（维持，但任务表述建议收窄为"父目录耐久性 + 异常路径复查"）。
- 实施任务：P2-01、P7-03。

### Z24 block/feedback清理不对称
- registry decision：统一状态转换与证据失效规则；实际停止与状态变更分开。
- 当前源码证据：core.py:558-574 `feedback()` 置 blocked 并同时 `task.pop("acceptance"/"verification"/"integration")`（:571-572）；core.py:576-585 `block()` 只 `task.pop("acceptance")`（:582）——同为转 blocked，证据清理字段集不同；两者都只改状态不触碰进程（:585 返回信息里明说"此命令不会替你终止外部智能体"）。
- evidence_status：source_audit（维持）。
- 实施任务：P5-01、P5-02。

### Z25 版本/CHANGELOG与可追溯性
- registry decision：发布输入单点、构建清单、版本校验；没有授权不创建 tag/push。
- 当前源码证据：全仓无 CHANGELOG*（find 为空）；版本号散落三处手工同步——marketplace.json:9/:16（0.2.1/0.2.0）、两个 plugin.json（0.2.1/0.2.0）、README.md:11-12；分发件 `dist/agents-dev-companion-0.1.2.zip` 文件名版本 0.1.2 与当前 plugin 0.2.0 **不一致**（陈旧构建产物无校验拦截）。
- evidence_status：source_audit（维持）。
- 实施任务：P7-05。

### Z26 技术检查命令非空约束
- registry decision：产品草案允许不完整，技术完成/READY 必须有有效 argv 检查。
- 当前源码证据：core.py:168-171 `validate_scope` 草案期允许 `check_commands` 为空列表（只要求"参数数组的列表"）；journey.py:114-115 technical `complete` 时逐功能强制非空；core.py:506-507 check 执行时 `if not commands: raise …不能标为通过`。语义与 registry 描述一致：缺口在于"空命令草案"与"READY"之间缺少显式的中间态校验点（当前靠三个分散检查拼出）。
- evidence_status：source_audit（维持）。
- 实施任务：P4-05。

### Z27 退出码/硬编码超时/deepcopy
- registry decision：退出码已有说明需核对真缺口；超时配置化、测进程树停止；不盲删必要 copy。
- 当前源码证据：**退出码已文档化且一致**——companion.py:158-173 实现 0/2/3，references/cli-contract.md:165 与 README.md:126/:488 均记载（原审计该项不成立，registry 重判正确）；**超时硬编码**——core.py:514 `run_argv(argv, self.root, 120, …)`、releases.py:10 `TIMEOUT_SECONDS = 120`；**deepcopy 三处**——journey.py:209（_view 每次状态渲染）、journey.py:268（历史快照）、releases.py:33（命令配置，防御性）。
- evidence_status：partially_requalified（维持）。
- 实施任务：P7-02、P7-03。

### Z28 source_role/字段命名/verified语义
- registry decision：来源角色与 finding ID 分开；结构通过不标事实 verified；兼容字段显式迁移。
- 当前源码证据：dwf.ts:248 高严重度 finding 转 claim 时 `source_role: f.id`——**把 finding ID 当作来源角色**（A3/A5 claims 用 "A3"/"A5"，同一字段两种语义）；:59/:293 `findings[].status` 三值 verified/refuted/unverified 由 verdict 映射，非高严重度 finding 未送验即标 unverified（口径尚可），但 "verified" 一词用于"独立复查确认"与日常"结构校验通过"易混（G 门禁通过不改变该字段，风险在文档层）。
- evidence_status：source_audit（维持）。
- 实施任务：P1-04、P4-05。

### Z29 忽略文件/说明与swarm README
- registry decision：补独立 README、场景命令、.gitignore 和安装说明，别忽略真实源码。
- 当前源码证据：**`.gitignore` 已存在**（根目录 4 条缓存类条目，未忽略源码，自 daecdb9 跟踪）——registry 该半诉求已过时；**swarm 独立 README 仍缺**——code-analysis-swarm/ 目录仅 DESIGN.md、agents/、command/、workflow/，无 README.md；场景命令在根 README.md:309-313。
- evidence_status：source_audit（维持，任务范围收窄为"补 swarm README + 安装说明"）。
- 实施任务：P7-02。

### Z30 A2输入描述与实际派发不符
- registry decision：按真实任务包与冻结邻块契约同步角色/流程/schema。
- 当前源码证据：agents/a2-module-analyst.md:15（"输入"节）声明任务参数含"邻块接口契约路径列表（黑板 `interfaces/` 下）"；而 dwf.ts:190-193 实际派发只给 `{ target, chunk }` + `map.tree_summary`，且 :193 明说"并行阶段没有已冻结的邻块契约…不猜测"。角色文件承诺的输入在真实派发中不存在（DESIGN.md:74 同样按"邻块契约"描述输入）。
- evidence_status：source_audit（维持）。
- 实施任务：P3-04、P4-05。

---

## C01–C18（设计要求类：核实"当前仓库缺失该能力"）

统一说明：C 项不声称现有已实现；下列证据回答"现在实际有什么"，作为新增能力的起点事实。

### C01 Feature与子任务/attempt分离
- registry decision：一功能多职责任务，依赖与重试不虚增完成度。
- 当前状态核实：状态机里**没有 Feature/Task/Attempt 三层**——`state["tasks"]` 直接按 feature id 一对一挂单任务对象（core.py:216-217、:340-342 初始化 `{status:"pending"}`）；任务五态 pending/running/awaiting_review/accepted/blocked（core.py:220-221）；重新派发 `packet()` 里 `task.clear()` 把上一次 run 整体抹掉（core.py:438-439），attempt 历史只剩 events 流水，不构成可查询的重试计数。无子任务、无依赖 DAG。
- evidence_status：design_requirement（维持）。
- 实施任务：P5-01。

### C02 产品与可行性研究负责人
- registry decision：先明确真实需求/能力，未知 API 不凭经验补。
- 当前状态核实：journey 六阶段（concept/requirements/product/flow/prototype/technical，journey.py:12-20）只是**数据记录阶段**，无对应研究角色；agents/ 目录仅 companion-developer.md 与 companion-checker.md 两个执行类角色（根 README.md:83-88 明说"不为每个阶段新建 Agent"）；无 API 可行性探测产物或字段。
- evidence_status：design_requirement（维持）。
- 实施任务：P4-01。

### C03 UX/UI与前端设计团队
- registry decision：设计与实现分开，完整页面状态和体验门。
- 当前状态核实：prototype 阶段 details 仅 screens/states/walkthrough 三个**自由文本字段**（journey.py:17-18），完成校验只要求 artifacts 非空 + 哈希可读（journey.py:91-92、:185-202）；无 UI/UX 角色文件、无页面状态完备性检查、"体验门"只有 requires_user_acceptance 布尔（core.py:551-552）。
- evidence_status：design_requirement（维持）。
- 实施任务：P4-02、P4-04。

### C04 后端/数据/契约专业团队
- registry decision：业务语义、接口和迁移先设计联审。
- 当前状态核实：technical 阶段 interfaces 为 `{feature_id, kind: http|local, contract: 文本, check_commands}`（journey.py:125-146），contract 是非空字符串（:136-138）；无后端/数据建模角色、无迁移设计产物、无联审 gate。
- evidence_status：design_requirement（维持）。
- 实施任务：P4-03、P4-05。

### C05 机器可验证的交接与设计版本
- registry decision：具体结构/样例/hash，非空字符串不等于理解一致。
- 当前状态核实：接口契约的唯一校验是 `text(item.get("contract"), key)` 非空（journey.py:136-138）——正是 decision 指名的"非空字符串"弱校验；设计版本仅有整体 revision 计数（journey.py:270），无逐契约 hash/样例/结构断言。
- evidence_status：design_requirement（维持）。
- 实施任务：P4-05。

### C06 独立集成责任和固定版本审查
- registry decision：批准候选才能集成，集成版本重新回归。
- 当前状态核实：integration 检查内嵌在单功能任务里（core.py:502-505 从 interfaces 收集命令、:547-550 accept 前校验 integration 新鲜度）；release-prepare 只绑定"当前全量指纹"（releases.py:117-130、:150-152）。无"候选版本批准→集成→集成版回归"分层，集成与功能验收同层。
- evidence_status：design_requirement（维持）。
- 实施任务：P5-04、P5-05。

### C07 隔离写工作区与最小工具权限
- registry decision：worktree 不等于沙箱；禁止共享工作目录并发写。
- 当前状态核实：全仓 Python/TS 源码 grep `worktree|sandbox|沙箱` 为零命中——无任何工作区隔离机制；写入直接发生在用户项目目录，靠 `allowed_paths` 白名单（core.py:432-433）+ 项目级排他锁（core.py:235-253）串行化；agents 角色文件 frontmatter 只有 name/description，**无 tools/disallowedTools 声明**（grep `^tools:` 为空，tests/swarm_workflow.test.mjs:192 的断言也只查 name+description）。
- evidence_status：design_requirement（维持）。
- 实施任务：P4-04、P5-03、P7-01。

### C08 功能级与版本级双层证据失效
- registry decision：依赖闭包用于增量验收，未知依赖保守扩展。
- 当前状态核实：失效判定是**全项目指纹等值比较**——`verification.fingerprint != snapshot.fingerprint` 或 planning_fingerprint 变化即整体 stale（core.py:607-611），journey 侧上游 stale 线性传染（journey.py:204-216）；无文件级依赖闭包、无"未知依赖保守扩展"概念。
- evidence_status：design_requirement（维持）。
- 实施任务：P3-02、P3-05。

### C09 持久流式索引与大文件/运行数据分离
- registry decision：普查有界，数据库/缓存不当源码送模型。
- 当前状态核实：`snapshot()` 每次调用全树重扫进内存 dict（core.py:304-333），无持久索引、无增量扫描；EXCLUDED_DIRS 排除表（core.py:113-114）是唯一的"运行数据分离"手段，且与硬上限 100MiB/10000 文件（:326-327）耦合（见 Z21）。swarm 侧 manifest 每次重造（dwf.ts:155-159）。
- evidence_status：design_requirement（维持）。
- 实施任务：P3-01、P3-02。

### C10 token切片与完整语义深读活动
- registry decision：预算不足暂停续接，不偷偷降抽样。
- 当前状态核实：全仓源码无 token/预算/budget 概念（grep 仅命中 archives 恢复令牌 token）；A6 全量 claims 单次 ask（dwf.ts:252，见 Z08）；唯一相关约束是 A2 coverage.gaps 禁止把抽样记作深读（dwf.ts:194、:201）——只有"不降级"没有"分片续接"。
- evidence_status：design_requirement（维持）。
- 实施任务：P3-03、P6-03。

### C11 分维度覆盖、失败成果保留
- registry decision：索引/深读/独立复核分别有分母、generation 和缺口。
- 当前状态核实：dwf coverage 只有 files/files_analyzed/chunks/verdicts 五个聚合计数（dwf.ts:269）；复核无 generation 概念（verdict 一次性，dwf.ts:251-265）；dev-companion 侧完成度是单一百分比（core.py:637）。失败成果保留现状尚可（blocked 留痕、refuted/unverified 保留，dwf.ts:291-295、core.py:576-585）。
- evidence_status：design_requirement（维持）。
- 实施任务：P3-04、P3-05。

### C12 检查点与跨会话续接
- registry decision：从事实存储续接，旧 epoch 拒绝，不声称自动唤醒宿主。
- 当前状态核实：dev-companion 侧已有 revision 乐观锁续接（core.py:356-359 `require_revision`；journey.py:252-253；commands/companion-resume.md 入口）——这部分能力**存在**；swarm 侧完全没有：run_id 一次性执行，失败即 blocked 终态（dwf.ts:297-303），无 epoch、无检查点、无续接。C12 的缺口主体在 swarm 与跨记录统一续接。
- evidence_status：design_requirement（维持）。
- 实施任务：P5-02、P6-04。

### C13 资源预算与背压
- registry decision：模型/写任务/磁盘各有界，有限重试，禁止无界 Promise.all。
- 当前状态核实：dwf.ts:188（全部 chunk 一次 `Promise.all`）与 :220（三专项 `Promise.all`）——并发度完全交给宿主，脚本层无上限、无队列；dev-companion 写任务靠单锁强串行（core.py:235-253，有界但粒度粗）；超时硬编码 120s（core.py:514、releases.py:10）。
- evidence_status：design_requirement（维持）。
- 实施任务：P5-02、P6-03。

### C14 用户能理解的实时性与功能进度
- registry decision：首页分页、显示新鲜度，静态看板不冒充实时。
- 当前状态核实：状态查看是快照式——`status` 命令渲染 markdown/html 一次性输出（companion.py:121-143、core.py:688-730），README.md:108 自称"静态看板"；历史仅 `state["events"][-10:]`（core.py:641）无分页；新鲜度有 observed_at 时间戳（core.py:636）与 evidence_stale 标记（:614），无自动刷新。
- evidence_status：design_requirement（维持）。
- 实施任务：P2-03、P7-02。

### C15 XL/XXL分片与真实容量认证
- registry decision：百万/千万按物理和索引分别实测，跨片完整性不遗漏。
- 当前状态核实：超出 100MiB/10000 文件直接抛错拒服务（core.py:326-327）；swarm 单块 ≤150 文件/≤5000 LOC（dwf.ts:179）但块数无上限也无跨运行分片；仓库内无任何容量基准/实测记录（tests/ 为功能回归）。
- evidence_status：design_requirement（维持）。
- 实施任务：P6-01、P6-02、P6-05。

### C16 兼容迁移与独立插件分发
- registry decision：旧项目可用、迁移不丢事实，单源码构建分发。
- 当前状态核实：三份记录 schema 校验失败一律抛"不兼容…请保留原文件后检查"（core.py:209-214、journey.py:162-166、releases.py:54-59）——**无迁移路径**，只有"保留现场"；SKILL.md 有"旧 state.json schema 1 与新 journey.json 并存，旧项目不需要迁移"的口径（能力=并列而非迁移）；分发件 dist/agents-dev-companion-0.1.2.zip 为手工产物且版本陈旧（0.1.2 ≠ 0.2.0，见 Z25），无单源码构建。
- evidence_status：design_requirement（维持）。
- 实施任务：P2-04、P2-05。

### C17 可选远端与宿主能力边界
- registry decision：未经验证不引入嵌套 Agent/API/共享网络 WAL/自动传代码。
- 当前状态核实：当前代码纯本地 stdlib（core.py:1 文档串"stdlib only"、releases.py:1 同），无远端/网络/共享 WAL；嵌套 Agent 被双向禁止（根 README.md:88"子代理不再派发子智能体"；companion-developer.md 正文同句）；宿主能力边界在 references/zcode-integration.md 有文字约定。即"未引入"现状成立，C17 是**维持性要求**（防未来违规引入）。
- evidence_status：design_requirement（维持）。
- 实施任务：P0-02、P6-02、P7-01。

### C18 稳定源码/模块身份与跨块链路
- registry decision：固定 commit/manifest，稳定 ID、外部/未知边，跨模块二次复核。
- 当前状态核实：manifest.meta 仅 `{target, generated_at, tool}`（dwf.ts:28、DESIGN.md:284）——**无 commit/版本固定**；模块名由 A2 每次运行自行命名（dwf.ts:189-196，仅跨块唯一性检查 :214）；依赖边端点只归位到当次模块名集合（dwf.ts:215），无外部/未知边类型；跨模块二次复核不存在（A6 只验 claims）。
- evidence_status：design_requirement（维持）。
- 实施任务：P3-02、P3-04、P6-01。

---

## 汇总表（48 项）

| ID | 状态（evidence_status / 核对结论） | 主要证据位置 |
|---|---|---|
| Z01 | host_compile_rejected_confirmed（升级；宿主探针 2026-09-20） | code-analysis-swarm/workflow/code-analysis.dwf.ts:119,134,154,187,219,244,268 |
| Z02 | static_confirmed_host_unverified（probe_pending H04/H05） | code-analysis.dwf.ts:12-15,126-128,278,288 |
| Z03 | static_confirmed（文档 2 轮 vs 实现 0 轮） | code-analysis.dwf.ts:297-303；README.md:256 |
| Z04 | static_confirmed | dev-companion/scripts/core.py:306,322-323,600 |
| Z05 | source_audit_and_prior_review（core↔journey/releases 模块环） | core.py:407,411,588,647；journey.py:9；releases.py:8 |
| Z06 | source_audit（5 份原子写、2+1 份哈希） | core.py:25-26,292-302；journey.py:199,288-300；archives.py:38-39,88-102；releases.py:104-115；companion.py:133-140 |
| Z07 | source_audit_decision_required（无 LICENSE 文件/字段） | 全仓 find LICENSE* 为空；marketplace.json；两个 plugin.json |
| Z08 | static_confirmed | code-analysis.dwf.ts:245-255 |
| Z09 | static_confirmed_api_unverified（probe_pending H05） | code-analysis.dwf.ts:135-145 |
| Z10 | source_audit（from_contract 三处定义漂移） | agents/a2-module-analyst.md:36；DESIGN.md:306；code-analysis.dwf.ts:46,208 |
| Z11 | source_audit（interfaces/、graph/*.csv 无消费者） | code-analysis.dwf.ts:192-193,226-236,279-287；DESIGN.md:146-147 |
| Z12 | source_audit（**范围收窄**：DESIGN.md:17/:383 仍未兑现） | DESIGN.md:214-218,17,383,266,394；code-analysis.dwf.ts（无路由） |
| Z13 | partially_requalified | journey.py:209,268,278-281,284-300；releases.py:102-115 |
| Z14 | source_audit（孤立 release 无诊断，通用报错） | companion.py:96-97；releases.py:117-121,232-236；core.py:590,607 |
| Z15 | source_audit | dev-companion/README.md:90；references/zcode-integration.md:48；dist/*.zip 清单 |
| Z16 | severity_requalified（显式声明，两侧各自合法） | 两个 .zcode-plugin/plugin.json；tests/swarm_workflow.test.mjs:186 |
| Z17 | source_audit（无 CI/lint） | 全仓无 .github/；README.md:352-362 |
| Z18 | source_audit（≥3 处重复维护） | README.md:90-126,452-488；references/cli-contract.md；commands/*.md |
| Z19 | static_confirmed（mock 宿主 + 中文 split + cards 无断言） | tests/swarm_workflow.test.mjs:10-11,53,64-65 |
| Z20 | partially_requalified | tests/swarm_workflow.test.mjs:4；README.md:362,724 |
| Z21 | static_confirmed | core.py:326-327,600；archives.py:19-22 |
| Z22 | source_audit（静态推演，未实机验证） | companion.py:163,167-173 |
| Z23 | source_audit（**fd 泄漏未复现**；父目录 fsync 缺失确认） | core.py:299；journey.py:295；archives.py:101,274,352；releases.py:112；companion.py:137 |
| Z24 | source_audit | core.py:558-574（feedback）vs 576-585（block） |
| Z25 | source_audit（无 CHANGELOG；dist 0.1.2≠0.2.0） | 全仓无 CHANGELOG*；dist/agents-dev-companion-0.1.2.zip |
| Z26 | source_audit | core.py:168-171,506-507；journey.py:114-115 |
| Z27 | partially_requalified（退出码已文档化；超时/deepcopy 确认） | companion.py:158-173；references/cli-contract.md:165；core.py:514；releases.py:10 |
| Z28 | source_audit（source_role:f.id 混用） | code-analysis.dwf.ts:248,59,293 |
| Z29 | source_audit（**.gitignore 已存在**；swarm README 仍缺） | 根 .gitignore；code-analysis-swarm/ 无 README.md |
| Z30 | source_audit（角色承诺邻块契约 vs 派发不给） | agents/a2-module-analyst.md:15；code-analysis.dwf.ts:190-193 |
| C01 | design_requirement（无 Task/Attempt 层） | core.py:216-221,340-342,438-439 |
| C02 | design_requirement（仅 developer/checker 两角色） | agents/ 目录；journey.py:12-20 |
| C03 | design_requirement（prototype 仅文本字段+哈希） | journey.py:17-18,91-92,185-202 |
| C04 | design_requirement（contract 为非空文本） | journey.py:125-146 |
| C05 | design_requirement（text() 弱校验） | journey.py:136-138 |
| C06 | design_requirement（集成内嵌功能任务） | core.py:502-505,547-550；releases.py:117-130 |
| C07 | design_requirement（无 worktree/沙箱/tools 声明） | grep worktree/sandbox/^tools: 均空；core.py:235-253,432-433 |
| C08 | design_requirement（全量指纹等值判失效） | core.py:607-611；journey.py:204-216 |
| C09 | design_requirement（全树内存重扫） | core.py:304-333,113-114 |
| C10 | design_requirement（无预算概念） | 全仓 grep 预算/budget 空；code-analysis.dwf.ts:252 |
| C11 | design_requirement（仅聚合计数） | code-analysis.dwf.ts:269,251-265 |
| C12 | design_requirement（companion 侧续接已有；swarm 无） | core.py:356-359；code-analysis.dwf.ts:297-303 |
| C13 | design_requirement（2 处无界 Promise.all） | code-analysis.dwf.ts:188,220；core.py:514 |
| C14 | design_requirement（静态快照看板） | companion.py:121-143；core.py:641,688-730 |
| C15 | design_requirement（硬上限拒服务） | core.py:326-327；code-analysis.dwf.ts:179 |
| C16 | design_requirement（失败即拒，无迁移；dist 陈旧） | core.py:209-214；journey.py:162-166；releases.py:54-59；dist/*.zip |
| C17 | design_requirement（纯本地现状成立，维持性要求） | core.py:1；releases.py:1；README.md:88 |
| C18 | design_requirement（无 commit 固定/稳定 ID） | code-analysis.dwf.ts:28,189-196,215 |

## 备注

- 本文件为范围冻结基线：后续修复任务的 claim 核对应以本表 `文件:行号` 为起点，实施后行号会漂移，需以 git blame/重新定位为准。
- Z02/Z09 的 H04/H05 宿主正例探针结果落地后，应回填这两项的 evidence_status，并同步登记表。
- 按任务约束未运行任何测试；"static_confirmed" 类结论均基于源码阅读，不含执行验证。

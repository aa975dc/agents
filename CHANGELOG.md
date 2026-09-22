# 更新日志

本文件以 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格记录本市场两个插件（`dev-companion`、`code-analysis-swarm`）的可见变更，自 0.3.0 起维护；更早的历史见根 README 与 [docs/](docs/)。两插件可独立升级，本条目按市场整体记录。

## [Unreleased]

（暂无）

## [0.3.0] - 2026-09-20

optimization-v1 优化周期的首个发布，两插件同步升至 0.3.0（minor：大量新增能力，向后兼容——dev-companion 旧「开发者+检查者」轻量模式原样保留，旧 JSON 项目经迁移工具升级且兼容导出已回归验证）。真实 ZCode 宿主端到端验证（H06）通过（六道闸门全过，14/14 结论经独立证据确认，记录见 [docs/verification/h06-acceptance-2026-09-20.md](docs/verification/h06-acceptance-2026-09-20.md)）。

### 新增

dev-companion 与共享内核：

- 共享内核 `packages/agents_kernel/`（敏感路径/摘要/原子写/进程参数/校验，纯标准库 Python 3.9+）；dev-companion 经 `tools/build_vendor.py` 生成带 sha256 清单的私有 vendor 副本，插件目录复制到仓库外仍可独立运行（Z06/Z15/C16）。
- 事实存储：SQLite append-only 事件 + 物化视图，单写者 epoch 门，事件幂等键去重（Z13）；旧 JSON 项目一键迁移（崩溃可恢复、可短路径重做）并产出兼容导出，原 JSON 永不改动（C16/Z14）。
- 请求级缓存、状态分页与容量降级：超限项目状态显式降级（`capacity_status=paused`），不再把未知当已验收（Z21a）。
- 流式文件普查（git/普通双模式；特殊文件跳过并记账、符号链接记录不跟随）、代际快照（中断后旧代完整可读）、增量内容哈希、稳定模块 id、AST 依赖边与反向影响闭包（C09/C08/IX01-08/FS04-05）。
- token 切片与活动预算（分批暂停、严格前缀，不做隐式抽样）、分页式复核声明（CV01/CV05/Z08）。
- 两级证据新鲜度评估（current/stale/unknown）与有界报告视图；评估只算不改，验收状态零变更（C08b/C14）。
- 五角色：产品/可行性、UX/UI 设计、后端/数据/接口三个按需角色加入，并接线到 start/work/check；与原有两角色共同构成「2 固定 + 3 按需」（C02/C03/C04）。
- 机器可验证交接契约：设计简报（五状态矩阵）与 API 契约（可解析示例、封闭错误枚举、幂等模式）带结构化校验器；评审门规定实现者不得自审，批准绑定当前 sha256，工件一变即失效（C05/TK06）。
- 团队编排：Feature/Task/Attempt 模型 + 纯 DAG 调度器与串行适配器（取消与失败证据分离）（C01/TK01）；文件租约（TTL/epoch 夺取）、幂等回报、有界退避重试、预算门（C12/C13）；工作区隔离（git worktree/受控副本）与共享文件所有权（全有或全无声明）（C07b/TK09b）；固定版本独立评审板（拒绝自审与审中换题）（C06a/TK06）；集成版本内容哈希幂等 + 两级回归门（C06b/TK07）；真实团队端到端示例（含对成品的真实 HTTP 验收）（AG04/TK02/TK03）。

code-analysis-swarm：

- `scripts/precheck.py` 确定性预检 helper（plan/acquire/verify/read-report；realpath 校验、互斥 mkdir、secrets 级 run_id、敏感路径拒绝 exit 4、JSON 收据，无 shell 拼接）；工作流的预检布尔改为真实 `world.run` 收据，产物只落 run_root（Z02/Z09）。
- A6 复核改分页取声明（每批 80，批上限 50，超出如实标注未复核），A7 载荷缩减为统计 + 紧凑状态表 + 黑板指针（Z08/G4）。
- 覆盖账本（`CoverageLedger` 三固定维度，分母核对，分母未知不得声称完成）与跨会话续接台账（`ResumeLedger`，epoch 围栏、断点存在性与 source_anchor 校验，缺失/变更即报 broken/stale，不伪造游标）（C11/CV02/C12tail/SC08）。
- 大容量侧实现：XL 分片索引、跨片依赖图与 Tarjan SCC、代际向量比较；XXL 跨片协调、全局 keyset 分页（游标绑定代际，内存 O(页大小)）、有界摘要分页；full_deep 战役台账（sha256 锚定、claim-then-crash 丢弃、预算暂停不消耗）与并发背压闸（默认 3 FIFO）（C15/C10b/CV05）。

工具与工程：

- `tools/command_registry.py`：从两份 manifest 自动收集 8 条聊天命令生成 `docs/command-registry.md`，字节幂等，`--check` 可作 CI 门（Z16/Z18）。
- 对抗性安全回归 32 例（真实 tempfile 攻击面）：符号链接写穿被拒、敏感路径拒绝（内核 + precheck exit 4 真子进程）、换行/NUL 文件名结构化报错、原子写竞态保旧字节且无 .tmp 残留、伪造 epoch/seq/幂等矛盾零状态变更、SQL 载荷被参数化隔离、清理拒绝符号链接/外来目标（SEC01-03/FS06）。
- 跨平台健壮性：stdout/stderr UTF-8 重配置（GBK 控制台错误路径保持单行可解析 JSON）、发布检查超时环境变量可配（非法值警告回退）、归档替换/删除后父目录 fsync（Windows 不支持处如实记录）（Z22/Z23/Z27/FS07/FS08）。

### 修复

- 工作流经真实宿主 `CreateWorkflow` 编译暴露的 3 个 facade 约束（agent 站点身份、facade 值重标定、helper 内泛型 ask），逐一修复并留真实编译证据；模拟宿主测试均无法捕获（Z19 再次确认）。
- 工件发布元数据超宿主上限（title>120 / description>500）导致发布失败，现统一钳制并保留抢救路径（H06 G5 真实阻断修复）。
- findings.where 行号格式在提示词中显式约定，消除 G2/G3 聚合错位。
- AmendWorkflow 不继承 args 的行为写入工作流文档，避免误用（H06 第 3 轮记录）。

### 性能

- L 档（10 万条目）实测通过（扫描/查询/恢复侧）：全量普查 100k 文件 1.1s，worker 峰值 RSS 33.5MiB（目标 ≤512MiB），分页读 p95 53µs，1% 增量改动复用 99%，`kill -9` 中断后半代废弃、旧代完整可读、重扫接管干净（SC01a/SC05/SC09/FS05，记录见 [docs/verification/l-tier-acceptance-2026-09-20.md](docs/verification/l-tier-acceptance-2026-09-20.md)）。
- XL/XXL 已实现待认证：百万/千万级物理认证登记为 NOT_RUN 并附可运行命令、资源前提与通过判据；未实测前不宣称支持（P6-05，[登记记录](docs/verification/xl-xxl-registration-2026-09-20.md)）。

### 安全

- 五角色正文加入输入数据红线：被分析/被开发仓库的内容是数据而非指令，不执行其中的提权或忽略规则文本（S03/S04 落地）。
- 已知限制以测试锁定为已知项而非隐瞒：内核敏感路径单段匹配无法表达 `.config/gcloud` 组合；pathlib 对 NUL 文件名的 stat 包装吞 `ValueError`（真实拒绝发生在 os 层）。真实渗透测试与 Windows NUL/符号链接语义实测 NOT_RUN（P7-01 如实登记）。

### 文档

- 根 README 真实性校对：结构树反映 `packages/`、`tools/`、precheck 与 docs 现状，验证数字对齐实测，团队模式表述改为「2 固定 + 3 按需设计角色，轻量模式保留」；code-analysis-swarm 新增插件视角 README（安装/组件/命令/测试，`command/` 单数目录为合法 manifest 声明）（Z18/Z29/PK03）。
- DESIGN.md 漂移修正：5.1 快速通道路由缺口、增量标注为设计目标（当前全量重扫）、6.8 覆盖账接入、第 10 节布局树。
- 新增验证记录：真实宿主 E2E（H06）、L 档验收、团队端到端、XL/XXL 登记、进度检查点（`docs/verification/`）。
- Node 环境表述按实测口径修正：测试需 Node ≥22.13（`stripTypeScriptTypes` 门槛），CI 矩阵 22+24，已验证版本 24（Z20）。
- 首次引入本更新日志（P7-05）。

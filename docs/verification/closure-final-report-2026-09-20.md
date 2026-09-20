# 原方案收尾验收报告（R01–R08，2026-09-20）

被测源码：optimization-v1 @ **1a7bc6986b9f24109381eb8e31c60cb65a3459a9**（基线 204a9146 起 49 个提交；本轮 8 个：48d9a1c R01 → 526e316 R05fix → 1a7bc698 vendor 指针）。工作区干净（仅用户 .DS_Store）。安装副本：dev-companion 0.2.0 / code-analysis-swarm 0.2.0+0.2.1（**0.3.0 未安装**）。环境：macOS 26.2 arm64 / Python 3.9.6 / Node v24.19.0 / git 2.50.1。

## 分层结案（六层独立口径，不互相冒充）

| 层 | 状态 | 依据与边界 |
|---|---|---|
| ① 代码实现（源码候选） | **PASS** | 干净 checkout 自洽验证：616→648 测试全过（R01 发现并修复 8ed8806 提交树缺件 38 错误的问题）；python 648（643+5 容量判定）/node 57；vendor 与内核哈希一致 |
| ② 入口接线（用户入口） | **PASS（已接线部分）** | team-init/status/task/migrate 四命令经真实 CLI 子进程使用 SQLite 事实库（R02，13 测试，事实源唯一、无双主、legacy 零感知）；集成/审查/回归/superseded 检查点经 **2–4 真实进程**恢复验证（R03）；legacy block/feedback 统一转换+资格失效+进程存活分离（R04，9 测试，**Z24 关闭**）；L 档 status 经真实 CLI 入口零 walk/零读取实测（R05 修复后 10/10 checks PASS，p50 36.4ms）。遗留如实列明：legacy status 无分页参数、事件库镜像走纯函数未接 views、team 路径未 realpath |
| ③ 真实宿主 | **PARTIAL** | 分析团 H06：PASS（真实编译+六闸门+14/14 verdicts，scope=小库分析）；**新版开发团队 0.3.0 宿主验收：NOT_RUN（安装授权门）**——demo runbook READY_TO_TEST（examples/team-host-demo/RUNBOOK.md，含记录表与通过判据）；17 职责映射 16 mapped/1 unmapped（V1 容量职责，补法已建议） |
| ④ 容量 | **L：PASS（声明范围）**；**XL/XXL：NOT_RUN_AUTH** | L=100,000 文件/238.5MiB 温热：扫描 1.111s、RSS 33.5MiB、分页 p95 53μs（底层）/CLI status p95 42.4ms（入口）、增量 1000:99002、kill -9 恢复——**不证明** 10GiB/冷缓存/全量语义深读。XL/XXL：实现+小样本实跑（800 行三 profile 分列、累计 2418≠distinct 811、无漏重）；大档命令+外推预算已备（XL 1M≈1.24GiB/3.1min；XXL 10M≈11.6GiB/31min），授权后按登记文档执行 |
| ⑤ 安装 | **NOT_RUN（待授权）** | 0.3.0 已版本三处一致+仓外打包验证（vendor 哈希自洽、清单闭合）；宿主缓存未升级 |
| ⑥ 发布 | **NOT_RUN（未授权）** | 未 push、未 tag、未合并 main、未发布。LICENSE（PK05）仍 BLOCKED 待权利人选择 |

## 最小关键门（04 §B 七门）

1. 41 任务映射+68 验收清点：**PASS**（closure-r01 文档；41/41 逐 ID、68/68 逐条；上轮"40"分母误计已修正）
2. 新 team 入口唯一事实源+隔离迁移切换/回退+legacy 兼容：**PASS**
3. 集成/审查/回归/失效跨真实进程恢复：**PASS**
4. Z24 关闭：**PASS**
5. 普通 status L 档真实入口零源码扫描：**PASS**（修复后）
6. XL/XXL 基准脚本可执行+小样本+dry-run：**PASS**（大档 NOT_RUN_AUTH）
7. 新版团队宿主门：**NOT_RUN（安装授权）**——不由 H06 或角色扮演替代

## 48 问题与 68 验收修订计数

- 48 项：**46 resolved / 1 blocked（Z07 LICENSE）/ 1 deferred（C15 XL/XXL 认证）/ 0 partial**（Z24 由 partial 转 resolved，证据=tests/test_z24_closure.py + core.py 统一转换接线）
- 68 验收：**54 PASS / 9 PARTIAL / 4 NOT_RUN（SC02–04、PK06）/ 1 BLOCKED（PK05）**。PARTIAL 明细：H03（负路径）、FS08（真 Windows）、ST05/SC06（n=10000 历史实验）、IX05（同 mtime+size 变更检测，已知限制）、AG01/AG02/AG04（0.3.0 宿主动态门）、PK04（hosted runner）。逐条见 closure-r01 文档。

## 两低危安全发现（不提升为渗透通过）

1. kernel 敏感清单单段匹配，无法表达 `.config/gcloud` 相邻段组合——测试 `test_known_gap_kernel_cannot_match_config_gcloud_pair` 锁定；缓解：precheck 侧独立维护相邻段口径并实测 exit 4；残余风险：仅当用户把 run_root 显式指向此类嵌套目录。
2. pathlib stat 包装吞 NUL 文件名 ValueError，safe_file 不拦——真实拒绝发生在 os 写入层（干净异常、无逃逸）；缓解：os 层防线 + 对抗测试锁定；残余风险：错误消息不指向产品校验层。

## 旧证据影响分析与复用

R01 处提交树缺件修复（15df3f2）后，凡引用"工作区测试全绿"的历史证据改绑新 SHA 复用（行为未变，测试为超集）；H06/L 档/团队 E2E 等固定 SHA 证据经逐项影响核对：R02–R07 改动不触及 dwf.ts 分析闸门语义与 bench 计时路径（test_l_tier 13 项复跑通过），原证据继续有效；受影响项（status 路径）已重验并出新证据。

## 待授权清单（各自独立，只阻断对应操作）

LICENSE 选择｜0.3.0 安装升级｜XL/XXL 大档压测｜全量深读模型调用｜push/tag/合并 main/发布｜真实用户项目迁移（本轮始终未做；迁移仅发生在隔离 scratch 副本）。

## 复核包

`agents-optimization-v1-review-1a7bc698.zip`（同目录 SHA256 见 MANIFEST）：候选已跟踪源码（git archive）+ 显式清单证据（docs/verification/ 全部记录与原始日志）+ 41/48/68 映射 + 宿主探针/H06/团队 E2E/L 档/R05 复测/XL-XXL 准备记录；排除 .git/.DS_Store/fixture 实体/任何密钥（扫描零命中）。

## 候选交付定点复核附录（2026-09-20 第二轮；候选更新为 82fe866）

用户四项定点核对结论（证据=tests/closure_store/test_team_identity_rollback.py 9 项、tests/test_ix05_acceptance.py 5 项、coverage.json 校验输出）：

1. **IX05 → PASS（验收正确性半）**：check/accept/receipt fingerprint=全量 sha256 现算、集成候选/版本 sha 对 sha、审查绑内容 sha——同 mtime+size 内容变化（等长内容+utime 恢复，stat 面确证一致）下已接受成果回落 awaiting_review、集成版本 superseded、错配审查结论拒绝；scanner 缓存不刷新确证为索引层已知限制（对照面 hash_reused=1/computed=0），**缓存限制 ≠ 验收缺陷**，无需产品修复。
2. **team 路径**：查实真实逃逸缺陷并已修复——team.db 为指向外部有效库的 symlink 时读写了项目外事实（外部文件 sha256 变化）；修复=项目目录 realpath 归一（四种别名同一事实源）+ .dev-companion/team.db symlink 无条件拒绝（错误含 readlink 目标，外部字节不变）；symlink 项目根仍为合法别名。
3. **team 回退分级**：新增 team-rollback——空初始化与迁移后无新写入可回退（manifest 计数核对、legacy JSON 哈希不变）；有新事实无导出拒绝（报 N 条），--export-first 先导出草稿+全量事件日志（round-trip 可再迁移）。新增任务/审批/集成事实不因删库静默丢失。
4. **V1 职责映射**：并入 companion-checker/Q1（测量执行+数字复核是检查者职责延伸），输入=technical check_commands 登记的测量命令+bench_runner 工具链，输出=实测数字+样本数+环境（未测逐条 NOT_MEASURED/NOT_RUN_AUTH），权限=只读测量+scratch 纪律。17/17 mapped，validate_coverage.py PASS，未新增角色文件。

**修订计数**：68 验收 54→**55 PASS**（IX05 验收正确性半）；PARTIAL 9→8。48 问题计数不变（本次 team 逃逸系新发现即修缺陷，不属 48 项原编号，已计入候选 82fe866 变更与测试）。测试：python 662（643+5 ix05+9 team+5 容量判定等）/node 57。**复核包以 82fe866 重建，旧 1a7bc698 包作废。**

## 独立复核修复轮（FIX-01–FIX-06，2026-09-21；被审对象 1a7bc698，修复后候选 548f03a）

独立复核者对 1a7bc698 包给出 CHANGES_REQUIRED（SR-01–07）。本轮先在真实 HEAD 复现、再最小修复，全部转为负向/正确性断言：

| SR | 复现→处置 | 修复提交 | 证据 |
|---|---|---|---|
| SR-01 team 门禁绕过 | team-task 直接写 done/ready/blocked 三条 exit 0 → 全部 exit 2 拒绝（门禁动作模型：done 需未完结 attempt+成功回报+有效批准+证据行同事务） | 95f2e9f | tests/closure_store/test_fix04_gates.py 9 项；探针 stderr 拒绝原文 |
| SR-02 越界库 | symlink 逃逸已由 36621cc 堵住；本轮补只读打开分离（ro URI、不建库/不迁移/无 journal）、目录/fifo/损坏/schema 未知拒绝 | 3ffdeb9 | tests/closure_store/test_fix01_readonly.py 8 项（外库哈希/表结构不变断言） |
| SR-03 双进程同 epoch | 复现（both epoch=2、A 覆盖）→ exclusive_lock 临界区 + (epoch,owner) 身份对；修复后探针双写者全拒、seed 台账原样 | 9559b93 | tests/recovery/test_resume_race.py 7 项 + boundaries/resume 探针终版 JSON |
| SR-04 回退丢事实 | 导出改事件日志全量折叠（sidecar 保不可映射事实+逐条 warnings）；done→awaiting_review 显式降级映射（不升 accepted）；草稿过真实 Project.load | 3ffdeb9 | tests/closure_store/test_fix03_rollback.py（new_task_missing_in_export=false） |
| SR-05 入口不识别 team | team 模式路由：有 team.db 无 state.json → status/progress/resume/check 走同一团队协调服务；team-only 项目正常 status exit 0 团队视图；RUNBOOK 重写为真实入口（精确 allowed_paths、双 worker attempt/approve/integrate） | 95f2e9f | tests/team_e2e/test_team_cli_e2e.py 6 断言（真实隔离/审查/HTTP/跨进程恢复） |
| SR-06 DWF 不接容量组件 | ≤200 文件保持直读（H06 路径），>200 走 vendor kernel 索引：A1 摘要+清单引用（不 stringify 全集）、G1 分页比对、G2 经 chunks-page 每批 ≤3+原子检查点+generation/anchor 绑定（混代拒绝）；覆盖三分母经 CoverageLedger | 2656364 | tests/test_precheck_index.py 27 项 + workflow 8 项断言；800 行 kill -9 续跑实测（只领剩余 10 块、并集=全集） |
| SR-07 测试口径 | /private/var 断言改可移植 symlink 用例+darwin skip；build_vendor archive 模式（AGENTS_SOURCE_COMMIT 强制 archive: 前缀，SHA 形态 exit 2 拒绝伪造）；scan 跳过条件改系统 git 可用性；计数口径文档（collected/testsRun/方法级/类级 skip 分列） | 548f03a | 两种形态实测：checkout packaging 9/9、去 .git 副本 3/3+git 组 skip；计数文档 |

**候选更新**：1a7bc698 → **548f03a056600fe5b896cf09cf87080e60866944**（本轮 8 提交）。测试：python **725 collected / 725 testsRun / 725 success / 0 failure / 0 error**（12 类级门控 skip=34 方法不运行，另 1 方法级 darwin skip 待 Linux 复跑确认）；node **65/65**。复核者两条 Linux 测试失败（r05 路径、vendor git 前提）均已修复为可移植/分形态断言；其证据身份保留于 docs/verification/fix-review/。

**边界（如实）**：真实宿主 0.3.0 团队验收、>200 文件 DWF 大清单端到端、XL/XXL 大档、冷缓存、Windows、Linux 本轮复跑——均 NOT_RUN/待授权。SR-05 的 runbook 交互细节（浏览器走查等）仍属宿主门。

## 三项收口轮（FIX02/04/05-followup，2026-09-21；候选 3cec37937a42005630f837a206fa259759aaf636）

独立复核者对 bf974a97 给出 CHANGES_REQUIRED（三条收口链）。先复现、再最小修复，全部转为
负向断言+正向对照（探针按 HARNESS_ERROR 归档，见 fix-review2/probe-status-3cec379.md）：

- **FIX02-followup**：exclusive_lock 空文件窗口（双进程同时入界，双解释器复现）→ v2 原子
  发布拥有者记录（fsync+os.link，锁文件要么不存在要么内容完整）+ owner_token 身份释放/
  接管；空/半写不当死 PID 证明（宽限后接管+警告）。10 项新测试（双进程 max 并发==1、顺序
  进展、产品错误码拒绝、强杀恢复）。旧探针 BrokenBarrierError 证据分类纠正为 HARNESS_ERROR。
- **FIX04-followup**：批准/集成未绑定实际产物 → report 采集逐文件真实 sha（changed_files ⊆
  allowed_paths 逐条核对），done/integrate 前复核当前 sha==报告 sha（漂移 exit 2 指名文件
  与新旧哈希）；integrate manifest 记录 regressed_on sha 集合，feature_level 仍如实 unknown；
  重报幂等键修复为 <attempt>:<sha>；合法新版本重报→重审→重测正向对照通过。
- **FIX05-followup**：G2 标记互覆 → per-chunk 唯一键文件（N 成功=N 恢复，幂等）；12k 块
  init/read 全有界；index-verify 流式聚合指纹（常规路径不装全集）；chunks-page 真分页；
  A1/argv 不再携带全路径；acquire resume 语义（同锚 adopt/异锚拒绝，新建排他不放松）；
  续接闭合 3+2=5 双证明。

测试：python **759 collected/759 testsRun/759 success/0 fail/0 error**（12 类级门控 skip）；
node **66/66**。两插件 vendor 逐文件核验一致（52+52）。保留项不变：宿主安装/真实团队验收、
>200 文件端到端、XL/XXL 大档、冷缓存、Windows、Linux 复跑、LICENSE——均 NOT_RUN/BLOCKED。

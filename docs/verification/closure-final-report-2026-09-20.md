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

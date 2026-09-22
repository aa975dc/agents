# 进度检查点（2026-09-20，配额中断前最后一批）

执行规则 10 要求的事实检查点。恢复时以本文件 + git log 核对，不凭聊天回忆。

## 已完成任务（全部已提交，基线 204a9146 → HEAD f9a9543）

| 任务 | 提交 | 验证证据 |
|---|---|---|
| P0-01 基线 | fccf78e | docs/audit/baseline-2026-09-20.md（HEAD==基线，隔离实跑 106/36/demo 全绿） |
| P0-02 宿主探针 | fccf78e→1359e6b | docs/host-contract/host-probe-2026-09-20.md（H01/H02 正反编译、H04 三态发布、H05 排他 mkdir、5 条 facade 硬约束） |
| P0-03 48项映射 | fccf78e | docs/audit/registry-mapping-2026-09-20.md（3 处 registry 修正：Z23/Z29/Z12） |
| P1-01 phase 字面量 | fccf78e | 六处字面量+静态守卫测试；真实宿主编译通过（dwfrun-2bea6e0b 起） |
| P1-02 四根+预检 | 3c6d6d3 | precheck.py 四子命令+19 测试；dwf 预检改真实回执 |
| P1-03 特殊文件/孤立状态 | fccf78e | Z04/Z14：非纳管跳过、纳管阻断、孤立诊断，3 测试 |
| P1-04 回流/partial/schema | 8fafb7b | 初次+2 轮回流、confirmed 保留、from_contract 对齐、8 新测试 |
| P1-05 CI + H06 | 92b0ef6 等 | CI 三作业（YAML 验证+manifest 本地过）；H06 见下节 |
| P2-01 共享内核 | 92b0ef6 | 环解除、四分叉合并、15 kernel 测试 |
| P2-02 SQLite 事实库 | 50670aa | 单写者 epoch、CAS、崩溃恢复，15 测试 |
| P2-03 缓存/分页/新鲜度 | 20f61ed | status 解析 3→1（计数证明）、分页≤100、超限降级，19 测试 |
| P2-04 迁移/恢复 | 88822e0 | dry-run/幂等/强杀恢复/回退导出（core.Project 实读通过），9 测试 |
| P2-05 vendor 分发 | d347a7e | 仓外独立运行证明、MANIFEST、版本门禁，6 测试 |
| P3-01 流式普查 | 9b1224b | git/plain 双模式、generation 快照、中断恢复，10 测试+6000 文件冒烟 |
| P4-01 产品角色 | 87807c4 | 角色+契约+5 测试 |
| P4-02 设计角色 | f9a9543 | 五态矩阵 schema+样例+7 测试，文档-样例零漂移 |

测试总量：**python 218 OK / node 48 pass**。

## H06 真实宿主 E2E 状态（P1-05 剩余）

- 第 4 轮（dwfrun-8141cfdf）：预检/G1/G2/G3/G4 **全部真实通过**，G4 中 A6 独立复核 14/14 verdicts confirmed（各带 own_evidence）；G5 因 `description` 超 500 字符被宿主拒绝——**受控失败路径再次实证**（17 发现+14 结论 salvage 保留，partial-report 发布）。
- 修复已提交（1359e6b clampMeta）。
- 第 5 轮（dwfrun-6859f047）：G3 阶段被供应商配额停住（1310）。**配额 2026-09-23 21:32:18 重置后** ResumeWorkflowRun 该 run（预检/G1/G2 从日志重放）；若不可恢复则全新 CreateWorkflow 带 args（Amend 不继承 args，见 host-contract 文档第 5 条）。
- H06 完整通过前，G5 发布与报告制品的最终验收记 **NOT_RUN**。

## 进行中（未提交）

- P3-02：`packages/agents_kernel/indexing/content_hash.py`（124 行，语法完整，**无测试**）留工作树；modules/edges/closure 与测试未开始。续接者先审阅补测再提交。

## 未开始（依赖就绪）

P3-03（切片）、P3-04（A6/A7 分片）、P3-05（证据失效）、P3-06（L 档实测）、P4-03/04/05、P5-01…06、P6-01…05、P7-01…06。

## 容量认证

L / XL / XXL = **NOT_RUN**（P3-06/P6-05 未执行；不得声称已支持）。

## 待授权/独立决策

- LICENSE 选择（Z07/P7-04）：权利人（用户）明确选择前不动。
- push / 合并 main / 发布插件 / 安装副本升级：未授权，未执行。
- XL/XXL 物理压测资源：未授权。

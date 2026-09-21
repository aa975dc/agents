# R01 结算：基线固定、41 任务映射、68 验收清点（2026-09-20）

## 基线固定

| 项 | 值 |
|---|---|
| 收尾轮起始 HEAD | 8ed8806（与上轮报告一致，无第三方漂移） |
| 工作区脏状态鉴定 | 全部为上轮批次 `git add` 范围遗漏的已完成成果：tasks.py G-REVIEW（P5-04 提交缺文件）、test_precheck ClaimsPagingTest（P3-03 同）、test_coverage.py（P3-04 同）、4 个 kernel `__init__` 文档串、vendor 自动同步、L 档原始日志 |
| **HEAD 树自洽性发现** | 8ed8806 干净 checkout 实测 **593 ran / 38 errors**（worktree + vendor 同步后）——此前"全绿"均基于工作区而非提交树 |
| 处置 | 遗留成果补提交（保留、非覆盖）→ **新自洽基线 `15df3f2`**：干净 checkout + vendor 同步 = 616 ran / 611 pass / 5 skip（L 档门）/ 0 fail。本轮全部后续证据绑定此 SHA |
| 实际安装版本 | dev-companion 0.2.0（另有 0.1.0–0.1.2）；code-analysis-swarm 0.2.0+0.2.1；**0.3.0 未安装**（待授权） |

## 41 任务映射（原 task_plan.json 逐 ID；上轮"40"为分母误计——P3 实为 6 项）

无漏项、无改分母。执行证据按任务分别见 docs/verification/ 各记录与提交。

| 原 ID | 执行情况 | 主要提交 | 验证 |
|---|---|---|---|
| P0-01 | 完成 | fccf78e | docs/audit/baseline（隔离实跑 106/36/demo） |
| P0-02 | 完成 | fccf78e→1359e6b | docs/host-contract（H01/H02/H04/H05 + 5 条 facade 约束） |
| P0-03 | 完成 | fccf78e | docs/audit/registry-mapping（48 项+3 修正） |
| P1-01 | 完成 | fccf78e | 六处字面量+静态守卫 |
| P1-02 | 完成 | 3c6d6d3 | precheck.py+19 测试 |
| P1-03 | 完成 | fccf78e | Z04/Z14+3 测试 |
| P1-04 | 完成 | 8fafb7b | 回流/partial/schema+8 测试 |
| P1-05 | 完成 | 92b0ef6, 3e11812 | CI+H06 通过 |
| P2-01 | 完成 | 92b0ef6 | kernel+15 测试，行为等价门 |
| P2-02 | 完成 | 50670aa | SQLite 事件库+15 测试 |
| P2-03 | 完成 | 20f61ed | 缓存 3→1+分页+19 测试 |
| P2-04 | 完成 | 88822e0 | 迁移+9 测试（强杀恢复） |
| P2-05 | 完成 | d347a7e | vendor+6 测试（仓外运行） |
| P3-01 | 完成 | 9b1224b | 流式普查+10 测试 |
| P3-02 | 完成 | 50defd4 | 增量/闭包+21 测试 |
| P3-03 | 完成 | fd03118 | 切片/预算+42 测试（precheck claims 测试曾遗漏，R01 补齐） |
| P3-04 | 完成 | 0d10a85 | A6 分片+coverage.py（单测曾遗漏，R01 补齐） |
| P3-05 | 完成 | 7dc5dfe | 证据新鲜度+15 测试 |
| P3-06 | 完成 | 594ca17 | **L 档实测通过**+10 测试（原始日志 R01 补入库） |
| P4-01 | 完成 | 87807c4 | 产品角色+5 测试 |
| P4-02 | 完成 | f9a9543 | 设计角色+7 测试 |
| P4-03 | 完成 | c11b7b8 | 后端角色+16 测试 |
| P4-04 | 完成 | 27d0b9a | 接线+静态宿主契约 7 测试 |
| P4-05 | 完成 | f100a1e | 交接 schema+24 测试（tasks.py 依赖曾遗漏，R01 补齐） |
| P5-01 | 完成 | 7017c97 | 三层模型/DAG+34 测试 |
| P5-02 | 完成 | 67b3175 | 租约/幂等/退避+27 测试 |
| P5-03 | 完成 | bd0f5fe | 隔离/ownership+26 测试 |
| P5-04 | 完成 | ac242d9 | 审查台+17 测试 |
| P5-05 | 完成 | f5c3a4b | 集成版本+21 测试 |
| P5-06 | 完成 | be4c289 | 团队 E2E+8 测试（真实链路+如实模拟点） |
| P6-01 | 完成 | 63761df | 分片+21 测试 |
| P6-02 | 完成 | a8e8f58 | 协调/分页汇总+22 测试 |
| P6-03 | 完成 | 610551c | campaign/背压+17 测试 |
| P6-04 | 完成 | 41e8bcf | ResumeLedger+12 测试 |
| P6-05 | 完成（登记） | 00c0030 | XL/XXL NOT_RUN 登记及命令 |
| P7-01 | 完成 | 35d2868 | 安全回归 32 项（无高危，2 低危锁定） |
| P7-02 | 完成 | 26b2f5b | 注册表/README/DESIGN+15 测试 |
| P7-03 | 完成 | 87eb36d | 跨平台+9 测试 |
| P7-04 | **blocked** | 无 | LICENSE 待权利人选择（不代选） |
| P7-05 | 完成 | 5a17bbc | 0.3.0+CHANGELOG+打包 5 测试 |
| P7-06 | 完成 | 8ed8806 | 终验报告（本轮 R01/R08 修订其口径） |

计数：40 完成/登记 + 1 blocked（P7-04）= 41/41。

## 68 条验收规格清点

状态口径：PASS=规格要求内真实证据；PARTIAL=部分子项实测+其余 NOT_RUN 并列明；NOT_RUN=未执行（原因）；BLOCKED=待授权。测试跳过不记 PASS。

| ID | 状态 | 证据/边界 |
|---|---|---|
| H01 | PASS | 真实编译正反探针（host-probe §H01） |
| H02 | PASS | 变量命令拒；字面量执行 exit 0；H06 全程 agent 循环 |
| H03 | PARTIAL | 角色加载+派发实证（a1–a7 会话调用、H06 内 7 角色）；缺角色/权限负路径 NOT_RUN |
| H04 | PASS | 三态发布探针（外/symlink 拒、内成） |
| H05 | PASS | 排他 mkdir 0/1/0 + precheck 19 单测 |
| H06 | PASS | 六闸门端到端（h06-acceptance；status=partial 为诚实语义） |
| FS01 | PASS | test_special_files a + scanner 特殊记账 |
| FS02 | PASS | test_special_files b（纳管阻断） |
| FS03 | PASS | precheck 逃逸用例 + workspace 清理拒 symlink + ownership（test_dispatch_security） |
| FS04 | PASS | 换行文件名扫描 + NUL 干净错误（security） |
| FS05 | PASS | 5MiB 零读取 + L 档 10 大文件；冷缓存 NOT_RUN 另列 |
| FS06 | PASS | 敏感路径 kernel+precheck 双口径 + 角色红线 |
| FS07 | PASS | fsync 顺序/fd 清理/竞态保旧（test_kernel+security） |
| FS08 | PARTIAL | GBK 注入近似测试过；真 Windows 控制台 NOT_RUN |
| ST01 | PASS | 132 行为等价门 + 回退导出 core.Project 实读 |
| ST02 | PASS | 孤立诊断 + STATE_MISSING_WITH_RELEASE |
| ST03 | PASS（测试级） | 迁移/回退/强杀恢复 9 项；真实用户项目迁移 NOT_RUN（待授权） |
| ST04 | PASS | CAS/epoch/崩溃注入 + 跨连接重开 |
| ST05 | PARTIAL | 1000 事件线性（3.4x）；n=10000 历史实验 NOT_RUN |
| ST06 | PARTIAL | status 缓存 3→1/降级单测过；**真实入口 L 档无源树扫描实测 = 本轮 R05** |
| ST07 | PASS | 新鲜度/两层失效/严格门（存储态不变断言） |
| ST08 | PASS | archives fsync+恢复回归 |
| IX01 | PASS | 流式枚举+L 档实测 |
| IX02 | PASS | ls-files -z+porcelain -uall |
| IX03 | PASS | plain scandir |
| IX04 | PASS | 1%→1000:99002 精确 |
| IX05 | PARTIAL | 同 mtime+size 内容变更检测未实现（size+mtime 复用口径的已知限制，代码注释声明） |
| IX06 | PASS | 跨代模块 id 稳定 |
| IX07 | PASS | 跨片边+unknown+二次解析 |
| IX08 | PASS | generation vector+陈旧游标拒 |
| CV01 | PASS | 切片边界/中文/超长行 |
| CV02 | PASS（机制） | campaign 严格前缀/暂停≠抽样/完整分母；真实模型全量深读 NOT_RUN |
| CV03 | PASS | A6 按批+合并闭合 |
| CV04 | PASS | 第 1 轮 E2E 真实回流证据 |
| CV05 | PASS | 有界批次+峰值内存断言 |
| CV06 | PASS | A7 只引用核验制品（H06 真实） |
| AG01 | PARTIAL | 静态宿主契约过；**0.3.0 动态加载 NOT_RUN**（未安装） |
| AG02 | PARTIAL | 联审门机器验证（B 层）；真实宿主设计联审 NOT_RUN |
| AG03 | PASS | 契约校验器+实现一致性（抓到 revision 漂移） |
| AG04 | PARTIAL | 团队 E2E 真实模块+真实 HTTP（B 层）；宿主体验 NOT_RUN |
| AG05 | PASS | 轻量模式关键词守护+行为等价门 |
| TK01 | PASS | DAG/环路径/传播 |
| TK02 | PASS | 隔离+冲突拒绝 |
| TK03 | PASS | TTL 接管+僵尸拒（子进程 kill） |
| TK04 | PASS | 幂等去重/矛盾拒 |
| TK05 | PASS | 取消传播+进程树杀回归 |
| TK06 | PASS | 自审拒/sha 漂移拒/失效 |
| TK07 | PASS | 版本幂等+两级门+superseded |
| TK08 | PASS | 三层分离+attempt 计数 |
| TK09 | PASS | plain 目录+无 Git 副本隔离 |
| SC01 | PARTIAL | L 档扫描/查询/恢复 PASS；**status 大项目端到端 = 本轮 R05** |
| SC02 | NOT_RUN | XL 物理（待授权；入口 R07 补） |
| SC03 | NOT_RUN | XXL 索引行（同上） |
| SC04 | NOT_RUN | XXL 物理（同上） |
| SC05 | PARTIAL | 底层 53μs 实测；**CLI 入口 30 样本 = 本轮 R05** |
| SC06 | PARTIAL | n=1000 线性；n=10000 NOT_RUN |
| SC07 | PASS | 预算暂停/游标 |
| SC08 | PASS | ResumeLedger 崩溃续接 |
| SC09 | PASS | dry-run/marker/清理+预算内 |
| PK01 | PASS | 仓外独立运行+打包门 |
| PK02 | PASS | manifest 一致+registry --check+校验器 |
| PK03 | PASS | 文档路径/退出码/命令同步测试 |
| PK04 | PARTIAL | YAML+manifest 作业本地实跑；hosted runner NOT_RUN（无 push 授权） |
| PK05 | BLOCKED | LICENSE 待决定 |
| PK06 | NOT_RUN | 实装 0.3.0 待授权（现装 0.2.0/0.2.1） |
| SEC01 | PASS | 注入面+参数化隔离 |
| SEC02 | PASS | 执行环境/清理边界 |
| SEC03 | PASS | remote_disabled 结构化拒绝 |

计数：**PASS 51 / PARTIAL 12 / NOT_RUN 4（SC02–04, PK06）/ BLOCKED 1（PK05）= 68**。

PARTIAL 的未测半项归属：ST06+SC01(status)+SC05(CLI 计时)→本轮 R05；FS08/AG01/AG02/AG04→安装授权门；ST05/SC06→历史压力实验（本地可做，列 R07 附带）；IX05→已知限制（不冒充）；PK04→push 授权。

## 续接摘要修正

resume-card-2026-09-20.json 与记忆摘要中的"15/40""40 任务"为过程时点数据，与 41 项分母冲突——已由本文件取代为权威进度；续接卡改指向本文件与 final-acceptance 修订版（R08 产出）。

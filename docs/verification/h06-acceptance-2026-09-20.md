# H06 真实宿主小库端到端验收记录（P1-05，2026-09-20）

## 结论

**通过（status=partial，partial 为诚实的部分完成而非失败）**。修复后的 `code-analysis.dwf.ts` 于真实 ZCode 宿主（CreateWorkflow 入口，run dwfrun-6859f047-0f8d-438a-b984-858c15a7d4b8）完整走通预检→G1→G2→G3→G4→G5 六道闸门，发布 341 行中文分析报告（主制品 `report`，28665 字节，sha256 c402844690b3…；文件制品 `report-file` 同步发布）。

## 验收对照（09_TEST §五层证据：本记录属 C 层——真实 ZCode 编译与 Agent/制品）

| 项 | 要求 | 结果 |
|---|---|---|
| 真实编译 | 非mock编译入口 | CreateWorkflow 编译通过（对照：基线版本 H01 反例被拒，见 host-probe 文档） |
| 真实代理 | 全部 7 角色 | A1/A2/A3/A4/A5/A6/A7 全部经 DWF agent() 真实调用并返回 |
| 闸门 | 六道全过 | 预检/G1/G2/G3/G4/G5 全过（gate_checks 六条留痕） |
| 独立复核 | 逐条 verdict | 14/14 送验结论全部有状态，14 confirmed / 0 refuted / 0 unverified，A6 各带 own_evidence（含本机实测命令输出：pytest 1 passed、slugify('你好世界')==''、fan_in/fan_out 脚本输出、DFS 环检测输出） |
| 报告核实 | 不信代理 path | helper read-report 核实 size/sha256/正文后发布 |
| 源码不变 | 被审计仓库零写入 | /tmp/h06-fixture HEAD bf5df8f 全程未变（git status 干净） |
| 回流 | ≤2 轮 | 本轮无需回流；第 1 轮 E2E（dwfrun-2bea…）曾实证：G3 可修复失败经初次+2 轮后仍不闭合→按设计 blocked，非静默通过 |
| partial 保留 | 失败不抹成果 | 第 1/4 轮 blocked 时 confirmed 结论与发现完整保留并发布 partial-report；本轮 not_covered 20 条逐条保留（含 WebSearch 配额受限导致的"待查证"保守标注，未编造 CVE） |
| 发布边界 | workspace 约束 | run_root 在宿主 workspace 下（排他创建），正文 artifact.markdown，文件 workspace 相对发布成功 |

## 容量与边界声明

- 本验收对象为 3 模块 7 文件小库（L/XL/XXL 容量认证仍为 **NOT_RUN**，归 P3-06/P6-05）。
- status=partial 的原因： specialties 存在如实声明的 not_covered（构建未实际执行、CVE 联网核验受限等）——partial 语义正确，不得读作"全部已验证"。
- 报告中的发现（slugify 非 ASCII 丢失、入口不可执行、测试覆盖不足、无构建后端、依赖未钉等）均为对 fixture 的观察，置信度与验证状态逐条随附。

## 对问题登记表的影响

- **Z01 关闭条件满足**：六处 phase 字面量修复 + 真实正反编译探针（H01/H02）+ 真实执行（H06）。登记状态：resolved（证据：host-probe 文档 + 本记录 + run id）。
- **Z19 部分关闭**：真实编译/看板/边端点已在守卫测试与 E2E 覆盖；跨块模块名冲突用例待多块 fixture（P3-04 分片时补）。
- **Z02/Z09 关闭**：H04 三态 + H05 排他 mkdir + helper 真实回执（见 host-probe 文档）。
- **Z03 关闭**：第 1 轮 E2E 实证可修复失败回流 ≤2 轮、超限即 blocked、partial 保留。

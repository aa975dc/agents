# 候选身份勘误与隔离安装配置声明（2026-09-21，复核者两项定点）

## 1. 被测版本勘误（四元分离）

host-acceptance 报告头部误写"候选 bf974a97"——**实际被测安装树=1977b365 的两插件子树**（树指纹逐字节对照证明）。勘误如下，原文行不改（本节为勘误记录）：

| 维度 | 值 |
|---|---|
| candidate_source_sha（被测源码树） | 1977b365 的 dev-companion 子树=`7cc5fc26ed1ceaee8e901da221a8247e98e62543039cefc3bef2795736b7cd14`（100 文件）、code-analysis-swarm 子树=`c134f018ec76025a00e11b2abb9dfc64ce698481b3cf5e124c9a04ea0ddcb8f8`（66 文件）——与 git archive 1977b365 同算法同值 |
| installed_tree_digest（安装树） | 与上完全一致（安装树=1977b365 内容的拷贝）；bf974a97 与 1977b365 的差异=三项收口两轮修复（2d4e857/59ec832/c6a1549），**被测安装树已含三项收口修复** |
| tested_cli_sha256 | `edb2d7ec7f95f19622974603d0befd3873827fc110ecbf5657abff5809d2d5b2`（=1977b365 companion.py；≠bf974a97 的 26ae3ca9…） |
| evidence_commit_sha | 1ea6713（仅新增 docs/verification 验收记录与证据拷贝，产品代码与 1977b365 零差异——git diff 1977b365..1ea6713 -- 产品目录为空，已实测） |

历史日志（含被测安装树为 1977b365 内容的实测记录）不因勘误改写；勘误只纠正头部标签。

## 2. 隔离安装配置声明（脱敏）

| 配置 | 变更 | 生产影响 |
|---|---|---|
| ~/.zcode/cli/plugins/known_marketplaces.json | +1 条目 agents-isolated-mkt（directory 源 /Users/youxididai/Documents/cj/agents-isolated-mkt，pluginCount 2） | 生产 aa975dc-agents 条目原样保留 |
| ~/.zcode/cli/plugins/installed_plugins.json | +2 条目 dev-companion@agents-isolated-mkt、code-analysis-swarm@agents-isolated-mkt（0.3.0，scope user） | 生产两条目（0.2.0/0.2.1）原样保留，实测 old_prod==new_prod |
| ~/.zcode/cli/config.json → plugins.enabledPlugins | +2 键 =true | 生产条目键未动 |
| cache/aa975dc-agents/** | **字节未变（未触碰）** | — |
| cache/agents-isolated-mkt/** | 新增（安装树，见上指纹） | — |

**回退方法**：删除 known_marketplaces.json 的 agents-isolated-mkt 条目与 marketplaces/agents-isolated-mkt/、删除 installed_plugins.json 两条 @agents-isolated-mkt 条目与 cache/agents-isolated-mkt/、删除 config.json 两个 enabledPlugins 键——即完全回到安装前状态（备份见 agents-isolated-mkt/.backup-20260921-204754/）。共享配置的三处变更属隔离安装必需接线，未含任何凭据。

## 3. 浏览器走查结论（页面级，候选 3cec379 线）

- 空态/添加/升序/勾选/刷新持久化/空输入 alert（`任务内容不能为空`，无新行无请求）/409 冲突提示（`数据已被其他操作更新，已自动刷新`+外部任务自动出现）全 PASS——截图 browser-walkthrough-3cec379-line.png（50837 字节）。
- 记录一处 UX 缺口（不阻塞）：输入框 placeholder 写"回车确认"但回车键未绑定添加行为，仅按钮生效——候选后续版本候选修复项。

# FIX04-followup 收口记录（2026-09-21）

对应：01_独立复核报告.md §4（原 FIX04 剩余问题：批准/集成没有绑定实际被执行的源码）、
02_三项收口任务.json `FIX04-followup`、evidence/11_boundaries_system.json。
基线：分支 optimization-v1，HEAD bf974a97；本任务未 commit。

## 1. 修复前复现（本机确认，非转抄）

按复核者步骤在本机重跑（/tmp 脚本，修复前 HEAD bf974a97 + 原 vendor）：

| 步骤 | 结果 |
|---|---|
| allowed_paths 仅 app.py 的任务，app.py VALUE=1 → running→report→approve | exit 0 |
| app.py 改 VALUE=2（等长 + 恢复 mtime）后 `team-task done` | **exit 0（应拒绝）** |
| `team-integrate` 在同目录跑 `import app; assert app.VALUE == 2` | **exit 0、completed、passed=true（被测内容≠批准内容）** |
| 另一任务仅允许 only.py 回报 outside.py | **exit 0（越界接受）** |

## 2. 实施内容（全部在文件域内）

- `packages/agents_kernel/storage/team.py`
  - report 收紧：changed_files 逐条核对 ⊆ 任务 allowed_paths（越界即拒并指出越界
    文件；未声明边界（空 allowed_paths）的任务不得回报 changed_files）；每个改动
    文件在报告时点从工作区（`--workspace`，缺省项目根）真实读取并计算内容 sha256
    （复用 `digest.sha256_file`）记入回报事实 `file_shas`——报告摘要由真实采集产生。
  - done 门升级（新前置 (e)）：回报的每个改动文件当前内容 sha 必须仍等于报告时
    采集值（漂移/缺失即拒绝，指出文件与新旧哈希——等长同 mtime 篡改因此无效），
    并复核 changed_files ⊆ allowed_paths；通过后把核验过的 file_shas 记入完成证据。
  - integrate 绑定冻结候选：入列前对每个候选任务重执行 done 门产物核验（回归目录
    cwd 内当前 sha == 报告时 sha），任一漂移整体拒绝、不落任何事件（候选失效，须
    重报重审）；通过后把每任务的报告 sha 集合（artifact + 逐文件）作为
    `regressed_on` 记入版本级回归证据与完成证据，并随 CLI 结果返回。版本级回归
    命令即在漂移核验通过的同一目录、同一内容上真实执行；feature_level 仍如实标
    unknown（conservative=true），未虚标已验证。
  - 重报幂等键修复：team-report 幂等键由 `team-report:<attempt>` 改为
    `team-report:<attempt>:<sha>`——同版本重报去重不变；内容变化的新版本必须落为
    新事实（修复前重报被旧键静默吞掉，重报→重审链根本走不通，正向对照发现）。
  - 历史兼容：本修复前落库的回报无 file_shas，done/integrate 跳过产物复核与范围
    复核（"门禁只管新写入"，与 TaskBoard.adopt_task 同一口径）。
  - gate_check 输出加 `audit_only: true`，docstring 明确台账审计≠功能验证。
- `dev-companion/scripts/companion.py`（team-* 段）：team-task、team-report 增加可选
  `--workspace`（done 产物核验目录 / 改动文件采集目录，缺省项目根）。
- `dev-companion/commands/companion-check.md`：加一句——team 模式 check 是只读台账
  审计（audit_only:true），不等同于功能验证，验收以真实检查/回归为准。
- 复用既有模块：WorkspaceManager 不需改（worker 场景由 --workspace 指向真实
  worktree）；ReviewBoard 的 sha 绑定原样复用（重报新 sha → 旧批准对同 attempt 的
  新提交自动失效，invalid_reason"审后 subject 已变化"）；IntegrationBoard 未改
  （regressed_on 记在 team 层事件证据与返回值，manifest 内容哈希语义不动——理由：
  版本号/哈希的既有幂等契约被多处测试逐字锁定，改 canonical 形状属另一层级变更）。

## 3. 绑定链路（修复后）

```
attempt(running)
  → team-report：changed_files ⊆ allowed_paths + 逐文件报告时 sha（file_shas，真实采集）
                + artifact_sha256（声明固定版本，提交审查）
  → team-approve：ReviewBoard 绑定 artifact_sha256（重报新 sha ⇒ 旧批准自动失效）
  → team-task done：当前文件 sha == 报告时 sha（逐文件）+ 范围复核 + 有效批准
                → 完成证据记录核验通过的 file_shas
  → team-integrate：对 cwd（被测目录）重核同一批 sha → 版本级回归真实运行于同一内容
                → regressed_on（每任务 artifact+逐文件 sha）进版本级回归/完成证据
```

## 4. 修复前后对照（同一脚本）

| 探针 | 修复前 | 修复后 |
|---|---|---|
| 漂移后 done | exit 0 | exit 2，逐文件指出新旧 sha（e13df8c4… → 3da6c4b3…） |
| 漂移后 integrate | completed/passed=true | 不再到达（done 拒）；done 后漂移亦被 integrate 拒（见测试） |
| 越界 changed_files 回报 | exit 0 | exit 2，指出 outside.py 越界 |
| check（源码删除后仍 passed） | 误导 | 输出 audit_only=true，明确是台账审计 |

合法重报正向对照：重报新 sha → done 拒（"审后 subject 已变化"，ReviewBoard sha
绑定）→ 重新 approve（subject=新 sha）→ done 成功 → integrate completed，
regressed_on 记录新 sha 集合（返回值与版本级回归事件证据一致）。

## 5. 测试

- tests/closure_store/test_fix04_gates.py：抽出 GateCliTestBase 公共夹具；新增
  Fix04FollowupBindingTests 5 例（越界拒绝、无边界拒绝、真实采集、复核者场景
  done 拒+重报链正向对照、done 后漂移 integrate 拒+恢复后 completed、done 缺文件拒、
  check audit_only）——文件 11→16 例，全过。
- tests/team_e2e/test_team_cli_e2e.py：report/done 传 --workspace（worker 真实
  worktree）；新增断言 regressed_on 逐任务 artifact+files sha、check audit_only。
  集成目录为 worktree 逐字节复制，integrate 重核通过——链路跨目录闭合。
- 受影响既有测试逐条更新（未删测试）：
  - test_fix04_gates.full_chain：按新门禁写入真实产物文件 app/x.py（VALUE = 1\n）；
  - test_done_gate_requires_report_and_review_when_policy_on：t1 补 --allowed-paths
    并写真实文件（原回报文件不存在，新门禁下必拒）；
  - test_integrate_requires_done_tasks_and_real_regression：app.mkdir(exist_ok=True)
    （full_chain 已建目录；写入内容与 full_chain 相同，无漂移）；
  - test_legacy_project_status_identical_without_team_db：legacy status 指纹覆盖项目
    文件树，before 快照前先写入同内容文件，隔离"team.db 存在"单一变量；
  - test_fix01_readonly (t2)、test_fix03_rollback (sum、p4)：迁移导入/无边界任务的
    回报去掉 --changed-files（新门禁拒绝无边界任务声明改动文件；这三处测试目的
    均不依赖改动清单）。
- 回归：`python3 -m unittest discover -s tests -q` 在本任务改动收尾时连续 3 次全绿
  （742 tests OK，skipped=12 为既有平台/Git 前提 skip）。随后共享工作区中并行的
  FIX02/FIX05-followup 任务对 precheck.py/atomicio.py 落入在途编辑（`precheck.py
  --help` 本身 exit 2），全量套件出现 6~16 个失败，全部集中于
  tests/test_precheck_index（FIX05 域），与本任务文件无交集；逐模块复跑
  closure_store/team_e2e/test_companion*/test_z24_closure/test_status_pagination/
  test_ix05_acceptance/test_special_files/test_evidence_freshness/test_releases
  全部 OK，node `--test` 65/65 通过。共享树统一收口时需以最终源树再跑全量。
- vendor：`tools/build_vendor.py` 同步，两插件 `agents_kernel/storage/team.py` 与源
  逐字节一致（MANIFEST generated_at 随之刷新）。工作区中 atomicio.py/precheck.py/
  tests/recovery/test_lock_v2.py 的改动属并行任务，本任务未触碰；build_vendor 按
  当前源树整树再生成（纯复制，无语义覆盖），统一收口时再按文件核验。

## 6. 遗留与边界（如实声明）

- 绑定粒度是"报告时点逐文件内容 sha + done/integrate 时点复核"，不是工作区级
  全量 diff。等价性理由：回报清单内的每个文件在 report/done/integrate 三个时点的
  内容都被逐字节哈希核对，清单外文件不在该 attempt 的产物声明内（范围由
  allowed_paths 在 report 与消费点双重圈定）——对"被批准/被回归的内容是否就是
  报告的内容"构成等价证明。缺口：attempt 未声明但对集成目录有效的额外文件变更
  （如从未入列 changed_files 的新增辅助文件）不在逐文件核对范围内，只能由版本级
  回归的功能性结果兜底；需要严格工作区级快照时应另立任务（复核报告允许依赖固定
  快照，不要求全仓重哈希）。
- artifact_sha256 仍是实现者声明的固定版本标识（与逐文件采集 sha 并列记录于
  regressed_on/完成证据）；本修复绑定的是文件内容本身，未强制 artifact 等于某
  单文件 sha（多文件产物无此统一口径）。
- 真实宿主/多模型并发、安装与发布路径未在本任务验证（维持 NOT_RUN 口径）。

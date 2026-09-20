# R05：真实 status 路径与 L 档端到端补验记录（2026-09-20）

执行角色：R05 收尾轮（原任务 P2-03、P3-05、P3-06；对应验收 ST06/SC01/SC05 收口）。
验收依据：`09_TEST_AND_BENCHMARK_ACCEPTANCE.md` §1/§3/§5、`02_BOUNDED_CLOSURE_TASKS.md` R05。
范围：tests/closure_r05/（新增）、本记录。**未修改任何产品代码**（工作树唯一产品差异为
`dev-companion/scripts/_kernel_vendor/MANIFEST.json` 的指针元数据，由并行工作流在
15:37Z 重生成，指向同一 HEAD，vendor .py 与 packages .py 对 HEAD 逐字节零差异，`git diff --name-only HEAD` 验证）。

## 1. 被测对象与环境

| 项 | 值 |
|---|---|
| 仓库 / 分支 | /Users/youxididai/Documents/cj/agents @ optimization-v1 |
| subject hash（HEAD） | 00be5f23b340abf31be957161bcd2a82d44b8601（00be5f2，R07 后） |
| 被测入口 | `dev-companion/scripts/companion.py`（legacy `status`/`init`/`confirm`/`packet`/`receipt`/`check`/`accept` + 新 `team-status` 组）；实际加载内核为 `dev-companion/scripts/_kernel_vendor/agents_kernel/`（kernel_bootstrap 优先 vendored，本次与 packages/ 同内容） |
| CPU / RAM（探测值） | Apple M4 10 核（os.cpu_count=10）/ 16 GiB（sysctl hw.memsize=17,179,869,184） |
| OS / 磁盘 | macOS 26.2 arm64 / APFS SSD，临时卷空闲 76.3 GiB |
| Python / SQLite / Git | 3.9.6 / 3.51.0 / 2.50.1 |
| provider 配置 | 无模型调用、无远端消耗（全部本地执行） |
| 计时口径 | 温热（页缓存热；预热 3 次不计入）；完整子进程墙钟**含解释器启动**；RSS=子进程 ru_maxrss（macOS 单位字节），不含宿主/浏览器/模型进程 |

## 2. 资源授权（09 §5）与 fixture

生成器：`tests/benchmarks/fixture_gen.py`（先 `--dry-run` 后实跑；产物仅在
`tempfile.gettempdir()` 下 `benchmark-fixture-` 前缀目录，带 BENCHMARK_FIXTURE marker +
manifest.json；全部在原已授权小档预算内 ≤20 万条目 / ≤2GiB）。三个 scratch：

| fixture | 参数 | dry-run 估算 | 盘上 | manifest sha256 |
|---|---|---|---|---|
| benchmark-fixture-r05L | `--files 100000`（seed 20260920） | files=100000 small=99990 big=10 bytes=251253664 (239.6MiB) inodes=110104 free=76.3GiB limit=2GiB | 110,104 项（100,000 内容文件 + marker + manifest = 100,002 文件 + 10,102 目录；不含后建 .dev-companion），生成 19.029s | `41c935b4dbfc1290f4db3267606e623ee4fb2781bf0d744912b74dcc8ef97a04` |
| benchmark-fixture-r05small | `--files 48` | bytes=55662 (0.1MiB) | 50 文件（含 marker+manifest） | `5fa04cd75452a289d2549ca82af525336c5c77867be911575f20c30e2349ba5b` |
| benchmark-fixture-r05team | `--files 48` | bytes=55662 (0.1MiB) | 50 文件（含 marker+manifest） | `90e32230ed98dcc36581011959f041f601769512f0338057308384778cb725d1` |

L fixture 目录即 dev-companion 项目根（真实用户布局：源码树在项目内），经真实 CLI 初始化：

```
python3 companion.py --project <fixture-r05L> init --input <scope.json>   → exit 0（revision 1）
python3 companion.py --project <fixture-r05L> confirm --revision 1        → exit 0（revision 2）
```

init/confirm 在 10 万文件项目上不做源码扫描、交互正常（无超时/卡顿），任务提示的
"源码树放子目录"后备策略无需启用。

## 3. 插桩方法（计数口径与边界）

`tests/closure_r05/instrument.py` 向 tempfile 构造目录写入 sitecustomize.py，经
PYTHONPATH 注入**单次取证子进程**（30 次计时运行不注入，保证测的是真实 CLI）：
包装 `builtins.open`/`io.open`/`os.open`（低层，pathlib accessor 经 `_NoBind` 补丁接入）/
`os.scandir`/`os.walk`/`os.stat`/`os.lstat`/`sqlite3.connect`，按前缀分类计数：

| 类别 | 前缀 | 语义 |
|---|---|---|
| facts | `<项目>/.dev-companion`（realpath 与原样两种拼写都收） | 事实库/台账，合法读取 |
| source_tree | `<项目根>` | 目标源码树（验收对象：应为 0） |
| plugin | `dev-companion/scripts`（含 _kernel_vendor）、`packages/` | 插件自身代码 |
| runtime | 其余 | 解释器/标准库 |

读打开另计 `read_calls`/`read_bytes`（内容读取量）。如实边界：① CPython import 机制与
SQLite C 层读写不经上述 Python 入口，插件模块加载的内容读取**不可见**（仅 importlib 的
stat 可见），故"插件自身读取"计数值偏低是口径所致，不据此声称零开销；② open 计数含失败
尝试。规则顺序 facts → source_tree（.dev-companion 在项目内，必须先判）。

## 4. 实测结果（check_id / argv / 退出码 / 插桩计数）

正式取证运行起 2026-09-20T15:31:19Z（fixture 生成）止 15:34:01Z（证据落盘），
执行器 `python3 tests/closure_r05/r05_l_tier.py`（fixture 幂等复用，manifest hash 不变）。
全部 check 的 argv、原始观测存于证据 JSON（§8），10 项断言 9 PASS / 1 FAIL：

### 4.1 L 档（10 万条目）legacy `status --format json`

argv：`python3 companion.py --project <fixture-r05L> status --format json`

| check_id | 预期 | 实测 | 结果 |
|---|---|---|---|
| L-status-exit0-paused-unavailable | exit 0；capacity_status=paused；fingerprint_status=unavailable；source_fingerprint=null；overall_percent=null | 全部相符（publication="项目超出当前检查上限；发布核验暂停…"，counts.accepted=0） | **PASS** |
| **L-status-zero-source-reads** | 源码树 open/scandir=0、内容读取=0 | **walk=1、scandir=2、open=10、read_bytes=96,470,601（≈92.0MiB）、stat=33、lstat=113** | **FAIL** |
| L-write-path-still-hard-fails | 超限项目写路径仍硬拒绝 | `packet --feature f1` → exit 2，"项目超出首版检查范围（单文件20MiB，总量100MiB，10000文件）" | **PASS** |

插桩计数全表（L 档 status 单次取证）：

| 类别 | open | fd_open | read | read_bytes | scandir | walk | stat | lstat | sqlite |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| facts（.dev-companion） | 1 | 1 | 1 | 1,815 | 0 | 0 | 6 | 8 | 0 |
| **source_tree** | **10** | 0 | **94** | **96,470,601** | **2** | **1** | **33** | **113** | 0 |
| plugin（scripts+packages） | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| runtime（解释器/标准库） | 0 | 0 | 0 | 0 | 0 | 0 | 324 | 0 | 0 |

walk top = 项目根（递归 walk 确实发生）。机制（按 core.py `_scan_snapshot` 源码）：快照
逐文件 stat 并累计字节，**触发上限之前已对途经文件做 sha256 内容哈希**；本 fixture 布局
下 `big/`（5–20MiB × 10）按字典序先被遍历，累计至 92MiB（第 9 个大文件）才抛
CapacityExceeded。30 次计时样本每次都重复该部分扫描（见 §5，净耗时 ≈62ms 与 ~92MiB
哈希量一致）。对照：既有单测用"单个 21MiB 文件"只覆盖了"首文件即超限"特例（0 内容
读取），未覆盖真实布局；真实布局下部分 walk+hash 恒定发生。

### 4.2 对照组：50 文件小项目（低于阈值，快照路径）

argv 同上（`--project <fixture-r05small>`），完整 accept 流程后取证：

| check_id | 预期 | 实测 | 结果 |
|---|---|---|---|
| small-accepted-normal-path | 低于阈值：accepted=1、progress=100 | accepted=1、overall_percent=100 | **PASS** |
| small-snapshot-path-source-reads-happen | 快照路径源码树读取发生（如实记录） | open=51、read_bytes=56,406、scandir=50、walk=1 | **PASS** |

阈值语义如实记录：**两种路径源码树读取都非零**——低于阈值走快照路径全量读；高于阈值走
降级路径但仍先部分扫描+哈希至触发上限（§4.1）。

### 4.3 历史验收展示（超限降级不冒充当前验收）

小项目先经真实 CLI 完成 packet→receipt→check→accept（accepted、percent=100），再写入
21MiB `big.bin` 超限：

| check_id | 预期 | 实测 | 结果 |
|---|---|---|---|
| small-degraded-historical-acceptance-display | accepted 不计数；状态降 awaiting_review；验收记录仍展示；accepted 事件保留 | counts.accepted=0；features[0].status=awaiting_review（evidence_stale=true）；acceptance.note="真实检查通过" 保留；history 含 accepted 事件（scope_drafted→scope_confirmed→dispatched→worker_reported→checked→accepted 全链在册） | **PASS** |

L 项目本体无法经 CLI 制造 accepted（写路径硬拒绝，§4.1 PASS，属设计语义）；该验收项在
"先验收后超限"的真实时序上成立。补充：小项目降级取证中源码树仍有 open=2/read=178B
（walk 在命中 big.bin 前已读 2 个小文件）——再次印证降级路径的非零读取。

### 4.4 team-status（新入口，team.db 事实库）

项目：benchmark-fixture-r05team（源码树 50 文件在场）+ `team-init core` + 120 次
`team-task`（CLI 写入 0 失败）：

| check_id | 预期 | 实测 | 结果 |
|---|---|---|---|
| team-task-120-writes | 120 次 CLI 写入全成功 | failures=0 | **PASS** |
| team-status-pagination-cli | 首页 100 条、has_more=true、次页 20 条且不重叠 | 100 / true / 20，task_id 无交集 | **PASS** |
| team-status-zero-source-reads | 零源码树读取；事实库经 sqlite3.connect | source open=0、scandir=0、read_bytes=0；facts sqlite_connects=1、stat=2 | **PASS** |

team-status 插桩全表：facts(open=0, fd_open=0, scandir=0, walk=0, stat=2, lstat=1,
sqlite=1)；source_tree(open=0, stat=2, 其余 0)；plugin(stat=1)；runtime(stat=324)。
SQLite C 层内部读写不可见（§3 边界），以 connect 目标路径计 1 次为准。

## 5. 30 次温热实测（样本数、原始序列、p50/p95、RSS）

计时器：tempfile 内 materialize 的 runner（perf_counter 包 `subprocess.run` 全程；
`getrusage(RUSAGE_CHILDREN).ru_maxrss`，每 runner 单子进程）。分位数取最近秩。

**L 档 legacy status**（n=30，预热 3 次未计入；argv=§4.1）：

- 原始序列（ms）：69.8, 73.5, 71.1, 90.8, 73.8, 75.3, 71.2, 72.3, 75.7, 72.8, 75.3, 71.5, 77.3, 71.6, 72.6, 73.3, 78.0, 73.1, 71.8, 72.3, 84.3, 72.0, 72.9, 75.3, 71.5, 76.6, 78.5, 74.8, 82.3, 89.4
- **p50=0.0733s，p95=0.0894s**（min 0.0698 / max 0.0908 / mean 0.0754）；峰值 RSS **20,791,296 B（19.8MiB）**
- 对照 SLO（09 §3 L 档：温热首页 p95 ≤2s；索引 worker RSS ≤512MiB）：数值达标。**但该
  耗时含每次 ~92MiB 的违规部分扫描+哈希**（见 §4.1），"零读取"修复后应更低，本数字不
  作为零读取语义达标的证据。

**启动对照** `python3 -c pass`（n=30）：p50=0.0115s，p95=0.0154s，RSS 8,650,752 B。
分解：解释器启动 ≈15.7% 的 L status p50，status 逻辑净耗时 ≈0.062s/次（含 ~92MiB 哈希
≈40–50ms，与 §4.1 计数互证）。

**team-status --offset 0 --limit 100**（1 feature + 120 tasks，n=30，预热 3）：

- 原始序列（ms）：38.5, 38.7, 38.5, 39.7, 39.3, 38.3, 39.0, 37.7, 39.2, 37.9, 37.9, 38.6, 37.9, 37.6, 38.1, 38.9, 37.5, 37.5, 39.8, 37.7, 39.5, 37.7, 37.6, 37.7, 38.4, 38.0, 37.8, 37.6, 37.5, 38.3
- **p50=0.0380s，p95=0.0397s**；峰值 RSS **17,252,352 B（16.5MiB）**——零源码树读取下的真实大页分页读。

## 6. 结论（严格限定）

**FAIL（部分）**：经真实 CLI 入口、L 档项目（100,000 条目/239.6MiB）、legacy 普通
`status`：降级语义本身成立（exit 0、capacity_status=paused、fingerprint_status=unavailable、
进度不冒充、accepted 不计数、历史验收与事件保留、写路径硬拒绝、发布核验暂停），
**但"普通 status 对目标源码无递归 walk/hash 及内容读取"不成立**——降级前每次调用对
源码树发生 walk=1、scandir=2、open=10、内容读取 96,470,601 字节（≈92MiB，30 次温热
调用均重复）。R05 验收"零源码树读取"记 **FAIL**；"缺索引只降级不伪通过"及历史展示
语义记 **PASS**；温热 p95 与 RSS 指标数值达标但含上述违规开销。**新入口 team-status**
（1 feature/120 tasks）零源码树读取、CLI 分页正确、p50=0.038s，记 **PASS**。

## 7. FAIL / 发现清单（只记录不修，未动产品代码）

1. **[FAIL→产品]** core.py `_scan_snapshot` 超限判定在累计 stat 之后、且对已途经文件先
   做了 sha256 内容哈希，导致超限项目的普通 status 每次先部分扫描源码树（L 布局
   ≈92MiB）。既有单测（21MiB 单文件）只覆盖首文件超限特例，掩盖了该行为。
2. **[发现→产品/文案]** 降级提示"请先为项目建立文件索引"——当前 CLI 无任何建立索引的
   用户入口（索引扫描仅存在于 tests/benchmarks 路径），用户按提示无路可走。
3. **[发现→接线缺口]** legacy `status` CLI 无分页参数（status_view.paginate 未接到
   legacy CLI，仅 team-status 接了 --offset/--limit）；"首页 100 条分页经真实用户入口"
   目前仅 team 入口成立。
4. **[观察]** storage.team 以命令行原样路径拼 team.db（不 realpath），core.Project 解析
   symlink——插桩需同时收两种拼写才能正确归账（本记录已照做）；语义不一致但 OS 层
   指向同一文件，无实际分叉。
5. **[观察]** legacy 全量 status 视图每次进程内重算（无跨进程缓存，Z13 只做请求作用域
   内缓存）——超限修复前，L 项目每次 status 都重复 92MiB 扫描开销。

## 8. NOT_RUN / 未测边界（如实声明，不扩大认证）

- 冷缓存/冷启动首次扫描：未测（fixture 刚生成，页缓存温热；本记录全部为温热口径）。
- L 档 accept/release 严格门的完整流程：**无法**经 CLI 在超限项目上执行（写路径硬拒绝，
  属设计）；已测的是硬拒绝本身（exit 2）。"索引建成后 L 档 accept/release 门"无产品路径
  可达，NOT_RUN。
- XL/XXL 档、索引库 XXL 分页：不在本任务授权与范围。
- 插件模块加载的内容读取量：Python 层不可见（§3 边界①），未用 OS 层工具（如 fs_usage）
  复测；"插件自身读取"仅有 stat 口径。
- 10000 文件阈值路径（纯小文件超限布局）：未单独取证（本 fixture 由 big/ 字节累计触发；
  机制同源，`len(files)>=10000` 分支已有单测）。
- 总进程树 RSS：仅覆盖被测子进程（无浏览器/宿主/模型进程参与本次路径）。

## 9. 常规回归

`python3 -m unittest discover -s tests -q` → **Ran 643 tests, OK (skipped=12)**。
基线 638 全过不变；+5 为本任务新增 `tests/closure_r05/test_r05_status_closure.py`
（真实 CLI 子进程断言，1.0s）。12 个 skip 全部为既有 L_TIER 环境门（7+5）与平台/vendor
条件跳过，非本任务产物、与本任务无关。

## 10. 证据与复现

- 证据 JSON（argv/起止/退出码/样本序列/计数全表/逐项断言）：
  `<TMPDIR>/r05-evidence/r05-l-tier-evidence.json`，sha256 =
  `9766a8ec7defe380ed03aa7e69907dc0bf6035550dd78d997a891b4f22688961`
- 复现：`python3 tests/closure_r05/r05_l_tier.py --output <path>.json`（fixture 幂等复用；
  删除 scratch 仅限带 BENCHMARK_FIXTURE marker+manifest 的自有目录）
- 单测：`python3 -m unittest tests.closure_r05.test_r05_status_closure -v`（5 项，1.0s）
- 被测 HEAD：00be5f23b340abf31be957161bcd2a82d44b8601；本任务产物：
  tests/closure_r05/（新增）、本记录（新增）。

## 9. 修复后复测（2026-09-20 收尾轮；证据 r05-remeasure-2026-09-20.json）

针对 §7.1 的 FAIL，core.py 已做最小修复（独立提交）：`snapshot()` 增加容量判定缓存
守卫——confirm 时做一次 **stat-only** 容量普查（零文件打开/读取）写
`.dev-companion/capacity-verdict.json`；此后普通 status 对源码树 **零 walk、零 open、
零内容读取**；`_scan_snapshot` 中途发现超限（项目在"未超限判定"后增长）会回写判定，
使该一次性代价被缓存；判定只用于拒绝（fail-safe），删除判定文件可强制重测。

复测结果（同 fixture 口径，10/10 checks PASS）：

| 项 | 修复前（§4/§5） | 修复后 |
|---|---|---|
| L status 插桩（open/scandir/walk/read_bytes） | 10 / 2 / 1 / 96,470,601 | **0 / 0 / 0 / 0** |
| L status 温热 p50 / p95（n=30，含解释器启动） | 73.3ms / 89.4ms | **36.4ms / 42.4ms** |
| 峰值 RSS | 19.8MiB | **15.8MiB** |
| team-status p50 / p95 | 38.0 / 39.7ms | 37.9 / 42.0ms |
| 小项目快照路径对照（读取应发生） | 发生 | 仍发生（阈值语义如实保留） |

**修订结论**：经真实 CLI 入口、L 档项目，legacy 普通 status 与 team-status 的
"零源码树 walk/hash 及内容读取"均记 **PASS**（stale/unknown 与严格 accept/release
门语义不变，历史验收展示不冒充）。遗留边界如实列明：① 项目在判定后增长的场景，
增长后首次 status 仍会有一次内容读取发现代价（随后被缓存，见 core.py 回写逻辑）；
② 冷缓存、索引建成后 L 档 accept/release 端到端仍 NOT_RUN；③ 判定缓存属 fail-safe
拒绝语义，"缩小后重测"需按提示删除判定文件（无自动失效探测）。

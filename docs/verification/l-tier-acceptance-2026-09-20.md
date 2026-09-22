# L 档（10 万条目）容量与资源保护验收记录（2026-09-20）

执行角色：P3-06（角色 V1）。验收依据：`09_TEST_AND_BENCHMARK_ACCEPTANCE.md` §1/§2/§3/§5。
范围：tests/benchmarks/（新增）、本记录。**未修改任何产品代码**（`git diff` 验证被测文件与 HEAD 一致，见下）。

## 1. 被测对象与环境

| 项 | 值 |
|---|---|
| 仓库 / 分支 | /Users/youxididai/Documents/cj/agents @ optimization-v1 |
| subject hash（HEAD） | 27d0b9aa75104772f4f65d2eda342a515363ef89（P4-04） |
| 被测文件 | packages/agents_kernel/indexing/{scanner,reader,content_hash}.py、filesystem/walk.py、storage/db.py —— 与 HEAD 逐字节一致（`git diff --stat HEAD -- <files>` 为空） |
| 工作树并发改动 | 含其他工作流未提交改动（scheduler.py、tasks.py、tests/tasks/、test_coverage.py、test_precheck.py 扩展等）；非本任务产物，未触碰 |
| CPU / RAM | Apple M4（10 核，arm64）/ 16 GiB |
| OS / 磁盘 | macOS 26.2（Build 25C56）/ APFS SSD（Apple Fabric），临时目录所在卷空闲约 73.2 GiB |
| Python / SQLite / Git / Node | 3.9.6 / 3.51.0 / 2.50.1（Apple Git-155）/ v24.19.0 |
| provider 配置 | 无模型调用、无远端/付费消耗（全部本地执行） |

## 2. 资源授权（09 §5）与 fixture

生成器：`tests/benchmarks/fixture_gen.py`（确定性：seed=20260920；产物只进 `tempfile.gettempdir()` 下 `benchmark-fixture-` 前缀目录，带 `BENCHMARK_FIXTURE` marker + manifest.json；目录名前缀/临时目录边界/symlink 拒绝在生成器内强制校验）。

dry-run（实跑前自动先行，未设 --max-bytes 覆盖，默认限额 2 GiB）：

```
DRY-RUN OK: files=100000 small=99990 big=10 bytes=250080232 (238.5MiB)
inodes=110112 free=78627713024 (73.2GiB) limit=2147483648
```

实际生成（子进程，18.751s）：100×100 目录树 × 9~10 个 33–78 行真实语法 Python 文件（写盘前逐个 compile() 校验）+ 10 个 5–20MiB 大文件（big/blob_00..09.bin，5/12/19/10/17/8/15/6/13/20 MiB），共 238.5 MiB / 110112 inode。盘上实际文件 100002 = 100000 payload + BENCHMARK_FIXTURE + manifest.json。产物目录在套件结束经 marker+manifest 核验后删除，运行后临时目录无残留。小规模冒烟验证过逐字节确定性（同 seed 两次生成 .py 树哈希一致）。

## 3. 实测结果（原始数字）

命令：`L_TIER=1 python3 -m unittest tests.benchmarks.test_l_tier -v`
起 2026-09-20T09:28:57Z，止 09:29:39Z，exit 0，**13/13 PASS**（套件内 0 skip/0 FAIL），净耗时 36.909s。

| # | 实测项 | result | 原始数字 |
|---|---|---|---|
| 1 | SC05：P3-01 全量普查 10 万文件 | **PASS** | 1.111 s（温热页缓存，plain 模式），21 批次，file_count=100002 与盘上枚举**集合全等**，吞吐 90010.8 文件/s，抽检 200 行 size/mtime_ns 与 lstat 一致，全部行 generation=1 |
| 2 | SC09：扫描 worker 峰值 RSS ≤512MiB | **PASS** | ru_maxrss（macOS 单位字节）基线 13,221,888 → 峰值 **35,160,064 B（33.5MiB）**；哈希路径峰值 45,940,736 B；恢复重扫峰值 46,006,272 B；均远低于 536,870,912 B 上限 |
| 3 | SC01(a)：reader 分页 100 条 p50/p95（30 次，perf_counter，3 次预热） | **PASS（查询侧）** | p50=**0.000053 s**，p95=**0.000053 s**，min 0.000050，max 0.000056（SLO ≤2 s）；count_files 走 manifest 物化（=100002）；iter_files keyset 全量遍历 100002 行无缺漏 |
| 4 | SC01(b)：dev-companion status 对照（50 文件真实项目，30 次 CLI 子进程） | 对照记录 | p50=**0.036863 s**，p95=**0.037595 s**（每次含解释器启动；status 恒为 live，跨进程无缓存）。**大项目 status 端到端未在此实测**：fixture 非 dev-companion 项目；大项目路径依赖 P2-03 索引降级，已有单测覆盖，不在此重复 |
| 5 | C08/增量：改 1% 文件后二次扫描 | **PASS** | 首轮回填 hashed=100002 / skipped=0 / 21 批；改 1000 个 .py（1000/100002≈1.0%）后：hash_computed=**1000**，hash_reused=**99002**（恰为未变更总数），耗时 1.595 s，变更文件 sha256 与现算一致、未变更抽样 sha256 不变 |
| 6 | FS05：扫描中 kill -9 → 恢复 | **PASS** | 子进程扫描 gen3 至 staging 落库 15000 行（3 批）时 SIGKILL（returncode=-9）；重开后：旧代 gen2 完整可读（count=100002、分页正常、files 全为 gen2），gen3=active 半代；重扫 gen4 完成 file_count=100002、gen3=**abandoned**、files_staging 清空、files 全量换装 gen4 |

## 4. 常规回归

`python3 -m unittest discover -s tests -q` → **Ran 384 tests, OK (skipped=5)**。5 个 skip 全部是本包 L_TIER 门（未设环境变量时打印 NOT_RUN 原因后跳过），基准步骤不进常规 discover。说明：本任务开始时该套件基线为 350 全过；384 与 350 的差值来自其他工作流在工作树中新落的未提交测试（tests/tasks/、test_coverage.py 等），与本任务无关（移除 tests/benchmarks/ 后复测仍为 384，且被测五个文件与 HEAD 一致）。

## 5. 测试缺陷记录（09 §4 分类）与产品 bug

- 第一轮实测（09:22:36–09:23:17Z）曾出现 6 项 FAIL：测试期望行数取 manifest 的 100000，漏计扫描根内的 marker 与 manifest.json 两个文件。分类：**测试期望错误，产品行为正确**（扫描根内全部常规文件理应如实入账）；修正期望为盘上实际枚举并追加路径集合全等校验后复跑全绿。修正只动测试文件，未动产品代码。
- 本轮未发现产品 bug。

## 6. NOT_RUN / 未覆盖（如实声明）

- XL/XXL/索引 XXL 档未测（不在本任务授权与范围）。
- 冷缓存/冷启动首次索引未单独测：fixture 刚生成，页缓存温热；第 3 节耗时为温热口径。
- dev-companion status 对 10 万文件项目的端到端 p95 未实测（SC01 仅关闭查询侧）。
- 总进程树 RSS：仅覆盖测试进程 + 扫描 worker 子进程（无浏览器/宿主/模型进程参与本次路径）。

## 7. 结论

**L 档（10 万条目）查询侧、扫描侧、恢复侧实测通过**；SC05/SC09/FS05 关闭，SC01 关闭查询侧（status 大项目端到端未测，见第 6 节）。本记录不构成对百万/千万级容量、XL 以上档位或 dev-companion status 大项目端到端的支持声明。

## 8. 证据

- 原始运行日志（含全部 `L-TIER-RESULT` 行、时间戳、退出码）：`docs/verification/l-tier-run-2026-09-20.log`
  sha256 = `b8b2fcb492b59adfa1e32849cd395e19d14f11a74ab3150bddc62885f5e5898e`
- 基准代码：`tests/benchmarks/fixture_gen.py`、`tests/benchmarks/test_l_tier.py`（L_TIER 门，手动运行见 `tests/benchmarks/__init__.py`）

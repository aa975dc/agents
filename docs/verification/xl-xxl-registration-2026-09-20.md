# XL/XXL 容量认证登记（P6-05，2026-09-20，R07 口径修正版）

## 结论

**XL / XXL 大档物理实跑 = NOT_RUN_AUTH**（未获资源预算授权，09 §5：更大档在执行前需对应资源预算）。**基准入口与生成器已真实可用并经小样本实测通过**（不再是"授权后落地"的占位）：fixture_gen 支持 `--rows`（current-generation distinct）与 `--generations`（history retention）模式，test_xl_tier / test_xxl_tier 是可导入可运行的 unittest 入口（小样本 21 项全过，日志见 §1）。授权到位后按 §4 命令原样执行即可。

在此之前不得声称"支持百万/千万文件"；同时不得用多代累计行数冒充当前代规模（§3）。

## 1. 基准入口（真实可用，本轮小样本实测）

| 入口 | 状态 | 证据 |
|---|---|---|
| `python3 tests/benchmarks/fixture_gen.py --help` | 可用 | docs/verification/xl-xxl-bench-cli-2026-09-20.log |
| `--dry-run`（文件数/内容字节/inode/磁盘余量/逐代行数估算；超 `--max-bytes` 拒绝） | 可用 | 同上（含 1M 行 dry-run 与限额拒绝演示） |
| `--rows N`：current-generation distinct profile（盘上恰好 N 个普通文件，扫描根即得 N 行） | 可用 | xl-tier-run-2026-09-20.log step1 |
| `--generations K`：history retention profile（base 扫描 + K-1 轮受控变更：每轮新增/删除/~1% 修改，逐代扫描） | 可用 | xl-tier-run step2 / xxl-tier-run step1 |
| `L_TIER=1 python3 -m unittest tests.benchmarks.test_xl_tier -v` | 小样本 11 项全过 | xl-tier-run-2026-09-20.log |
| `L_TIER=1 python3 -m unittest tests.benchmarks.test_xxl_tier -v` | 小样本 10 项全过 | xxl-tier-run-2026-09-20.log |

环境门语义：未设 `L_TIER=1` → 全部 skip（NOT_RUN，skip 不算 PASS）；请求规模 >1000 行（小样本保守档上界，64MiB 写入上限）时需 `XL_TIER_AUTH=1`/`XXL_TIER_AUTH=1` + 显式 `*_MAX_BYTES`，否则 skip 并打印 **NOT_RUN_AUTH**。本轮实跑为 800 行 ×（1 代 / 3 代）小样本。

## 2. 小样本实测（2026-09-20，本机 macOS arm64 / Py3.9.6，仅证明口径与入口）

- fixture 800 行：内容 950,971 B；生成 0.185s；census 扫描 0.015s（固定开销主导）。
- history 3 代（800 基础 +7/-3/代变更）：逐代 800/807/811 行；**count_total_rows=2418（累计）≠ count_distinct_current_entries=811**；每代扫描 ≈0.015s。
- 分片 5 片（每模块一片）：行数合计=全量；edges internal 794 + unknown 1,599（fixture 模块含确定性递减 import 链，非零实测）；MergedPageReader 翻页全集 == 逐片直读 oracle（无漏、无重、全局有序）；根清单 capture/load/拒改通过。
- 索引库字节/行 ≈ 333–338 B/行（小样本偏高：SQLite 固定页开销摊薄少）。
- 分页首页 100 条 30 次温热 p95 ≈ 1.6–2.3ms（含每轮新建 reader；**小样本时延不外推为档位达标**）。
- 1M 行 dry-run（只估算不写盘）：内容 ≈1,271.1MiB、inode 1,010,102、10 个大文件。

## 3. 口径修正（R07 核心）

三个 profile **分列报告**，不得互相换算或冒充：

| profile | 生成方式 | 主字段 | 它能证明 | 它不能证明 |
|---|---|---|---|---|
| current-generation distinct | `--rows N --generations 1`（单代快照） | count_distinct_current_entries | 当前代可查的唯一条目规模 | 物理文件扫描负载 |
| physical files | `--files N`（或 rows 口径的盘上核对） | regular_files / included_bytes | 真实枚举、hash、盘上字节 | 索引行规模 |
| history retention | `--rows N --generations K`（受控变更逐代扫描） | count_total_rows、generation_count、逐代账 | 多代扫描吞吐与世代语义 | **任何"当前唯一条目"规模** |

**声明：多代累计 1000 万行 ≠ 当前代 1000 万不同条目。** 当前实现 files 表为单快照（旧代行在完成事务清除，历史只留 scan_generations 逐代账）；`count_total_rows` 是 Σ 逐代 file_count 的工作量口径。09 §2"索引数据库 XXL（10,000,000 元数据行）"若以多代累积达成，仅证明吞吐，不证明"同时可查 10M 条当前条目"；要认证 distinct 1000 万须单代 `--rows 10000000` 实跑。原登记"对 XL fixture 以多代扫描累积 10M 索引行"作为 XXL 认证路径的说法据此修正。每档报告同时列 entries、regular_files、included_bytes、edges、tasks、events（本轮 tasks/events 未采集，如实 NOT_MEASURED）。

## 4. 授权后的执行命令（入口已就绪，非"待实现"）

```bash
# XL：1,000,000 行单代 distinct（磁盘空闲 ≥ 估算字节 2 倍；先 dry-run）
python3 tests/benchmarks/fixture_gen.py /tmp/benchmark-fixture-xl --rows 1000000 --generations 1 --dry-run
L_TIER=1 XL_TIER_ROWS=1000000 XL_TIER_GENERATIONS=1 XL_TIER_AUTH=1 XL_TIER_MAX_BYTES=3200000000 \
  python3 -m unittest tests.benchmarks.test_xl_tier -v
# 判定（09 §3）：entries=1,000,000 分项报告；扫描 RSS≤1GiB；分页 p95≤3s；跨片完整性=Σ分片行数==全量

# XXL：10,000,000 行 × 10 代（索引行口径；单机分批；远端协议保持禁用）
L_TIER=1 XXL_TIER_ROWS=10000000 XXL_TIER_GENERATIONS=10 XXL_TIER_AUTH=1 XXL_TIER_MAX_BYTES=40000000000 \
  python3 -m unittest tests.benchmarks.test_xxl_tier -v
# 判定：分页 p95≤5s；每分片 RSS≤2GiB；漏片/重片/环检测；count_total_rows 与 distinct 分列（§3）
```

## 5. 大档外推估算表（**基于小样本/L 档实测的外推，非实测认证**）

公式与假设：线性外推 T≈N/吞吐、D≈N×单位字节；锚点 A=本轮小样本（800 行），锚点 B=L 档 100k 实测（l-tier-acceptance-2026-09-20.md）。假设：确定性内容函数字节/行恒定、扫描/生成随 N 线性、无目录压力与碎片衰减、同机同盘。小样本单位字节偏高（固定开销摊薄少）→ 字节外推为保守上界；百万级扁平树的 inode/dentry 压力在小样本不可观测，实测可能劣于估算。

| 项目 | 外推公式 | XL（1M 行）估算 | XXL（10M 行）估算 |
|---|---|---:|---:|
| 内容字节 | N × 1,202 B（1M dry-run 反推，不含大文件）+ 大文件 125MiB（固定 10 个） | ≈1.24GiB（1M dry-run 实算 1,271MiB） | ≈11.6GiB |
| inode | 1,010,102（1M dry-run 实算） | ≈1.01e6 | ≈1.01e7 |
| fixture 生成耗时 | N ÷ 5,333 files/s（B 实测 18.751s/100k） | ≈3.1min | ≈31min |
| census 扫描耗时 | N ÷ 90,010 rows/s（B 实测 1.111s/100k） | ≈11s | ≈111s |
| 索引库字节 | N × 338 B/行（A 实测上界） | ≈0.32GiB | ≈3.2GiB |
| 历史累计（K 代） | 逐代行数 × K（口径 §3，只证吞吐） | 认证不采用 | 10×10M=1e8 累计（不作 distinct 声称） |
| 分页首页 100 条 p95 | 成本 O(page_size+分片数)，与 N 无关（设计）；**只能大档实测** | SLO ≤3s（待实测） | SLO ≤5s（待实测） |
| 索引 worker RSS | L 档实测 35.2MiB/100k；分页内存有界 | 目标 ≤1GiB（待实测） | 每分片 ≤2GiB（待实测） |

## 6. 明确 NOT_RUN / NOT_RUN_AUTH 的子项

- XL/XXL 档大规模物理实跑（扫描/RSS/吞吐/分页 p95 的档位达标判定）：**NOT_RUN_AUTH**（入口已就绪，§4 命令 + §5 预算估算可直接执行）
- 索引数据库 XXL 单代 1000 万 distinct 行实跑：NOT_RUN_AUTH（多代累计口径按 §3 不作 distinct 认证）
- 全量深读活动（CV02 的真实模型消耗）：NOT_RUN（模型额度与时长预算另需授权）
- 冷缓存首次索引耗时：NOT_RUN（需可控页缓存条件）
- dev-companion status 对大项目端到端：部分实测（L 档对照 50 文件；大项目路径依赖 P2-03 降级，单测覆盖，未端到端复测）

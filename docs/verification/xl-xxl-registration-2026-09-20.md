# XL/XXL 容量认证登记（P6-05，2026-09-20）

## 结论

**XL / XXL 物理容量认证 = NOT_RUN**（未获资源授权，09 §5：更大档在执行前需对应资源预算）。实现层（P6-01 分片、P6-02 协调/分页/汇总）与小样本正确性测试已入库；本文件登记待验收项与可运行命令，授权到位后按此执行，**在此之前不得声称"支持百万/千万文件"**。

## 已有实现与小样本证据（非容量认证）

| 能力 | 代码 | 小样本测试 |
|---|---|---|
| 按模块分片写库（独立 generation/epoch） | indexing/sharding.py ShardPlanner/ShardWriter | tests/benchmarks/test_sharding.py 21 项（500 文件树） |
| 跨片图汇总/跨片环检测 | sharding.py CrossShardGraph（Tarjan SCC） | 同上 |
| generation vector 比较 | sharding.py（equal/ahead/behind/diverged） | 同上 |
| 全局 keyset 分页（内存 O(page+分片数)） | indexing/coordination.py MergedPageReader | tests/benchmarks/test_coordination.py 22 项（8 分片） |
| 根 manifest 完整性/指纹 | coordination.py ShardRootManifest | 同上 |
| 分页汇总报告（返回项数有界） | services/summary.py ReportPaginator | 同上 |
| L 档物理实测（对照基线） | — | docs/verification/l-tier-acceptance-2026-09-20.md（10 万文件：扫描 1.111s、RSS 33.5MiB、分页 53μs、增量 1000:99002、kill -9 恢复） |

## XL 档（100 万条目）待执行命令

前提：磁盘空闲 ≥ 目标字节的 1.5 倍（预估 ≥15GiB 内容负载时 ≥25GiB）；执行前先 dry-run。

```bash
# 1. dry-run 估算（不落盘）
python3 tests/benchmarks/fixture_gen.py /tmp/xl-fixture --files 1000000 --dry-run
# 2. 生成（确定性种子；预期写入 ~2.4GiB 文本 + 10 个大文件，以 dry-run 输出为准）
python3 tests/benchmarks/fixture_gen.py /tmp/xl-fixture --files 1000000
# 3. 分片扫描 + 跨片图（复用 P6-01/02 API 的基准入口；执行前补 tests/benchmarks/test_xl_tier.py 于授权后落地）
L_TIER=1 python3 -m unittest tests.benchmarks.test_xl_tier -v
# 4. 判定标准（09 §2/§3）：entries=1,000,000 分项报告；扫描吞吐与 RSS≤1GiB；
#    分页 p95≤3s；增量 1% 变更 computed≈10,000；跨片完整性=Σ分片行数==全量
```

## XXL 档（1000 万条目）待执行命令

前提：索引行口径 10,000,000 行（09 §2 允许"索引数据库 XXL"与"真实文件 XXL"分别认证）；预估 ≥24GiB 元数据负载；单机分批优先，远端协议保持禁用（P6-02 SEC03 边界）。

```bash
# 索引行口径：以 fixture_gen 生成 10M 行元数据集（授权后实现 --rows 10000000 生成模式）
# 或对 XL fixture 以多代扫描累积 10M 索引行
L_TIER=1 python3 -m unittest tests.benchmarks.test_xxl_tier -v
# 判定：分页 p95≤5s；每分片 RSS≤2GiB；跨片汇总正确性（漏片/重片/环检测）；
# 重建检查点续跑（P6-04 ResumeLedger）在强杀后接管成功
```

## 明确 NOT_RUN 的子项

- SC02/SC03/SC04/SC05（XL/XXL 档物理指标）：NOT_RUN
- 全量深读活动（CV02 的真实模型消耗）：NOT_RUN（模型额度与时长预算另需授权）
- 冷缓存首次索引耗时：NOT_RUN（需可控页缓存条件）
- dev-companion status 对大项目端到端：部分实测（L 档对照 50 文件；大项目路径依赖 P2-03 降级，单测覆盖，未端到端复测）

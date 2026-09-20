"""基准/容量验收专用包。

test_l_tier.py 是 L 档（10 万条目）长跑基准，带 L_TIER 环境变量门：未设置
L_TIER=1 时全部用例 skip（NOT_RUN），常规 `python3 -m unittest discover -s tests`
只多出若干 skip，不执行任何基准步骤。手动运行：
L_TIER=1 python3 -m unittest tests.benchmarks.test_l_tier -v

test_sharding.py 是 XL 分片索引的小样本（≤2000 文件）功能测试，不设 L_TIER 门、
常规 discover 可跑；XL 百万级物理压测归 P6-05（NOT_RUN）。

test_xl_tier.py / test_xxl_tier.py 是 XL/XXL 档可执行基准入口（R07），同带
L_TIER=1 环境门（未设即 skip，skip 不算 PASS）；缺省为小样本保守档
（≤1000 行 / 64MiB）真实执行，大档（>1000 行）需 XL_TIER_AUTH/XXL_TIER_AUTH=1
且显式 *_MAX_BYTES，否则 skip 并打印 NOT_RUN_AUTH。手动运行：
L_TIER=1 python3 -m unittest tests.benchmarks.test_xl_tier -v
L_TIER=1 python3 -m unittest tests.benchmarks.test_xxl_tier -v
（共享 harness 在 bench_runner.py；fixture 生成统一经 fixture_gen.py，
--rows=当前代 distinct 口径、--generations=多代累计口径，二者分列不互混。）

fixture 产物只写入 tempfile.gettempdir() 下以 benchmark-fixture 命名、带
BENCHMARK_FIXTURE marker 的目录（09_TEST_AND_BENCHMARK_ACCEPTANCE.md §5）。
"""

"""基准/容量验收专用包。

test_l_tier.py 是 L 档（10 万条目）长跑基准，带 L_TIER 环境变量门：未设置
L_TIER=1 时全部用例 skip（NOT_RUN），常规 `python3 -m unittest discover -s tests`
只多出若干 skip，不执行任何基准步骤。手动运行：
L_TIER=1 python3 -m unittest tests.benchmarks.test_l_tier -v

test_sharding.py 是 XL 分片索引的小样本（≤2000 文件）功能测试，不设 L_TIER 门、
常规 discover 可跑；XL 百万级物理压测归 P6-05（NOT_RUN）。

fixture 产物只写入 tempfile.gettempdir() 下以 benchmark-fixture 命名、带
BENCHMARK_FIXTURE marker 的目录（09_TEST_AND_BENCHMARK_ACCEPTANCE.md §5）。
"""

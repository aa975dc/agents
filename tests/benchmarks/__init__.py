"""基准/容量验收专用包——不属于常规回归面。

tests/benchmarks 下的 test_*..py 是 L 档（10 万条目）长跑基准，带 L_TIER 环境变量
门：未设置 L_TIER=1 时全部用例 skip（NOT_RUN），因此常规
`python3 -m unittest discover -s tests` 不会执行任何基准步骤，只多出若干 skip。

手动运行：L_TIER=1 python3 -m unittest tests.benchmarks.test_l_tier -v
fixture 产物只写入 tempfile.gettempdir() 下以 benchmark-fixture 命名、带
BENCHMARK_FIXTURE marker 的目录（09_TEST_AND_BENCHMARK_ACCEPTANCE.md §5）。
"""

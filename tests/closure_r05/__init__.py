"""R05 收尾（真实 status 路径与 L 档端到端补验）专用包。

instrument.py：sitecustomize 风格插桩（open/scandir/walk/stat/lstat/sqlite3.connect
按路径前缀分类计数），只注入子进程环境，不改产品代码。
r05_l_tier.py：L 档（10 万条目）实测入口，手动运行（fixture 生成 + 30 次温热
计时 + 插桩计数），产物为 JSON 证据，转写进 docs/verification/ 记录。
test_r05_status_closure.py：真实 CLI 子进程快速断言（小项目对照、超限降级、
历史验收展示、team-status 分页），进常规 discover。
"""

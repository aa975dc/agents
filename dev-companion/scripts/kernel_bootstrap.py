"""agents_kernel 引导（P2-05 单处收敛，所有脚本共用，禁止复制第二套定位逻辑）。

定位优先级：
1. 本目录 _kernel_vendor/agents_kernel —— tools/build_vendor.py 生成的私有副本，
   插件独立安装（仓库外）时随包分发，勿手改；
2. 回退仓库布局 <repo>/packages/agents_kernel —— 开发态兼容。

import 本模块即完成 sys.path 注入；重复导入不重复插入。
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent


def ensure_kernel():
    """使 `import agents_kernel` 可解析；返回实际来源 "vendor" 或 "packages"。"""
    vendor = _SCRIPTS / "_kernel_vendor"
    if (vendor / "agents_kernel").is_dir():
        if str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
        return "vendor"
    for base in _SCRIPTS.parents:
        if (base / "packages" / "agents_kernel").is_dir():
            if str(base / "packages") not in sys.path:
                sys.path.insert(0, str(base / "packages"))
            return "packages"
    raise ImportError("找不到 agents_kernel：优先本目录 _kernel_vendor/（由 tools/build_vendor.py 生成），"
                      "或回退仓库根 packages/agents_kernel")


ensure_kernel()

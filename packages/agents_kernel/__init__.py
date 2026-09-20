"""Shared kernel for the Dev Companion scripts (stdlib only, Python 3.9+).

下沉的单一实现：路径安全与敏感清单（paths）、文件哈希口径（digest）、纯校验
（validation）、原子写与 JSON 读取（atomicio）、子进程执行与输出脱敏（process）、
SQLite 事实库与单协调写者（storage）、事件折叠的当前视图与只读分页查询（domain）。
上层（core/journey/releases/archives/CLI）从这里 import；kernel 不反向依赖任何上层模块。

分发边界（P2-05 前的现状）：仅支持仓库布局——<repo>/packages/agents_kernel 与
<repo>/dev-companion/scripts 通过各脚本里的 sys.path 引导互相可见。脱离仓库根的插件
安装产物由 P2-05 的 vendor 构建生成（02_TARGET_ARCHITECTURE.md §3）；在那一刻之前，
已安装的插件副本不包含本目录，重建安装前不要直接替换缓存里的脚本。
"""

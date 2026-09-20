"""请求级缓存（Z13 性能半）：单次命令执行内共享解析、指纹与快照。

边界（明确不做）：
- 不做跨进程缓存：缓存只存在于 with_scope() 打开的请求作用域内，作用域结束即丢弃；
  不落盘、不跨进程、不跨线程。每个 CLI 进程每次命令独立计算。
- 作用域外的 get() 直接调用 factory，行为与无缓存完全一致——写路径永远读到最新磁盘状态。
- 作用域内只允许只读操作：当前唯一开作用域的入口是 Project.status（一次 status 内
  Journey 三次解析/产物重哈希与快照扫描复用为一次）。写命令（packet/check/accept/save
  等）不得进入作用域：check 依赖前后两次快照对比检测文件变化，缓存会使其失真。
"""
import contextlib
import threading


class RequestCache:
    """键值缓存 + 显式请求作用域：get(key, factory) 在作用域内同 key 只算一次。"""

    _scope = threading.local()

    @classmethod
    def current(cls):
        return getattr(cls._scope, "active", None)

    @classmethod
    @contextlib.contextmanager
    def with_scope(cls):
        """打开请求作用域；已处于作用域内时复用外层（嵌套安全，最外层决定生命周期）。"""
        if cls.current() is not None:
            yield cls.current()
            return
        scope = {"values": {}, "hits": set()}
        cls._scope.active = scope
        try:
            yield scope
        finally:
            cls._scope.active = None

    @classmethod
    def get(cls, key, factory):
        scope = cls.current()
        if scope is None:
            return factory()
        if key in scope["values"]:
            scope["hits"].add(key)
            return scope["values"][key]
        value = factory()
        scope["values"][key] = value
        return value

    @classmethod
    def hit(cls, key):
        """key 在当前作用域内是否命中过缓存（呈现层据此标注 cached/stale，见 presentation）。"""
        scope = cls.current()
        return scope is not None and key in scope["hits"]

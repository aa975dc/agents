"""R05 插桩：按路径前缀分类计数子进程的系统入口调用（09 §3 "status 源码内容读取量"）。

原理：向 tempfile 构造的目录写入 sitecustomize.py，经 PYTHONPATH 注入目标子进程；
包装 builtins.open / io.open / os.scandir / os.walk / os.stat / os.lstat /
sqlite3.connect，按最长前缀规则分类（facts → source_tree → plugin → runtime）并
计数。读打开另计 read_calls / read_bytes（内容读取量）。进程退出时 atexit 落
JSON 计数文件，由本模块读回。

边界（如实）：
- open 计数含打开失败尝试；字节只对成功读到的部分计数。
- CPython import 机制与 SQLite C 层读写不经 builtins.open/os.stat，插件模块加载
  的内容读取不可见，仅 importlib 的 stat/lstat 可见；SQLite 访问以 connect 目标
  计数。两类边界在证据记录中注明，不据此声称"零读取"。
- 计数器经 Python 层包装，插桩仅用于单次取证运行；30 次温热计时运行不注入。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SITECUST_SOURCE = '''"""R05 计数注入（运行时装配，非产品代码）。"""
import atexit
import builtins
import io
import json
import os
import sqlite3 as _sqlite3
import sys

_COUNT_FILE = os.environ["R05_COUNT_FILE"]
with io.open(os.environ["R05_RULES_FILE"], encoding="utf-8") as _handle:
    _RULES = json.load(_handle)

_real_open = io.open
_real_os_open = os.open
_real_scandir = os.scandir
_real_walk = os.walk
_real_stat = os.stat
_real_lstat = os.lstat
_real_connect = _sqlite3.connect
_counts = {}


def _bucket(path):
    if isinstance(path, os.PathLike):
        try:
            path = os.fspath(path)
        except Exception:
            return "runtime"
    if isinstance(path, bytes):
        try:
            path = os.fsdecode(path)
        except Exception:
            return "runtime"
    if not isinstance(path, str):
        path = getattr(path, "path", None)
        if not isinstance(path, str):
            return "runtime"
    full = os.path.abspath(path)
    for rule in _RULES:
        prefix = rule["prefix"]
        if full == prefix or full.startswith(prefix + os.sep):
            return rule["category"]
    return "runtime"


def _rec(category, key, value=1):
    bucket = _counts.setdefault(category, {})
    if isinstance(value, int):
        bucket[key] = bucket.get(key, 0) + value
    else:
        bucket.setdefault(key, [])
        if value not in bucket[key]:
            bucket[key].append(value)


class _CountingReader(object):
    __slots__ = ("_handle", "_category")

    def __init__(self, handle, category):
        self._handle = handle
        self._category = category

    def read(self, *args):
        data = self._handle.read(*args)
        if data:
            _rec(self._category, "read_calls")
            _rec(self._category, "read_bytes", len(data))
        return data

    def readline(self, *args):
        line = self._handle.readline(*args)
        _rec(self._category, "read_calls")
        _rec(self._category, "read_bytes", len(line))
        return line

    def readlines(self, *args):
        lines = self._handle.readlines(*args)
        _rec(self._category, "read_calls")
        _rec(self._category, "read_bytes", sum(len(line) for line in lines))
        return lines

    def __iter__(self):
        return self

    def __next__(self):
        line = self._handle.readline()
        if not line:
            raise StopIteration
        return line

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._handle, name)


def _wrapped_open(file, *args, **kwargs):
    mode = args[0] if args else kwargs.get("mode", "r")
    if isinstance(mode, int):
        # os.open 风格 flags：pathlib._opener 二次进入（同一逻辑打开会先走高层
        # io.open 再走低层 fd 打开），单独计数避免与高层 open_calls 重复。
        if isinstance(file, (str, bytes, os.PathLike)):
            _rec(_bucket(file), "open_fd_calls")
        return _real_open(file, *args, **kwargs)
    counting = isinstance(file, (str, bytes, os.PathLike)) and "r" in (mode or "r")
    if isinstance(file, (str, bytes, os.PathLike)):
        _rec(_bucket(file), "open_calls")
    handle = _real_open(file, *args, **kwargs)
    return _CountingReader(handle, _bucket(file)) if counting else handle


def _wrapped_os_open(path, flags, mode=0o777, *args, **kwargs):
    # 低层 os.open 语义（pathlib._NormalAccessor.open 原为 os.open：(path, flags, mode)）
    if isinstance(path, (str, bytes, os.PathLike)):
        _rec(_bucket(path), "open_fd_calls")
    return _real_os_open(path, flags, mode, *args, **kwargs)


def _wrapped_scandir(path="."):
    _rec(_bucket(path), "scandir_calls")
    return _real_scandir(path)


def _wrapped_walk(top, *args, **kwargs):
    category = _bucket(top)
    _rec(category, "walk_calls")
    top = top if isinstance(top, (str, bytes)) else str(top)
    _rec(category, "walk_tops", os.path.abspath(top))
    return _real_walk(top, *args, **kwargs)


def _wrapped_stat(path, *args, **kwargs):
    _rec(_bucket(path), "stat_calls")
    return _real_stat(path, *args, **kwargs)


def _wrapped_lstat(path, *args, **kwargs):
    _rec(_bucket(path), "lstat_calls")
    return _real_lstat(path, *args, **kwargs)


def _wrapped_connect(database, *args, **kwargs):
    target = database
    if isinstance(target, str) and target.startswith("file:"):
        # FIX-01 起只读打开经 SQLite URI（file:...?mode=ro[&immutable=1]）：
        # 按其内嵌路径归类，仍计入 facts（连接目标没有变，只是 URI 形式）。
        from urllib.parse import unquote, urlsplit
        target = unquote(urlsplit(target).path)
    _rec(_bucket(target) if isinstance(target, (str, bytes, os.PathLike))
         else "runtime", "sqlite_connects")
    return _real_connect(database, *args, **kwargs)


builtins.open = _wrapped_open
io.open = _wrapped_open
os.scandir = _wrapped_scandir
os.walk = _wrapped_walk
os.stat = _wrapped_stat
os.lstat = _wrapped_lstat
_sqlite3.connect = _wrapped_connect


class _NoBind(object):
    """让包装函数挂到 pathlib._NormalAccessor 上不触发描述符绑定：
    Path.stat 等经 `self._accessor.stat(self)` 调用，Python 函数作类属性会被
    绑定成方法多传一个 self（builtin 无 __get__ 无此问题）。"""

    __slots__ = ("_fn",)

    def __init__(self, fn):
        self._fn = fn

    def __get__(self, obj, objtype=None):
        return self._fn


try:
    import pathlib as _pathlib
    _pathlib_access = _pathlib._NormalAccessor
    _pathlib_access.open = _NoBind(_wrapped_os_open)
    _pathlib_access.stat = _NoBind(_wrapped_stat)
    _pathlib_access.lstat = _NoBind(_wrapped_lstat)
except AttributeError:  # pathlib 内部结构变化时降级：os 层计数仍有效
    _pathlib_access = None


@atexit.register
def _dump():
    with _real_open(_COUNT_FILE, "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "argv": sys.argv[1:],
                   "pathlib_accessor_patched": _pathlib_access is not None,
                   "counts": _counts},
                  handle, ensure_ascii=False, indent=1)
'''

TIMING_RUNNER_SOURCE = '''"""R05 计时装配：报告单次子进程墙钟与峰值 RSS（macOS ru_maxrss 单位为字节）。"""
import json
import resource
import subprocess
import sys
import time

argv = sys.argv[1:]
started = time.perf_counter()
proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
elapsed = time.perf_counter() - started
rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
print(json.dumps({"seconds": round(elapsed, 6), "exit": proc.returncode,
                  "rss_peak": rss, "rss_unit": "bytes" if sys.platform == "darwin" else "kB",
                  "stdout_bytes": len(proc.stdout), "stderr_bytes": len(proc.stderr)}))
'''

_KEY_ORDER = ("open_calls", "open_fd_calls", "read_calls", "read_bytes", "scandir_calls",
              "walk_calls", "walk_tops", "stat_calls", "lstat_calls", "sqlite_connects")


def _scalars(counts, category):
    """把单类计数压成标量表（walk_tops 单列为长度，完整清单在 tops 字段）。"""
    bucket = counts.get(category, {})
    values = {key: bucket.get(key, 0) for key in _KEY_ORDER if key != "walk_tops"}
    values["walk_tops"] = len(bucket.get("walk_tops", []))
    return values


def build_rules(project_root, plugin_roots):
    """分类规则：facts（.dev-companion）优先于 source_tree（项目根），再次 plugin。

    core.Project 对项目根做 realpath（/var → /private/var），而 storage.team 直接用
    命令行传入路径拼接 team.db（不解析 symlink）——两类拼写都要收，否则事实库
    访问会被误归 runtime。
    """
    rules = []

    def add(prefix, category):
        prefix = str(prefix)
        if prefix and prefix not in {rule["prefix"] for rule in rules}:
            rules.append({"prefix": prefix, "category": category})

    resolved = Path(os.path.realpath(project_root))
    add(resolved / ".dev-companion", "facts")
    add(Path(project_root).absolute() / ".dev-companion", "facts")
    add(resolved, "source_tree")
    add(Path(project_root).absolute(), "source_tree")
    for root in plugin_roots:
        add(os.path.realpath(root), "plugin")
        add(Path(root).absolute(), "plugin")
    return rules


def run_counted(argv, rules, cwd=None, env_extra=None, timeout=600):
    """注入插桩运行一次真实子进程，返回 (completed, 计数, walk_tops明细分册)。"""
    workdir = Path(tempfile.mkdtemp(prefix="r05-instr-"))
    (workdir / "sitecustomize.py").write_text(SITECUST_SOURCE, encoding="utf-8")
    rules_file = workdir / "rules.json"
    rules_file.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
    count_file = workdir / "counts.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(workdir) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["R05_RULES_FILE"] = str(rules_file)
    env["R05_COUNT_FILE"] = str(count_file)
    env.update(env_extra or {})
    proc = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout)
    if not count_file.exists():
        raise RuntimeError("插桩计数文件缺失（子进程可能经 os._exit 退出）：" + str(argv))
    payload = json.loads(count_file.read_text(encoding="utf-8"))
    counts = payload["counts"]
    summary = {category: _scalars(counts, category) for category in
               ("facts", "source_tree", "plugin", "runtime")}
    detail = {category: counts.get(category, {}).get("walk_tops", [])
              for category in counts}
    return proc, summary, detail


class TimedRunner:
    """计时运行器：materialize 一次 runner，逐次调用报告墙钟/RSS/退出码。

    RUSAGE_CHILDREN.ru_maxrss 在"每 runner 进程只跑一个子命令"的前提下即该子进程
    峰值；每次采样都新起 runner 进程保证单子进程语义。
    """

    def __init__(self, interpreter=sys.executable):
        workdir = Path(tempfile.mkdtemp(prefix="r05-timed-"))
        self.path = workdir / "timed_runner.py"
        self.path.write_text(TIMING_RUNNER_SOURCE, encoding="utf-8")
        self.interpreter = interpreter

    def sample(self, argv, timeout=600):
        proc = subprocess.run([self.interpreter, str(self.path)] + list(argv),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError("计时 runner 失败：%s %s" % (proc.returncode, proc.stderr.decode()[:400]))
        return json.loads(proc.stdout.decode("utf-8"))


def percentile(sorted_samples, fraction):
    """最近秩分位（样本已升序）：返回第 ceil(fraction*n) 个（1 基）。"""
    import math
    rank = max(1, math.ceil(fraction * len(sorted_samples)))
    return sorted_samples[rank - 1]

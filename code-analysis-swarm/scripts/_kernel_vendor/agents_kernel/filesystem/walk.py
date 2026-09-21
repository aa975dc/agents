"""流式文件普查枚举（stdlib only, Python 3.9+）。

03_CAPACITY_AND_INDEXING.md §4/§5/§10（L0 文件普查）：
- 确定性流式产出 FileEntry/ExcludedEntry，绝不全量 read/all 数组；内存只持有
  单目录的 scandir 流与 64KiB 的 git stdout 分块。
- Git 仓库优先 `git ls-files -z`（NUL 分隔，文件名含空格/换行/非 ASCII 不按行拆）
  + `git status --porcelain -z -uall` 补未跟踪；无 git 或普通目录退回 os.scandir
  增量遍历（不用 os.walk 的整树列表）。tracked 项逐条 lstat：工作树已删记 missing
  （sparse 未物化同此账，不当不存在）；status 的 D 信号由同一 lstat 覆盖，不重复记账。
- 排除账（不静默）：EXCLUDED_DIRS/敏感清单（kernel.paths）+ 可配置 extra_excluded；
  每个跳过项产出 {path, reason}。special（fifo/socket/device）一律不 open、不等待；
  symlink 只记录不跟随。粒度差异：plain 模式在目录下降前拦截（记目录路径），
  git 模式只见文件、目录排除按首个命中文件记账。
- 大文件只记 size/mtime_ns，绝不读内容——单文件内容读取（切片/哈希）是上层的事。

已知边界：非 UTF-8 文件名以 os.fsdecode(surrogateescape) 解码，直接写 SQLite TEXT
会失败——字节级身份保留归 P3-02+ 的索引身份设计，本层如实传递不丢弃。tracked 目录
（gitlink/sparse 残留）记 reason=submodule。mtime/size 只是变更候选信号，不构成
真实性证明（03 §6）。
"""
import os
import stat
import subprocess
from pathlib import Path
from typing import NamedTuple

from agents_kernel.paths import EXCLUDED_DIRS, sensitive

GIT_CHUNK = 64 * 1024

# 排除账 reason 词汇表：excluded_dir/sensitive/extra 是策略排除；special/symlink
# 是"登记但不纳入/不跟随"；missing/submodule/error 是工作树异常状态的如实登记。
REASON_EXTRA = "extra"
REASON_EXCLUDED_DIR = "excluded_dir"
REASON_SENSITIVE = "sensitive"
REASON_SPECIAL = "special"
REASON_SYMLINK = "symlink"
REASON_MISSING = "missing"
REASON_ERROR = "error"


class FileEntry(NamedTuple):
    """一个纳入普查的普通文件；size/mtime_ns 来自 lstat，内容不读。"""
    path: str        # 相对 root 的 POSIX 路径（/ 分隔）
    size: int
    mtime_ns: int


class ExcludedEntry(NamedTuple):
    """一个被跳过项的排除账记录（不静默）。"""
    path: str
    reason: str


class WalkError(Exception):
    """枚举无法继续（git 命令失败/根目录不可读）；不吞错、不静默降级。"""


def detect_mode(root):
    """探测枚举模式："git"（root 在某工作树内）或 "plain"。git 缺失/失败一律 plain。"""
    try:
        return "git" if _git_toplevel(root) is not None else "plain"
    except OSError:  # git 二进制缺失等
        return "plain"


def policy_reason(relpath, extra_excluded):
    """对任意相对路径做逐段策略检查；命中返回 reason，否则 None。

    目录与文件统一走本函数（两模式一致）：任一路段命中 extra/EXCLUDED_DIRS/敏感
    清单即排除——目录在下降前拦截，文件在产出前拦截。
    """
    for segment in relpath.split("/"):
        if segment in extra_excluded:
            return REASON_EXTRA
        if segment in EXCLUDED_DIRS:
            return REASON_EXCLUDED_DIR
        if sensitive(segment):
            return REASON_SENSITIVE
    return None


def walk_tree(root, extra_excluded=(), mode=None):
    """流式枚举 root 下的文件条目与排除账；产出 FileEntry/ExcludedEntry 混合流。

    mode 缺省时自动探测；调用方（scanner）为把 mode 记入 manifest 可显式传入，
    避免二次探测。顺序不做全局排序（流式约定，03 §10）。
    """
    root = Path(root)
    if (mode or detect_mode(root)) == "git":
        toplevel = _git_toplevel(root)
        if toplevel is not None:
            return _walk_git(root, toplevel, extra_excluded)
    return _walk_plain(root, extra_excluded)


def _git_toplevel(root):
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        timeout=30)
    if result.returncode != 0:
        return None
    return Path(os.fsdecode(result.stdout.strip()))


def _is_special(mode_bits):
    return (stat.S_ISFIFO(mode_bits) or stat.S_ISSOCK(mode_bits)
            or stat.S_ISCHR(mode_bits) or stat.S_ISBLK(mode_bits))


def _classify_mode(mode_bits):
    """lstat 后的分类：None=普通文件（可纳入），否则为排除账 reason。"""
    if stat.S_ISLNK(mode_bits):
        return REASON_SYMLINK  # symlink 一律记录不跟随（含指向 fifo/越界目标的）
    if _is_special(mode_bits):
        return REASON_SPECIAL  # 不 open、不等待、不导致扫描失败
    if stat.S_ISREG(mode_bits):
        return None
    return REASON_SPECIAL


def _walk_plain(root, extra_excluded):
    """os.scandir 增量遍历：逐目录流式产出，仅暂存待下降的子目录相对路径。"""
    pending = [""]
    while pending:
        rel_dir = pending.pop()
        directory = root / rel_dir if rel_dir else root
        try:
            children = os.scandir(directory)
        except OSError:
            if not rel_dir:
                raise WalkError("根目录不可读：%s" % root)
            yield ExcludedEntry(rel_dir + "/", REASON_ERROR)
            continue
        with children:
            for child in children:
                rel = f"{rel_dir}/{child.name}" if rel_dir else child.name
                reason = policy_reason(rel, extra_excluded)
                if reason:
                    yield ExcludedEntry(rel, reason)
                    continue
                try:
                    if child.is_dir(follow_symlinks=False):
                        pending.append(rel)  # 只下降真实目录，不跟随 symlink
                        continue
                    info = child.stat(follow_symlinks=False)
                except OSError:
                    yield ExcludedEntry(rel, REASON_ERROR)
                    continue
                reason = _classify_mode(info.st_mode)
                if reason is None:
                    yield FileEntry(path=rel, size=info.st_size, mtime_ns=info.st_mtime_ns)
                else:
                    yield ExcludedEntry(rel, reason)


def _walk_git(root, toplevel, extra_excluded):
    """git 模式：tracked（ls-files）∪ untracked（status），逐条 lstat 分类。

    实测语义（git 2.39+）：ls-files 路径相对 cwd 且只列 cwd 之下；status
    --porcelain 路径恒相对仓库根（cwd 无关）。统一在 toplevel 执行，按 root 的
    相对前缀过滤——root 之外（同仓库其他目录）不属本普查范围。重复路径（理论
    竞态）靠 scanner 端 staging 主键去重，本层不持全量集合。
    """
    root_real = root.resolve()
    try:
        prefix = root_real.relative_to(toplevel.resolve()).as_posix()
    except ValueError:
        prefix = ""
    prefix = "" if prefix == "." else prefix + "/"
    for path in _git_lines(["ls-files", "-z"], toplevel):
        if prefix and not path.startswith(prefix):
            continue
        yield from _classify_git_path(root, path[len(prefix):], extra_excluded)
    for status_path, untracked in _git_status_paths(toplevel):
        if not untracked or (prefix and not status_path.startswith(prefix)):
            continue  # 非未跟踪项已由 tracked 流覆盖；root 之外跳过
        yield from _classify_git_path(root, status_path[len(prefix):], extra_excluded)


def _classify_git_path(root, rel, extra_excluded):
    """git 候选路径 → 策略过滤 + lstat 分类；产出 0..1 条记录。"""
    reason = policy_reason(rel, extra_excluded)
    if reason:
        yield ExcludedEntry(rel, reason)
        return
    try:
        info = os.lstat(root / rel)
    except FileNotFoundError:
        yield ExcludedEntry(rel, REASON_MISSING)  # tracked 已删/sparse 未物化：登记非不存在
        return
    except OSError:
        yield ExcludedEntry(rel, REASON_ERROR)
        return
    reason = _classify_mode(info.st_mode)
    if reason is None:
        yield FileEntry(path=rel, size=info.st_size, mtime_ns=info.st_mtime_ns)
    elif stat.S_ISDIR(info.st_mode):
        yield ExcludedEntry(rel, "submodule")  # tracked 目录只能是 gitlink/sparse 残留
    else:
        yield ExcludedEntry(rel, reason)


def _git_lines(argv, cwd):
    """流式消费 git stdout（NUL 分隔），分块读取绝不整表入内存（03 §10）。

    argv 传 git 子命令（如 ["ls-files", "-z"]），此处统一补 "git" 可执行名。
    """
    process = subprocess.Popen(["git", *argv], cwd=str(cwd), stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    try:
        buffer = b""
        for chunk in iter(lambda: process.stdout.read(GIT_CHUNK), b""):
            buffer += chunk
            parts = buffer.split(b"\0")
            buffer = parts.pop()
            for item in parts:
                if item:
                    yield os.fsdecode(item)
        if buffer:  # git -z 恒以 NUL 结尾；正常为空，残余按最后一条处理
            yield os.fsdecode(buffer)
    finally:
        process.stdout.close()
        code = process.wait()
    if code != 0:
        raise WalkError("git %s 失败（exit=%d）" % (argv[0], code))


def _git_status_paths(cwd):
    """解析 `git status --porcelain -z -uall`：产出 (path, is_untracked)。

    -z 布局：NUL 结尾的 "XY path"；XY 首位为 R/C 时后随第二条 NUL 字段（原路径）。
    --no-optional-locks：只读普查不刷新/锁索引。
    """
    stream = _git_lines(["--no-optional-locks", "status", "--porcelain",
                         "-z", "-uall"], cwd)
    for record in stream:
        if not record:
            continue
        status, path = record[:2], record[3:]
        if status[0] in ("R", "C"):
            next(stream, None)  # 丢弃紧随的重命名原路径字段
        yield path, status == "??"

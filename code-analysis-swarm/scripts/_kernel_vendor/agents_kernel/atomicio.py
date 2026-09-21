"""Atomic file writes and JSON reads (stdlib only).

唯一原子写实现（原 core.commit/journey._write/releases._commit/archives._atomic_json 四份合一）：
同目录临时文件 → 写入 → flush+fsync 文件 →（可选 pre-replace 守护回调）→ os.replace →
父目录尽力 fsync（Z23 遗留补齐）。fd 异常安全：写/同步抛错时由 with 块关闭，
finally 清理临时文件；before_replace 抛错同样清理。

另提供跨进程互斥临界区 exclusive_lock（SR-03 下沉的共享原语）：锁记录（pid+token）
先完整写入同目录唯一临时文件并 fsync，再 os.link 原子发布——锁文件要么不存在要么
内容完整，不存在 v1"O_CREAT|O_EXCL 成功后、写 PID 前"的空文件窗口；冲突按持有者
pid 存活检测等待/接管，释放按 owner_token 身份核对。与 storage/db.acquire_writer
的写者锁同思路（显式创建、崩溃安全清理）；execution/lease.py 是同思路的带 TTL 租约
（执行域），这里只下沉"短临界区互斥"这一层，不复制租约语义。
"""
import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from agents_kernel.validation import CompanionError


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CompanionError("无法读取有效的 JSON：%s" % path) from exc


def fsync_directory(directory):
    """尽力 fsync 父目录使 rename 持久；平台/文件系统不支持时静默跳过，不吞文件级错误。"""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_atomic(path, data, *, prefix=".tmp-", suffix=".tmp", before_replace=None):
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=str(Path(path).parent))
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
        fsync_directory(Path(path).parent)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value, *, prefix=".tmp-", suffix=".tmp", before_replace=None):
    """状态文件口径：ensure_ascii=False、indent=2、末尾换行（与原四份实现逐字节一致）。"""
    write_atomic(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
                 prefix=prefix, suffix=suffix, before_replace=before_replace)


def _pid_alive(pid):
    """进程存活探测（与 storage/db._pid_alive 同口径）：无权探测保守视为存活。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # 无权探测（他人进程）等情形保守视为存活
    return True


_OWNER_STATE = None  # (pid, token)：进程级持有者标识；fork 后 pid 变化自动换新
_LOCK_DEPTH = {}     # (pid, 锁路径) → 同进程重入深度


def _owner_token():
    """进程级唯一持有者 token（pid+随机后缀）：释放与重入判定都认它，不只认 pid。"""
    global _OWNER_STATE
    if _OWNER_STATE is None or _OWNER_STATE[0] != os.getpid():
        _OWNER_STATE = (os.getpid(), "%d-%s" % (os.getpid(), os.urandom(16).hex()))
    return _OWNER_STATE[1]


def _read_lock(path):
    """读锁记录：返回 (解析值或 None, inode 或 None)；文件不存在 → (None, None)。

    解析失败（空/半写/非 JSON）返回 None——空/半写记录不是持有者已死的证明，
    调用方不得据此立即接管。
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except FileNotFoundError:
        return None, None
    try:
        inode = os.fstat(fd).st_ino
        try:
            data = os.read(fd, 65536)
        except OSError:
            return None, inode
    finally:
        os.close(fd)
    try:
        return json.loads(data.decode("utf-8")), inode
    except (UnicodeDecodeError, ValueError):
        return None, inode


def _remove_stale(path, inode):
    """删除已判定陈旧的锁文件；先核对 inode 未变，防止误删并发接管者刚发布的锁。
    返回 True 表示可立即重试发布（锁已消失或已由本方删除）。"""
    if inode is None:
        return True  # 锁已消失
    try:
        if os.stat(str(path)).st_ino != inode:
            return False  # 已被他人替换：按新锁重新评估
    except FileNotFoundError:
        return True
    try:
        os.unlink(str(path))
    except FileNotFoundError:
        pass
    return True


def _release_own(path, token):
    """身份释放：锁记录的 owner_token 与 pid 都仍是本进程才删除，不误删接管者。"""
    try:
        info, _ = _read_lock(path)
    except OSError:
        return
    if isinstance(info, dict) and info.get("owner_token") == token \
            and info.get("pid") == os.getpid():
        try:
            os.unlink(str(path))
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def exclusive_lock(path, *, timeout=10.0, poll_interval=0.005, grace=1.0):
    """跨进程互斥临界区：唯一临时记录 + os.link 原子发布，整个 with 块为临界区。

    获取：锁记录 JSON{pid, owner_token, started_at} 完整写入同目录唯一临时文件并
    fsync，再 os.link(tmp, lockpath) 原子发布——link 对已存在目标抛 FileExistsError，
    锁文件要么不存在要么内容完整，杜绝 v1 "O_CREAT|O_EXCL 成功后、写 PID 前"可被
    另一进程观察为空文件的窗口（复核 §5：B 误判陈旧删除与 A 同时入界）。
    冲突时读锁记录：
    - 记录 pid 存活 → 轮询等待至 timeout（临界区应当极短）；超时 CompanionError。
    - 记录 pid 已死（崩溃/强杀遗留）→ 陈旧锁接管（unlink 前核对 inode，防误删
      并发接管者刚发布的锁），保留崩溃恢复、不留死锁。
    - 空/半写/缺 pid/不可解析 → 不当死锁证明（"未知记录 ≠ 已死"）：可疑锁，等待
      宽限 grace 仍不可解析才接管，接管时向 stderr 打印警告。
    释放：读回锁记录，owner_token 与 pid 都仍是本进程才删除——已被他人接管的锁
    不误删。同进程重入：token 进程级唯一 + 深度计数，重入不落盘，最外层才删文件。
    边界：不做心跳租约（TTL/续约在 execution/lease.py）；pid 复用属已知边界，由
    调用方的持久 epoch/owner 身份围栏兜底；发布依赖同目录硬链接（link），接管删除
    按名（unlink），"inode 核对→unlink"之间存在指令级竞态窗口，未根除。
    """
    path = Path(path)
    token = _owner_token()
    reentry_key = (os.getpid(), str(path))
    if _LOCK_DEPTH.get(reentry_key, 0) > 0:
        _LOCK_DEPTH[reentry_key] += 1
        try:
            yield
        finally:
            _LOCK_DEPTH[reentry_key] -= 1
        return
    deadline = time.monotonic() + timeout
    ambiguous_since = None  # 可疑锁（空/半写/缺 pid）首次观察时刻：宽限后才接管
    while True:
        value, inode = _read_lock(path)
        if value is not None or inode is not None:
            pid = value.get("pid") if isinstance(value, dict) else None
            if isinstance(pid, int) and not isinstance(pid, bool):
                ambiguous_since = None
                if _pid_alive(pid):
                    if time.monotonic() >= deadline:
                        raise CompanionError(
                            "获取互斥锁超时（持有者 pid=%s 仍存活）：%s" % (pid, path))
                    time.sleep(poll_interval)
                    continue
                if _remove_stale(path, inode):  # 持有者已死：陈旧锁接管
                    continue
                time.sleep(poll_interval)
                continue
            now = time.monotonic()
            if ambiguous_since is None:
                ambiguous_since = now
            if now >= deadline:
                raise CompanionError(
                    "获取互斥锁超时（锁记录空/不可解析，持有者未知）：%s" % path)
            if now - ambiguous_since < grace:
                time.sleep(poll_interval)
                continue
            if _remove_stale(path, inode):
                sys.stderr.write(
                    "[agents_kernel.atomicio] 警告：接管无法解析的锁文件"
                    "（宽限 %.1fs 后按陈旧处理，原记录不构成死锁证明）：%s\n"
                    % (grace, path))
            ambiguous_since = None
            time.sleep(poll_interval)
            continue
        # 无锁 → 完整写临时记录并 fsync，os.link 原子发布；EEXIST 即他人抢先
        fd, tmp = tempfile.mkstemp(prefix=".lock-", suffix=".tmp",
                                   dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "owner_token": token,
                           "started_at": time.time()}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(str(tmp), str(path))
                break  # 发布成功 = 获得锁：内容自此刻起对他人完整可见
            except FileExistsError:
                pass  # 他人抢先发布：下一轮按其记录处理
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    _LOCK_DEPTH[reentry_key] = 1
    try:
        yield
    finally:
        _LOCK_DEPTH[reentry_key] -= 1
        _release_own(path, token)

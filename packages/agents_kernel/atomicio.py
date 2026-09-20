"""Atomic file writes and JSON reads (stdlib only).

唯一原子写实现（原 core.commit/journey._write/releases._commit/archives._atomic_json 四份合一）：
同目录临时文件 → 写入 → flush+fsync 文件 →（可选 pre-replace 守护回调）→ os.replace →
父目录尽力 fsync（Z23 遗留补齐）。fd 异常安全：写/同步抛错时由 with 块关闭，
finally 清理临时文件；before_replace 抛错同样清理。

另提供跨进程互斥临界区 exclusive_lock（SR-03 下沉的共享原语）：O_CREAT|O_EXCL
独占锁文件 + 持有者 pid 存活检测接管，与 storage/db.acquire_writer 的写者锁同思路
（显式创建、崩溃安全清理）；execution/lease.py 是同思路的带 TTL 租约（执行域），
这里只下沉"短临界区互斥"这一层，不复制租约语义。
"""
import contextlib
import json
import os
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


@contextlib.contextmanager
def exclusive_lock(path, *, timeout=10.0, poll_interval=0.005):
    """跨进程互斥临界区：O_CREAT|O_EXCL 独占锁文件，整个 with 块为临界区。

    - 活持有者在场 → 轮询等待至 timeout（临界区应当极短）；超时 CompanionError。
    - 锁文件存在但记录的 pid 已死（崩溃/强杀遗留）或内容空/半写 → 按陈旧锁接管，
      不留死锁（与 db.acquire_writer 陈旧锁同策略）。
    - 释放时仅当锁文件仍记录本进程 pid 才删除，不误删接管者的锁。
    边界：不做心跳租约（TTL/续约在 execution/lease.py）；pid 复用属已知边界，
    由调用方的持久 epoch/owner 身份围栏兜底。
    """
    path = Path(path)
    deadline = time.monotonic() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            info = None
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass  # 空文件/半写按陈旧锁处理
            if info is not None and _pid_alive(info.get("pid")):
                if time.monotonic() >= deadline:
                    raise CompanionError(
                        "获取互斥锁超时（持有者 pid=%s 仍存活）：%s" % (info.get("pid"), path))
                time.sleep(poll_interval)
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    try:
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid()}, handle)
        except OSError:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        yield
    finally:
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(info, dict) and info.get("pid") == os.getpid():
                path.unlink()
        except (OSError, ValueError):
            pass  # 锁已被他人接管/删除：不误删

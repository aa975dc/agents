"""Atomic file writes and JSON reads (stdlib only).

唯一原子写实现（原 core.commit/journey._write/releases._commit/archives._atomic_json 四份合一）：
同目录临时文件 → 写入 → flush+fsync 文件 →（可选 pre-replace 守护回调）→ os.replace →
父目录尽力 fsync（Z23 遗留补齐）。fd 异常安全：写/同步抛错时由 with 块关闭，
finally 清理临时文件；before_replace 抛错同样清理。
"""
import json
import os
import tempfile
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

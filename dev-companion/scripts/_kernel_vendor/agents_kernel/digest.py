"""Hashing: one implementation per input kind, versioned domains (stdlib only).

07_STORAGE_MIGRATION_AND_COMPAT.md §3：哈希函数统一，但不同域的含义用版本化口径分离。
这里的两个 JSON 口径刻意不合并——切换分隔符会改变 state.json 基线指纹与存档
catalog 里已存的 manifest_sha256，属于破坏存量数据的回归。
"""
import hashlib
import json

# 统一分块读取口径：1 MiB。
READ_SIZE = 1024 * 1024


def digest(value):
    """sha256 hex of canonical JSON, default separators — core state 与 journey 指纹域。"""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def canonical_bytes(value):
    """Compact canonical JSON bytes — archives manifest 域（存量 catalog 校验依赖紧凑分隔符）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path, read_size=READ_SIZE):
    """分块读取的文件摘要；返回 (hexdigest, 实际读到的字节数) 供"读取期间变化"核对复用。"""
    hasher = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(read_size)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def content_digest(sha256_hex, mode):
    """文件条目指纹：{"sha256": ..., "mode": ...}，core 快照与 journey 产物共用此形状。"""
    return digest({"sha256": sha256_hex, "mode": mode})

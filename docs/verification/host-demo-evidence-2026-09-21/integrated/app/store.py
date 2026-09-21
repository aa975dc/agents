"""data/tasks.json 存储：{schema, revision, tasks} 包裹对象。

服务进程是唯一写者；写经临时文件 + os.replace 原子替换。
文件不存在视为空库；JSON 损坏或 schema 不符按契约报 E_PROJECT。
"""
import json
import os
import tempfile

SCHEMA_VERSION = 1


class StoreError(Exception):
    """存储级错误；code 按契约错误码（本模块仅产生 E_PROJECT）。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def empty_store():
    return {"schema": SCHEMA_VERSION, "revision": 0, "tasks": []}


def load_store(path):
    if not os.path.exists(path):
        return empty_store()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise StoreError("E_PROJECT", f"数据文件无法解析：{path}（{exc}）") from exc
    revision = data.get("revision") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION
            or not isinstance(revision, int) or isinstance(revision, bool)
            or not isinstance(data.get("tasks"), list)):
        raise StoreError("E_PROJECT", f"数据文件 schema 不符：{path}")
    return data


def save_store(path, data):
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        raise StoreError("E_PROJECT", f"数据文件目录缺失：{parent}")
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".tasks-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

"""模块归属：文件路径 → 稳定 module_id，及 modules 表维护（stdlib only, Py3.9+）。

03_CAPACITY_AND_INDEXING.md §6：模块用 repo/package/module_id 标识，不以显示名
全局唯一。默认规则（C08）：路径首段目录即模块（src/a.py → src，src/deep/b.py 仍
归 src）；顶层散文件归 _root（README.md → _root）。可用 rules 覆盖：有序
(路径前缀, module_id) 列表，最长前缀优先、按段边界匹配（src/core → core 不吞
src/core2）。

跨代稳定：推导是纯函数——同路径恒同 id，无论哪一代重建（Z12 增量半的前提）。
modules 表沿用 files 的"单快照"语义：rebuild 后只保留当前 generation 的行，
旧行在完成事务里整体清除，绝不把不同 generation 拼成"当前模块清单"。
防御性核对：枚举文件总数必须等于 manifest 物化 file_count（少了=半代、多了=
脏数据），不符即拒绝发布；module_id 非空且不含 "/"。
"""
from typing import NamedTuple

from agents_kernel.storage.db import require_epoch
from agents_kernel.validation import CompanionError

DEFAULT_ROOT_MODULE = "_root"
_PAGE_SIZE = 5000

_MODULES_DDL = (
    """CREATE TABLE IF NOT EXISTS modules (
        module_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        file_count INTEGER NOT NULL,
        PRIMARY KEY (module_id, generation))""",
)

_FILES_PAGE = ("SELECT path FROM files WHERE generation = ? AND path > ?"
               " ORDER BY path LIMIT ?")


class ModulesResult(NamedTuple):
    generation: int
    module_count: int
    file_count: int


class ModuleMapper:
    """纯函数式路径归属器：rules 最长前缀优先，缺省首段目录 / _root。"""

    def __init__(self, rules=()):
        normalized = []
        for prefix, module_id in rules:
            normalized.append((_check_path(prefix, "规则前缀"),
                               _check_module_id(module_id)))
        # 最长前缀优先：src/core 先于 src 参与匹配
        self._rules = sorted(normalized, key=lambda item: -len(item[0]))

    def module_of(self, path):
        path = _check_path(path, "文件路径")
        for prefix, module_id in self._rules:
            if path == prefix or path.startswith(prefix + "/"):
                return module_id
        parts = path.split("/")
        return DEFAULT_ROOT_MODULE if len(parts) == 1 else parts[0]


def build_modules(store, writer, generation, mapper=None):
    """由已发布 files 快照重建 modules 表（流式聚合 + 单事务原子换装）。"""
    if mapper is None:
        mapper = ModuleMapper()
    manifest = store.query_one(
        "SELECT status, file_count FROM scan_generations WHERE generation = ?",
        (generation,))
    if manifest is None or manifest["status"] != "complete":
        raise CompanionError("世代 %r 不存在或未完成，拒绝重建模块表" % (generation,))
    counts = {}
    after = ""
    while True:
        rows = store.query_all(_FILES_PAGE, (generation, after, _PAGE_SIZE))
        if not rows:
            break
        for row in rows:
            module_id = mapper.module_of(row["path"])
            counts[module_id] = counts.get(module_id, 0) + 1
        after = rows[-1]["path"]
        if len(rows) < _PAGE_SIZE:
            break
    total = sum(counts.values())
    if total != int(manifest["file_count"] or 0):
        raise CompanionError("模块核对失败：枚举 %d 个文件与 manifest file_count=%s 不符"
                             % (total, manifest["file_count"]))
    with store.transaction() as conn:
        require_epoch(conn, writer.epoch)
        for statement in _MODULES_DDL:
            conn.execute(statement)
        conn.execute("DELETE FROM modules")  # 单快照：旧行（已变/未变）一并清除
        conn.executemany(
            "INSERT INTO modules (module_id, generation, file_count) VALUES (?, ?, ?)",
            [(module_id, generation, counts[module_id]) for module_id in sorted(counts)])
    return ModulesResult(generation=generation, module_count=len(counts),
                         file_count=total)


def _check_path(value, label):
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise CompanionError("%s必须是相对 POSIX 路径：%r" % (label, value))
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise CompanionError("%s含非法路径段：%r" % (label, value))
    return value


def _check_module_id(value):
    if not isinstance(value, str) or not value or "/" in value:
        raise CompanionError("module_id 必须为不含 / 的非空字符串：%r" % (value,))
    return value

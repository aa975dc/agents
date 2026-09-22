"""变化影响闭包：变更文件集 → 受影响模块反向闭包（stdlib only, Py3.9+）。

06_TASKS_ISOLATION_AND_GATES.md §6：allowed_paths 不是完整依赖集；未知动态依赖
时扩展到保守闭包，不瞎缩小范围。口径：
- 反向闭包：谁（直接或传递）import 了变更模块，谁受影响；闭包恒含变更模块自身。
- 保守扩展：unknown 边的导入目标解析不到，无法证明"它不是变更模块"——凡持有
  unknown 出边的模块一律并入闭包并标 conservative=True；无法证明无关时宁可扩大。
- 闭包基于指定 generation 的 edges 快照（缺省最近 complete 世代）；edges 表尚
  不存在时如实按"无依赖数据"回答（闭包=变更模块自身，conservative=False）。

已知边界：邻接表加载进内存（百万边级的外存 BFS 归后续分片工作）；变更文件不必
已在索引中——module_of 是纯函数，新文件的模块归属照常推导。
"""
from typing import NamedTuple

from agents_kernel.validation import CompanionError


class ClosureResult(NamedTuple):
    generation: int
    changed_modules: tuple    # 变更文件直接归属的模块（排序去重）
    closure: tuple            # 反向闭包（含自身与保守扩展，排序）
    conservative: bool        # True=做了 unknown 保守扩展，闭包不完整可信


def affected_closure(store, changed_files, generation=None, mapper=None):
    """计算变更文件集的受影响模块闭包；只读 store，不写任何表。"""
    if isinstance(changed_files, str) or not changed_files:
        raise CompanionError("changed_files 必须是非空文件路径集合")
    if mapper is None:
        from agents_kernel.indexing.modules import ModuleMapper
        mapper = ModuleMapper()
    if generation is None:
        row = store.query_one(
            "SELECT MAX(generation) AS g FROM scan_generations WHERE status = 'complete'")
        if row is None or row["g"] is None:
            raise CompanionError("索引中尚无完整世代，无法计算闭包")
        generation = int(row["g"])
    changed = sorted({mapper.module_of(path) for path in changed_files})

    reverse = {}          # 被依赖模块 → 依赖方集合（仅 internal 边）
    unknown_sources = set()
    if store.query_one(
            "SELECT 1 AS ok FROM sqlite_master WHERE type = 'table' AND name = 'edges'"
    ) is not None:
        for row in store.query_all(
                """SELECT from_module, to_module_or_unknown, kind FROM edges
                   WHERE generation = ?""", (generation,)):
            if row["kind"] == "internal":
                reverse.setdefault(row["to_module_or_unknown"], set()).add(
                    row["from_module"])
            else:
                unknown_sources.add(row["from_module"])

    closure = set(changed)
    frontier = list(changed)
    while frontier:
        for dependent in reverse.get(frontier.pop(), ()):
            if dependent not in closure:
                closure.add(dependent)
                frontier.append(dependent)
    conservative = bool(unknown_sources)  # 闭包恒非空（含自身），有 unknown 即扩展
    if conservative:
        closure.update(unknown_sources)
    return ClosureResult(generation=generation, changed_modules=tuple(changed),
                         closure=tuple(sorted(closure)), conservative=conservative)

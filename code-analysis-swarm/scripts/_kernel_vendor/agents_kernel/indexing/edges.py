"""Python import 依赖边抽取与 edges 表维护（stdlib only, Py3.9+）。

03_CAPACITY_AND_INDEXING.md §1 L1：结构索引记录依赖；解析能力覆盖不到的
（标准库/第三方/动态拼接/越出根的相对导入）如实标 unknown 并保留原始导入文本，
不硬猜、不静默丢弃（IX04-08）。语法错误/不可读文件跳过并计入 skipped，不中断
整轮——L1 索引不求完美解析。

解析口径：
- 项目内名字空间由本代 files 表的 .py 路径推出：a/b/c.py 隐含 a、a.b、a.b.c
  （兼容无 __init__.py 的命名空间包）；每个隐含名映射到该路径的 module_id。
- import x.y → 对点分名取最长可解析前缀；from x import y 依次尝试 x.y 与 x。
- 相对导入按所在包上溯 level 层；越出项目根 → unknown。
- 边先分批写 edges_staging，完成事务原子换装（单快照语义同 files：edges 只保留
  当前 generation，旧行清除）。

已知边界：读取的是扫描时 root 下的当前内容，扫描后又被改动的文件按新内容记边
（与 backfill 的 size+mtime 核对口径不同，如需严格快照一致由调用方在扫描后
立即建边）；闭包图加载进内存，外存 BFS 不在本层（XXL 分片归后续）。
"""
import ast
from pathlib import Path
from typing import NamedTuple

from agents_kernel.storage.db import require_epoch
from agents_kernel.validation import CompanionError

DEFAULT_BATCH_SIZE = 5000
_PAGE_SIZE = 5000

EDGE_INTERNAL = "internal"
EDGE_UNKNOWN = "unknown"

_EDGES_DDL = (
    """CREATE TABLE IF NOT EXISTS edges (
        generation INTEGER NOT NULL,
        from_module TEXT NOT NULL,
        to_module_or_unknown TEXT NOT NULL,
        kind TEXT NOT NULL,
        source_file TEXT NOT NULL,
        line INTEGER NOT NULL,
        PRIMARY KEY (generation, from_module, to_module_or_unknown, kind,
                     source_file, line))""",
    """CREATE TABLE IF NOT EXISTS edges_staging (
        generation INTEGER NOT NULL,
        from_module TEXT NOT NULL,
        to_module_or_unknown TEXT NOT NULL,
        kind TEXT NOT NULL,
        source_file TEXT NOT NULL,
        line INTEGER NOT NULL,
        PRIMARY KEY (generation, from_module, to_module_or_unknown, kind,
                     source_file, line))""",
)

_FILES_PAGE = ("SELECT path FROM files WHERE generation = ? AND path > ?"
               " ORDER BY path LIMIT ?")
_STAGING_UPSERT = """INSERT OR IGNORE INTO edges_staging
    (generation, from_module, to_module_or_unknown, kind, source_file, line)
    VALUES (?, ?, ?, ?, ?, ?)"""


class EdgesResult(NamedTuple):
    generation: int
    internal_edges: int      # 解析到项目内 module_id 的边数
    unknown_edges: int       # 解析不到、保留原始导入文本的边数
    parsed_files: int        # 成功解析的 .py 文件数
    skipped: tuple           # ((path, 原因摘要), ...) 按路径排序的跳过账


def build_edges(store, writer, generation, mapper=None, batch_size=DEFAULT_BATCH_SIZE):
    """对本代 files 快照中的全部 .py 抽取 import 边（流式分批 + 原子换装）。"""
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise CompanionError("batch_size 必须为正整数")
    if mapper is None:
        from agents_kernel.indexing.modules import ModuleMapper
        mapper = ModuleMapper()
    manifest = store.query_one(
        "SELECT status FROM scan_generations WHERE generation = ?", (generation,))
    if manifest is None or manifest["status"] != "complete":
        raise CompanionError("世代 %r 不存在或未完成，拒绝构建依赖边" % (generation,))
    root = _generation_root(store, generation)
    name_map = _project_names(store, generation, mapper)
    with store.transaction() as conn:  # 建表先行，staging 批写才有落点
        require_epoch(conn, writer.epoch)
        for statement in _EDGES_DDL:
            conn.execute(statement)
    counts = {"internal": 0, "unknown": 0, "parsed": 0, "batches": 0}
    skipped = []
    buffer = []

    def flush():
        if buffer:
            with store.transaction() as conn:
                require_epoch(conn, writer.epoch)
                conn.executemany(_STAGING_UPSERT, buffer)
            buffer.clear()
            counts["batches"] += 1

    after = ""
    while True:
        rows = store.query_all(_FILES_PAGE, (generation, after, _PAGE_SIZE))
        if not rows:
            break
        for row in rows:
            path = row["path"]
            if not path.endswith(".py"):
                continue
            for edge in _file_edges(root, path, name_map, mapper, skipped):
                counts[edge[2]] += 1
                buffer.append((generation,) + edge)
            counts["parsed"] += 1
            if len(buffer) >= batch_size:
                flush()
        after = rows[-1]["path"]
        if len(rows) < _PAGE_SIZE:
            break
    flush()
    with store.transaction() as conn:  # 完成事务：staging → edges 原子换装
        require_epoch(conn, writer.epoch)
        conn.execute("DELETE FROM edges")
        conn.execute(
            """INSERT INTO edges SELECT * FROM edges_staging WHERE generation = ?""",
            (generation,))
        conn.execute("DELETE FROM edges_staging WHERE generation = ?", (generation,))
    return EdgesResult(generation=generation, internal_edges=counts["internal"],
                       unknown_edges=counts["unknown"], parsed_files=counts["parsed"],
                       skipped=tuple(sorted(skipped)))


def _file_edges(root, path, name_map, mapper, skipped):
    """单个 .py → 产出 (from_module, to, kind, source_file, line)；失败记 skipped。"""
    from_module = mapper.module_of(path)
    try:
        source = (Path(root) / path).read_bytes()
        tree = ast.parse(source)
    except SyntaxError as error:
        skipped.append((path, "语法错误：%s" % error.msg))
        return
    except (OSError, ValueError) as error:  # 消失/不可读/非 UTF-8 声明等
        skipped.append((path, "不可读：%s" % error.__class__.__name__))
        return
    package = path.split("/")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield _edge(from_module, alias.name, path, node.lineno,
                            _resolve(alias.name, name_map))
        elif isinstance(node, ast.ImportFrom):
            base_parts = _relative_base(package, node.level)
            for alias in node.names:
                written = "." * node.level + (node.module or "")
                if alias.name != "*":
                    written += "." + alias.name if written else alias.name
                resolved = None
                if base_parts is not None:
                    prefix = ".".join(base_parts)
                    candidates = []
                    if node.module:
                        base = prefix + "." + node.module if prefix else node.module
                        candidates.append(base)
                        if alias.name != "*":
                            candidates.append(base + "." + alias.name)
                    else:
                        if alias.name != "*" and prefix:
                            candidates.append(prefix + "." + alias.name)
                        candidates.append(prefix)
                    for candidate in candidates:  # 最具体的名字优先
                        resolved = _resolve(candidate, name_map)
                        if resolved is not None:
                            break
                yield _edge(from_module, written, path, node.lineno, resolved)


def _edge(from_module, written, path, line, resolved):
    if resolved is None:
        return (from_module, written, EDGE_UNKNOWN, path, line)
    return (from_module, resolved, EDGE_INTERNAL, path, line)


def _relative_base(package, level):
    """相对导入的基准包段；越出项目根返回 None。"""
    if level - 1 > len(package):
        return None
    return package[: len(package) - (level - 1)]


def _resolve(dotted, name_map):
    """点分名 → module_id；取最长可解析前缀，全不可解析返回 None。"""
    parts = dotted.split(".")
    for end in range(len(parts), 0, -1):
        hit = name_map.get(".".join(parts[:end]))
        if hit is not None:
            return hit
    return None


def _project_names(store, generation, mapper):
    """本代全部 .py 路径 → 隐含点分名集合（含各级目录包名）到 module_id 的映射。"""
    name_map = {}
    after = ""
    while True:
        rows = store.query_all(_FILES_PAGE, (generation, after, _PAGE_SIZE))
        if not rows:
            break
        for row in rows:
            path = row["path"]
            if not path.endswith(".py"):
                continue
            module_id = mapper.module_of(path)
            parts = path[:-3].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            for depth in range(1, len(parts) + 1):
                name_map.setdefault(".".join(parts[:depth]), module_id)
        after = rows[-1]["path"]
        if len(rows) < _PAGE_SIZE:
            break
    return name_map


def _generation_root(store, generation):
    row = store.query_one(
        "SELECT root FROM scan_generations WHERE generation = ?", (generation,))
    if row is None:
        raise CompanionError("世代不存在：%r" % (generation,))
    return row["root"]

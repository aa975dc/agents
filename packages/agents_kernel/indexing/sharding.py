"""XL 索引分片与跨片图（03_CAPACITY_AND_INDEXING.md §6/§8/§10；stdlib only, Py3.9+）。

单库过大时按 module_id 切成多个独立分片库（shard-<id>.sqlite），表结构与
generation 语义完全复用 scanner（files/scan_generations 单快照、半代 staging、
epoch 单写者），每片独立可读可查（IX08：单分片查询不打开其他分片库）。

- ShardPlanner：module_id → 分片清单。可配置每片最大模块数/文件数（§8 起点
  5万–20万条目，政策值非硬保证）；_root 与单模块即超限的归专门 shard（独占一片）。
  推导确定性：模块按字典序贪心装箱，同输入恒同分片。
- ShardWriter：单次 walk 流式分发（绝不全量物化，§4/§10），各分片 scanner 只收
  本片模块的行；写完逐片核对 实际行数==期望行数 后发布。计划外模块（漏片）整批
  拒绝；中断留下的半代由下次扫描按 scanner 既有语义作废，不污染旧代。
- 根 manifest（shards.json，§6"跨分片保存 generation vector 和根 manifest"）：
  分片清单 + 模块归属 + 期望行数 + 写入时 generation vector。加载时完整性校验：
  分片库缺失（漏片）/多余库文件/模块重复（重片）/行数合计≠期望 全部拒绝。
- CrossShardGraph：汇总各片 edges 表 → 全局边。片内 import 已在片内解析为
  internal 边；指向他片模块的 import 在本片解析不到（IX08 单片自洽），按原始
  点分名落 unknown 边——全局层用根 manifest 的全量模块映射对其做与 edges 模块
  同口径的最长前缀二次解析，解析命中即标注 from_shard/to_shard（kind 仍如实记
  unknown，不冒充 internal）。分片级依赖图做 Tarjan SCC——分片级环 ⇔ 存在跨片
  模块循环依赖（每条分片边都来自真实模块边）；片内环归各分片单库自查。某片从未
  建边时全局环判定标 incomplete，不以各片无环冒充全局无环（§10）。
- generation vector：{shard_id: generation}（读取自各片库的最新 complete 世代）；
  比较函数给出 equal/ahead/behind/diverged——键集不同（分片增删）即 diverged。

已知边界：分片级重扫（only=）按整树重走再过滤（内存有界、正确性优先）；XL 百万
级物理压测归 P6-05（NOT_RUN）；远端分片默认禁用（§8）。模块级跨片商图精化、
外存迭代归后续。
"""
import json
from pathlib import Path
from typing import NamedTuple

from agents_kernel.filesystem import walk
from agents_kernel.indexing.modules import DEFAULT_ROOT_MODULE, ModuleMapper
from agents_kernel.indexing.scanner import DEFAULT_BATCH_SIZE, IndexScanner
from agents_kernel.storage import db
from agents_kernel.storage.db import utcnow
from agents_kernel.validation import CompanionError

REGISTRY_NAME = "shards.json"
SHARD_PREFIX = "shard-"
DEFAULT_MAX_MODULES = 64        # §8 政策起点：每片模块数上限（可调）
DEFAULT_MAX_FILES = 200000      # §8 政策起点：每片 5万–20万条目取上界（可调）
_REGISTRY_VERSION = 1

VECTOR_EQUAL = "equal"
VECTOR_AHEAD = "ahead"
VECTOR_BEHIND = "behind"
VECTOR_DIVERGED = "diverged"


class ShardSpec(NamedTuple):
    """一个分片的清单项：包含模块与期望文件数（Σ模块）。"""
    shard_id: str
    modules: tuple            # module_id 元组（非空）
    expected_files: int       # 期望已发布行数


class ShardPlan(NamedTuple):
    """分片方案：有序 ShardSpec + 模块归属；shard_of 对计划外模块抛错（漏片拒绝）。"""
    shards: tuple

    def shard_of(self, module_id):
        for spec in self.shards:
            if module_id in spec.modules:
                return spec
        raise CompanionError("模块 %r 不在任何分片中（漏片拒绝）" % (module_id,))

    def expected_total(self):
        return sum(spec.expected_files for spec in self.shards)

    def module_shard_map(self):
        return {module_id: spec.shard_id
                for spec in self.shards for module_id in spec.modules}


class ShardWriteResult(NamedTuple):
    shard_count: int
    file_count: int           # Σ 全清单各片已发布行数（== plan.expected_total()）
    excluded_count: int       # Σ 本次写入各片排除账
    generations: dict         # shard_id -> 世代向量（未重扫片沿用上次写入记录）
    registry_path: str


class ShardPlanner:
    """按 module_id 分桶：字典序贪心装箱；_root 与超限模块强制独占一片。"""

    def __init__(self, max_modules=DEFAULT_MAX_MODULES, max_files=DEFAULT_MAX_FILES):
        for name, value in (("max_modules", max_modules), ("max_files", max_files)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise CompanionError("%s 必须为正整数" % name)
        self.max_modules = max_modules
        self.max_files = max_files

    def plan(self, module_counts):
        """{module_id: 文件数} → ShardPlan；确定性（同输入恒同分片）。"""
        counts = {}
        for module_id, count in module_counts.items():
            if not isinstance(module_id, str) or not module_id or "/" in module_id:
                raise CompanionError("module_id 必须为不含 / 的非空字符串：%r" % (module_id,))
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise CompanionError("模块 %r 的文件数必须为非负整数：%r" % (module_id, count))
            counts[module_id] = count
        shards = []
        current = []
        current_files = 0

        def close():
            nonlocal current, current_files
            if current:
                shards.append(ShardSpec(
                    shard_id=SHARD_PREFIX + "%04d" % len(shards),
                    modules=tuple(current), expected_files=current_files))
                current, current_files = [], 0

        for module_id in sorted(counts):
            count = counts[module_id]
            dedicated = (module_id == DEFAULT_ROOT_MODULE      # _root 永远独占
                         or count > self.max_files)            # 单模块即超限：无法共片
            if dedicated:
                close()
                shards.append(ShardSpec(
                    shard_id=SHARD_PREFIX + "%04d" % len(shards),
                    modules=(module_id,), expected_files=count))
            elif (current and (len(current) >= self.max_modules
                               or current_files + count > self.max_files)):
                close()
                current, current_files = [module_id], count
            else:
                current.append(module_id)
                current_files += count
        close()
        return ShardPlan(shards=tuple(shards))


class _ShardRun:
    """写分片期间一个分片的运行态（store/writer/scanner/缓冲/计数）。"""

    def __init__(self, spec, store, writer, scanner, generation, buffers, counts):
        self.spec = spec
        self.store = store
        self.writer = writer
        self.scanner = scanner
        self.generation = generation
        self.buffers = buffers
        self.counts = counts

    def close(self):
        try:
            self.writer.close()
        finally:
            self.store.close()


class ShardWriter:
    """单次 walk → 按模块分发到各分片库；逐片独立走 scanner 的完整世代语义。"""

    def __init__(self, mapper=None, batch_size=DEFAULT_BATCH_SIZE):
        self.mapper = mapper if mapper is not None else ModuleMapper()
        self.batch_size = batch_size

    def write_shards(self, root, plan, shard_dir, *,
                     only=None, extra_excluded=(), hash_hook=None):
        """写/重写分片并落根 manifest；only 限定重扫的分片 id 子集（其余不动）。

        计划外模块（漏片）与行数核对不符 → CompanionError，整批不发布（半代由
        下次扫描作废——scanner 崩溃安全语义，不污染各片旧代）。
        """
        root = Path(root).absolute()
        shard_dir = Path(shard_dir)
        targets = plan.shards
        if only is not None:
            known = {spec.shard_id for spec in plan.shards}
            unknown = sorted(set(only) - known)
            if unknown:
                raise CompanionError("only 含未知分片 id：%r" % (unknown,))
            targets = tuple(spec for spec in plan.shards if spec.shard_id in set(only))
        shard_dir.mkdir(parents=True, exist_ok=True)
        mode = walk.detect_mode(root)
        runs, results, orphans = [], {}, {}
        target_ids = {spec.shard_id for spec in targets}
        try:
            for spec in targets:
                store = db.Store(shard_dir / (spec.shard_id + ".sqlite"))
                store.open()
                writer = db.acquire_writer(store)
                scanner = IndexScanner(store, self.batch_size)
                generation, buffers = scanner._open_generation(root, mode, writer)
                counts = {"files": 0, "excluded": 0, "batches": 0,
                          "reused": 0, "hashed": 0}
                runs.append(_ShardRun(spec, store, writer, scanner,
                                      generation, buffers, counts))
            module_shard = plan.module_shard_map()
            run_by_id = {run.spec.shard_id: run for run in runs}
            for item in walk.walk_tree(root, extra_excluded=extra_excluded, mode=mode):
                path = item.path[:-1] if item.path.endswith("/") else item.path
                module_id = self.mapper.module_of(path)
                shard_id = module_shard.get(module_id)
                if shard_id is None:
                    # 计划外模块 = 漏片；排除账不追究归属缺口（主账在行数核对）
                    if isinstance(item, walk.FileEntry):
                        orphans[module_id] = orphans.get(module_id, 0) + 1
                    continue
                run = run_by_id.get(shard_id)
                if run is None:
                    continue  # only 重扫：他片文件不属于本轮，静默跳过
                if isinstance(item, walk.FileEntry):
                    run.buffers["files"].append(run.scanner._observe(
                        root, run.generation, item, hash_hook, run.counts))
                elif isinstance(item, walk.ExcludedEntry):
                    run.buffers["excluded"].append((run.generation, item.path, item.reason))
                    run.counts["excluded"] += 1
                else:
                    raise CompanionError("未知枚举记录类型：%r" % (item,))
                if (len(run.buffers["files"]) >= self.batch_size
                        or len(run.buffers["excluded"]) >= self.batch_size):
                    run.scanner._flush(run.buffers, run.writer, run.counts)
            if orphans:
                raise CompanionError(
                    "发现 %d 个分片计划外模块（漏片拒绝，请重新 plan）：%s"
                    % (len(orphans), ", ".join(
                        "%s(%d 文件)" % item for item in sorted(orphans.items()))))
            for run in runs:  # 收尾清空残余缓冲（同 scanner.scan 的末次 flush）
                run.scanner._flush(run.buffers, run.writer, run.counts)
            for run in runs:
                scan = run.scanner._publish(run.generation, run.writer,
                                            run.counts, mode)
                if scan.file_count != run.spec.expected_files:
                    raise CompanionError(
                        "分片 %s 行数核对失败：实际 %d != 期望 %d（重片/漏片拒绝）"
                        % (run.spec.shard_id, scan.file_count, run.spec.expected_files))
                results[run.spec.shard_id] = scan
        finally:
            for run in runs:
                run.close()
        return self._write_registry(root, plan, shard_dir, only, results)

    # ---- 内部：根 manifest ----

    def _write_registry(self, root, plan, shard_dir, only, results):
        """写 shards.json：分片清单 + 写入时 generation vector（§6 根 manifest）。

        only 重扫时保留未重扫分片在旧 manifest 里的记录（世代未变，如实沿用）。
        """
        registry_path = shard_dir / REGISTRY_NAME
        previous = {}
        if registry_path.is_file() and only is not None:
            try:
                previous = {row["shard_id"]: row for row in
                            json.loads(registry_path.read_text(encoding="utf-8"))["shards"]}
            except (OSError, ValueError, KeyError, TypeError):
                previous = {}
        rows, generations = [], {}
        for spec in plan.shards:
            if spec.shard_id in results:
                scan = results[spec.shard_id]
                row = {"shard_id": spec.shard_id, "modules": list(spec.modules),
                       "expected_files": spec.expected_files,
                       "db": spec.shard_id + ".sqlite",
                       "generation": scan.generation, "file_count": scan.file_count}
            elif spec.shard_id in previous:
                row = previous[spec.shard_id]
            else:
                continue  # only 重扫不该出现；防御性跳过（清单以 plan 为准）
            rows.append(row)
            generations[row["shard_id"]] = row["generation"]
        total = sum(int(row["file_count"]) for row in rows)
        document = {"version": _REGISTRY_VERSION, "root": str(root),
                    "created_at": utcnow(), "shards": rows,
                    "generation_vector": generations,
                    "expected_total_files": total}
        registry_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        return ShardWriteResult(shard_count=len(results),
                                file_count=total,
                                excluded_count=sum(scan.excluded_count
                                                   for scan in results.values()),
                                generations=dict(generations),
                                registry_path=str(registry_path))


class ShardRegistry:
    """根 manifest 的加载与校验：漏片/重片/多余库文件在此拒绝。"""

    def __init__(self, shard_dir, document):
        self.dir = Path(shard_dir)
        self.document = document
        shards = document.get("shards")
        if not isinstance(shards, list):
            raise CompanionError("根 manifest 缺少 shards 列表")
        self.shards = []
        self.module_shard = {}
        for row in shards:
            shard_id = row.get("shard_id")
            modules = row.get("modules")
            if (not isinstance(shard_id, str) or not shard_id.startswith(SHARD_PREFIX)
                    or row.get("db") != shard_id + ".sqlite"
                    or not isinstance(modules, list) or not modules):
                raise CompanionError("根 manifest 分片项非法：%r" % (row,))
            for module_id in modules:
                if module_id in self.module_shard:
                    raise CompanionError(
                        "模块 %r 同时属于 %s 与 %s（重片拒绝）"
                        % (module_id, self.module_shard[module_id], shard_id))
                self.module_shard[module_id] = shard_id
            self.shards.append(row)

    @classmethod
    def load(cls, shard_dir):
        path = Path(shard_dir) / REGISTRY_NAME
        if not path.is_file():
            raise CompanionError("根 manifest 不存在：%s" % path)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise CompanionError("根 manifest 损坏：%s" % error)
        return cls(shard_dir, document)

    def shard_ids(self):
        return [row["shard_id"] for row in self.shards]

    def spec_of(self, shard_id):
        for row in self.shards:
            if row["shard_id"] == shard_id:
                return row
        raise CompanionError("分片不存在：%r" % (shard_id,))

    def db_path(self, shard_id):
        return self.dir / (shard_id + ".sqlite")

    def expected_total(self):
        return sum(int(row["expected_files"]) for row in self.shards)


class CrossShardEdge(NamedTuple):
    """全局图中的一条边；跨片边 from_shard != to_shard。

    internal 边的 to_module 是项目内 module_id，to_shard 直接查映射；unknown 边
    保留原始导入文本，全局层对其做最长前缀二次解析，命中项目模块即标 to_shard，
    否则（标准库/第三方/动态）为 None。
    """
    from_module: str
    to_module_or_unknown: str
    kind: str
    source_file: str
    line: int
    from_shard: str
    to_shard: object


class IntegrityReport(NamedTuple):
    shard_count: int
    file_count: int           # Σ 各片实际行数
    expected_files: int       # 根 manifest 期望合计


class ShardCycleResult(NamedTuple):
    cycles: tuple             # ((shard_id, ...), ...) 长度>1 的强连通分量
    complete: bool            # 全部片都已建边才为 True；否则全局环判定不完整
    missing_edges: tuple      # 尚未建边的分片 id


class CrossShardGraph:
    """跨片图查询面：按需懒打开各分片库（只读查询走 db.Store 只读入口）。"""

    def __init__(self, registry):
        self.registry = registry
        self._stores = {}

    @classmethod
    def load(cls, shard_dir):
        return cls(ShardRegistry.load(shard_dir))

    def close(self):
        for store in self._stores.values():
            store.close()
        self._stores = {}

    # ---- 完整性 ----

    def verify_integrity(self):
        """漏片/多余库/重片/行数合计核对；任一不符抛 CompanionError。"""
        for shard_id in self.registry.shard_ids():
            if not self.registry.db_path(shard_id).is_file():
                raise CompanionError("分片库缺失（漏片拒绝）：%s"
                                     % self.registry.db_path(shard_id))
        present = {path.name for path in self.registry.dir.glob(SHARD_PREFIX + "*.sqlite")}
        expected = {row["db"] for row in self.registry.shards}
        extra = sorted(present - expected)
        if extra:
            raise CompanionError("存在清单外分片库（重片/孤儿拒绝）：%s" % ", ".join(extra))
        total = 0
        for row in self.registry.shards:
            count = self._shard_file_count(row["shard_id"])
            if count != int(row["expected_files"]):
                raise CompanionError(
                    "分片 %s 行数核对失败：实际 %d != 期望 %d"
                    % (row["shard_id"], count, row["expected_files"]))
            total += count
        if total != self.registry.expected_total():
            raise CompanionError("全局文件数 %d != 清单合计 %d（漏片/重片拒绝）"
                                 % (total, self.registry.expected_total()))
        return IntegrityReport(shard_count=len(self.registry.shards),
                               file_count=total,
                               expected_files=self.registry.expected_total())

    # ---- 单分片独立查询（IX08：不需要打开其他分片库） ----

    def open_shard(self, shard_id):
        """打开单个分片库供独立查询；库文件缺失拒绝（不新建空库掩盖漏片）。"""
        path = self.registry.db_path(shard_id)
        if not path.is_file():
            raise CompanionError("分片库缺失：%s" % path)
        if shard_id not in self._stores:
            store = db.Store(path)
            store.open()
            self._stores[shard_id] = store
        return self._stores[shard_id]

    def _shard_file_count(self, shard_id):
        store = self.open_shard(shard_id)
        row = store.query_one(
            """SELECT file_count FROM scan_generations
               WHERE generation = (SELECT MAX(generation) FROM scan_generations
                                   WHERE status = 'complete')""")
        if row is None or row["file_count"] is None:
            raise CompanionError("分片 %s 无完整世代" % shard_id)
        return int(row["file_count"])

    # ---- generation vector ----

    def generation_vector(self):
        """{shard_id: 最新 complete 世代}，读取自各片库本身（非清单缓存）。"""
        return {shard_id: self._shard_generation(shard_id)
                for shard_id in self.registry.shard_ids()}

    def _shard_generation(self, shard_id):
        store = self.open_shard(shard_id)
        row = store.query_one(
            "SELECT MAX(generation) AS g FROM scan_generations WHERE status = 'complete'")
        if row is None or row["g"] is None:
            raise CompanionError("分片 %s 无完整世代" % shard_id)
        return int(row["g"])

    # ---- 跨片图 ----

    def iter_edges(self):
        """汇总各片 edges 表 → 全局边流；分片归属由根 manifest 模块映射标注。"""
        module_shard = self.registry.module_shard
        for shard_id in self.registry.shard_ids():
            store = self.open_shard(shard_id)
            if not store.query_one(
                    "SELECT 1 AS ok FROM sqlite_master WHERE type = 'table'"
                    " AND name = 'edges'"):
                continue
            for row in store.query_all(
                    """SELECT from_module, to_module_or_unknown, kind, source_file, line
                       FROM edges"""):
                if row["kind"] == "internal":
                    to_shard = module_shard.get(row["to_module_or_unknown"])
                else:  # 片内解析不到的 import：按全局模块映射二次解析（同口径）
                    to_shard = _resolve_shard(row["to_module_or_unknown"], module_shard)
                yield CrossShardEdge(
                    from_module=row["from_module"],
                    to_module_or_unknown=row["to_module_or_unknown"],
                    kind=row["kind"], source_file=row["source_file"],
                    line=int(row["line"]), from_shard=shard_id, to_shard=to_shard)

    def shard_graph(self):
        """分片级依赖图 {shard_id: {to_shard: 边数}}；全部片都作为节点出现。"""
        graph = {shard_id: {} for shard_id in self.registry.shard_ids()}
        for edge in self.iter_edges():
            if edge.to_shard is not None and edge.to_shard != edge.from_shard:
                graph[edge.from_shard][edge.to_shard] = \
                    graph[edge.from_shard].get(edge.to_shard, 0) + 1
        return graph

    def find_shard_cycles(self):
        """分片级 SCC：非平凡分量即跨片循环依赖；有片未建边时 complete=False。"""
        missing = tuple(shard_id for shard_id in self.registry.shard_ids()
                        if not self.open_shard(shard_id).query_one(
                            "SELECT 1 AS ok FROM sqlite_master WHERE type = 'table'"
                            " AND name = 'edges'"))
        graph = self.shard_graph()
        cycles = tuple(sorted(
            (component for component in _strongly_connected(graph) if len(component) > 1),
            key=lambda component: component[0]))
        return ShardCycleResult(cycles=cycles, complete=not missing,
                                missing_edges=missing)


def compare_vectors(vector, baseline):
    """generation vector 比较：equal / ahead / behind / diverged。

    ahead = 逐片全部 >= 且至少一片严格 >（整体重扫或部分推进）；
    键集不同（分片增删）或各有领先片 = diverged。
    """
    for name, item in (("vector", vector), ("baseline", baseline)):
        if not isinstance(item, dict):
            raise CompanionError("%s 必须是 {shard_id: generation} 字典" % name)
    if vector.keys() != baseline.keys():
        return VECTOR_DIVERGED
    ahead = behind = False
    for shard_id in vector:
        mine, theirs = vector[shard_id], baseline[shard_id]
        if not isinstance(mine, int) or not isinstance(theirs, int):
            raise CompanionError("generation 必须为整数：%r vs %r" % (mine, theirs))
        if mine > theirs:
            ahead = True
        elif mine < theirs:
            behind = True
    if ahead and behind:
        return VECTOR_DIVERGED
    if ahead:
        return VECTOR_AHEAD
    if behind:
        return VECTOR_BEHIND
    return VECTOR_EQUAL


def _resolve_shard(dotted, module_shard):
    """点分名 → 分片 id：对全量模块映射取最长可解析前缀（与 edges._resolve 同口径）。

    module_shard 的键是 module_id（含隐含命名空间名，如 gamma 与 gamma.mod_000 同源），
    因此 "gamma.mod_000" 命中 "gamma"；全不可解析（标准库/第三方）返回 None。
    """
    if not isinstance(dotted, str) or not dotted:
        return None
    parts = dotted.split(".")
    for end in range(len(parts), 0, -1):
        hit = module_shard.get(".".join(parts[:end]))
        if hit is not None:
            return hit
    return None


def _strongly_connected(graph):
    """迭代式 Tarjan（shard 级图很小，仍避免递归深度依赖）；返回全部 SCC。"""
    index_of, low, on_stack = {}, {}, set()
    stack, components = [], []
    counter = 0
    for start in sorted(graph):
        if start in index_of:
            continue
        index_of[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        work = [(start, iter(sorted(graph[start])))]
        while work:
            node, neighbors = work[-1]
            descended = False
            for nxt in neighbors:
                if nxt not in graph:
                    continue
                if nxt not in index_of:
                    index_of[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(graph[nxt]))))
                    descended = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index_of[nxt])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(tuple(sorted(component)))
    return components

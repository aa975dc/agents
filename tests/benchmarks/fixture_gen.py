#!/usr/bin/env python3
"""确定性基准 fixture 生成器（L/XL/XXL 档容量验收用；生成路径 stdlib only, Py3.9+）。

09_TEST_AND_BENCHMARK_ACCEPTANCE.md §5 授权边界：
- 产物只写入 tempfile.gettempdir() 之下、目录名以 benchmark-fixture 开头的显式
  标识目录；根目录写 BENCHMARK_FIXTURE marker + manifest.json（数量/字节/树参数/
  生成时间），删除方只能认 marker+manifest，不得碰其它目录。
- 必须先 --dry-run 估算（文件数、实际内容字节、inode、磁盘余量、逐代索引行），
  限额 --max-bytes 默认 2GiB（自动小档上限），超限拒绝实跑。
- 确定性：同一 --seed 与规模参数生成逐字节相同的树（内容由纯函数 f(index, seed)
  决定，与生成顺序无关）；每个模块写盘前 compile() 校验真实语法。

三种规模口径（同一执行路径扩参数，不造第二生成器；R07 口径修正）：
- --files N：physical files profile——N 个内容文件（marker/manifest 另计不入预算，
  与历史 L 档语义一致）。
- --rows N：current-generation distinct profile——盘上恰好 N 个普通文件（含
  marker+manifest 各 1 个），对 fixture 根做一轮普查即得 N 行当前代 distinct
  索引行（行数≈文件数的确定性映射，逐行路径唯一）。
- --generations K：history retention profile——base 扫描 + (K-1) 轮受控变更
  （每轮新增 adds 个、删除 deletes 个、修改 ~1% 文件）并逐代扫描；逐代行数与
  累计行数（count_total_rows）记入 manifest.json 的 bench 字段。**累计行数是
  多代工作量口径，≠ 当前代 distinct 条目数**（当前实现 files 表为单快照，
  旧代行在完成事务清除，历史只留 scan_generations 逐代账）。
  K=1 即只做 base 扫描（current profile）；缺省 0 = 纯 fixture 不建索引。
  K≥1 需要 packages/agents_kernel（懒加载；纯 fixture 生成不需要）。

模块内容含对"第 index-1 个模块"的确定性 import（全树构成严格递减链），为依赖边
（09 §2 edges 字段）提供真实 internal 边。

用法：
    python3 fixture_gen.py <dir> --files 100000 --dry-run
    python3 fixture_gen.py <dir> --rows 1000000 --generations 1
    python3 fixture_gen.py <dir> --rows 800 --generations 3 --top-dirs 4 --sub-dirs 4
"""
import argparse
import hashlib
import json
import os
import platform
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

MARKER_TAG = "benchmark-fixture"
MARKER_NAME = "BENCHMARK_FIXTURE"
DEFAULT_SEED = 20260920
DEFAULT_TOP_DIRS = 100
DEFAULT_SUB_DIRS = 100
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 09 §5：自动小档 ≤2GiB 实际写入
DEFAULT_BIG_FILES = 10                       # 仅当规模 ≥ 10000 时生成（占少数）
WRITE_CHUNK = 1024 * 1024
INDEX_DIR_NAME = "index"                     # 索引库目录（扫描时排除，防自索引）
INDEX_DB_REL = INDEX_DIR_NAME + "/facts.sqlite"

_IMPORTS = ("import hashlib", "import json", "import math", "import os",
            "import random", "import statistics", "import string", "import textwrap")


def validate_target(raw):
    """目标目录必须在 gettempdir() 下且目录名以 benchmark-fixture 开头。"""
    directory = Path(raw).absolute()
    if not directory.name.startswith(MARKER_TAG):
        raise ValueError("目录名必须以 %s 开头（显式标识，09 §5）：%s" % (MARKER_TAG, directory))
    temp_root = Path(tempfile.gettempdir()).resolve()
    resolved = directory.resolve()
    if not resolved.is_relative_to(temp_root) or resolved == temp_root:
        raise ValueError("fixture 只能生成在临时目录 %s 之下，拒绝：%s" % (temp_root, directory))
    if directory.exists() and not directory.is_dir():
        raise ValueError("目标已存在且不是目录（拒绝 symlink/文件逃逸）：%s" % directory)
    return resolved


def big_file_specs(count):
    """大文件路径与字节数：5–20MiB 确定性分布，占少数（测流式路径）。"""
    return [{"index": index, "path": "big/blob_%02d.bin" % index,
             "bytes": (5 + (index * 7) % 16) * 1024 * 1024}
            for index in range(count)]


def big_block(index):
    """大文件内容：32 字节确定性模式循环，写盘按 1MiB 分块。"""
    return hashlib.sha256(b"benchmark-fixture-big-%d" % index).digest()


def _block(style, rng, index, offset):
    """一个原子语法块（5–15 行）；返回 (行列表, __main__ 调用名)。"""
    if style == 0:
        name = "pick_%d_%d" % (index % 1000, offset)
        return ["def %s(values, limit):" % name,
                "    \"\"\"筛选并平方，返回前 limit 个。\"\"\"",
                "    picked = [x * x for x in values if x %% %d]" % rng.randint(2, 9),
                "    return picked[:limit]", ""], name
    if style == 1:
        name = "tally_%d_%d" % (index % 1000, offset)
        return ["def %s(values, limit):" % name,
                "    \"\"\"累加直到超限，返回累计与计数。\"\"\"",
                "    total = 0", "    used = 0",
                "    for value in values:",
                "        if total >= limit:",
                "            break",
                "        total += value + %d" % rng.randint(1, 99),
                "        used += 1",
                "    return total, used", ""], name
    if style == 2:
        name = "bucket_%d_%d" % (index % 1000, offset)
        return ["def %s(values, limit):" % name,
                "    \"\"\"键值聚合：按整除余数分桶。\"\"\"",
                "    buckets = {'r0': [], 'r1': []}",
                "    for value in values:",
                "        key = 'r0' if value %% %d == 0 else 'r1'" % rng.randint(2, 7),
                "        buckets[key].append(value)",
                "    return {k: v[:limit] for k, v in buckets.items()}", ""], name
    name = "Sample%d_%d" % (index % 1000, offset)
    return ["class %s:" % name,
            "    \"\"\"最小状态机：记录并回放操作序列。\"\"\"",
            "",
            "    def __init__(self, start=0):",
            "        self.state = start",
            "        self.history = []",
            "",
            "    def apply(self, delta):",
            "        self.state += delta",
            "        self.history.append(delta)",
            "        return self.state",
            "",
            "    def replay(self):",
            "        current = 0",
            "        for delta in self.history:",
            "            current += delta",
            "        return current", ""], name


def _leaf_of(index, plan):
    """小文件序号 → 叶目录序号（leaf_plan 分配的精确逆运算，O(1)）。"""
    per_leaf, remainder = plan["files_per_leaf"], plan["remainder_leaves"]
    if per_leaf > 0:
        boundary = remainder * (per_leaf + 1)
        if index < boundary:
            return index // (per_leaf + 1)
        return remainder + (index - boundary) // per_leaf
    return index  # per_leaf == 0：每个活跃叶恰好 1 个文件


def small_rel(index, plan, sub_dirs):
    """基础小文件序号 → 树内相对路径（与写盘顺序逐一致）。"""
    top, sub = divmod(_leaf_of(index, plan), sub_dirs)
    return "pkg_%02d/sub_%02d/mod_%06d.py" % (top, sub, index)


def added_rel(round_no, local):
    """第 round_no 轮新增的第 local 个文件的相对路径（受控变更产物）。"""
    return "gen_%02d/add_%04d.py" % (round_no, local)


def change_line(round_no):
    """受控变更追加到已存在文件末尾的确定性内容行（保持语法有效）。"""
    return "\n# benchmark fixture change (round %d)\n" % round_no


def modified_indices(round_no, small_total):
    """第 round_no 轮修改的基础小文件序号（~1%，stride 100、偏移轮号）。"""
    return range(round_no - 1, small_total, 100)


def _import_line(index, layout):
    """第 index 个模块对第 index-1 个模块的确定性 import（index≥1 且有 layout 时）。

    全树构成严格的模块级递减链（j → j-1，无环），为 edges 字段提供真实 internal
    边；被删目标已不在当代快照时按 edges 既有语义落为 unknown 边（如实）。
    """
    if layout is None or index < 1:
        return None
    target_index = index - 1
    if target_index < layout["small_total"]:
        target = small_rel(target_index, layout["plan"], layout["sub_dirs"])
    else:
        round_no, local = divmod(target_index - layout["small_total"], layout["adds"])
        target = added_rel(round_no + 1, local)
    return "import %s" % target[:-3].replace("/", ".")


def module_source(index, seed, layout=None):
    """第 index 个小型 Python 模块的完整源码（30–80 行真实语法，纯函数确定性）。"""
    rng = random.Random((seed * 1000003 + index) % (1 << 48))
    lines = ["# benchmark fixture module %06d (seed=%d, deterministic)" % (index, seed),
             ""]
    chain_import = _import_line(index, layout)
    if chain_import:
        lines.append(chain_import)
    lines += [rng.choice(_IMPORTS), rng.choice(_IMPORTS), ""]
    for slot in range(rng.randint(2, 4)):
        lines.append("CONST_%d = %d" % (slot, rng.randrange(1, 10 ** 6)))
    lines.append("")
    last_call = None
    while len(lines) < 30:  # 保底：块最大 17 行，30+17+4 恒 ≤ 80
        last_call = _block(rng.randint(0, 3), rng, index, len(lines))
        lines += last_call[0]
        last_call = last_call[1]
    while len(lines) < 70 and rng.random() < 0.5:  # 可选拉高：放得下才追加
        block, call = _block(rng.randint(0, 3), rng, index, len(lines))
        if len(lines) + len(block) + 5 > 80:
            break
        lines += block
        last_call = call
    if last_call.startswith("Sample"):
        lines += ["", "if __name__ == '__main__':",
                  "    print('fixture %06d ->', %s().apply(7))" % (index, last_call)]
    else:
        lines += ["", "if __name__ == '__main__':",
                  "    print('fixture %06d ->', %s(range(20), 50))" % (index, last_call)]
    return "\n".join(lines) + "\n"


def leaf_plan(small_files, top_dirs, sub_dirs):
    """把 small_files 摊到 top×sub 树：每叶 per_leaf 个，前 remainder 叶 +1。"""
    leaves = top_dirs * sub_dirs
    per_leaf, remainder = divmod(small_files, leaves)
    active = leaves if per_leaf > 0 else remainder
    return {"top_dirs": top_dirs, "sub_dirs": sub_dirs, "leaves_total": leaves,
            "files_per_leaf": per_leaf, "remainder_leaves": remainder,
            "leaves_with_files": active}


def estimate(files, seed, big_count, top_dirs, sub_dirs, rows_mode=False):
    """与实跑同一套内容函数逐个求字节（不写盘）：文件数/字节/inode 精确。

    rows_mode=True 时 marker+manifest 计入行数预算（rows 口径承诺"扫描根即得
    N 行"）；否则保持 --files 历史语义（marker/manifest 在预算外另 +2）。
    """
    big_specs = big_file_specs(big_count)
    reserved = 2 if rows_mode else 0
    small_total = files - big_count - reserved
    plan = leaf_plan(small_total, top_dirs, sub_dirs)
    layout = {"plan": plan, "small_total": small_total,
              "adds": max(1, small_total // 100), "sub_dirs": sub_dirs}
    total_bytes = sum(spec["bytes"] for spec in big_specs)
    small = plan["files_per_leaf"]
    index = 0
    for leaf in range(plan["leaves_with_files"]):
        count = small + (1 if leaf < plan["remainder_leaves"] else 0)
        for _ in range(count):
            total_bytes += len(module_source(index, seed, layout).encode("utf-8"))
            index += 1
    tops = len({leaf // sub_dirs for leaf in range(plan["leaves_with_files"])})
    # inode 精确账：内容文件 + (rows 口径已含 marker/manifest，files 口径另 +2)
    # + 叶目录 + 顶层目录 + 根目录 + big/ 目录（有大文件时）
    inodes = (files + (0 if rows_mode else 2)
              + plan["leaves_with_files"] + tops + 1 + (1 if big_count else 0))
    return {"files": files, "rows_mode": rows_mode, "small_files": small_total,
            "big_files": big_specs, "plan": plan, "layout": layout,
            "bytes": total_bytes, "inodes": inodes}


def generation_plan(small_total):
    """受控变更计划（确定性）：每轮新增 adds、删除 deletes（上一轮新增的前
    deletes 个）、修改 ~1% 基础小文件。adds > deletes 保证累计行数单调可分账。"""
    adds = max(1, small_total // 100)
    return {"adds": adds, "deletes": adds // 2}


def estimate_generations(base, generations, seed, gen_plan):
    """在 base 估算上追加 K 代口径：逐代行数（解析式）与最终盘上字节/inode。"""
    layout, small_total = base["layout"], base["small_files"]
    adds, deletes = gen_plan["adds"], gen_plan["deletes"]
    rows = [base["files"] + (0 if base["rows_mode"] else 2)]  # base 扫描实际行数
    bytes_total, inodes = base["bytes"], base["inodes"]
    for round_no in range(1, generations):  # 轮 1..K-1（每轮后扫描一次）
        added = sum(len(module_source(
            small_total + (round_no - 1) * adds + local, seed, layout).encode("utf-8"))
            for local in range(adds))
        changed = sum(len(change_line(round_no).encode("utf-8"))
                      for _ in modified_indices(round_no, small_total))
        removed = 0
        if round_no >= 2:
            removed = sum(len(module_source(
                small_total + (round_no - 2) * adds + local, seed, layout).encode("utf-8"))
                for local in range(deletes))
        bytes_total += added + changed - removed
        inodes += adds + 1 - (deletes if round_no >= 2 else 0)  # 新文件+gen目录-删除
        rows.append(rows[-1] + adds - (deletes if round_no >= 2 else 0))
    return {"rows_per_generation": rows, "rows_total": sum(rows),
            "bytes_final": bytes_total, "inodes_final": inodes}


def scan_generations(directory, layout, seed, generations, gen_plan):
    """base 扫描 + (K-1) 轮受控变更逐代扫描（history retention profile）。

    索引库在 <dir>/index/facts.sqlite（扫描经 extra_excluded 排除，不自索引）；
    逐代实际行数/耗时来自 scanner 的 generation 语义。返回逐代记录列表。
    """
    packages = Path(__file__).resolve().parents[2] / "packages"
    try:
        sys.path.insert(0, str(packages))
        from agents_kernel.indexing.scanner import IndexScanner
        from agents_kernel.storage import db
    except ImportError as exc:
        raise ValueError("--generations 需要仓库内 packages/agents_kernel（导入失败：%s）；"
                         "纯 fixture 生成请省略 --generations" % exc)
    adds, deletes = gen_plan["adds"], gen_plan["deletes"]
    small_total = layout["small_total"]
    index_dir = directory / INDEX_DIR_NAME
    index_dir.mkdir(exist_ok=True)
    store = db.Store(index_dir / "facts.sqlite")
    store.open()
    writer = db.acquire_writer(store)
    scanner = IndexScanner(store)
    records = []
    try:
        for generation in range(1, generations + 1):
            added = deleted = modified = 0
            if generation > 1:
                round_no = generation - 1
                for local in range(adds):
                    content = small_total + (round_no - 1) * adds + local
                    source = module_source(content, seed, layout)
                    compile(source, added_rel(round_no, local), "exec")  # 真实语法保证
                    target = directory / added_rel(round_no, local)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(source, encoding="utf-8")
                added = adds
                modified = len(modified_indices(round_no, small_total))
                for rel in (small_rel(pos, layout["plan"], layout["sub_dirs"])
                            for pos in modified_indices(round_no, small_total)):
                    with open(directory / rel, "a", encoding="utf-8") as handle:
                        handle.write(change_line(round_no))
                if round_no >= 2:  # 删除上一轮新增的前 deletes 个（本轮不删自己）
                    for local in range(deletes):
                        (directory / added_rel(round_no - 1, local)).unlink()
                    deleted = deletes
            started = time.perf_counter()
            result = scanner.scan(directory, writer, extra_excluded=(INDEX_DIR_NAME,))
            records.append({
                "generation": result.generation, "file_count": result.file_count,
                "added": added, "deleted": deleted, "modified": modified,
                "excluded": result.excluded_count, "batch_count": result.batch_count,
                "hash_reused": result.hash_reused, "hash_computed": result.hash_computed,
                "scan_seconds": round(time.perf_counter() - started, 3)})
    finally:
        writer.close()
        store.close()
    return records


def generate(directory, files, seed, big_count, top_dirs, sub_dirs, max_bytes,
             dry_run, rows_mode=False, generations=0):
    """dry_run=True 只估算打印；False 实跑（marker 先行，manifest 收口）。

    generations≥1 时先落含占位 bench 字段的 manifest（保证"扫描根即 N 行"的
    文件数预算在扫描期间即成立），扫描完成后用逐代实际数据回填重写。
    """
    result = estimate(files, seed, big_count, top_dirs, sub_dirs, rows_mode)
    gen_plan = generation_plan(result["small_files"])
    gen_ext = estimate_generations(result, generations, seed, gen_plan) if generations else None
    usage = shutil.disk_usage(tempfile.gettempdir())
    final_bytes = gen_ext["bytes_final"] if gen_ext else result["bytes"]
    final_inodes = gen_ext["inodes_final"] if gen_ext else result["inodes"]
    estimate_line = ("target=%s files=%d small=%d big=%d bytes=%d (%.1fMiB) "
                     "inodes=%d free=%d (%.1fGiB) limit=%d"
                     % (directory, result["files"], result["small_files"],
                        len(result["big_files"]), final_bytes,
                        final_bytes / 1048576, final_inodes,
                        usage.free, usage.free / 1024 ** 3, max_bytes))
    if gen_ext:
        estimate_line += (" rows_per_gen=%s rows_total=%d rows_mode=%s"
                          % (gen_ext["rows_per_generation"], gen_ext["rows_total"],
                             "1" if rows_mode else "0"))
    if final_bytes > max_bytes:
        print("DRY-RUN REJECT（超出 --max-bytes 限额）: " + estimate_line)
        return 3
    print(("DRY-RUN OK: " if dry_run else "ESTIMATE: ") + estimate_line)
    if dry_run:
        return 0
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("目标目录非空，拒绝覆盖：%s" % directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MARKER_NAME).write_text(json.dumps(
        {"marker": "BENCHMARK_FIXTURE",
         "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "generator": "tests/benchmarks/fixture_gen.py", "seed": seed},
        ensure_ascii=False) + "\n", encoding="utf-8")
    started = time.perf_counter()
    for spec in result["big_files"]:
        target = directory / spec["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        block, remaining = big_block(spec["index"]), spec["bytes"]
        with open(target, "wb") as handle:
            while remaining > 0:
                chunk = (block * (WRITE_CHUNK // len(block)))[:min(WRITE_CHUNK, remaining)]
                handle.write(chunk)
                remaining -= len(chunk)
    index = 0
    small = result["plan"]["files_per_leaf"]
    for leaf in range(result["plan"]["leaves_with_files"]):
        count = small + (1 if leaf < result["plan"]["remainder_leaves"] else 0)
        top, sub = divmod(leaf, result["plan"]["sub_dirs"])
        leaf_dir = directory / ("pkg_%02d" % top) / ("sub_%02d" % sub)
        leaf_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(count):
            source = module_source(index, seed, result["layout"])
            compile(source, "mod_%06d.py" % index, "exec")  # 真实语法保证
            (leaf_dir / ("mod_%06d.py" % index)).write_text(source, encoding="utf-8")
            index += 1
    generation_records = []
    final_files = result["files"]
    if gen_ext:  # 受控变更后的最终盘上内容文件数（rows_per_generation 末代即扫描行数）
        final_files = gen_ext["rows_per_generation"][-1] - (0 if rows_mode else 2)
    if generations:
        # 先写 manifest 占位（文件数预算在扫描期间即成立），扫描后回填实际逐代数据。
        _write_manifest(directory, result, files, final_files, seed, generations,
                        final_bytes, final_inodes, started, [], rows_mode)
        generation_records = scan_generations(directory, result["layout"], seed,
                                              generations, gen_plan)
    for spec in result["big_files"]:
        actual = (directory / spec["path"]).stat().st_size
        if actual != spec["bytes"]:
            raise OSError("大文件字节数不符：%s %d != %d" % (spec["path"], actual, spec["bytes"]))
    _write_manifest(directory, result, files, final_files, seed, generations,
                    final_bytes, final_inodes, started, generation_records, rows_mode)
    print("GENERATED: %s (manifest.json + %s)" % (directory, MARKER_NAME))
    return 0


def _write_manifest(directory, result, requested, final_files, seed, generations,
                    total_bytes, total_inodes, started, generation_records, rows_mode):
    """manifest.json：物理终态 + bench 字段（逐代实际行数与两套口径分列）。

    count_total_rows = Σ 逐代 file_count（多代累计，工作量口径）；
    count_distinct_current_entries = 当前代单快照行数；二者不等时不得互相冒充。
    total_files = 受控变更后的最终盘上内容文件数（base 记入 base_files）。
    """
    bench = None
    if generation_records:
        bench = {"generations_requested": generations,
                 "generation_count": len(generation_records),
                 "per_generation": generation_records,
                 "count_total_rows": sum(row["file_count"]
                                         for row in generation_records),
                 "count_distinct_current_entries":
                     generation_records[-1]["file_count"],
                 "count_total_rows_caliber":
                     "多代累计（Σ scan_generations.file_count，工作量口径，"
                     "≠ 当前代 distinct 条目数）",
                 "index_path": INDEX_DB_REL}
    manifest = {"marker": "BENCHMARK_FIXTURE",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "seed": seed,
                "requested_files": None if rows_mode else requested,
                "requested_rows": requested if rows_mode else None,
                "counting": "rows" if rows_mode else "files",
                "base_files": result["files"],
                "generations": generations,
                "layout": result["plan"], "small_files": result["small_files"],
                "big_files": result["big_files"], "total_files": final_files,
                "total_bytes": total_bytes, "inodes_estimate": total_inodes,
                "python": platform.python_version(), "platform": sys.platform,
                "generation_seconds": round(time.perf_counter() - started, 3)}
    if bench:
        manifest["bench"] = bench
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="确定性基准 fixture 生成器（先 dry-run 后实跑）")
    parser.add_argument("directory", help="目标目录：必须位于临时目录下且目录名以 %s 开头" % MARKER_TAG)
    parser.add_argument("--files", type=int, default=None,
                        help="physical files profile：内容文件总数（marker/manifest 不计入，与 --rows 二选一）")
    parser.add_argument("--rows", type=int, default=None,
                        help="current-distinct profile：当前代索引行数（含 marker/manifest 各 1 的确定性映射，与 --files 二选一）")
    parser.add_argument("--generations", type=int, default=0,
                        help="history retention profile：扫描代数。0=纯 fixture 不建索引；1=base 扫描"
                             "（current 口径）；K≥2=每轮受控变更后逐代扫描（累计行数为多代工作量口径）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--big-files", type=int, default=None,
                        help="大文件个数（缺省：规模≥10000 时 %d 个，否则 0）" % DEFAULT_BIG_FILES)
    parser.add_argument("--top-dirs", type=int, default=DEFAULT_TOP_DIRS)
    parser.add_argument("--sub-dirs", type=int, default=DEFAULT_SUB_DIRS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                        help="实际写入字节限额（默认 2GiB，09 §5 自动小档上限）")
    parser.add_argument("--dry-run", action="store_true", help="只估算打印，不写任何文件")
    args = parser.parse_args(argv)
    if (args.files is None) == (args.rows is None):
        parser.error("--files 与 --rows 必须恰好给出一个")
    rows_mode = args.rows is not None
    files = args.rows if rows_mode else args.files
    if files < 1:
        parser.error("规模必须为正整数")
    big_count = args.big_files
    if big_count is None:
        big_count = DEFAULT_BIG_FILES if files >= 10000 else 0
    if not 0 <= big_count < files:
        parser.error("--big-files 必须小于规模且非负")
    reserved = 2 if rows_mode else 0  # rows 口径：marker+manifest 计入行数预算
    if files - big_count - reserved < 1:
        parser.error("扣除大文件与 marker/manifest 预算后至少需要 1 个内容文件")
    if args.generations < 0:
        parser.error("--generations 不能为负")
    try:
        directory = validate_target(args.directory)
    except ValueError as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        return 2
    try:
        return generate(directory, files, args.seed, big_count,
                        args.top_dirs, args.sub_dirs, args.max_bytes, args.dry_run,
                        rows_mode=rows_mode, generations=args.generations)
    except (OSError, ValueError) as exc:
        print("FAILED: %s" % exc, file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""确定性基准 fixture 生成器（L 档容量验收用；stdlib only, Py3.9+）。

09_TEST_AND_BENCHMARK_ACCEPTANCE.md §5 授权边界：
- 产物只写入 tempfile.gettempdir() 之下、目录名以 benchmark-fixture 开头的显式
  标识目录；根目录写 BENCHMARK_FIXTURE marker + manifest.json（数量/字节/树参数/
  生成时间），删除方只能认 marker+manifest，不得碰其它目录。
- 必须先 --dry-run 估算（文件数、实际内容字节、inode、磁盘余量），限额 --max-bytes
  默认 2GiB（自动小档上限），超限拒绝实跑。
- 确定性：同一 --seed 与 --files 生成逐字节相同的树（内容由纯函数 f(index, seed)
  决定，与生成顺序无关）；每个模块写盘前 compile() 校验真实语法。

用法：
    python3 fixture_gen.py <dir> --files 100000 --dry-run
    python3 fixture_gen.py <dir> --files 100000
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
DEFAULT_BIG_FILES = 10                       # 仅当 files ≥ 10000 时生成（占少数）
WRITE_CHUNK = 1024 * 1024

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


def module_source(index, seed):
    """第 index 个小型 Python 模块的完整源码（30–80 行真实语法，纯函数确定性）。"""
    rng = random.Random((seed * 1000003 + index) % (1 << 48))
    lines = ["# benchmark fixture module %06d (seed=%d, deterministic)" % (index, seed),
             "", rng.choice(_IMPORTS), rng.choice(_IMPORTS), ""]
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


def estimate(files, seed, big_count, top_dirs, sub_dirs):
    """与实跑同一套内容函数逐个求字节（不写盘）：文件数/字节/inode 精确。"""
    big_specs = big_file_specs(big_count)
    plan = leaf_plan(files - len(big_specs), top_dirs, sub_dirs)
    total_bytes = sum(spec["bytes"] for spec in big_specs)
    small = plan["files_per_leaf"]
    index = 0
    for leaf in range(plan["leaves_with_files"]):
        count = small + (1 if leaf < plan["remainder_leaves"] else 0)
        for _ in range(count):
            total_bytes += len(module_source(index, seed).encode("utf-8"))
            index += 1
    tops = len({leaf // sub_dirs for leaf in range(plan["leaves_with_files"])})
    inodes = files + len(big_specs) + plan["leaves_with_files"] + tops + 2  # 文件+叶目录+顶层目录+marker+manifest
    return {"files": files, "small_files": index, "big_files": big_specs,
            "plan": plan, "bytes": total_bytes, "inodes": inodes}


def generate(directory, files, seed, big_count, top_dirs, sub_dirs, max_bytes, dry_run):
    """dry_run=True 只估算打印；False 实跑（marker 先行，manifest 收口）。"""
    result = estimate(files, seed, big_count, top_dirs, sub_dirs)
    usage = shutil.disk_usage(tempfile.gettempdir())
    estimate_line = ("target=%s files=%d small=%d big=%d bytes=%d (%.1fMiB) "
                     "inodes=%d free=%d (%.1fGiB) limit=%d"
                     % (directory, result["files"], result["small_files"],
                        len(result["big_files"]), result["bytes"],
                        result["bytes"] / 1048576, result["inodes"],
                        usage.free, usage.free / 1024 ** 3, max_bytes))
    if result["bytes"] > max_bytes:
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
            source = module_source(index, seed)
            compile(source, "mod_%06d.py" % index, "exec")  # 真实语法保证
            (leaf_dir / ("mod_%06d.py" % index)).write_text(source, encoding="utf-8")
            index += 1
    for spec in result["big_files"]:
        actual = (directory / spec["path"]).stat().st_size
        if actual != spec["bytes"]:
            raise OSError("大文件字节数不符：%s %d != %d" % (spec["path"], actual, spec["bytes"]))
    manifest = {"marker": "BENCHMARK_FIXTURE",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "seed": seed, "requested_files": files,
                "layout": result["plan"], "small_files": result["small_files"],
                "big_files": result["big_files"], "total_files": result["files"],
                "total_bytes": result["bytes"], "inodes_estimate": result["inodes"],
                "python": platform.python_version(), "platform": sys.platform,
                "generation_seconds": round(time.perf_counter() - started, 3)}
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("GENERATED: %s (manifest.json + %s)" % (directory, MARKER_NAME))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="确定性基准 fixture 生成器（先 dry-run 后实跑）")
    parser.add_argument("directory", help="目标目录：必须位于临时目录下且目录名以 %s 开头" % MARKER_TAG)
    parser.add_argument("--files", type=int, required=True, help="文件总数（含大文件）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--big-files", type=int, default=None,
                        help="大文件个数（缺省：files≥10000 时 %d 个，否则 0）" % DEFAULT_BIG_FILES)
    parser.add_argument("--top-dirs", type=int, default=DEFAULT_TOP_DIRS)
    parser.add_argument("--sub-dirs", type=int, default=DEFAULT_SUB_DIRS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                        help="实际写入字节限额（默认 2GiB，09 §5 自动小档上限）")
    parser.add_argument("--dry-run", action="store_true", help="只估算打印，不写任何文件")
    args = parser.parse_args(argv)
    if args.files < 1:
        parser.error("--files 必须为正整数")
    big_count = args.big_files
    if big_count is None:
        big_count = DEFAULT_BIG_FILES if args.files >= 10000 else 0
    if not 0 <= big_count < args.files:
        parser.error("--big-files 必须小于 --files 且非负")
    try:
        directory = validate_target(args.directory)
    except ValueError as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        return 2
    try:
        return generate(directory, args.files, args.seed, big_count,
                        args.top_dirs, args.sub_dirs, args.max_bytes, args.dry_run)
    except (OSError, ValueError) as exc:
        print("FAILED: %s" % exc, file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""XL/XXL 档基准执行共享 harness（test_xl_tier / test_xxl_tier 复用）。

职责（R07；所有规模都经 fixture_gen.py CLI 同一执行路径，不造第二生成器）：
- 门与规模解析：L_TIER=1 环境门（未设即 skip，skip 不算 PASS）；小样本保守档
  默认 ≤1000 行 / 64MiB 写入上限（09 §5 自动小档边界内）真实执行；大档
  （>1000 行）必须显式 <PREFIX>_AUTH=1 且给出 <PREFIX>_MAX_BYTES，否则按
  NOT_RUN_AUTH / NOT_RUN_ENV skip，绝不静默执行大档。
- fixture_gen 子进程调用（先 --dry-run 后实跑）与 manifest/bench 字段读取。
- 分片构建（ShardPlanner/ShardWriter，逐模块一片）+ 逐片建边（edges）+
  独立真值 oracle（逐片直读 files 排序合并，与 MergedPageReader 全集对比）。
- 09 §2 容量字段 JSON 组装：entries/regular_files/included_bytes/edges/tasks/
  events + count_total_rows/count_distinct_current_entries/generation_count；
  本基准未采集的字段如实写 NOT_MEASURED，不虚构；大档目标未授权执行写
  NOT_RUN_AUTH。
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGES = REPO_ROOT / "packages"
FIXTURE_GEN = Path(__file__).resolve().parent / "fixture_gen.py"
sys.path.insert(0, str(PACKAGES))

from agents_kernel.indexing import edges as edges_module
from agents_kernel.indexing.modules import ModuleMapper
from agents_kernel.indexing.sharding import ShardPlanner, ShardWriter
from agents_kernel.storage import db

L_TIER = os.environ.get("L_TIER") == "1"
L_TIER_SKIP = ("NOT_RUN：未设置 L_TIER=1 —— XL/XXL 档基准不进常规 discover；"
               "手动运行 L_TIER=1 python3 -m unittest tests.benchmarks.test_xl_tier"
               " [或 test_xxl_tier] -v")
SMALL_ROWS_MAX = 1000                    # 本轮小样本保守档上界（09 §2 单元小样本 100–1000）
SMALL_MAX_BYTES = 64 * 1024 * 1024       # 小样本写入上限（远低于 09 §5 的 2GiB）
NOT_MEASURED = "NOT_MEASURED"
SEED = 20260920


def resolve_scale(prefix, default_rows, default_generations,
                  target_rows, target_generations):
    """环境驱动规模解析；返回带 skip_reason（None=可跑）的参数字典。

    - 缺省 = 小样本保守档（≤1000 行、64MiB、4×4 目录），L_TIER=1 即可真实执行。
    - rows>1000 需 <PREFIX>_AUTH=1（资源授权）+ <PREFIX>_MAX_BYTES（写入限额），
      缺任一分别 NOT_RUN_AUTH / NOT_RUN_ENV，不冒进。
    """
    rows = int(os.environ.get(prefix + "_ROWS", str(default_rows)))
    generations = int(os.environ.get(prefix + "_GENERATIONS",
                                     str(default_generations)))
    top_dirs = int(os.environ.get(prefix + "_TOP_DIRS",
                                  "4" if rows <= SMALL_ROWS_MAX else "100"))
    sub_dirs = int(os.environ.get(prefix + "_SUB_DIRS",
                                  "4" if rows <= SMALL_ROWS_MAX else "100"))
    scale = {"rows": rows, "generations": generations, "top_dirs": top_dirs,
             "sub_dirs": sub_dirs, "target_rows": target_rows,
             "target_generations": target_generations, "skip_reason": None,
             "note": ("small-conservative(≤%d 行/64MiB，小样本不外推为容量认证)"
                      % SMALL_ROWS_MAX) if rows <= SMALL_ROWS_MAX
                      else "authorized-large"}
    if rows > SMALL_ROWS_MAX:
        if os.environ.get(prefix + "_AUTH") != "1":
            scale["skip_reason"] = (
                "NOT_RUN_AUTH：请求规模 %d 行 > %d，未获资源授权（需 %s_AUTH=1 且"
                " %s_MAX_BYTES=<字节>）；大档参数保留未执行"
                % (rows, SMALL_ROWS_MAX, prefix, prefix))
            return scale
        max_bytes = os.environ.get(prefix + "_MAX_BYTES")
        if not max_bytes or int(max_bytes) <= 0:
            scale["skip_reason"] = ("NOT_RUN_ENV：已授权大档但缺 %s_MAX_BYTES 写入"
                                    "限额（09 §5 要求执行前显式限额）" % prefix)
            return scale
        scale["max_bytes"] = int(max_bytes)
    else:
        scale["max_bytes"] = SMALL_MAX_BYTES
    return scale


def gated(scale):
    """环境门：未设 L_TIER=1 → NOT_RUN skip；规模未授权 → NOT_RUN_AUTH skip。"""
    if not L_TIER:
        raise unittest.SkipTest(L_TIER_SKIP)
    if scale["skip_reason"]:
        print(scale["skip_reason"], file=sys.stderr)
        raise unittest.SkipTest(scale["skip_reason"])


def percentile(samples, quantile):
    """最近位次法分位数（小样本 30 次，与 test_l_tier 同口径）。"""
    ordered = sorted(samples)
    rank = max(1, -(-int(quantile * 100) * len(ordered) // 100))
    return ordered[rank - 1]


def emit(prefix, step, payload, records):
    """原始结果账本：stdout 打 <PREFIX>-TIER-RESULT 行（验收文档直接引用）。"""
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    print("\n%s-TIER-RESULT %s %s" % (prefix, step, line))
    records[step] = payload


class TierRunner:
    """一个档位的进程内单例：fixture（current/history 两套）+ 分片 + 结果账本。"""

    def __init__(self, name, scale):
        self.name = name
        self.prefix = name.upper()
        self.scale = scale
        self.records = {}
        self.current_dir = Path(tempfile.gettempdir()) / (
            "benchmark-fixture-%s-%d" % (name, os.getpid()))
        self.history_dir = Path(tempfile.gettempdir()) / (
            "benchmark-fixture-%s-hist-%d" % (name, os.getpid()))
        self.scratch = Path(tempfile.mkdtemp(prefix="%s-tier-shards-" % name))
        self.current = None      # {"manifest", "store_path", "dry_run", "generate"}
        self.history = None
        self.shards = None       # {"dir", "plan", "result", "edges", "graph_report"}
        atexit.register(self.cleanup)

    # ---- fixture（09 §5 流程：先 dry-run 估算后实跑，产物带 marker+manifest） ----

    def _gen(self, directory, rows, generations, extra=()):
        args = [sys.executable, str(FIXTURE_GEN), str(directory),
                "--rows", str(rows), "--generations", str(generations),
                "--top-dirs", str(self.scale["top_dirs"]),
                "--sub-dirs", str(self.scale["sub_dirs"]),
                "--big-files", "0", "--seed", str(SEED),
                "--max-bytes", str(self.scale["max_bytes"])] + list(extra)
        done = subprocess.run(args, capture_output=True, text=True, timeout=3600)
        if done.returncode != 0:
            raise AssertionError("fixture_gen 失败 rc=%s：%s%s"
                                 % (done.returncode, done.stdout, done.stderr))
        return done.stdout.strip().splitlines()[-1]

    def _build(self, slot, directory, generations):
        dry_run = self._gen(directory, self.scale["rows"], generations, ("--dry-run",))
        generate = self._gen(directory, self.scale["rows"], generations)
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        info = {"dir": directory, "manifest": manifest, "dry_run": dry_run,
                "generate": generate,
                "store_path": directory / "index" / "facts.sqlite"}
        setattr(self, slot, info)
        return info

    def ensure_current(self):
        """current-generation distinct profile：--rows N --generations 1（单代快照）。"""
        if self.current is None:
            self.current = self._build("current", self.current_dir, 1)
        return self.current

    def ensure_history(self):
        """history retention profile：--rows N --generations K（受控变更逐代扫描）。"""
        if self.history is None:
            self.history = self._build("history", self.history_dir,
                                       self.scale["generations"])
        return self.history

    def open_store(self, info):
        store = db.Store(info["store_path"])
        store.open()
        return store

    def regular_files(self, info):
        """盘上全部普通文件（含 marker/manifest，不含 index 库）——rows 口径对应物。"""
        return [path for path in info["dir"].rglob("*") if path.is_file()
                and "index" not in path.relative_to(info["dir"]).parts]

    def content_files(self, info):
        """盘上内容文件（不含 marker/manifest/index 库）——字节负载口径。"""
        return [path for path in self.regular_files(info)
                if path.name not in ("BENCHMARK_FIXTURE", "manifest.json")]

    # ---- 分片 + 建边（同一棵树，复用既有 sharding/edges 路径） ----

    def ensure_shards(self, info=None):
        """对 fixture 树按模块分片（每模块一片）并逐片建边；MergedPageReader 用。"""
        if self.shards is not None:
            return self.shards
        info = info or self.ensure_current()
        mapper = ModuleMapper()
        counts = {}
        store = self.open_store(info)
        try:
            for row in store.query_all("SELECT path FROM files"):
                module_id = mapper.module_of(row["path"])
                counts[module_id] = counts.get(module_id, 0) + 1
        finally:
            store.close()
        shard_dir = self.scratch / "shards"
        plan = ShardPlanner(max_modules=1).plan(counts)
        result = ShardWriter().write_shards(info["dir"], plan, shard_dir,
                                            extra_excluded=("index",))
        edge_totals = {"internal": 0, "unknown": 0, "parsed": 0}
        for shard_id, generation in sorted(result.generations.items()):
            store = db.Store(shard_dir / (shard_id + ".sqlite"))
            store.open()
            try:
                writer = db.acquire_writer(store)
                try:
                    built = edges_module.build_edges(store, writer, generation)
                finally:
                    writer.close()
                edge_totals["internal"] += built.internal_edges
                edge_totals["unknown"] += built.unknown_edges
                edge_totals["parsed"] += built.parsed_files
            finally:
                store.close()
        self.shards = {"dir": shard_dir, "plan": plan, "result": result,
                       "counts": counts, "edges": edge_totals}
        return self.shards

    # ---- 清理（09 §5：只删 marker+manifest 一致的本目录） ----

    def cleanup(self):
        for info in (self.current, self.history):
            if info is None:
                continue
            directory = Path(info["dir"])
            marker, manifest = directory / "BENCHMARK_FIXTURE", directory / "manifest.json"
            if marker.is_file() and manifest.is_file():
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except ValueError:
                    data = {}
                if (data.get("marker") == "BENCHMARK_FIXTURE"
                        and directory.name.startswith("benchmark-fixture-")):
                    shutil.rmtree(directory, ignore_errors=True)
        shutil.rmtree(self.scratch, ignore_errors=True)


def shard_oracle(shard_dir):
    """独立真值：逐片直读 files 的 (shard_id, path) 排序全集（无重无漏的对照）。"""
    registry = json.loads((Path(shard_dir) / "shards.json").read_text(encoding="utf-8"))
    expected = []
    for row in sorted(registry["shards"], key=lambda item: item["shard_id"]):
        store = db.Store(Path(shard_dir) / row["db"])
        store.open()
        try:
            expected.extend((row["shard_id"], item["path"]) for item in
                            store.query_all("SELECT path FROM files ORDER BY path"))
        finally:
            store.close()
    return sorted(expected)


def capacity_document(runner, *, entries, regular_files, included_bytes, edges,
                      count_total_rows, count_distinct_current_entries,
                      generation_count, elapsed_seconds=None, extra=None):
    """09 §2 容量字段 JSON：每档分列；未采集字段如实 NOT_MEASURED；大档 NOT_RUN_AUTH。"""
    scale = runner.scale
    document = {
        "tier": runner.name.upper(),
        "scale_note": scale["note"],
        # 09 §2 分项（tasks/events 本基准未采集，不虚构）
        "entries": entries,
        "regular_files": regular_files,
        "included_bytes": included_bytes,
        "edges": edges,
        "tasks": NOT_MEASURED,
        "events": NOT_MEASURED,
        # R07 要求的三口径分列
        "count_total_rows": count_total_rows,
        "count_total_rows_caliber": "多代累计（工作量口径，≠ 当前代 distinct 条目数）",
        "count_distinct_current_entries": count_distinct_current_entries,
        "generation_count": generation_count,
        "elapsed_seconds": elapsed_seconds,
        # 大档目标：参数已定义、未授权执行
        "targets_not_executed": {
            "rows": scale["target_rows"],
            "generations": scale["target_generations"],
            "status": "NOT_RUN_AUTH",
            "reason": "未获资源预算授权（09 §5）；估算见登记文档外推表"},
    }
    if extra:
        document.update(extra)
    return document

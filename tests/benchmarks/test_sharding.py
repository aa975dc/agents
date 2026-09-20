# -*- coding: utf-8 -*-
"""XL 分片索引小样本测试（C15 前半/C18 尾/SC02 实现半/IX07/IX08）。

小 fixture（3 模块 500 文件真实 tempfile 树，≤2000 文件），**不设 L_TIER 门、
常规 discover 可跑**（与 test_l_tier 的长跑基准区分；XL 百万级物理压测归
P6-05，NOT_RUN）。覆盖：

- ShardPlanner：确定性分桶、_root/超限模块强制独占、非法输入拒绝（IX07）。
- ShardWriter：分片行数合计=全量、只含本片模块、计划外模块（漏片）与行数
  不符（重片）整批拒绝。
- 完整性：单分片独立查询闭合（IX08 精神：不开其他分片库）、删一片/多一片
  即拒绝、根 manifest（generation vector）落盘。
- CrossShardGraph：跨片边 from_shard/to_shard 标注、分片级环检测（模拟
  alpha→beta→gamma→alpha 跨片循环依赖）、单分片方案无跨片环。
- generation vector：整体重扫 ahead、两副本各推进一片 diverged、全等。
"""
import shutil
import sys
import tempfile
import unittest
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))

from agents_kernel.indexing import edges as edges_module
from agents_kernel.indexing import sharding
from agents_kernel.indexing.sharding import (
    CrossShardGraph, ShardPlanner, ShardWriter, compare_vectors)
from agents_kernel.storage import db
from agents_kernel.validation import CompanionError

MODULE_FILES = 160          # alpha/beta/gamma 各 160 个 .py
ROOT_FILES = 20             # 顶层散文件 → _root 模块
TOTAL_FILES = MODULE_FILES * 3 + ROOT_FILES  # 500 ≤ 2000（小 fixture 约束）


def _build_tree(directory):
    """确定性小树：alpha→beta→gamma→alpha 的 import 链（构造跨片循环依赖）。

    跨片 import 在本片解析不到（IX08 单片自洽），按 unknown 落库、由全局层二次
    解析标注；"import json" 是项目外名字，全局层也不得误标（to_shard 恒 None）。
    """
    imports = {"alpha": "beta", "beta": "gamma", "gamma": "alpha"}
    for module, target in imports.items():
        for index in range(MODULE_FILES):
            path = directory / module / ("mod_%03d.py" % index)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "import json\nimport %s.mod_%03d\n\n\ndef value_%03d():\n    return %d\n"
                % (target, index, index, index), encoding="utf-8")
    for index in range(ROOT_FILES):
        (directory / ("note_%02d.txt" % index)).write_text(
            "root file %d\n" % index, encoding="utf-8")


def _counts():
    return {"alpha": MODULE_FILES, "beta": MODULE_FILES, "gamma": MODULE_FILES,
            "_root": ROOT_FILES}


def _plan(**options):
    return ShardPlanner(**options).plan(_counts())


def _write(tree, shard_dir, plan, **kwargs):
    return ShardWriter().write_shards(tree, plan, shard_dir, **kwargs)


def _build_edges_all(shard_dir, result):
    """对每个分片独立建边（复用既有 edges 模块，验证可逐片执行）。"""
    for shard_id, generation in sorted(result.generations.items()):
        store = db.Store(Path(shard_dir) / (shard_id + ".sqlite"))
        store.open()
        try:
            writer = db.acquire_writer(store)
            try:
                edges_module.build_edges(store, writer, generation)
            finally:
                writer.close()
        finally:
            store.close()


class _TreeCase(unittest.TestCase):
    """共享小树 fixture（真实 tempfile，每类一套，类结束清理）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sharding-test-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.tree = cls.tmp / "tree"
        _build_tree(cls.tree)


class PlannerTest(unittest.TestCase):
    """IX07：分桶规则与拒绝语义（纯函数，无文件系统）。"""

    def test_plan_buckets_deterministic_and_totals(self):
        plan = _plan(max_modules=2, max_files=350)
        self.assertEqual([spec.shard_id for spec in plan.shards],
                         ["shard-0000", "shard-0001", "shard-0002"])
        self.assertEqual(plan.shards[0].modules, ("_root",))
        self.assertEqual(plan.shards[0].expected_files, ROOT_FILES)
        self.assertEqual(plan.shards[1].modules, ("alpha", "beta"))
        self.assertEqual(plan.shards[1].expected_files, MODULE_FILES * 2)
        self.assertEqual(plan.shards[2].modules, ("gamma",))
        self.assertEqual(plan.expected_total(), TOTAL_FILES)
        self.assertEqual(_plan(max_modules=2, max_files=350), plan)  # 确定性

    def test_root_and_oversize_modules_dedicated(self):
        planner = ShardPlanner(max_modules=8, max_files=100)
        plan = planner.plan({"_root": 5, "big": 999, "a": 10, "b": 10})
        dedicated = {spec.modules: spec.shard_id for spec in plan.shards}
        self.assertIn(("_root",), dedicated)
        self.assertIn(("big",), dedicated)          # 单模块超限：无法共片
        self.assertIn(("a", "b"), dedicated)        # 其余正常合片
        self.assertEqual(len(plan.shards), 3)

    def test_shard_of_unknown_rejected(self):
        plan = _plan(max_modules=2, max_files=350)
        with self.assertRaises(CompanionError):
            plan.shard_of("delta")

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(CompanionError):
            ShardPlanner(max_modules=0)
        with self.assertRaises(CompanionError):
            ShardPlanner(max_files=True)
        with self.assertRaises(CompanionError):
            ShardPlanner().plan({"bad/id": 3})
        with self.assertRaises(CompanionError):
            ShardPlanner().plan({"alpha": -1})
        with self.assertRaises(CompanionError):
            ShardPlanner().plan({"alpha": 1.5})


class WriteAndIntegrityTest(_TreeCase):
    """分片写入、行数合计=全量、漏片/重片拒绝、单分片独立查询。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.plan = _plan(max_modules=2, max_files=350)
        cls.shard_dir = cls.tmp / "shards"
        cls.result = _write(cls.tree, cls.shard_dir, cls.plan)

    def test_shard_row_counts_sum_to_full(self):
        self.assertEqual(self.result.file_count, TOTAL_FILES)
        self.assertEqual(self.result.shard_count, 3)
        total = 0
        for spec in self.plan.shards:
            store = db.Store(self.shard_dir / (spec.shard_id + ".sqlite"))
            store.open()
            try:
                rows = store.query_all("SELECT path FROM files")
                self.assertEqual(len(rows), spec.expected_files)
                total += len(rows)
            finally:
                store.close()
        self.assertEqual(total, TOTAL_FILES)

    def test_shards_only_contain_own_modules(self):
        store = db.Store(self.shard_dir / "shard-0001.sqlite")
        store.open()
        try:
            paths = [row["path"] for row in store.query_all("SELECT path FROM files")]
        finally:
            store.close()
        self.assertTrue(paths)
        for path in paths:
            self.assertTrue(path.startswith(("alpha/", "beta/")), path)

    def test_single_shard_query_is_closed(self):
        # IX08 精神：只打开一个分片库即可完成本片查询，不触及其他分片
        store = db.Store(self.shard_dir / "shard-0002.sqlite")
        store.open()
        try:
            self.assertEqual(store.query_one(
                "SELECT COUNT(*) AS c FROM files WHERE path LIKE 'gamma/%'")["c"],
                MODULE_FILES)
            self.assertIsNotNone(store.query_one(
                "SELECT 1 AS ok FROM scan_generations WHERE status = 'complete'"))
        finally:
            store.close()

    def test_verify_integrity_passes(self):
        graph = CrossShardGraph.load(self.shard_dir)
        try:
            report = graph.verify_integrity()
            self.assertEqual((report.shard_count, report.file_count,
                              report.expected_files), (3, TOTAL_FILES, TOTAL_FILES))
        finally:
            graph.close()

    def test_missing_shard_db_rejected(self):
        victim = self.shard_dir / "shard-0002.sqlite"
        backup = self.tmp / "shard-0002.sqlite.bak"
        shutil.move(str(victim), str(backup))
        try:
            graph = CrossShardGraph.load(self.shard_dir)
            try:
                with self.assertRaises(CompanionError) as caught:
                    graph.verify_integrity()
                self.assertIn("漏片", str(caught.exception))
            finally:
                graph.close()
        finally:
            shutil.move(str(backup), str(victim))

    def test_extra_shard_db_rejected(self):
        extra = self.shard_dir / "shard-9999.sqlite"
        extra.write_bytes(b"")
        try:
            graph = CrossShardGraph.load(self.shard_dir)
            try:
                with self.assertRaises(CompanionError) as caught:
                    graph.verify_integrity()
                self.assertIn("清单外", str(caught.exception))
            finally:
                graph.close()
        finally:
            extra.unlink()

    def test_orphan_module_write_rejected(self):
        narrow = ShardPlanner(max_modules=2, max_files=350).plan({"alpha": MODULE_FILES})
        scratch = self.tmp / "shards-orphan"
        with self.assertRaises(CompanionError) as caught:
            _write(self.tree, scratch, narrow)
        self.assertIn("漏片", str(caught.exception))
        self.assertIn("beta", str(caught.exception))
        self.assertFalse((scratch / sharding.REGISTRY_NAME).exists())

    def test_expected_count_mismatch_rejected(self):
        wrong = ShardPlanner(max_modules=4, max_files=100).plan(
            {"alpha": 1, "beta": 1, "gamma": 1, "_root": 1})
        scratch = self.tmp / "shards-mismatch"
        with self.assertRaises(CompanionError) as caught:
            _write(self.tree, scratch, wrong)
        self.assertIn("行数核对失败", str(caught.exception))

    def test_registry_vector_recorded(self):
        self.assertEqual(self.result.generations,
                         {"shard-0000": 1, "shard-0001": 1, "shard-0002": 1})
        document = json.loads(
            (self.shard_dir / sharding.REGISTRY_NAME).read_text(encoding="utf-8"))
        self.assertEqual(document["generation_vector"], self.result.generations)
        self.assertEqual(document["expected_total_files"], TOTAL_FILES)


class EdgesAndGraphTest(_TreeCase):
    """跨片边标注与分片级环检测（alpha→beta→gamma→alpha 跨片循环）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.plan = _plan(max_modules=2, max_files=350)  # [_root] [alpha,beta] [gamma]
        cls.shard_dir = cls.tmp / "shards"
        cls.result = _write(cls.tree, cls.shard_dir, cls.plan)
        _build_edges_all(cls.shard_dir, cls.result)
        cls.graph = CrossShardGraph.load(cls.shard_dir)

    @classmethod
    def tearDownClass(cls):
        cls.graph.close()
        super().tearDownClass()

    def test_cross_shard_edges_labeled(self):
        # 跨片 import 在本片落为 unknown（单片自洽），全局层二次解析后标注 shard
        cross = [edge for edge in self.graph.iter_edges()
                 if edge.to_shard is not None and edge.to_shard != edge.from_shard]
        beta_gamma = [edge for edge in cross if edge.from_module == "beta"]
        gamma_alpha = [edge for edge in cross if edge.from_module == "gamma"]
        self.assertEqual(len(beta_gamma), MODULE_FILES)
        self.assertEqual(len(gamma_alpha), MODULE_FILES)
        for edge in beta_gamma:   # beta 在 shard-0001，gamma 在 shard-0002
            self.assertEqual((edge.from_shard, edge.to_shard),
                             ("shard-0001", "shard-0002"))
            self.assertEqual(edge.kind, "unknown")
            self.assertTrue(edge.to_module_or_unknown.startswith("gamma."),
                            edge.to_module_or_unknown)
        for edge in gamma_alpha:
            self.assertEqual((edge.from_shard, edge.to_shard),
                             ("shard-0002", "shard-0001"))

    def test_intra_shard_edges_share_shard(self):
        # alpha 与 beta 同片：片内已解析为 internal 边，不进跨片图
        alpha_beta = [edge for edge in self.graph.iter_edges()
                      if edge.from_module == "alpha" and edge.kind == "internal"]
        self.assertEqual(len(alpha_beta), MODULE_FILES)
        for edge in alpha_beta:
            self.assertEqual(edge.from_shard, edge.to_shard)

    def test_shard_cycle_detected(self):
        outcome = self.graph.find_shard_cycles()
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.missing_edges, ())
        self.assertEqual(outcome.cycles, (("shard-0001", "shard-0002"),))
        graph = self.graph.shard_graph()
        self.assertEqual(graph["shard-0000"], {})
        self.assertEqual(graph["shard-0001"], {"shard-0002": MODULE_FILES})
        self.assertEqual(graph["shard-0002"], {"shard-0001": MODULE_FILES})

    def test_unknown_edge_resolution_boundary(self):
        """全局二次解析的边界：项目内名字命中分片，项目外（json）恒不标注。"""
        json_edges = [edge for edge in self.graph.iter_edges()
                      if edge.to_module_or_unknown == "json"]
        self.assertEqual(len(json_edges), MODULE_FILES * 3)
        for edge in json_edges:
            self.assertEqual(edge.kind, "unknown")
            self.assertIsNone(edge.to_shard)

    def test_single_shard_plan_no_cross_cycles(self):
        single = _plan(max_modules=8, max_files=1000)  # alpha/beta/gamma 同片
        self.assertEqual(len(single.shards), 2)        # _root 仍独占一片
        shard_dir = self.tmp / "shards-single"
        result = _write(self.tree, shard_dir, single)
        _build_edges_all(shard_dir, result)
        graph = CrossShardGraph.load(shard_dir)
        try:
            outcome = graph.find_shard_cycles()
            self.assertTrue(outcome.complete)
            self.assertEqual(outcome.cycles, ())       # 无跨片边 → 分片级 DAG
            self.assertEqual([edge for edge in graph.iter_edges()
                              if edge.to_shard is not None
                              and edge.from_shard != edge.to_shard], [])
            report = graph.verify_integrity()
            self.assertEqual(report.file_count, TOTAL_FILES)
        finally:
            graph.close()


class GenerationVectorTest(_TreeCase):
    """generation vector：全等/领先/落后/分叉；重扫推进与副本分叉。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.plan = _plan(max_modules=2, max_files=350)

    def test_compare_vectors_units(self):
        v1 = {"a": 1, "b": 1}
        self.assertEqual(compare_vectors(v1, v1), "equal")
        self.assertEqual(compare_vectors({"a": 2, "b": 1}, v1), "ahead")
        self.assertEqual(compare_vectors(v1, {"a": 2, "b": 1}), "behind")
        self.assertEqual(compare_vectors({"a": 2, "b": 1}, {"a": 1, "b": 2}),
                         "diverged")                       # 各有领先片
        self.assertEqual(compare_vectors({"a": 1}, {"a": 1, "b": 1}),
                         "diverged")                       # 键集变化（分片增删）
        with self.assertRaises(CompanionError):
            compare_vectors("nope", v1)

    def test_rescan_subset_advances_vector(self):
        shard_dir = self.tmp / "vec-advance"
        first = _write(self.tree, shard_dir, self.plan)
        graph = CrossShardGraph.load(shard_dir)
        try:
            baseline = graph.generation_vector()
            self.assertEqual(baseline, first.generations)
            self.assertEqual(compare_vectors(baseline, baseline), "equal")
            second = _write(self.tree, shard_dir, self.plan,
                            only={"shard-0001", "shard-0002"})
            # 结果向量覆盖全清单：重扫片=新代，未重扫片沿用上次写入记录
            self.assertEqual(second.generations,
                             {"shard-0000": 1, "shard-0001": 2, "shard-0002": 2})
            live = graph.generation_vector()
            self.assertEqual(live, {"shard-0000": 1, "shard-0001": 2,
                                    "shard-0002": 2})
            self.assertEqual(compare_vectors(live, baseline), "ahead")
            self.assertEqual(compare_vectors(baseline, live), "behind")
            # 未重扫的片不动：行数与文件内容保持原样
            self.assertEqual(graph.verify_integrity().file_count, TOTAL_FILES)
        finally:
            graph.close()

    def test_independent_replica_rescan_diverges(self):
        """两副本各重扫不同片 → 分叉（不可互相拼成"完整当前结果"的前置判据）。"""
        dir_a = self.tmp / "vec-replica-a"
        dir_b = self.tmp / "vec-replica-b"
        _write(self.tree, dir_a, self.plan)
        _write(self.tree, dir_b, self.plan)
        _write(self.tree, dir_a, self.plan, only={"shard-0000"})
        _write(self.tree, dir_b, self.plan, only={"shard-0001"})
        graph_a = CrossShardGraph.load(dir_a)
        graph_b = CrossShardGraph.load(dir_b)
        try:
            vector_a = graph_a.generation_vector()
            vector_b = graph_b.generation_vector()
            self.assertEqual(vector_a, {"shard-0000": 2, "shard-0001": 1,
                                        "shard-0002": 1})
            self.assertEqual(vector_b, {"shard-0000": 1, "shard-0001": 2,
                                        "shard-0002": 1})
            self.assertEqual(compare_vectors(vector_a, vector_b), "diverged")
            self.assertEqual(compare_vectors(vector_b, vector_a), "diverged")
            for graph in (graph_a, graph_b):
                self.assertEqual(graph.verify_integrity().file_count, TOTAL_FILES)
        finally:
            graph_a.close()
            graph_b.close()


if __name__ == "__main__":
    unittest.main()

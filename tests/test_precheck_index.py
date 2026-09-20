# -*- coding: utf-8 -*-
"""SR-06 主链路容量桥接单测：precheck index/g2/resume/coverage 子命令（全部真实执行）。

覆盖（验收口径，无模型成本）：
- index-count 粗计数阈值分类；index-scan 世代/manifest/anchor（vendor kernel 引导）
- chunks-write 分块 schema 校验（尺寸/邻块/跨块唯一）；index-verify 全集闭合
  （绝对路径归一、缺口/多余/重复拒绝、回执不含全量清单）
- chunks-page 跳过检查点已完成块 + anchor 混代拒绝；g2-progress 原子检查点 round-trip
- resume-register / resume-check：源锚变化 → stale_anchor（拒绝混代续跑）
- coverage-record 三维度覆盖账（分母/覆盖校验）
- 有界性实测：800 文件 fixture（复用 fixture_gen --rows 800）分页领块每批 ≤3、
  批内不越限、kill -9 中断后重跑跳过已完成块且 generation/anchor 不变
- 大清单载荷断言：≥1000 文件 index 的 index-verify 回执长度有界（不 stringify 全集）
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PRECHECK = REPO_ROOT / "code-analysis-swarm" / "scripts" / "precheck.py"
FIXTURE_GEN = REPO_ROOT / "tests" / "benchmarks" / "fixture_gen.py"

# kill 续跑消费者：等价于协调者在 G2 的领块循环（无模型调用）。
# index manifest 已存在则不重扫——重扫会开新世代改变 anchor，正是混代拒绝要防的操作。
CONSUMER = r'''
import json, os, subprocess, sys
PRECHECK, RUN, SRC, PAGE_SIZE = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])

def call(sub, payload):
    proc = subprocess.run([sys.executable, PRECHECK, sub, "--json", json.dumps(payload)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit("%s failed: exit %s %s" % (sub, proc.returncode, proc.stderr))
    return json.loads(proc.stdout)

manifest_path = os.path.join(RUN, "index", "manifest.json")
if os.path.exists(manifest_path):
    manifest = json.load(open(manifest_path, encoding="utf-8"))
else:
    call("index-scan", {"source_root": SRC, "run_root": RUN})
    manifest = json.load(open(manifest_path, encoding="utf-8"))
anchor, generation = manifest["source_anchor"], manifest["generation"]

rows, after = [], None
while True:
    page = call("index-page", {"run_root": RUN, "after": after, "limit": 200})
    rows.extend(page["rows"])
    after = page["cursor"]
    if after is None:
        break
chunks = [{"id": "chunk-%04d" % i,
           "files": [row["path"] for row in rows[i * 50:(i + 1) * 50]],
           "loc_est": 100, "neighbors": [], "rationale": "fixture"}
          for i in range((len(rows) + 49) // 50)]
call("chunks-write", {"run_root": RUN, "chunks": chunks})
call("g2-progress", {"run_root": RUN, "action": "init", "source_anchor": anchor,
                     "generation": generation, "chunk_order": [c["id"] for c in chunks]})
call("resume-register", {"run_root": RUN, "kind": "custom", "activity_id": "g2-fixture",
                         "checkpoint_path": os.path.join(RUN, "checkpoints", "g2-progress.json"),
                         "schema_version": 1, "source_anchor": anchor})
batches = []
while True:
    page = call("chunks-page", {"run_root": RUN, "page": 0, "page_size": PAGE_SIZE, "anchor": anchor})
    if not page["chunks"]:
        break
    assert len(page["chunks"]) <= PAGE_SIZE, "batch bound violated: %d" % len(page["chunks"])
    batches.append([c["id"] for c in page["chunks"]])
    for c in page["chunks"]:
        call("g2-progress", {"run_root": RUN, "action": "mark", "chunk_id": c["id"]})
json.dump({"batches": batches, "total": len(chunks)},
          open(os.path.join(RUN, "consumer-result.json"), "w"))
print("DONE")
'''


def run_helper(sub, payload):
    proc = subprocess.run(
        [sys.executable, str(PRECHECK), sub, "--json", json.dumps(payload, ensure_ascii=False)],
        capture_output=True, text=True)
    out = None
    if proc.stdout.strip():
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1])
        except ValueError:
            out = None
    return proc.returncode, out, proc.stderr


class PrecheckIndexTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="precheck-index-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / "src"
        self.run_root = self.base / "run"
        self.source.mkdir()
        self.run_root.mkdir()

    def write_tree(self, count, prefix="mod"):
        """在 source 下写 count 个小文件（分目录），返回相对路径排序列表。"""
        rels = []
        for i in range(count):
            rel = f"{prefix}{i % 7}/f{i:05d}.py"
            path = self.source / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"print({i})\n", encoding="utf-8")
            rels.append(rel)
        return sorted(rels)

    def scan(self):
        code, receipt, _ = run_helper("index-scan", {"source_root": str(self.source), "run_root": str(self.run_root)})
        self.assertEqual(code, 0, receipt)
        return receipt

    def plan_chunks(self, rels, per=25):
        return [{"id": f"chunk-{i:04d}",
                 "files": rels[i * per:(i + 1) * per],
                 "loc_est": 100, "neighbors": [], "rationale": "fixture"}
                for i in range((len(rels) + per - 1) // per)]

    def setup_full_pipeline(self, rels, per=25):
        """scan → chunks-write → init 检查点 → 登记，返回 (receipt, anchor, chunk ids)。"""
        scan = self.scan()
        chunks = self.plan_chunks(rels, per)
        code, receipt, _ = run_helper("chunks-write", {"run_root": str(self.run_root), "chunks": chunks})
        self.assertEqual(code, 0, receipt)
        code, ok, _ = run_helper("index-verify", {"run_root": str(self.run_root)})
        self.assertEqual(code, 0, ok)
        anchor = scan["source_anchor"]
        code, receipt, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "init",
                                                      "source_anchor": anchor, "generation": scan["generation"],
                                                      "chunk_order": [c["id"] for c in chunks]})
        self.assertEqual(code, 0, receipt)
        return scan, anchor, [c["id"] for c in chunks]


class IndexCountAndScanTest(PrecheckIndexTestBase):
    def test_index_count_counts_plain_tree(self):
        self.write_tree(7)
        code, out, _ = run_helper("index-count", {"source_root": str(self.source)})
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["file_count"], 7)

    def test_index_count_rejects_missing_source(self):
        code, out, _ = run_helper("index-count", {"source_root": str(self.base / "no-such")})
        self.assertEqual(code, 2)
        self.assertEqual(out["kind"], "argument")

    def test_index_scan_publishes_generation_manifest_and_anchor(self):
        rels = self.write_tree(9)
        scan = self.scan()
        self.assertEqual(scan["generation"], 1)
        self.assertEqual(scan["file_count"], len(rels))
        self.assertTrue(os.path.isfile(scan["index_db"]))
        self.assertTrue(os.path.isfile(scan["manifest"]))
        manifest = json.loads(Path(scan["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["kind"], "index_manifest")
        self.assertEqual(manifest["source_anchor"], scan["source_anchor"])
        self.assertEqual(len(scan["source_anchor"]), 64)
        # anchor 是世代内容指纹（generation/root/mode/file_count 的 sha256）
        import hashlib
        expect = hashlib.sha256(json.dumps(
            {"generation": 1, "root": os.path.realpath(self.source), "mode": scan["mode"],
             "file_count": len(rels)}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(scan["source_anchor"], expect)

    def test_rescan_opens_new_generation_with_new_anchor(self):
        self.write_tree(5)
        first = self.scan()
        self.write_tree(2, prefix="extra")
        second = self.scan()
        self.assertEqual(second["generation"], first["generation"] + 1)
        self.assertNotEqual(second["source_anchor"], first["source_anchor"])
        self.assertEqual(second["file_count"], 7)

    def test_index_scan_rejects_run_root_inside_source(self):
        self.write_tree(3)
        code, out, _ = run_helper("index-scan", {"source_root": str(self.source), "run_root": str(self.source / "nested")})
        self.assertEqual(code, 3)
        self.assertEqual(out["kind"], "conflict")

    def test_kernel_source_resolves_to_vendor_or_packages(self):
        rels = self.write_tree(2)
        scan = self.scan()
        self.assertIn(scan["kernel_source"], ("vendor", "packages"))
        self.assertTrue(rels)

    def test_index_page_keyset_pagination(self):
        rels = self.write_tree(30)
        self.scan()
        seen, after, pages = [], None, 0
        while True:
            code, page, _ = run_helper("index-page", {"run_root": str(self.run_root), "after": after, "limit": 7})
            self.assertEqual(code, 0, page)
            seen.extend(row["path"] for row in page["rows"])
            after = page["cursor"]
            pages += 1
            if after is None:
                break
        self.assertEqual(sorted(seen), rels)
        self.assertEqual(pages, 5)

    def test_index_page_requires_manifest(self):
        code, out, _ = run_helper("index-page", {"run_root": str(self.run_root)})
        self.assertEqual(code, 3)


class ChunksWriteAndVerifyTest(PrecheckIndexTestBase):
    def setUp(self):
        super().setUp()
        self.rels = self.write_tree(12)

    def test_write_and_verify_close_full_set(self):
        self.setup_full_pipeline(self.rels)
        code, receipt, _ = run_helper("index-verify", {"run_root": str(self.run_root)})
        self.assertEqual(code, 0)
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["covered"], 12)
        self.assertEqual(receipt["missing_count"], 0)
        self.assertEqual(receipt["extra_count"], 0)

    def test_verify_accepts_absolute_paths_by_stripping_root(self):
        scan = self.scan()
        chunks = [{"id": "chunk-0",
                   "files": [str(self.source / r) for r in self.rels[:6]],
                   "loc_est": 10, "neighbors": ["chunk-1"], "rationale": "a"},
                  {"id": "chunk-1",
                   "files": [str(self.source / r) for r in self.rels[6:]],
                   "loc_est": 10, "neighbors": ["chunk-0"], "rationale": "b"}]
        run_helper("chunks-write", {"run_root": str(self.run_root), "chunks": chunks})
        code, receipt, _ = run_helper("index-verify", {"run_root": str(self.run_root)})
        self.assertEqual(code, 0, receipt)
        self.assertTrue(receipt["ok"])

    def test_verify_rejects_missing_file_with_sample(self):
        self.scan()
        chunks = self.plan_chunks(self.rels, per=6)
        missing = chunks[0]["files"][-1]
        chunks[0]["files"] = chunks[0]["files"][:-1]  # 少报一个：索引有而分块无
        code, written, _ = run_helper("chunks-write", {"run_root": str(self.run_root), "chunks": chunks})
        self.assertEqual(code, 0, written)
        code, receipt, _ = run_helper("index-verify", {"run_root": str(self.run_root)})
        self.assertEqual(code, 3)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["missing_count"], 1)
        self.assertEqual(receipt["missing_sample"], [missing])

    def test_verify_rejects_extra_file_with_sample(self):
        self.scan()
        chunks = self.plan_chunks(self.rels, per=6)
        chunks[1]["files"] = chunks[1]["files"] + ["ghost/phantom.py"]  # 多报一个：分块有而索引无
        run_helper("chunks-write", {"run_root": str(self.run_root), "chunks": chunks})
        code, receipt, _ = run_helper("index-verify", {"run_root": str(self.run_root)})
        self.assertEqual(code, 3)
        self.assertEqual(receipt["extra_count"], 1)
        self.assertIn("ghost/phantom.py", receipt["extra_sample"])

    def test_chunks_write_rejects_bad_schema(self):
        self.scan()
        good = self.plan_chunks(self.rels, per=6)
        cases = [
            ("重复块 ID", [dict(good[0]), dict(good[1], id=good[0]["id"])] if len(good) > 1 else [dict(good[0]), dict(good[0])]),
            ("块内文件重复", [dict(good[0], files=[good[0]["files"][0]] * 2)]),
            ("邻块引用不存在", [dict(good[0], neighbors=["chunk-999"])]),
            ("邻块自引用", [dict(good[0], neighbors=[good[0]["id"]])]),
            ("loc_est 超限", [dict(good[0], loc_est=5001)]),
            ("loc_est 非整数", [dict(good[0], loc_est="big")]),
            ("缺分块依据", [dict(good[0], rationale="")]),
            ("块 ID 非法", [dict(good[0], id="block-0")]),
            ("files 为空", [dict(good[0], files=[])]),
        ]
        for label, chunks in cases:
            code, out, _ = run_helper("chunks-write", {"run_root": str(self.run_root), "chunks": chunks})
            self.assertEqual(code, 2, f"{label} 应 exit 2：{out}")
        # 跨块重复文件
        code, out, _ = run_helper("chunks-write", {"run_root": str(self.run_root), "chunks": [
            dict(good[0]), dict(good[1], files=[good[0]["files"][0]])]})
        self.assertEqual(code, 2, "跨块重复应 exit 2")
        # 单块超 150 文件
        many = self.write_tree(151, prefix="bulk")
        code, out, _ = run_helper("chunks-write", {"run_root": str(self.run_root), "chunks": [
            {"id": "chunk-0", "files": many, "loc_est": 1, "neighbors": [], "rationale": "r"}]})
        self.assertEqual(code, 2, "151 文件块应 exit 2")

    def test_verify_needs_chunks_written(self):
        self.scan()
        code, out, _ = run_helper("index-verify", {"run_root": str(self.run_root)})
        self.assertEqual(code, 3)


class ChunksPageAndCheckpointTest(PrecheckIndexTestBase):
    def setUp(self):
        super().setUp()
        self.rels = self.write_tree(25)
        self.scan, self.anchor, self.chunk_ids = self.setup_full_pipeline(self.rels, per=5)
        # 5 块，每块 5 文件

    def fetch(self):
        return run_helper("chunks-page", {"run_root": str(self.run_root), "page": 0,
                                          "page_size": 3, "anchor": self.anchor})

    def test_pages_are_bounded_and_advance(self):
        code, page, _ = self.fetch()
        self.assertEqual(code, 0)
        self.assertEqual(page["pages"], 2)
        self.assertEqual([c["id"] for c in page["chunks"]], self.chunk_ids[:3])
        self.assertEqual(page["remaining"], 5)
        for cid in self.chunk_ids[:3]:
            run_helper("g2-progress", {"run_root": str(self.run_root), "action": "mark", "chunk_id": cid})
        code, page, _ = self.fetch()
        self.assertEqual(code, 0)
        self.assertEqual([c["id"] for c in page["chunks"]], self.chunk_ids[3:])
        self.assertEqual(page["completed"], 3)

    def test_completed_chunks_are_skipped(self):
        run_helper("g2-progress", {"run_root": str(self.run_root), "action": "mark", "chunk_id": self.chunk_ids[0]})
        code, page, _ = self.fetch()
        self.assertEqual(code, 0)
        self.assertNotIn(self.chunk_ids[0], [c["id"] for c in page["chunks"]])
        self.assertEqual(page["remaining"], 4)

    def test_empty_pending_is_legal_terminal_state(self):
        for cid in self.chunk_ids:
            run_helper("g2-progress", {"run_root": str(self.run_root), "action": "mark", "chunk_id": cid})
        code, page, _ = self.fetch()
        self.assertEqual(code, 0, page)
        self.assertEqual(page["chunks"], [])
        self.assertEqual(page["remaining"], 0)

    def test_anchor_mismatch_is_generation_conflict(self):
        code, page, _ = run_helper("chunks-page", {"run_root": str(self.run_root), "page": 0,
                                                   "page_size": 3, "anchor": "0" * 64})
        self.assertEqual(code, 3)
        self.assertEqual(page["kind"], "conflict")

    def test_overflow_page_rejected_while_pending(self):
        code, page, _ = run_helper("chunks-page", {"run_root": str(self.run_root), "page": 9,
                                                   "page_size": 3, "anchor": self.anchor})
        self.assertEqual(code, 2)

    def test_g2_progress_roundtrip(self):
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "read"})
        self.assertEqual(code, 0)
        self.assertTrue(out["exists"])
        checkpoint = out["checkpoint"]
        self.assertEqual(checkpoint["chunk_order"], self.chunk_ids)
        self.assertEqual(checkpoint["completed"], [])
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "mark",
                                                  "chunk_id": self.chunk_ids[1]})
        self.assertEqual(code, 0)
        self.assertEqual(out["completed"], 1)
        # 幂等重复标记
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "mark",
                                                  "chunk_id": self.chunk_ids[1]})
        self.assertEqual(code, 0)
        self.assertTrue(out["idempotent"])
        self.assertEqual(out["completed"], 1)
        # 未知块拒绝
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "mark",
                                                  "chunk_id": "chunk-ghost"})
        self.assertEqual(code, 2)

    def test_g2_init_rejects_anchor_conflict(self):
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "init",
                                                  "source_anchor": "f" * 64, "generation": 1,
                                                  "chunk_order": self.chunk_ids})
        self.assertEqual(code, 3)
        self.assertEqual(out["kind"], "conflict")

    def test_resume_register_and_stale_anchor(self):
        code, out, _ = run_helper("resume-register", {
            "run_root": str(self.run_root), "kind": "custom", "activity_id": "g2-test",
            "checkpoint_path": str(self.run_root / "checkpoints" / "g2-progress.json"),
            "schema_version": 1, "source_anchor": self.anchor})
        self.assertEqual(code, 0, out)
        self.assertEqual(out["activity"]["source_anchor"], self.anchor)
        # 当前锚一致 → ok
        code, out, _ = run_helper("resume-check", {"run_root": str(self.run_root), "kind": "custom",
                                                   "activity_id": "g2-test", "current_anchor": self.anchor})
        self.assertEqual(code, 0)
        self.assertEqual(out["condition"], "ok")
        # 源锚变化 → stale_anchor（拒绝混代续跑）
        code, out, _ = run_helper("resume-check", {"run_root": str(self.run_root), "kind": "custom",
                                                   "activity_id": "g2-test", "current_anchor": "e" * 64})
        self.assertEqual(code, 0)
        self.assertEqual(out["condition"], "stale_anchor")
        self.assertIn("不自动续跑", out["detail"])
        # 未登记活动拒绝
        code, out, _ = run_helper("resume-check", {"run_root": str(self.run_root), "kind": "custom",
                                                   "activity_id": "g2-ghost", "current_anchor": self.anchor})
        self.assertEqual(code, 2)

    def test_resume_register_rejects_checkpoint_outside_run_root(self):
        code, out, _ = run_helper("resume-register", {
            "run_root": str(self.run_root), "kind": "custom", "activity_id": "g2-x",
            "checkpoint_path": str(self.base / "outside.json"),
            "schema_version": 1, "source_anchor": self.anchor})
        self.assertEqual(code, 3)


class CoverageRecordTest(PrecheckIndexTestBase):
    def test_writes_account_and_checks_bounds(self):
        self.write_tree(3)
        self.scan()
        code, out, _ = run_helper("coverage-record", {
            "run_root": str(self.run_root), "run_id": "run-x", "generation": 1,
            "dimensions": {
                "index_files": {"denominator": 3, "covered": 3, "gaps": []},
                "semantics_deep": {"denominator": 3, "covered": 3, "gaps": []},
                "independent_review": {"denominator": 5, "covered": 4, "gaps": ["c1"]},
            }})
        self.assertEqual(code, 0, out)
        account_path = self.run_root / "coverage_account.json"
        self.assertTrue(account_path.is_file())
        account = json.loads(account_path.read_text(encoding="utf-8"))
        self.assertFalse(account["complete"])  # independent_review 有缺口
        self.assertEqual(account["dimensions"]["index_files"]["denominator"], 3)
        self.assertEqual(account["dimensions"]["independent_review"]["gaps"], ["c1"])

    def test_rejects_covered_over_denominator(self):
        code, out, _ = run_helper("coverage-record", {
            "run_root": str(self.run_root), "run_id": "run-x",
            "dimensions": {"index_files": {"denominator": 1, "covered": 2}}})
        self.assertEqual(code, 2)


class BoundednessOnFixtureTest(PrecheckIndexTestBase):
    """有界性实测（无模型成本）：800 文件 fixture 分页领块 + kill -9 中断续跑。"""

    PAGE_SIZE = 3

    def _make_fixture(self, rows=800):
        fixture = self.base / f"benchmark-fixture-{rows}"
        proc = subprocess.run(
            [sys.executable, str(FIXTURE_GEN), str(fixture), "--rows", str(rows),
             "--top-dirs", "4", "--sub-dirs", "4"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return fixture

    def _spawn_consumer(self, fixture):
        # start_new_session：硬杀时用 killpg 连在途 precheck 孙进程一并终止，
        # 避免"孙进程在检查点读取之后补写 mark"的竞态（真实硬杀同此语义）。
        return subprocess.Popen(
            [sys.executable, "-c", CONSUMER, str(PRECHECK), str(self.run_root),
             str(fixture), str(self.PAGE_SIZE)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)

    def _completed_count(self):
        path = self.run_root / "checkpoints" / "g2-progress.json"
        if not path.is_file():
            return 0
        try:
            return len(json.loads(path.read_text(encoding="utf-8"))["completed"])
        except (ValueError, KeyError):
            return 0

    def test_800_file_fixture_kill_resume_and_bounds(self):
        fixture = self._make_fixture(800)
        run_root_before = None
        proc = self._spawn_consumer(fixture)
        deadline = time.time() + 120
        while time.time() < deadline:
            if self._completed_count() >= 2 * self.PAGE_SIZE:
                break
            if proc.poll() is not None:
                self.fail(f"消费者过早退出：{proc.stderr.read()}")
            time.sleep(0.05)
        # 模拟硬杀（G2 进行中断电/kill）：整组终止（含在途 precheck 孙进程）
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait()
        self.assertFalse((self.run_root / "consumer-result.json").exists(), "中断后不应有完整结果")
        checkpoint = json.loads((self.run_root / "checkpoints" / "g2-progress.json").read_text(encoding="utf-8"))
        killed_done = len(checkpoint["completed"])
        done_set = set(checkpoint["completed"])
        self.assertGreaterEqual(killed_done, 2 * self.PAGE_SIZE)
        manifest_before = json.loads((self.run_root / "index" / "manifest.json").read_text(encoding="utf-8"))
        # 重跑：index manifest 已存在不重扫（generation/anchor 不变），已完成块跳过
        proc2 = self._spawn_consumer(fixture)
        out, err = proc2.communicate(timeout=300)
        self.assertEqual(proc2.returncode, 0, err)
        self.assertIn("DONE", out)
        result = json.loads((self.run_root / "consumer-result.json").read_text(encoding="utf-8"))
        manifest_after = json.loads((self.run_root / "index" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest_after["generation"], manifest_before["generation"], "重跑不得开新世代（混代拒绝）")
        self.assertEqual(manifest_after["source_anchor"], manifest_before["source_anchor"])
        self.assertEqual(result["total"], 16, "800 文件 / 每块 50 = 16 块")
        # 续跑语义：第二次运行只领"剩余块"（已完成块跳过），两轮并集 = 全集 16 块
        seen_second = [cid for batch in result["batches"] for cid in batch]
        self.assertEqual(len(seen_second), 16 - killed_done,
                         "续跑只应领取未完成块")
        self.assertEqual(sorted(set(seen_second) | done_set), [f"chunk-{i:04d}" for i in range(16)],
                         "两轮并集应恰为全集（无重无漏）")
        self.assertLessEqual(max(len(b) for b in result["batches"]), self.PAGE_SIZE, "续跑批次仍受批上限约束")
        # 收尾后检查点闭合：completed == 全集
        final_checkpoint = json.loads((self.run_root / "checkpoints" / "g2-progress.json").read_text(encoding="utf-8"))
        self.assertEqual(len(final_checkpoint["completed"]), 16)
        # 台账与检查点闭合：resume-check 对正确锚给 ok
        code, out, _ = run_helper("resume-check", {"run_root": str(self.run_root), "kind": "custom",
                                                   "activity_id": "g2-fixture",
                                                   "current_anchor": manifest_after["source_anchor"]})
        self.assertEqual(code, 0)
        self.assertIn(out["condition"], ("ok", "completed"))


class LargeListingPayloadTest(PrecheckIndexTestBase):
    """大清单载荷断言：≥1000 文件 index 的回执不携带全量清单。"""

    def test_verify_receipt_stays_bounded_on_1000_files(self):
        rels = self.write_tree(1000)
        self.scan()
        chunks = self.plan_chunks(rels, per=100)
        code, receipt, _ = run_helper("chunks-write", {"run_root": str(self.run_root), "chunks": chunks})
        self.assertEqual(code, 0, receipt)
        self.assertEqual(receipt["file_count_total"], 1000)
        proc = subprocess.run(
            [sys.executable, str(PRECHECK), "index-verify", "--json",
             json.dumps({"run_root": str(self.run_root)})], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["covered"], 1000)
        # 回执长度有界：不 stringify 全集（全集清单约为 1000×40 字节，回执必须远小于它）
        self.assertLess(len(proc.stdout), 2048, "index-verify 回执不应携带全量清单")
        self.assertEqual(len(payload["missing_sample"]), 0)
        # 破坏闭合时样本仍有界（≤20 条）
        broken = [dict(chunks[0])]
        broken[0]["files"] = broken[0]["files"][:-1] + [f"ghost/g{i}.py" for i in range(30)]
        run_helper("chunks-write", {"run_root": str(self.run_root), "chunks": broken})
        proc = subprocess.run(
            [sys.executable, str(PRECHECK), "index-verify", "--json",
             json.dumps({"run_root": str(self.run_root)})], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 3)
        payload = json.loads(proc.stdout)
        self.assertLessEqual(len(payload["missing_sample"]), 20)
        self.assertLessEqual(len(payload["extra_sample"]), 20)
        self.assertLess(len(proc.stdout), 4096, "失败回执同样不得携带全量清单")


if __name__ == "__main__":
    unittest.main()

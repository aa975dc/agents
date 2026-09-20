# -*- coding: utf-8 -*-
"""SR-06 主链路容量桥接单测：precheck index/g2/resume/coverage 子命令（全部真实执行）。

覆盖（验收口径，无模型成本）：
- index-count 粗计数阈值分类；index-scan 世代/manifest/anchor（vendor kernel 引导）
- chunks-write 双模式落盘（argv 兼容 + plan_file 有界导入：argv 只带路径+锚点）；
  index-verify 流式全集闭合（聚合指纹+精确差集，绝对路径归一、缺口/多余/重复拒绝、
  回执不含全量清单）
- chunks-page 从分块库+完成标记分页领取（loaded_entries ≤ 页大小可证）+ anchor 混代拒绝
- g2-progress per-chunk 完成标记（FIX05）：两真实进程对不同块 mark 无丢失（复核者探针
  转正）、同 ID 重复 mark 幂等、init/read 在 12,000 块下恒有界（不再有单体检查点的
  init/read 边界不一致）
- resume-register / resume-check：源锚变化 → stale_anchor（拒绝混代续跑）
- acquire 显式 resume 续接：有效回执+同源 → adopt；无回执/异源 → 拒绝（FIX05-7.3）
- coverage-record 三维度覆盖账（分母/覆盖校验）
- 有界性实测：800 文件 fixture（复用 fixture_gen --rows 800）分页领块每批 ≤3、
  批内不越限、kill -9 中断后重跑跳过已完成块且 generation/anchor 不变
- 续接闭合（FIX05）：中断 3 块（结果落盘+标记）→ 续跑补 2 块 → 聚合 5 块无重无漏
- 大清单载荷断言：≥1000 文件 index 的 index-verify 回执长度有界（不 stringify 全集）
"""

import hashlib
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
# FIX05 契约：分块规划走 plan_file 有界导入；g2 init 只登记 O(1) 元数据；
# 完成记录为 per-chunk 标记文件；标记的 completed 数 = 目录列举派生。
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
plan_path = os.path.join(RUN, "chunk-plan.json")
with open(plan_path, "w", encoding="utf-8") as fh:
    json.dump(chunks, fh, ensure_ascii=False)
call("chunks-write", {"run_root": RUN, "plan_file": plan_path, "anchor": anchor})
call("g2-progress", {"run_root": RUN, "action": "init", "source_anchor": anchor,
                     "generation": generation})
call("resume-register", {"run_root": RUN, "kind": "custom", "activity_id": "g2-fixture",
                         "checkpoint_path": os.path.join(RUN, "g2", str(generation), "meta.json"),
                         "schema_version": 1, "source_anchor": anchor})
batches = []
while True:
    page = call("chunks-page", {"run_root": RUN, "page": 0, "page_size": PAGE_SIZE, "anchor": anchor})
    if not page["chunks"]:
        break
    assert len(page["chunks"]) <= PAGE_SIZE, "batch bound violated: %d" % len(page["chunks"])
    assert page["loaded_entries"] <= PAGE_SIZE, "load bound violated: %d" % page["loaded_entries"]
    batches.append([c["id"] for c in page["chunks"]])
    for c in page["chunks"]:
        call("g2-progress", {"run_root": RUN, "action": "mark", "chunk_id": c["id"],
                             "generation": generation})
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
        """scan → 分块规划写文件 → 有界导入 → init 标记元数据 → 登记，
        返回 (scan 回执, anchor, chunk ids)。"""
        scan = self.scan()
        chunks = self.plan_chunks(rels, per)
        plan_path = self.run_root / "chunk-plan.json"
        plan_path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
        code, receipt, _ = run_helper("chunks-write", {"run_root": str(self.run_root),
                                                       "plan_file": str(plan_path),
                                                       "anchor": scan["source_anchor"]})
        self.assertEqual(code, 0, receipt)
        self.assertEqual(receipt["mode"], "plan_import")
        code, ok, _ = run_helper("index-verify", {"run_root": str(self.run_root)})
        self.assertEqual(code, 0, ok)
        anchor = scan["source_anchor"]
        code, receipt, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "init",
                                                      "source_anchor": anchor, "generation": scan["generation"]})
        self.assertEqual(code, 0, receipt)
        return scan, anchor, [c["id"] for c in chunks]

    def mark(self, chunk_id, generation=1, result=False):
        payload = {"run_root": str(self.run_root), "action": "mark", "chunk_id": chunk_id,
                   "generation": generation}
        if result:
            path = self.run_root / "chunks" / f"{chunk_id}.json"
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({"meta": {"chunk_id": chunk_id}}), encoding="utf-8")
            payload["result_path"] = str(path)
        code, out, _ = run_helper("g2-progress", payload)
        self.assertEqual(code, 0, out)
        return out


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
        self.assertEqual(page["loaded_entries"], 3, "领取加载量 ≤ 页大小（FIX05 真分页）")
        for cid in self.chunk_ids[:3]:
            self.mark(cid)
        code, page, _ = self.fetch()
        self.assertEqual(code, 0)
        self.assertEqual([c["id"] for c in page["chunks"]], self.chunk_ids[3:])
        self.assertEqual(page["completed"], 3)
        self.assertEqual(page["loaded_entries"], 2)

    def test_completed_chunks_are_skipped(self):
        self.mark(self.chunk_ids[0])
        code, page, _ = self.fetch()
        self.assertEqual(code, 0)
        self.assertNotIn(self.chunk_ids[0], [c["id"] for c in page["chunks"]])
        self.assertEqual(page["remaining"], 4)

    def test_empty_pending_is_legal_terminal_state(self):
        for cid in self.chunk_ids:
            self.mark(cid)
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
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "read",
                                                  "generation": 1})
        self.assertEqual(code, 0)
        self.assertTrue(out["exists"])
        self.assertEqual(out["meta"]["total"], len(self.chunk_ids))
        self.assertEqual(out["completed_count"], 0)
        # mark 可绑定块结果制品（核验存在并记录 sha256）
        result_path = self.run_root / "chunks" / f"{self.chunk_ids[1]}.json"
        result_path.parent.mkdir(exist_ok=True)
        result_path.write_text("{}", encoding="utf-8")
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "mark",
                                                  "chunk_id": self.chunk_ids[1], "generation": 1,
                                                  "result_path": str(result_path)})
        self.assertEqual(code, 0)
        self.assertEqual(out["completed"], 1)
        marker_path = self.run_root / "g2" / "1" / f"{self.chunk_ids[1]}.done"
        self.assertEqual(out["marker_path"], str(marker_path.resolve()))
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(marker["result_sha256"], hashlib.sha256(b"{}").hexdigest())
        # 幂等重复标记
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "mark",
                                                  "chunk_id": self.chunk_ids[1], "generation": 1,
                                                  "result_path": str(result_path)})
        self.assertEqual(code, 0)
        self.assertTrue(out["idempotent"])
        self.assertEqual(out["completed"], 1)
        # 未知块拒绝（不在分块库，点查即可判定，不载全量）
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "mark",
                                                  "chunk_id": "chunk-ghost", "generation": 1})
        self.assertEqual(code, 2)
        # read 恒有界：completed 只给计数；ID 清单经 list 分页领取
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "read",
                                                  "generation": 1})
        self.assertEqual(out["completed_count"], 1)
        self.assertNotIn("completed", out)
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "list",
                                                  "generation": 1, "page": 0, "page_size": 10})
        self.assertEqual(code, 0, out)
        self.assertEqual(out["ids"], [self.chunk_ids[1]])
        # init 幂等：重复 init 保留既有标记
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "init",
                                                  "source_anchor": self.anchor, "generation": 1})
        self.assertEqual(code, 0)
        self.assertTrue(out["idempotent"])
        self.assertEqual(out["completed"], 1)

    def test_g2_init_rejects_anchor_conflict(self):
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "init",
                                                  "source_anchor": "f" * 64, "generation": 1})
        self.assertEqual(code, 3)
        self.assertEqual(out["kind"], "conflict")

    def test_resume_register_and_stale_anchor(self):
        meta_path = str(self.run_root / "g2" / "1" / "meta.json")
        code, out, _ = run_helper("resume-register", {
            "run_root": str(self.run_root), "kind": "custom", "activity_id": "g2-test",
            "checkpoint_path": meta_path,
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
        """FIX05：完成数 = per-chunk 标记目录列举派生（无单体检查点可读）。"""
        g2_dir = self.run_root / "g2" / "1"
        if not g2_dir.is_dir():
            return 0
        return len(list(g2_dir.glob("*.done")))

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
        killed_done = self._completed_count()
        done_set = {p[:-5] for p in os.listdir(self.run_root / "g2" / "1") if p.endswith(".done")}
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
        # 收尾后标记闭合：completed == 全集（每块一个 .done 文件）
        final_markers = [p for p in os.listdir(self.run_root / "g2" / "1") if p.endswith(".done")]
        self.assertEqual(len(final_markers), 16)
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


class ChunksWritePlanImportTest(PrecheckIndexTestBase):
    """chunks-write 有界导入模式：argv 只带 plan_file+anchor；schema/混代守卫。"""

    def setUp(self):
        super().setUp()
        self.rels = self.write_tree(12)
        self.scan = self.scan()
        self.anchor = self.scan["source_anchor"]

    def _plan(self, chunks):
        plan_path = self.run_root / "chunk-plan.json"
        plan_path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
        return run_helper("chunks-write", {"run_root": str(self.run_root),
                                           "plan_file": str(plan_path), "anchor": self.anchor})

    def test_import_writes_store_and_bounded_summary(self):
        chunks = self.plan_chunks(self.rels, per=6)
        code, out, _ = self._plan(chunks)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["mode"], "plan_import")
        self.assertEqual(out["chunk_count"], 2)
        self.assertEqual(out["file_count_total"], 12)
        store = self.run_root / "index" / "chunks"
        self.assertEqual(sorted(p[:-5] for p in os.listdir(store)), ["chunk-0000", "chunk-0001"])
        summary = json.loads((self.run_root / "index" / "chunks-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["kind"], "chunk_plan_summary")
        self.assertEqual(summary["source_anchor"], self.anchor)
        self.assertEqual(summary["chunk_count"], 2)
        self.assertEqual(summary["file_count_total"], 12)
        # 导入产物可直接过 index-verify
        code, receipt, _ = run_helper("index-verify", {"run_root": str(self.run_root)})
        self.assertEqual(code, 0, receipt)

    def test_import_rejects_anchor_mismatch_with_manifest(self):
        chunks = self.plan_chunks(self.rels, per=6)
        plan_path = self.run_root / "chunk-plan.json"
        plan_path.write_text(json.dumps(chunks), encoding="utf-8")
        code, out, _ = run_helper("chunks-write", {"run_root": str(self.run_root),
                                                   "plan_file": str(plan_path), "anchor": "0" * 64})
        self.assertEqual(code, 3)
        self.assertEqual(out["kind"], "conflict")

    def test_import_rejects_outside_file(self):
        outside = self.base / "plan.json"
        outside.write_text("[]", encoding="utf-8")
        code, out, _ = run_helper("chunks-write", {"run_root": str(self.run_root),
                                                   "plan_file": str(outside), "anchor": self.anchor})
        self.assertEqual(code, 3)

    def test_import_rejects_empty_and_non_array_plan(self):
        code, out, _ = self._plan([])
        self.assertEqual(code, 2)
        plan_path = self.run_root / "chunk-plan.json"
        plan_path.write_text('{"chunks": []}', encoding="utf-8")
        code, out, _ = run_helper("chunks-write", {"run_root": str(self.run_root),
                                                   "plan_file": str(plan_path),
                                                   "anchor": self.anchor})
        self.assertEqual(code, 2)

    def test_import_rejects_bad_schema_and_intra_chunk_duplicate(self):
        plan_path = self.run_root / "chunk-plan.json"
        cases = [
            ("loc_est 超限", [{"id": "chunk-0", "files": ["a.py"], "loc_est": 5001, "neighbors": [], "rationale": "r"}]),
            ("块内文件重复", [{"id": "chunk-0", "files": ["a.py", "a.py"], "loc_est": 1, "neighbors": [], "rationale": "r"}]),
            ("块 ID 非法", [{"id": "block-0", "files": ["a.py"], "loc_est": 1, "neighbors": [], "rationale": "r"}]),
        ]
        for label, chunks in cases:
            plan_path.write_text(json.dumps(chunks), encoding="utf-8")
            code, out, _ = self._plan(chunks)
            self.assertEqual(code, 2, f"{label} 应 exit 2：{out}")


class MarkerConcurrencyTest(PrecheckIndexTestBase):
    """复核者探针转正（FIX05-C）：两真实进程对不同块 mark——per-chunk 唯一键文件
    下并发成功数 == 恢复项数，不再有读-改-原子替换互覆丢写入。"""

    def setUp(self):
        super().setUp()
        self.rels = self.write_tree(9)
        self.scan, self.anchor, self.chunk_ids = self.setup_full_pipeline(self.rels, per=3)

    def _spawn_mark(self, chunk_id, result=False):
        payload = {"run_root": str(self.run_root), "action": "mark",
                   "chunk_id": chunk_id, "generation": 1}
        if result:
            path = self.run_root / "chunks" / f"{chunk_id}.json"
            path.parent.mkdir(exist_ok=True)
            path.write_text("{}", encoding="utf-8")
            payload["result_path"] = str(path)
        return subprocess.Popen(
            [sys.executable, str(PRECHECK), "g2-progress", "--json", json.dumps(payload)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def _read_progress(self):
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root),
                                                  "action": "read", "generation": 1})
        self.assertEqual(code, 0, out)
        return out

    def test_two_processes_mark_different_chunks_no_loss(self):
        pa, pb = self._spawn_mark(self.chunk_ids[0], result=True), self._spawn_mark(self.chunk_ids[1], result=True)
        oa, ob = pa.communicate(), pb.communicate()
        self.assertEqual(pa.returncode, 0, oa[1])
        self.assertEqual(pb.returncode, 0, ob[1])
        # 两进程都 ok=true → completed 列表必须恰 2 条（成功确认数 == 可恢复项数）
        self.assertEqual(self._read_progress()["completed_count"], 2)
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "list",
                                                  "generation": 1, "page": 0, "page_size": 10})
        self.assertEqual(sorted(out["ids"]), sorted(self.chunk_ids[:2]))
        # 领取跳过两者；锚与标记元数据一致
        code, page, _ = run_helper("chunks-page", {"run_root": str(self.run_root), "page": 0,
                                                   "page_size": 10, "anchor": self.anchor})
        self.assertEqual(code, 0, page)
        self.assertEqual(page["completed"], 2)
        self.assertNotIn(self.chunk_ids[0], [c["id"] for c in page["chunks"]])
        self.assertNotIn(self.chunk_ids[1], [c["id"] for c in page["chunks"]])

    def test_repeated_mark_same_id_is_idempotent(self):
        for expected_idempotent in (False, True):
            proc = self._spawn_mark(self.chunk_ids[2])
            out, err = proc.communicate()
            self.assertEqual(proc.returncode, 0, err)
            self.assertEqual(json.loads(out)["idempotent"], expected_idempotent)
        self.assertEqual(self._read_progress()["completed_count"], 1)

    def test_mark_unknown_chunk_rejected(self):
        proc = self._spawn_mark("chunk-ghost")
        out, err = proc.communicate()
        self.assertEqual(proc.returncode, 2)


class TwelveThousandChunkBoundsTest(PrecheckIndexTestBase):
    """复核者探针转正（FIX05-7.1）：12,000 块 ID 元数据 init→read 全程成功且恒有界。
    旧单体检查点 init 写 264,192 字节后被自身 200KB 读取上限拒绝——init/read 边界
    不一致；per-chunk 标记 + O(1) 元数据下 init/read/mark/list 同界。"""

    def test_12k_init_read_mark_list_stay_bounded(self):
        ids = ["chunk-%08d" % i for i in range(12000)]
        plan = [{"id": cid, "files": ["m/f.py"], "loc_est": 1,
                 "neighbors": [], "rationale": "probe"} for cid in ids]
        plan_path = self.run_root / "chunk-plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        code, out, _ = run_helper("chunks-write", {"run_root": str(self.run_root),
                                                   "plan_file": str(plan_path),
                                                   "anchor": "a" * 64})
        self.assertEqual(code, 0, out)
        self.assertEqual(out["chunk_count"], 12000)
        # init：只登记 O(1) 元数据（不再持久化 chunk_order 全表）
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "init",
                                                  "source_anchor": "b" * 64, "generation": 1})
        self.assertEqual(code, 0, out)
        self.assertEqual(out["total"], 12000)
        meta_path = self.run_root / "g2" / "1" / "meta.json"
        self.assertLess(meta_path.stat().st_size, 512, "标记元数据必须 O(1)")
        # read：12,000 块下回执仍是 O(1)（旧实现此处被 200KB 上限拒绝）
        proc = subprocess.run(
            [sys.executable, str(PRECHECK), "g2-progress", "--json",
             json.dumps({"run_root": str(self.run_root), "action": "read", "generation": 1})],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["exists"])
        self.assertEqual(payload["total"], 12000)
        self.assertEqual(payload["completed_count"], 0)
        self.assertLess(len(proc.stdout), 1024, "read 回执必须 O(1)，不得携带块 ID 全表")
        # mark/list 在 12k 规模可用且各自的回执同样有界
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "mark",
                                                  "chunk_id": "chunk-00000000", "generation": 1})
        self.assertEqual(code, 0, out)
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "list",
                                                  "generation": 1, "page": 0, "page_size": 100})
        self.assertEqual(code, 0, out)
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["ids"], ["chunk-00000000"])
        # mark 的响应同样不携带全表
        self.assertLess(len(json.dumps(out)), 512)


class ResumeClosureTest(PrecheckIndexTestBase):
    """续接闭合（FIX05-7.3）：中断 3 块（结果已落盘+标记）→ 续跑补 2 块 →
    聚合 = 历史 + 新结果 = 5 块无重无漏。"""

    def setUp(self):
        super().setUp()
        self.rels = self.write_tree(15)
        self.scan, self.anchor, self.chunk_ids = self.setup_full_pipeline(self.rels, per=3)
        # 5 块，每块 3 文件

    def _write_result(self, cid):
        chunk = next(c for c in json.loads(
            (self.run_root / "chunk-plan.json").read_text(encoding="utf-8")) if c["id"] == cid)
        body = {"meta": {"chunk_id": cid, "author": "A2"}, "modules": [], "edges": [],
                "findings": [], "coverage": {"files_claimed": len(chunk["files"]),
                                             "files_analyzed": len(chunk["files"]),
                                             "analyzed_files": chunk["files"], "gaps": []}}
        path = self.run_root / "chunks" / f"{cid}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(body), encoding="utf-8")
        return path, body

    def _mark(self, cid):
        path, _ = self._write_result(cid)
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "mark",
                                                  "chunk_id": cid, "generation": 1,
                                                  "result_path": str(path)})
        self.assertEqual(code, 0, out)

    def _reload_completed(self):
        """恢复者视角：标记列举分页 + 逐块 read-report 回载（DWF 同构）。"""
        restored, page = [], 0
        while True:
            code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "list",
                                                      "generation": 1, "page": page, "page_size": 2})
            self.assertEqual(code, 0, out)
            for cid in out["ids"]:
                code, insp, _ = run_helper("read-report", {
                    "path": str(self.run_root / "chunks" / f"{cid}.json"),
                    "within_root": str(self.run_root)})
                self.assertEqual(code, 0, insp)
                restored.append(json.loads(insp["body"]))
            if len(out["ids"]) < 2:
                return restored
            page += 1

    def test_interrupt_3_resume_2_aggregates_5(self):
        # —— 第一次执行：3 块完成（A2 结果落盘 + 标记），进程消失 ——
        done_ids = self.chunk_ids[:3]
        for cid in done_ids:
            self._mark(cid)
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "read",
                                                  "generation": 1})
        self.assertEqual(out["completed_count"], 3)
        # —— 续跑：历史块结果逐块回载（分页标记列举 + read-report）——
        restored = self._reload_completed()
        self.assertEqual([r["meta"]["chunk_id"] for r in restored], done_ids)
        # —— 领取只含剩余 2 块；新块结果继续落盘 + 标记 ——
        code, page_out, _ = run_helper("chunks-page", {"run_root": str(self.run_root), "page": 0,
                                                       "page_size": 10, "anchor": self.anchor})
        self.assertEqual([c["id"] for c in page_out["chunks"]], self.chunk_ids[3:])
        fresh = []
        for cid in self.chunk_ids[3:]:
            self._mark(cid)
            fresh.append(json.loads(
                (self.run_root / "chunks" / f"{cid}.json").read_text(encoding="utf-8")))
        # —— 聚合闭合：历史 3 + 新 2 = 全集 5，无重无漏 ——
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "read",
                                                  "generation": 1})
        self.assertEqual(out["completed_count"], 5)
        all_results = restored + fresh
        self.assertEqual(sorted(r["meta"]["chunk_id"] for r in all_results), sorted(self.chunk_ids))
        self.assertEqual(len({r["meta"]["chunk_id"] for r in all_results}), 5)

    def test_anchor_change_refuses_mixed_generation(self):
        self._mark(self.chunk_ids[0])
        # 标记锚与请求锚不符 → chunks-page 混代拒绝
        code, out, _ = run_helper("chunks-page", {"run_root": str(self.run_root), "page": 0,
                                                  "page_size": 3, "anchor": "0" * 64})
        self.assertEqual(code, 3)
        # init 换锚 → 混代拒绝
        code, out, _ = run_helper("g2-progress", {"run_root": str(self.run_root), "action": "init",
                                                  "source_anchor": "c" * 64, "generation": 1})
        self.assertEqual(code, 3)
        # 台账层：源锚变化 → stale_anchor
        code, out, _ = run_helper("resume-register", {
            "run_root": str(self.run_root), "kind": "custom", "activity_id": "g2-resume",
            "checkpoint_path": str(self.run_root / "g2" / "1" / "meta.json"),
            "schema_version": 1, "source_anchor": self.anchor})
        self.assertEqual(code, 0, out)
        code, out, _ = run_helper("resume-check", {"run_root": str(self.run_root), "kind": "custom",
                                                   "activity_id": "g2-resume", "current_anchor": "d" * 64})
        self.assertEqual(code, 0)
        self.assertEqual(out["condition"], "stale_anchor")


class AcquireResumeTest(PrecheckIndexTestBase):
    """acquire 显式续接语义（FIX05-7.3）：有效回执+同源 → adopt；无回执/异源 →
    拒绝（新建排他保护不放松）；目录不存在 → 正常排他创建。"""

    def _acquire(self, run_id="fixed-run", resume=None):
        payload = {"source_root": str(self.source), "run_root_parent": str(self.base / "runs"),
                   "run_id": run_id}
        if resume is not None:
            payload["resume"] = resume
        return run_helper("acquire", payload)

    def test_fresh_then_adopt_keeps_original_receipt(self):
        code, first, _ = self._acquire()
        self.assertEqual(code, 0)
        self.assertEqual(first["mode"], "mkdir_exclusive")
        code, second, _ = self._acquire(resume=True)
        self.assertEqual(code, 0, second)
        self.assertEqual(second["mode"], "adopt")
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertTrue(second["resumed_at"])
        # 首笔回执不被续接改写
        on_disk = json.loads((self.base / "runs" / "fixed-run" / "precheck.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["mode"], "mkdir_exclusive")
        # 续接后制品保留：写入文件仍在
        (self.base / "runs" / "fixed-run" / "keep.txt").write_text("x", encoding="utf-8")
        code, third, _ = self._acquire(resume=True)
        self.assertEqual(code, 0)
        self.assertTrue((self.base / "runs" / "fixed-run" / "keep.txt").exists())

    def test_existing_without_resume_rejected(self):
        code, _, _ = self._acquire()
        self.assertEqual(code, 0)
        code, out, _ = self._acquire()
        self.assertEqual(code, 3)
        self.assertEqual(out["kind"], "conflict")
        self.assertIn("resume=true", out["error"])

    def test_existing_without_valid_receipt_rejected_even_with_resume(self):
        self._acquire()
        (self.base / "runs" / "fixed-run" / "precheck.json").unlink()
        code, out, _ = self._acquire(resume=True)
        self.assertEqual(code, 3)
        self.assertEqual(out["kind"], "conflict")

    def test_adopt_rejects_foreign_source(self):
        self._acquire()
        receipt_path = self.base / "runs" / "fixed-run" / "precheck.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["roots"]["source_root"]["realpath"] = str(self.base / "elsewhere")
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        code, out, _ = self._acquire(resume=True)
        self.assertEqual(code, 3)
        self.assertIn("其他源", out["error"])

    def test_resume_flag_false_creates_fresh(self):
        code, out, _ = self._acquire(resume=False)
        self.assertEqual(code, 0)
        self.assertEqual(out["mode"], "mkdir_exclusive")

    def test_resume_flag_must_be_bool(self):
        code, out, _ = self._acquire(resume="yes")
        self.assertEqual(code, 2)
        self.assertEqual(out["kind"], "argument")


if __name__ == "__main__":
    unittest.main()

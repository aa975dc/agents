"""L 档（10 万条目）容量与资源保护验收基准（SC01/SC05/SC09/FS05）。

独立长跑用例，不进常规回归：L_TIER 环境变量门——未设置 L_TIER=1 时全部 skip
（NOT_RUN，打印原因），`python3 -m unittest discover -s tests` 只会看到 skip，
不会执行任何基准步骤。手动运行：

    L_TIER=1 python3 -m unittest tests.benchmarks.test_l_tier -v

资源授权（09_TEST_AND_BENCHMARK_ACCEPTANCE.md §5）：fixture 只生成在
tempfile.gettempdir() 下 benchmark-fixture 前缀目录（fixture_gen.py 强制校验），
先 dry-run 估算后实跑，写入 ≤2GiB 限额；清理只认 BENCHMARK_FIXTURE marker +
manifest.json 一致的目录。

实测面（全部真实执行，原始数字经 L-TIER-RESULT 行输出）：
- SC05/SC09：P3-01 scanner 10 万文件全量普查耗时与批次行数；子进程 ru_maxrss
  峰值 ≤512MiB（扫描前/中/后采样）。
- SC01：reader keyset 分页 100 条 p50/p95（30 次重复，perf_counter，温热）；
  dev-companion status 对小型真实项目（50 文件）作对照记录（含解释器启动）。
- C08/增量：改 1% 文件后二次扫描 reused/computed 计数。
- FS05：扫描中 kill -9 子进程 → 重开后旧代完整可读、半代作废、重扫接管。

步骤类按字母序执行（TestStep1..TestStep5 共享同一 fixture 与事实库，进程内
单例 _BENCH）；单跑某一步会自动补齐其前置扫描。
"""
import atexit
import json
import math
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGES = REPO_ROOT / "packages"
SCRIPTS = REPO_ROOT / "dev-companion" / "scripts"
FIXTURE_GEN = Path(__file__).resolve().parent / "fixture_gen.py"

L_TIER = os.environ.get("L_TIER") == "1"
SKIP_REASON = ("NOT_RUN：未设置 L_TIER=1 —— L 档（10 万条目）长跑基准不进常规 discover；"
               "手动运行 L_TIER=1 python3 -m unittest tests.benchmarks.test_l_tier -v")

if not L_TIER:
    print(SKIP_REASON, file=sys.stderr)
else:
    sys.path.insert(0, str(PACKAGES))
    from agents_kernel import digest
    from agents_kernel.indexing import content_hash, reader
    from agents_kernel.storage import db

MIB = 1024 * 1024
RSS_LIMIT_BYTES = 512 * MIB          # 09 §3：L 档索引 worker 峰值 RSS 目标
QUERY_SLO_SECONDS = 2.0              # 09 §3：L 档温热 status 首页 100 条 p95 目标
QUERY_REPS = 30                      # 09 §3：至少重复 30 次
PAGE_SIZE = 100
L_TIER_FILES = 100000
KILL_STAGING_THRESHOLD = 15000       # kill 前 staging 至少落库的半代行数
CHILD_SCAN_CODE = r'''
import json, resource, sys, threading, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from agents_kernel.indexing import content_hash, scanner
from agents_kernel.storage import db
tree, store_path, mode = sys.argv[2], sys.argv[3], sys.argv[4]

def rss():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

peak = [0]
stop = threading.Event()
def sampler():
    while not stop.is_set():
        peak[0] = max(peak[0], rss())
        time.sleep(0.05)
baseline = rss()
watcher = threading.Thread(target=sampler, daemon=True)
watcher.start()
store = db.Store(Path(store_path))
store.open()
writer = db.acquire_writer(store)
started = time.perf_counter()
hook = content_hash.ContentHasher() if mode == "hash" else None
result = scanner.IndexScanner(store).scan(tree, writer, hash_hook=hook)
elapsed = time.perf_counter() - started
writer.close()
stop.set()
watcher.join()
rss_unit = 1 if sys.platform == "darwin" else 1024  # macOS=字节，Linux=KiB
print("L-TIER-SCAN " + json.dumps({
    "generation": result.generation, "mode": result.mode,
    "file_count": result.file_count, "batch_count": result.batch_count,
    "hash_reused": result.hash_reused, "hash_computed": result.hash_computed,
    "elapsed_seconds": round(elapsed, 3),
    "rss_baseline_bytes": baseline * rss_unit,
    "rss_peak_bytes": max(peak[0], rss()) * rss_unit,
    "rss_unit_note": "macOS ru_maxrss 单位为字节" if sys.platform == "darwin"
                     else "Linux ru_maxrss 单位为 KiB，已换算为字节"}))
store.close()
'''


def percentile(samples, quantile):
    """最近位次法分位数（样本数 30）。"""
    ordered = sorted(samples)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[rank - 1]


def record(step, payload):
    """原始结果账本：stdout 打 L-TIER-RESULT 行（验收文档直接引用）。"""
    print("\nL-TIER-RESULT %s %s" % (step, json.dumps(payload, ensure_ascii=False, sort_keys=True)))
    _BENCH.records[step] = payload


class _Bench:
    """进程内单例：fixture + 事实库 + 结果账本；步骤类共享（按类名字母序）。"""

    def __init__(self):
        self.records = {}
        self.fixture_dir = Path(tempfile.gettempdir()) / ("benchmark-fixture-l-tier-%d" % os.getpid())
        self.scratch = Path(tempfile.mkdtemp(prefix="l-tier-scratch-"))
        self.db_path = self.scratch / "facts.sqlite"
        self.manifest = None
        self.dry_run_line = None
        self.generate_line = None
        self.store = None
        atexit.register(self.cleanup)

    # ---- fixture（授权流程：先 dry-run 后实跑，产物带 marker+manifest） ----

    def build_fixture(self):
        self.dry_run_line = self._fixture_gen(["--dry-run"])
        self.generate_line = self._fixture_gen([])
        self.manifest = json.loads((self.fixture_dir / "manifest.json").read_text(encoding="utf-8"))
        # 扫描根即 fixture 根：marker 与 manifest.json 也是树内文件，扫描器如实入账
        self.expected_paths = {str(path.relative_to(self.fixture_dir))
                               for path in self.fixture_dir.rglob("*") if path.is_file()}
        if len(self.expected_paths) != self.manifest["total_files"] + 2:
            raise AssertionError("fixture 自检失败：盘上文件 %d != manifest %d + 2"
                                 % (len(self.expected_paths), self.manifest["total_files"]))
        record("fixture", {"dir": str(self.fixture_dir), "dry_run": self.dry_run_line,
                           "generate": self.generate_line, "manifest": self.manifest,
                           "on_disk_files": len(self.expected_paths)})

    def _fixture_gen(self, extra):
        command = [sys.executable, str(FIXTURE_GEN), str(self.fixture_dir),
                   "--files", str(L_TIER_FILES)] + extra
        done = subprocess.run(command, capture_output=True, text=True, timeout=3600)
        if done.returncode != 0:
            raise AssertionError("fixture_gen 失败 rc=%s：%s%s" % (done.returncode, done.stdout, done.stderr))
        return done.stdout.strip().splitlines()[-1]

    def build_control_fixture(self, files=50):
        directory = self.scratch / ("benchmark-fixture-control-%d" % files)
        done = subprocess.run([sys.executable, str(FIXTURE_GEN), str(directory),
                               "--files", str(files), "--big-files", "0"],
                              capture_output=True, text=True, timeout=600)
        if done.returncode != 0:
            raise AssertionError("对照 fixture 生成失败：%s%s" % (done.stdout, done.stderr))
        return directory

    # ---- 存储与扫描 ----

    def ensure_store(self):
        if self.store is None:
            self.store = db.Store(self.db_path)
            self.store.open()
        return self.store

    def scan_child(self, mode="census", timeout=3600):
        done = subprocess.run(
            [sys.executable, "-c", CHILD_SCAN_CODE, str(PACKAGES),
             str(self.fixture_dir), str(self.db_path), mode],
            capture_output=True, text=True, timeout=timeout)
        for line in done.stdout.splitlines():
            if line.startswith("L-TIER-SCAN "):
                return json.loads(line[len("L-TIER-SCAN "):]), done
        raise AssertionError("扫描子进程未产出结果 rc=%s stderr=%s"
                             % (done.returncode, done.stderr[-2000:]))

    def ensure_generation(self):
        """步骤类可单跑：库中无完整世代时先补一轮普查扫描。"""
        self.ensure_store()
        if reader.IndexReader(self.store).latest_complete() is None:
            result, _ = self.scan_child("census")
            record("precondition_scan", result)
        return reader.IndexReader(self.store).latest_complete()

    # ---- 清理（09 §5：只删 marker+manifest 一致的本目录） ----

    def cleanup(self):
        if self.store is not None:
            self.store.close()
            self.store = None
        marker = self.fixture_dir / "BENCHMARK_FIXTURE"
        manifest_path = self.fixture_dir / "manifest.json"
        if marker.is_file() and manifest_path.is_file():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except ValueError:
                data = {}
            if data.get("marker") == "BENCHMARK_FIXTURE" and "benchmark-fixture-" in self.fixture_dir.name:
                shutil.rmtree(self.fixture_dir, ignore_errors=True)
        shutil.rmtree(self.scratch, ignore_errors=True)


_BENCH = None


def bench():
    global _BENCH
    if _BENCH is None:
        _BENCH = _Bench()
        _BENCH.build_fixture()
        _BENCH.ensure_store()
    return _BENCH


def gated(testcase):
    if not L_TIER:
        raise unittest.SkipTest(SKIP_REASON)


class TestStep1_FullScanAndRss(unittest.TestCase):
    """SC05/SC09：10 万文件全量普查 + 子进程 RSS 峰值。"""

    @classmethod
    def setUpClass(cls):
        gated(cls)
        b = bench()
        cls.bench = b
        cls.expected = len(b.expected_paths)
        cls.scan, cls.raw = b.scan_child("census")
        record("step1_full_scan", {"child": cls.scan, "returncode": cls.raw.returncode,
                                   "throughput_files_per_second":
                                       round(cls.expected / cls.scan["elapsed_seconds"], 1),
                                   "parent_rss_peak_bytes":
                                       _rss_bytes() if L_TIER else None})

    def test_scan_row_count_matches_manifest(self):
        self.assertEqual(self.scan["file_count"], self.expected)
        rows = self.bench.store.query_all("SELECT path FROM files")
        self.assertEqual(len(rows), self.expected)
        self.assertEqual({row["path"] for row in rows}, self.bench.expected_paths)
        generations = {row["g"] for row in self.bench.store.query_all(
            "SELECT DISTINCT generation AS g FROM files")}
        self.assertEqual(generations, {self.scan["generation"]})

    def test_batches_committed_and_progress_visible(self):
        self.assertGreater(self.scan["batch_count"], 0)
        per_batch = self.expected / self.scan["batch_count"]
        self.assertLessEqual(per_batch, 5000)  # DEFAULT_BATCH_SIZE 上界

    def test_spot_check_rows_against_filesystem(self):
        rows = self.bench.store.query_all(
            "SELECT path, size, mtime_ns FROM files LIMIT 200")
        self.assertEqual(len(rows), 200)
        for row in rows:
            info = (self.bench.fixture_dir / row["path"]).stat()
            self.assertEqual(info.st_size, row["size"])
            self.assertEqual(info.st_mtime_ns, row["mtime_ns"])

    def test_rss_peak_within_512mib(self):
        self.assertLessEqual(self.scan["rss_peak_bytes"], RSS_LIMIT_BYTES)


class TestStep2_ReaderLatency(unittest.TestCase):
    """SC01(a)：reader keyset 分页 100 条，30 次重复温热 p50/p95。"""

    @classmethod
    def setUpClass(cls):
        gated(cls)
        cls.bench = bench()
        cls.generation = cls.bench.ensure_generation()
        cls.reader = reader.IndexReader(cls.bench.store)
        cls.expected = len(cls.bench.expected_paths)

    def test_warm_page100_p50_p95(self):
        for _ in range(3):  # 预热（温热口径）
            self.reader.page_files(limit=PAGE_SIZE)
        samples = []
        for _ in range(QUERY_REPS):
            started = time.perf_counter()
            page = self.reader.page_files(limit=PAGE_SIZE)
            samples.append(time.perf_counter() - started)
            self.assertEqual(len(page.rows), PAGE_SIZE)
            self.assertEqual(page.generation, self.generation)
        p50, p95 = percentile(samples, 0.50), percentile(samples, 0.95)
        record("step2_reader_page100", {"reps": QUERY_REPS, "page_size": PAGE_SIZE,
                                        "p50_s": round(p50, 6), "p95_s": round(p95, 6),
                                        "min_s": round(min(samples), 6),
                                        "max_s": round(max(samples), 6),
                                        "samples_s": [round(s, 6) for s in samples]})
        self.assertLessEqual(p95, QUERY_SLO_SECONDS)

    def test_count_from_manifest_not_table_scan(self):
        self.assertEqual(self.reader.count_files(), self.expected)

    def test_full_keyset_iteration_covers_all_rows(self):
        counted = sum(1 for _ in self.reader.iter_files(limit=500))
        self.assertEqual(counted, self.expected)


class TestStep3_CompanionStatusControl(unittest.TestCase):
    """SC01(b)：dev-companion status 对 50 文件小型真实项目的对照记录。"""

    @classmethod
    def setUpClass(cls):
        gated(cls)
        cls.bench = bench()
        fixture = cls.bench.build_control_fixture(files=50)
        cls.project = cls.bench.scratch / "control-project"
        cls.project.mkdir()
        shutil.copytree(fixture, cls.project, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("BENCHMARK_FIXTURE", "manifest.json"))
        scope = {"title": "L 档对照项目", "goal": "status 对照", "audience": "验收", "scenario": "基准",
                 "out_of_scope": [], "assumptions": [],
                 "features": [{"id": "probe", "title": "探针", "acceptance_criteria": ["命令可运行"],
                               "allowed_paths": ["probe.py"], "requires_user_acceptance": False,
                               "check_commands": [[sys.executable, "-c", "assert True"]]}]}
        scope_path = cls.bench.scratch / "control-scope.json"
        scope_path.write_text(json.dumps(scope, ensure_ascii=False), encoding="utf-8")
        cls.companion = [sys.executable, str(SCRIPTS / "companion.py"), "--project", str(cls.project)]
        subprocess.run(cls.companion + ["init", "--input", str(scope_path)],
                       check=True, capture_output=True, text=True, timeout=300)
        subprocess.run(cls.companion + ["confirm", "--revision", "1"],
                       check=True, capture_output=True, text=True, timeout=300)

    def test_status_thirty_timed_runs(self):
        first = subprocess.run(self.companion + ["status", "--format", "json"],
                               capture_output=True, text=True, check=True, timeout=300)
        self.assertIn('"confirmed": true', first.stdout)
        self.assertIn('"source_fingerprint"', first.stdout)
        samples = []
        for _ in range(QUERY_REPS):
            started = time.perf_counter()
            done = subprocess.run(self.companion + ["status", "--format", "json"],
                                  capture_output=True, text=True, check=True, timeout=300)
            samples.append(time.perf_counter() - started)
            self.assertIn('"source_fingerprint"', done.stdout)
        p50, p95 = percentile(samples, 0.50), percentile(samples, 0.95)
        record("step3_companion_status_control", {
            "project_files": 50, "reps": QUERY_REPS, "p50_s": round(p50, 6),
            "p95_s": round(p95, 6), "min_s": round(min(samples), 6),
            "max_s": round(max(samples), 6),
            "note": "subprocess 每次含解释器启动；status 恒为 live（跨进程无缓存）",
            "samples_s": [round(s, 6) for s in samples]})


class TestStep4_IncrementalHash(unittest.TestCase):
    """C08/SC05 增量：改 1% 文件后二次扫描 reused/computed（预期 ~99010/1000）。"""

    @classmethod
    def setUpClass(cls):
        gated(cls)
        cls.bench = bench()
        cls.generation = cls.bench.ensure_generation()
        cls.expected = len(cls.bench.expected_paths)
        all_py = sorted(str(path.relative_to(cls.bench.fixture_dir))
                        for path in cls.bench.fixture_dir.rglob("*.py"))
        cls.changed = all_py[::100]                      # 每 100 个取 1 个 ≈ 1%
        changed_set = set(cls.changed)
        cls.unchanged_sample = [path for path in all_py[1::997]
                                if path not in changed_set][:200]  # 不相交集抽样
        store = cls.bench.store
        writer = db.acquire_writer(store)
        try:
            cls.backfill = content_hash.backfill_hashes(store, writer)  # 首轮：待哈希全补
        finally:
            writer.close()
        cls.old_sha = {row["path"]: row["sha256"] for row in store.query_all(
            "SELECT path, sha256 FROM files")}
        for rel in cls.changed:  # 内容变更：追加一行，size+mtime 变化
            target = cls.bench.fixture_dir / rel
            with open(target, "a", encoding="utf-8") as handle:
                handle.write("# changed-for-l-tier-incremental\n")
        cls.scan, cls.raw = cls.bench.scan_child("hash")
        record("step4_incremental_hash", {"backfill_first_round": {
            "hashed": cls.backfill.hashed, "skipped": cls.backfill.skipped,
            "batches": cls.backfill.batches},
            "changed_files": len(cls.changed), "child": cls.scan,
            "throughput_files_per_second":
                round(len(cls.bench.expected_paths) / cls.scan["elapsed_seconds"], 1),
            "returncode": cls.raw.returncode})

    def test_reused_and_computed_counts(self):
        self.assertEqual(self.scan["hash_computed"], len(self.changed))
        self.assertEqual(self.scan["hash_reused"], self.expected - len(self.changed))

    def test_row_count_stable_and_generation_advanced(self):
        self.assertEqual(self.scan["file_count"], self.expected)
        self.assertGreater(self.scan["generation"], self.generation)

    def test_changed_files_rehashed_and_others_reused(self):
        rows = {row["path"]: row["sha256"] for row in self.bench.store.query_all(
            "SELECT path, sha256 FROM files")}
        for rel in self.changed:
            actual = digest.sha256_file(str(self.bench.fixture_dir / rel))[0]
            self.assertEqual(rows[rel], actual)
            self.assertNotEqual(rows[rel], self.old_sha[rel])
        for rel in self.unchanged_sample:
            self.assertEqual(rows[rel], self.old_sha[rel])

    def test_hash_path_rss_within_512mib(self):
        self.assertLessEqual(self.scan["rss_peak_bytes"], RSS_LIMIT_BYTES)


class TestStep5_KillRecovery(unittest.TestCase):
    """FS05：扫描中 kill -9 子进程 → 旧代完整可读、半代作废、重扫接管。"""

    @classmethod
    def setUpClass(cls):
        gated(cls)
        cls.bench = bench()
        cls.generation = cls.bench.ensure_generation()
        cls.expected = len(cls.bench.expected_paths)

    def test_kill9_mid_scan_then_recover(self):
        store = self.bench.store
        process = subprocess.Popen(
            [sys.executable, "-c", CHILD_SCAN_CODE, str(PACKAGES),
             str(self.bench.fixture_dir), str(self.bench.db_path), "census"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        killed_generation = self.generation + 1
        deadline = time.time() + 600
        staged = 0
        while time.time() < deadline:
            if process.poll() is not None:
                self.fail("子进程在 kill 前已自行退出 rc=%s（环境异常）" % process.returncode)
            row = store.query_one(
                "SELECT COUNT(*) AS c FROM files_staging WHERE generation = ?",
                (killed_generation,))
            staged = row["c"] if row is not None else 0
            if staged >= KILL_STAGING_THRESHOLD:
                break
            time.sleep(0.05)
        else:
            process.kill()
            self.fail("600s 内半代 staging 未达 %d 行，无法落在扫描中" % KILL_STAGING_THRESHOLD)
        os.kill(process.pid, signal.SIGKILL)
        process.wait()
        process.stdout.close()
        process.stderr.close()
        self.assertEqual(process.returncode, -signal.SIGKILL)

        # 重开后：旧代完整可读，半代只在 staging，files 未被污染
        live = reader.IndexReader(store)
        self.assertEqual(live.latest_complete(), self.generation)
        self.assertEqual(live.count_files(), self.expected)
        page = live.page_files(limit=PAGE_SIZE)
        self.assertEqual(len(page.rows), PAGE_SIZE)
        self.assertEqual({row["g"] for row in store.query_all(
            "SELECT DISTINCT generation AS g FROM files")}, {self.generation})
        status_row = store.query_one(
            "SELECT status FROM scan_generations WHERE generation = ?",
            (killed_generation,))
        self.assertIsNotNone(status_row)
        self.assertEqual(status_row["status"], "active")

        # 重扫接管：新代作废半代并清 staging，发布完整新快照
        result, raw = self.bench.scan_child("census")
        record("step5_kill_recovery", {
            "killed_child_returncode": process.returncode,
            "staging_rows_at_kill": staged,
            "killed_generation_status": "active",
            "rescan": result, "rescan_returncode": raw.returncode})
        self.assertEqual(result["generation"], killed_generation + 1)
        self.assertEqual(result["file_count"], self.expected)
        self.assertEqual(store.query_one(
            "SELECT status FROM scan_generations WHERE generation = ?",
            (killed_generation,))["status"], "abandoned")
        self.assertEqual(store.query_one(
            "SELECT COUNT(*) AS c FROM files_staging")["c"], 0)
        self.assertEqual({row["g"] for row in store.query_all(
            "SELECT DISTINCT generation AS g FROM files")}, {result["generation"]})
        self.assertEqual(reader.IndexReader(store).count_files(), self.expected)


def _rss_bytes():
    """本进程峰值 RSS（macOS ru_maxrss 单位字节，Linux 为 KiB，统一换算字节）。"""
    unit = 1 if sys.platform == "darwin" else 1024
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit


if __name__ == "__main__":
    unittest.main()

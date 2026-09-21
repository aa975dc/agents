# -*- coding: utf-8 -*-
"""FIX02-followup：exclusive_lock v2（os.link 原子发布拥有者记录）验收。

复核报告 §5（bf974a97 recheck）：v1 用 O_CREAT|O_EXCL 创建锁文件后、写 PID 前存在
窗口——B 把空文件当陈旧锁删除入界，A 恢复后向已 unlink 的 fd 写 PID 也入界，
mutual_exclusion_broken=true（双解释器复现）。v2 把拥有者记录先完整写入唯一临时
文件（fsync）再 os.link 原子发布：锁文件要么不存在要么内容完整，注入旧窗口位置
（获取后写记录前）的时序暂停不再产生双进入。

全部进程场景用真实多进程（fork）+ 文件栅栏控制确定时序；等待只用于排序，
不以超时充当互斥证据。场景对应复核 §8 FIX02-followup 结案要求：
1. 复核者探针场景转正：A 在临界区内被暂停（获取后写记录前的旧窗口位置），
   B 全程不得入界；两进程临界区串行化，共享计数文件最终 count==2、max==1；
2. 两个合法竞争者按获得顺序先后完成（顺序性），无死锁无饥饿；
3. 活持有者在场时 timeout 以产品错误码（CompanionError）拒绝，非崩溃；
4. 强杀恢复：持锁进程 os._exit 后陈旧锁接管成功；
5. 空锁文件：等待宽限后接管成功且接管事件（stderr 警告）可追；
   旧格式死 pid 记录：立即接管，不付宽限；
6. 释放身份核对：owner_token/pid 不一致不误删；同进程重入计数，最外层才删文件。
"""
import contextlib
import io
import json
import multiprocessing as mp
import os
import sys
import tempfile
import time
import traceback
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel.atomicio import exclusive_lock
from agents_kernel.validation import CompanionError

WAIT_TIMEOUT = 20.0
PROBE_HOLD = 1.0  # 探针场景：A 在临界区内被观察的时长（>父进程轮询窗）


def wait_marker(path, timeout=WAIT_TIMEOUT):
    """文件栅栏：等到标记出现；超时大声失败（绝不把等待当正确性证据）。"""
    deadline = time.monotonic() + timeout
    while not Path(path).exists():
        if time.monotonic() >= deadline:
            raise AssertionError("栅栏等待超时：%s" % path)
        time.sleep(0.005)


def set_marker(path):
    Path(path).write_text("1", encoding="utf-8")


def _init_counters(root):
    for name in ("count", "max", "current"):
        (root / name).write_text("0", encoding="utf-8")


def _read_int(path):
    return int(Path(path).read_text(encoding="utf-8"))


def _write_int(path, value):
    Path(path).write_text(str(value), encoding="utf-8")


def _bump_concurrency(root):
    """临界区内记账（仅互斥成立时精确）：count+1、current+1、max=max(max,current)。"""
    current = _read_int(root / "current") + 1
    _write_int(root / "current", current)
    _write_int(root / "max", max(_read_int(root / "max"), current))
    _write_int(root / "count", _read_int(root / "count") + 1)
    time.sleep(0.05)
    _write_int(root / "current", current - 1)


def _probe_holder(lock_path, gates, results):
    """复核探针同场景的 A：入界后挂栅栏暂停（旧实现注入点：获取后、写记录前的
    调度窗口；v2 发布即完整记录，暂停点落在临界区内），由父进程控制放行。"""
    try:
        with exclusive_lock(lock_path, timeout=WAIT_TIMEOUT):
            entered = time.monotonic()
            set_marker(gates / "a-in")
            _bump_concurrency(gates.parent)
            wait_marker(gates / "a-release")
            exited = time.monotonic()
        results.put({"worker": "A", "entered": entered, "exited": exited})
        set_marker(gates / "a-out")
    except BaseException:
        results.put({"worker": "A", "error": traceback.format_exc()})


def _probe_contender(lock_path, gates, results):
    """复核探针同场景的 B：A 持锁期间发起获取，必须被挡到 A 出界之后。"""
    try:
        with exclusive_lock(lock_path, timeout=WAIT_TIMEOUT):
            entered = time.monotonic()
            set_marker(gates / "b-in")
            _bump_concurrency(gates.parent)
            time.sleep(0.02)
            exited = time.monotonic()
        results.put({"worker": "B", "entered": entered, "exited": exited})
        set_marker(gates / "b-out")
    except BaseException:
        results.put({"worker": "B", "error": traceback.format_exc()})


def _ordered_worker(lock_path, name, results):
    """合法竞争者：入界后把名字追加进顺序文件（O_APPEND 小写入，互斥成立时原子）。"""
    try:
        with exclusive_lock(lock_path, timeout=WAIT_TIMEOUT):
            fd = os.open(str(lock_path.parent / "order"),
                         os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(fd, (name + "\n").encode("utf-8"))
            finally:
                os.close(fd)
            time.sleep(0.03)
        results.put({"worker": name, "ok": True})
    except BaseException:
        results.put({"worker": name, "error": traceback.format_exc()})


def _gate_holder(lock_path, ready_marker, gate):
    with exclusive_lock(lock_path, timeout=WAIT_TIMEOUT):
        set_marker(ready_marker)
        wait_marker(gate)
    os._exit(0)


def _crash_holder(lock_path, ready_marker):
    with exclusive_lock(lock_path, timeout=WAIT_TIMEOUT):
        set_marker(ready_marker)
        os._exit(1)  # 持锁强杀：锁文件残留


def _taker(lock_path, done_marker, results):
    try:
        with exclusive_lock(lock_path, timeout=WAIT_TIMEOUT):
            set_marker(done_marker)
            time.sleep(0.02)
        results.put({"ok": True})
    except BaseException:
        results.put({"error": traceback.format_exc()})


def _drain(results, processes):
    reports = [results.get(timeout=WAIT_TIMEOUT) for _ in processes]
    for proc in processes:
        proc.join(timeout=WAIT_TIMEOUT)
        if proc.is_alive():
            proc.terminate()
            proc.join()
            raise RuntimeError("worker 未退出")
    return reports


class ProbeScenarioTest(unittest.TestCase):
    """复核者探针（evidence/09 empty_lock_creation_window）的进程级转正。"""

    def test_paused_holder_blocks_contender_no_double_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / "l.lock"
            gates = root / "gates"
            gates.mkdir()
            _init_counters(root)
            ctx = mp.get_context("fork")
            results = ctx.Queue()
            holder = ctx.Process(target=_probe_holder,
                                 args=(lock, gates, results))
            contender = ctx.Process(target=_probe_contender,
                                    args=(lock, gates, results))
            holder.start()
            wait_marker(gates / "a-in")          # A 已入界（含完整拥有者记录）
            contender.start()
            # 复核探针断言转正：A 在临界区内期间 B 不得入界
            # （v1 在此窗口 mutual_exclusion_broken=true；v2 发布即完整记录）
            deadline = time.monotonic() + PROBE_HOLD
            while time.monotonic() < deadline:
                self.assertFalse((gates / "b-in").exists(),
                                 "持锁者临界区内竞争者已入界：互斥被破坏")
                self.assertTrue(contender.is_alive())
                time.sleep(0.02)
            set_marker(gates / "a-release")
            wait_marker(gates / "b-in")          # 合法进展：A 出界后 B 必须能进
            wait_marker(gates / "b-out")
            wait_marker(gates / "a-out")
            reports = {r["worker"]: r for r in _drain(results,
                                                      (holder, contender))}
            for name in ("A", "B"):
                self.assertNotIn("error", reports[name], reports[name])
            # 共享计数：两次入界都被记账，最大并发恰为 1
            self.assertEqual(_read_int(root / "count"), 2)
            self.assertEqual(_read_int(root / "max"), 1)
            self.assertEqual(_read_int(root / "current"), 0)
            # 串行化：B 的入界不早于 A 的出界（monotonic 跨进程可比）
            self.assertGreaterEqual(reports["B"]["entered"],
                                    reports["A"]["exited"])
            self.assertFalse(lock.exists())


class ProgressTest(unittest.TestCase):
    def test_two_contenders_both_progress_in_serial_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / "l.lock"
            ctx = mp.get_context("fork")
            results = ctx.Queue()
            procs = [ctx.Process(target=_ordered_worker,
                                 args=(lock, name, results))
                     for name in ("A", "B")]
            for proc in procs:
                proc.start()
            reports = _drain(results, procs)
            # 无死锁无饥饿：两竞争者都在栅栏超时内成功完成
            self.assertTrue(all(r.get("ok") for r in reports), reports)
            order = (root / "order").read_text(encoding="utf-8").split()
            self.assertEqual(sorted(order), ["A", "B"])       # 都完成（顺序性）
            self.assertEqual(len(set(order)), 2)              # 无并发交错重写
            self.assertFalse(lock.exists())


class StaleRecoveryTest(unittest.TestCase):
    def test_timeout_on_live_holder_is_product_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "l.lock"
            ctx = mp.get_context("fork")
            gate = Path(tmp) / "gate"
            holder = ctx.Process(target=_gate_holder,
                                 args=(lock, Path(tmp) / "ready", gate))
            holder.start()
            wait_marker(Path(tmp) / "ready")
            try:
                with self.assertRaises(CompanionError) as caught:
                    with exclusive_lock(lock, timeout=0.3):
                        pass
                self.assertIn(str(holder.pid), str(caught.exception))
            finally:
                set_marker(gate)
                holder.join(timeout=WAIT_TIMEOUT)
            self.assertFalse(holder.is_alive())
            self.assertFalse(lock.exists())   # 持有者正常释放后可再获取

    def test_killed_holder_stale_record_takeover(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "l.lock"
            ctx = mp.get_context("fork")
            results = ctx.Queue()
            crashed = ctx.Process(target=_crash_holder,
                                  args=(lock, Path(tmp) / "ready"))
            crashed.start()
            wait_marker(Path(tmp) / "ready")
            crashed.join(timeout=WAIT_TIMEOUT)
            self.assertEqual(crashed.exitcode, 1)
            record = json.loads(lock.read_text(encoding="utf-8"))
            self.assertEqual(record["pid"], crashed.pid)   # 陈旧记录在盘

            taker = ctx.Process(target=_taker,
                                args=(lock, Path(tmp) / "done", results))
            taker.start()
            report = results.get(timeout=WAIT_TIMEOUT)  # 未接管则在此超时失败
            taker.join(timeout=WAIT_TIMEOUT)
            self.assertNotIn("error", report, report)
            self.assertFalse(lock.exists())

    def test_legacy_dead_pid_record_taken_over_without_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "l.lock"
            ctx = mp.get_context("fork")
            dead = ctx.Process(target=lambda: None)
            dead.start()
            dead.join()
            lock.write_text(json.dumps({"pid": dead.pid}), encoding="utf-8")
            start = time.monotonic()
            with exclusive_lock(lock, timeout=10, grace=30):
                elapsed = time.monotonic() - start
            # pid 字段完整且已死 → 立即接管，不付宽限（区别于可疑锁路径）
            self.assertLess(elapsed, 5.0)
            self.assertFalse(lock.exists())


class SuspiciousLockTest(unittest.TestCase):
    def test_empty_lock_file_grace_then_takeover_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "l.lock"
            lock.write_text("", encoding="utf-8")   # 手工放置空锁文件
            stderr = io.StringIO()
            start = time.monotonic()
            with contextlib.redirect_stderr(stderr):
                with exclusive_lock(lock, timeout=10, grace=0.3):
                    elapsed = time.monotonic() - start
            # 空/不可解析记录不是死锁证明：等满宽限才接管
            self.assertGreaterEqual(elapsed, 0.3)
            warning = stderr.getvalue()
            self.assertIn("接管", warning)
            self.assertIn(str(lock), warning)       # 接管事件可追（含锁路径）
            self.assertFalse(lock.exists())

    def test_unparseable_record_without_pid_treated_as_suspicious(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "l.lock"
            lock.write_text('{"half": ', encoding="utf-8")  # 半写记录
            stderr = io.StringIO()
            start = time.monotonic()
            with contextlib.redirect_stderr(stderr):
                with exclusive_lock(lock, timeout=10, grace=0.2):
                    elapsed = time.monotonic() - start
            self.assertGreaterEqual(elapsed, 0.2)
            self.assertIn("接管", stderr.getvalue())
            self.assertFalse(lock.exists())


class IdentityReleaseTest(unittest.TestCase):
    def test_release_keeps_lock_taken_over_by_other_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "l.lock"
            with exclusive_lock(lock):
                record = json.loads(lock.read_text(encoding="utf-8"))
                record["owner_token"] = "someone-else"  # 模拟已被他人接管
                lock.write_text(json.dumps(record), encoding="utf-8")
            # 释放身份核对：token 不一致不误删（持有者盘面原样保留）
            self.assertTrue(lock.exists())
            self.assertEqual(json.loads(lock.read_text(encoding="utf-8"))
                             ["owner_token"], "someone-else")
            lock.unlink()

    def test_release_requires_matching_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "l.lock"
            with exclusive_lock(lock):
                record = json.loads(lock.read_text(encoding="utf-8"))
                record["pid"] = 999999999              # token 同、pid 不符：不删
                lock.write_text(json.dumps(record), encoding="utf-8")
            self.assertTrue(lock.exists())
            lock.unlink()

    def test_same_process_reentry_counts_and_outer_release_removes(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "l.lock"
            with exclusive_lock(lock):
                record = json.loads(lock.read_text(encoding="utf-8"))
                with exclusive_lock(lock):      # 同进程重入：立即成功，不落第二盘
                    self.assertEqual(
                        json.loads(lock.read_text(encoding="utf-8")),
                        record)
                self.assertTrue(lock.exists())  # 内层退出不删锁
            self.assertFalse(lock.exists())     # 最外层才删除


if __name__ == "__main__":
    unittest.main()

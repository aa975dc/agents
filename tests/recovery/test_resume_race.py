# -*- coding: utf-8 -*-
"""SR-03 修复验收：ResumeLedger 跨进程并发接管与 (epoch, owner) 围栏。

全部进程场景用真实多进程（fork）+ 文件栅栏控制确定时序；互斥由 run_root 级
锁文件（atomicio.exclusive_lock）承担，等待只用于排序，不以超时充当互斥证据。
场景对应复核报告 §6 / FIX-02 验收：
1. 双进程同时接管：磁盘上不存在两个都"有效"的写权（无重复有效 epoch/owner）；
2. 已成功登记的活动不因旧内存快照覆盖丢失（复核探针 both_acquired_same_epoch /
   successful_A_activity_lost 两断言的进程级转正）；
3. 接管与旧持有者心跳/登记交错、旧持有者迟到写入：全部拒绝且不落盘；
4. 强杀恢复：接管者 os._exit 后新进程可再接管、已提交活动保留；持锁进程强杀
   不留死锁（陈旧锁按 pid 存活接管）。
"""
import json
import multiprocessing as mp
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))
from agents_kernel.atomicio import exclusive_lock
from agents_kernel.services import resume as resume_mod
from agents_kernel.services.resume import ResumeLedger
from agents_kernel.validation import CompanionError

WAIT_TIMEOUT = 20.0


def wait_marker(path):
    """文件栅栏：等到标记出现；超时大声失败（绝不把等待当正确性证据）。"""
    deadline = time.monotonic() + WAIT_TIMEOUT
    while not Path(path).exists():
        if time.monotonic() >= deadline:
            raise AssertionError("栅栏等待超时：%s" % path)
        time.sleep(0.005)


def set_marker(path):
    Path(path).write_text("1", encoding="utf-8")


def _drain(results, processes):
    reports = [results.get(timeout=WAIT_TIMEOUT) for _ in processes]
    for proc in processes:
        proc.join(timeout=WAIT_TIMEOUT)
        if proc.is_alive():
            proc.terminate()
            proc.join()
            raise RuntimeError("worker 未退出")
    return reports


def _race_worker(run_root, name, gates, results):
    """复核探针同场景：双方先各自接管（互斥锁决定先后），A 先登记、B 后登记。"""
    try:
        ledger = ResumeLedger.open(run_root, name)
        set_marker(gates / ("opened-" + name))
        wait_marker(gates / "opened-A")            # 双方都接管完才进入登记段
        wait_marker(gates / "opened-B")
        if name == "B":
            wait_marker(gates / "A-done")          # 探针同款：B 等 A 的登记落定
        outcome = {"writer": name, "epoch": ledger.epoch,
                   "owner": ledger.owner_id, "success": True}
        try:
            row = ledger.register("custom", name,
                                  str(Path(run_root) / "checkpoint.json"), 1,
                                  source_anchor="fixed-source")
            outcome["registered"] = row["id"]
        except CompanionError as exc:
            outcome["success"] = False             # 被围栏拒绝：如实上报，不写盘
            outcome["error"] = str(exc)
        finally:
            set_marker(gates / "A-done")
        results.put(outcome)
    except BaseException:
        import traceback
        results.put({"writer": name, "success": False,
                     "error": traceback.format_exc()})


def _late_writer_worker(run_root, name, gates, results, activity_id):
    """旧持有者：接管发生后仍尝试心跳/登记/完结/失败——全部应被拒。"""
    try:
        ledger = ResumeLedger.open(run_root, name)
        ledger.register("custom", activity_id,
                        str(Path(run_root) / "checkpoint.json"), 1)
        set_marker(gates / "old-ready")
        wait_marker(gates / "taken")
        attempts = {}
        for action, call in (
                ("heartbeat", lambda: ledger.heartbeat("custom", activity_id,
                                                       note="迟到心跳")),
                ("register", lambda: ledger.register(
                    "custom", "late-" + name,
                    str(Path(run_root) / "checkpoint.json"), 1)),
                ("complete", lambda: ledger.complete("custom", activity_id)),
                ("fail", lambda: ledger.fail("custom", activity_id, "迟到回报"))):
            try:
                call()
                attempts[action] = True
            except CompanionError:
                attempts[action] = False
        results.put({"writer": name, "epoch": ledger.epoch, "attempts": attempts})
    except BaseException:
        import traceback
        results.put({"writer": name, "error": traceback.format_exc()})


def _taker_worker(run_root, name, activity_id, results, done_marker=None):
    try:
        ledger = ResumeLedger.open(run_root, name)
        ledger.register("custom", activity_id,
                        str(Path(run_root) / "checkpoint.json"), 1)
        if done_marker is not None:
            set_marker(done_marker)
        results.put({"writer": name, "epoch": ledger.epoch,
                     "owner": ledger.owner_id, "success": True,
                     "activity_ids": sorted(a["id"] for a in ledger.activities())})
    except BaseException:
        import traceback
        results.put({"writer": name, "success": False,
                     "error": traceback.format_exc()})


def _crash_worker(run_root, name, marker, activity_id):
    """登记后 os._exit 强杀：无 complete/fail、无清理路径。"""
    ledger = ResumeLedger.open(run_root, name)
    ledger.register("custom", activity_id,
                    str(Path(run_root) / "checkpoint.json"), 1)
    set_marker(marker)
    os._exit(9)


def _hold_lock_then_die(run_root, marker):
    with exclusive_lock(Path(run_root) / resume_mod.LOCK_FILENAME):
        set_marker(marker)
        os._exit(1)  # 持锁强杀：锁文件必然残留


class RaceTakeoverTest(unittest.TestCase):
    def test_two_processes_hostile_takeover_single_valid_writer(self):
        """双进程交错接管：磁盘上不存在两个都"有效"的写权（无重复有效 epoch/owner）。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            (run_root / "checkpoint.json").write_text("{}", encoding="utf-8")
            ResumeLedger.open(run_root, "seed")
            gates = run_root / "gates"
            gates.mkdir()
            ctx = mp.get_context("fork")
            results = ctx.Queue()
            procs = [ctx.Process(target=_race_worker,
                                 args=(run_root, name, gates, results))
                     for name in ("A", "B")]
            for proc in procs:
                proc.start()
            reports = {r["writer"]: r for r in _drain(results, procs)}

            disk = json.loads((run_root / resume_mod.LEDGER_FILENAME)
                              .read_text(encoding="utf-8"))
            winners = [r for r in reports.values() if r.get("success")]
            losers = [r for r in reports.values() if not r.get("success")]
            self.assertEqual(len(reports), 2)
            # 恰一个有效写者：其 (epoch, owner) 与磁盘一致；被拒者错误指明被接管
            self.assertEqual(len(winners), 1)
            self.assertEqual(len(losers), 1)
            winner, loser = winners[0], losers[0]
            self.assertEqual((disk["writer_epoch"], disk["writer_owner"]),
                             (winner["epoch"], winner["owner"]))
            self.assertIn("接管", loser["error"])
            # 无重复有效 epoch/owner（复核探针 both_acquired_same_epoch=false 转正）
            self.assertEqual(len({r["epoch"] for r in reports.values()}), 2)
            self.assertEqual(len({r["owner"] for r in reports.values()}), 2)
            # 磁盘活动 = 唯一有效写者登记的行；被拒方的写入不存在
            self.assertEqual([row["id"] for row in disk["activities"]],
                             [winner["writer"]])
            self.assertFalse((run_root / resume_mod.LOCK_FILENAME).exists())

    def test_takeover_after_commit_preserves_registered_activity(self):
        """接管以磁盘为基读-改-写：先成功登记的活动不被覆盖丢失。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            (run_root / "checkpoint.json").write_text("{}", encoding="utf-8")
            ResumeLedger.open(run_root, "seed")
            gates = run_root / "gates"
            gates.mkdir()
            ctx = mp.get_context("fork")
            results = ctx.Queue()

            def worker(name):
                if name == "B":
                    wait_marker(gates / "A-done")   # B 在 A 提交之后才接管
                ledger = ResumeLedger.open(run_root, name)
                row = ledger.register("custom", name,
                                      str(run_root / "checkpoint.json"), 1,
                                      source_anchor="fixed-source")
                results.put({"writer": name, "epoch": ledger.epoch,
                             "owner": ledger.owner_id, "registered": row["id"],
                             "success": True})
                if name == "A":
                    set_marker(gates / "A-done")

            procs = [ctx.Process(target=worker, args=(name,))
                     for name in ("A", "B")]
            procs[1].start()   # B 先起，栅栏保证接管顺序 A → B
            procs[0].start()
            reports = {r["writer"]: r for r in _drain(results, procs)}

            disk = json.loads((run_root / resume_mod.LEDGER_FILENAME)
                              .read_text(encoding="utf-8"))
            self.assertTrue(all(r["success"] for r in reports.values()))
            self.assertEqual(sorted(r["epoch"] for r in reports.values()), [2, 3])
            # 复核探针 successful_A_activity_lost=true 的反面：A、B 活动都在盘上
            self.assertEqual(sorted(row["id"] for row in disk["activities"]),
                             ["A", "B"])
            self.assertEqual((disk["writer_epoch"], disk["writer_owner"]),
                             (3, reports["B"]["owner"]))
            self.assertFalse((run_root / resume_mod.LOCK_FILENAME).exists())
            takeovers = [json.loads(line) for line in
                         (run_root / resume_mod.TAKEOVER_LOG)
                         .read_text(encoding="utf-8").splitlines()]
            self.assertEqual([(t["from"]["epoch"], t["to"]["epoch"])
                              for t in takeovers], [(1, 2), (2, 3)])


class LateWriterTest(unittest.TestCase):
    def test_old_owner_heartbeat_register_complete_fail_all_rejected(self):
        """接管与旧持有者交错：旧持有者四类写入全被拒且不落盘。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            (run_root / "checkpoint.json").write_text("{}", encoding="utf-8")
            ResumeLedger.open(run_root, "seed")
            gates = run_root / "gates"
            gates.mkdir()
            ctx = mp.get_context("fork")
            results = ctx.Queue()
            old = ctx.Process(target=_late_writer_worker,
                              args=(run_root, "old", gates, results, "old-work"))
            taker = ctx.Process(target=_taker_worker,
                                args=(run_root, "new-owner", "new-work", results,
                                      gates / "taker-done"))
            old.start()
            wait_marker(gates / "old-ready")
            taker.start()
            wait_marker(gates / "taker-done")
            before = (run_root / resume_mod.LEDGER_FILENAME).read_bytes()
            set_marker(gates / "taken")            # 放行旧持有者的迟到写入
            # 完成顺序不假设：按 writer 名匹配两份报告
            reports = {r["writer"]: r
                       for r in (results.get(timeout=WAIT_TIMEOUT),
                                 results.get(timeout=WAIT_TIMEOUT))}
            report_old, report_taker = reports["old"], reports["new-owner"]
            for proc in (old, taker):
                proc.join(timeout=WAIT_TIMEOUT)
                self.assertFalse(proc.is_alive())

            self.assertNotIn("error", report_old)
            self.assertEqual(report_old["epoch"], 2)
            self.assertEqual(report_old["attempts"],
                             {"heartbeat": False, "register": False,
                              "complete": False, "fail": False})
            self.assertTrue(report_taker["success"])
            after = json.loads((run_root / resume_mod.LEDGER_FILENAME)
                               .read_text(encoding="utf-8"))
            self.assertEqual((after["writer_epoch"], after["writer_owner"]),
                             (report_taker["epoch"], report_taker["owner"]))
            # 被拒写入没有留下任何行（迟到登记 late-old 不存在）；
            # 旧持有者已提交的活动经接管快照保留
            self.assertEqual(sorted(row["id"] for row in after["activities"]),
                             ["new-work", "old-work"])
            # 迟到写入尝试期间磁盘字节不变
            self.assertEqual((run_root / resume_mod.LEDGER_FILENAME)
                             .read_bytes(), before)


class CrashRecoveryTest(unittest.TestCase):
    def test_killed_holder_recoverable_and_activities_preserved(self):
        """强杀恢复：接管者 os._exit 后新进程可再接管，已提交活动保留。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            (run_root / "checkpoint.json").write_text("{}", encoding="utf-8")
            ResumeLedger.open(run_root, "seed")
            gates = run_root / "gates"
            gates.mkdir()
            ctx = mp.get_context("fork")
            results = ctx.Queue()
            crashed = ctx.Process(target=_crash_worker,
                                  args=(run_root, "crashed",
                                        gates / "crashed-ready", "crashed-work"))
            crashed.start()
            wait_marker(gates / "crashed-ready")
            crashed.join(timeout=WAIT_TIMEOUT)
            self.assertEqual(crashed.exitcode, 9)

            taker = ctx.Process(target=_taker_worker,
                                args=(run_root, "recovery", "recovered-work",
                                      results))
            taker.start()
            report = results.get(timeout=WAIT_TIMEOUT)
            taker.join(timeout=WAIT_TIMEOUT)

            self.assertTrue(report["success"], report)
            self.assertEqual(report["epoch"], 3)
            self.assertEqual(report["activity_ids"],
                             ["crashed-work", "recovered-work"])
            disk = json.loads((run_root / resume_mod.LEDGER_FILENAME)
                              .read_text(encoding="utf-8"))
            self.assertEqual([row["id"] for row in disk["activities"]],
                             ["crashed-work", "recovered-work"])
            self.assertFalse((run_root / resume_mod.LOCK_FILENAME).exists())

    def test_holder_killed_while_holding_lock_no_deadlock(self):
        """持锁进程强杀：锁文件残留按 pid 存活检测接管，不留死锁。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            (run_root / "checkpoint.json").write_text("{}", encoding="utf-8")
            ResumeLedger.open(run_root, "seed")
            gates = run_root / "gates"
            gates.mkdir()
            ctx = mp.get_context("fork")
            holder = ctx.Process(target=_hold_lock_then_die,
                                 args=(run_root, gates / "holding"))
            holder.start()
            wait_marker(gates / "holding")
            holder.join(timeout=WAIT_TIMEOUT)
            self.assertEqual(holder.exitcode, 1)
            self.assertTrue((run_root / resume_mod.LOCK_FILENAME).exists())

            results = ctx.Queue()
            taker = ctx.Process(target=_taker_worker,
                                args=(run_root, "after-kill", "post-kill-work",
                                      results))
            taker.start()
            report = results.get(timeout=WAIT_TIMEOUT)  # 陈旧锁未接管则在此超时失败
            taker.join(timeout=WAIT_TIMEOUT)
            self.assertTrue(report["success"], report)
            self.assertEqual(report["activity_ids"], ["post-kill-work"])
            self.assertFalse((run_root / resume_mod.LOCK_FILENAME).exists())


class IdentityFenceTest(unittest.TestCase):
    def test_same_epoch_different_owner_cannot_overwrite(self):
        """SR-03 病理场景回归：数字 epoch 相同、owner 不同 → 仍拒（不再只看数字）。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            checkpoint = run_root / "checkpoint.json"
            checkpoint.write_text("{}", encoding="utf-8")
            path = run_root / resume_mod.LEDGER_FILENAME
            first = ResumeLedger(path, "a", 9, 8, {}, time.time,
                                 owner_id="owner-a")
            second = ResumeLedger(path, "b", 9, 8, {}, time.time,
                                  owner_id="owner-b")  # 同 epoch 不同 owner
            # 把盘面置于 first 的身份（模拟 first 是当前有效写者）
            path.write_text(json.dumps(
                {"version": 1, "kind": "resume_ledger", "writer_id": "a",
                 "writer_epoch": 9, "writer_owner": "owner-a", "activities": []}),
                encoding="utf-8")
            first.register("custom", "x", str(checkpoint), 1)
            with self.assertRaises(CompanionError):
                second.register("custom", "y", str(checkpoint), 1)
            with self.assertRaises(CompanionError):
                second.heartbeat("custom", "x")
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([row["id"] for row in disk["activities"]], ["x"])
            self.assertEqual(disk["writer_owner"], "owner-a")

    def test_legacy_ledger_without_owner_is_takeable(self):
        """旧格式台账（无 writer_owner）可读可接管：新持有者落盘即带 owner。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            (run_root / resume_mod.LEDGER_FILENAME).write_text(
                json.dumps({"version": 1, "kind": "resume_ledger",
                            "writer_id": "legacy", "writer_epoch": 4,
                            "activities": []}), encoding="utf-8")
            ledger = ResumeLedger.open(run_root, "taker")
            self.assertEqual(ledger.epoch, 5)
            disk = json.loads((run_root / resume_mod.LEDGER_FILENAME)
                              .read_text(encoding="utf-8"))
            self.assertEqual(disk["writer_owner"], ledger.owner_id)


if __name__ == "__main__":
    unittest.main()

"""任务租约：文件锁 + TTL + epoch 接管（stdlib only, Py3.9+）。

存储选择（按要求注明）：每任务一个锁文件 <dir>/<task_id>.lease（JSON：worker、
epoch、expires_at），不用 SQLite——台账库是单协调写者（ST04），worker 无 epoch
不得触碰事实表；租约是执行层的跨进程热状态，落文件与 db.acquire_writer 的
锁文件同风格：领取用 O_CREAT|O_EXCL 原子创建；续约/接管走 kernel.atomicio
.write_atomic 的 before_replace 复查（CAS：文件仍为读到的 epoch 才落盘）。

- 时钟：clock 可注入（默认 time.time 墙钟）。跨进程比较必须用墙钟
  （time.monotonic 各进程起点不同）；NTP 回拨属已知边界，TTL 只做粗粒度判定。
- TTL 语义：now >= expires_at 即过期，过期即失格——"超时不等于旧进程已停止"
  （06 计划 §4），持约者续约/释放都要求未过期且 worker_id+epoch 双匹配；被
  接管后复活的旧持约者因 epoch 不符被拒，其旧回报由幂等键层拒绝。
- 接管：过期后任何 worker 可 steal，epoch 递增并追加接管事件到
  <dir>/takeovers.jsonl（单行 O_APPEND 写；接进 P2-02 事件库归 P5-05）。
- 损坏/半写内容按陈旧租约处理（epoch=0、视为过期，可接管），与 db 的陈旧锁
  同策略；接管即覆盖，不留死锁。

边界声明：本模块只回答"此刻谁在跑"，不构成跨系统恰好一次——执行语义是
at-least-once，重复回报由 execution.idempotency 的幂等键去重兜底。
"""
import json
import os
import time
from pathlib import Path
from typing import NamedTuple

from agents_kernel.atomicio import write_atomic
from agents_kernel.validation import CompanionError, text

LEASE_VERSION = 1
TAKEOVER_LOG = "takeovers.jsonl"
# 损坏/半写租约按陈旧处理：epoch=0 恒过期，可被任何 worker 接管。
_STALE = {"task_id": None, "worker_id": None, "epoch": 0, "expires_at": 0.0}


class Lease(NamedTuple):
    """一次成功领取/续约/接管后的租约快照。"""
    task_id: str
    worker_id: str
    epoch: int
    expires_at: float


def _task_id(task_id):
    tid = text(task_id, "任务编号")
    if "/" in tid or "\\" in tid or tid in (".", ".."):
        raise CompanionError("任务编号不能作租约文件名：%s" % tid)
    return tid


def _ttl(ttl):
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
        raise CompanionError("TTL 必须是正数")
    return float(ttl)


class LeaseManager:
    """每任务文件租约的领取/续约/接管/释放面。"""

    def __init__(self, directory, clock=time.time):
        self._dir = Path(directory)
        self._clock = clock

    # ---- 公开入口 ----

    def acquire(self, task_id, worker_id, ttl):
        """领取租约：无租约则新建（epoch 从 1）；已过期则接管（epoch+1、记接管事件）；
        未过期被他方持有则拒绝。返回 Lease。"""
        tid = _task_id(task_id)
        wid = text(worker_id, "worker 编号")
        expires_at = self._clock() + _ttl(ttl)
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(tid)
        previous = self._read(path)
        if previous is not None and not self._expired(previous):
            raise CompanionError("任务 %s 的租约未过期，被 worker=%s 持有（epoch=%d）"
                                 % (tid, previous["worker_id"], int(previous["epoch"])))
        epoch = int(previous["epoch"]) + 1 if previous is not None else 1
        lease = Lease(tid, wid, epoch, expires_at)
        if previous is None:
            self._create(path, lease)
        else:
            self._replace(path, lease, expect_epoch=int(previous["epoch"]))
            self._record_takeover(tid, previous, lease)
        return lease

    def renew(self, task_id, worker_id, ttl):
        """续约：只有未过期的持约者（worker_id+epoch 匹配）可以延长 TTL。"""
        tid = _task_id(task_id)
        wid = text(worker_id, "worker 编号")
        path = self._path(tid)
        payload = self._require_holder(path, wid, tid)
        lease = Lease(tid, wid, int(payload["epoch"]), self._clock() + _ttl(ttl))
        self._replace(path, lease, expect_epoch=int(payload["epoch"]))
        return lease

    def release(self, task_id, worker_id):
        """释放：只有未过期的持约者可以释放；不存在/非持约者/已过期一律拒绝。"""
        tid = _task_id(task_id)
        wid = text(worker_id, "worker 编号")
        path = self._path(tid)
        self._require_holder(path, wid, tid)
        path.unlink()

    def state(self, task_id):
        """租约快照：无租约返回 None；过期租约原样返回并标 expired=True（只读）。"""
        payload = self._read(self._path(_task_id(task_id)))
        if payload is None:
            return None
        return {"task_id": payload["task_id"], "worker_id": payload["worker_id"],
                "epoch": int(payload["epoch"]), "expires_at": payload["expires_at"],
                "expired": self._expired(payload)}

    def takeovers(self, task_id=None):
        """接管事件日志（审计读）；task_id 给定时只看该任务。"""
        path = self._dir / TAKEOVER_LOG
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        items = [json.loads(line) for line in lines if line.strip()]
        if task_id is None:
            return items
        tid = _task_id(task_id)
        return [item for item in items if item["task_id"] == tid]

    # ---- 内部 ----

    def _path(self, tid):
        return self._dir / ("%s.lease" % tid)

    def _read(self, path):
        """读租约文件：不存在 → None；损坏/字段不全 → 陈旧占位（可接管）。"""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return dict(_STALE)
        if (not isinstance(data, dict)
                or not isinstance(data.get("worker_id"), str)
                or not isinstance(data.get("epoch"), int) or isinstance(data.get("epoch"), bool)
                or not isinstance(data.get("expires_at"), (int, float))
                or isinstance(data.get("expires_at"), bool)):
            return dict(_STALE)
        return data

    def _expired(self, payload):
        """TTL 语义：now >= expires_at 即过期（过期即失格，见模块 docstring）。"""
        return self._clock() >= payload["expires_at"]

    def _require_holder(self, path, wid, tid):
        payload = self._read(path)
        if payload is None:
            raise CompanionError("任务 %s 没有租约" % tid)
        if payload["worker_id"] != wid:
            raise CompanionError("操作被拒：任务 %s 的租约属于 worker=%s（epoch=%d），"
                                 "worker=%s 不是持约者"
                                 % (tid, payload["worker_id"], int(payload["epoch"]), wid))
        if self._expired(payload):
            raise CompanionError("操作被拒：worker=%s 的租约已过期（任务 %s，epoch=%d），"
                                 "持约者身份已失效" % (wid, tid, int(payload["epoch"])))
        return payload

    def _create(self, path, lease):
        data = json.dumps(self._payload(lease), ensure_ascii=False, sort_keys=True) + "\n"
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise CompanionError("租约领取竞争失败（他人抢先创建）：%s" % path) from None
        try:
            os.write(fd, data.encode("utf-8"))
        finally:
            os.close(fd)

    def _replace(self, path, lease, expect_epoch):
        """CAS 覆盖：落盘前复查文件仍为期望 epoch，被并发变更即拒。"""
        data = json.dumps(self._payload(lease), ensure_ascii=False, sort_keys=True) + "\n"

        def guard():
            current = self._read(path)
            if current is None or int(current["epoch"]) != expect_epoch:
                raise CompanionError("租约已被并发变更（期望 epoch=%d）：%s" % (expect_epoch, path))

        write_atomic(path, data.encode("utf-8"), before_replace=guard)

    @staticmethod
    def _payload(lease):
        return {"version": LEASE_VERSION, "task_id": lease.task_id,
                "worker_id": lease.worker_id, "epoch": lease.epoch,
                "expires_at": lease.expires_at}

    def _record_takeover(self, tid, previous, lease):
        record = {"type": "lease_takeover", "task_id": tid,
                  "from": {"worker_id": previous["worker_id"], "epoch": int(previous["epoch"])},
                  "to": {"worker_id": lease.worker_id, "epoch": lease.epoch},
                  "at": self._clock()}
        line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(str(self._dir / TAKEOVER_LOG),
                     os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

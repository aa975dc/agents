"""文件 ownership 台账：写任务精确路径的归属与共享串行（stdlib only, Py3.9+）。

TK09 后半/C07 前半：allowed_paths 是精确文件闭集。claim 全有或全无：任一路径
被他任务 active 持有且不在共享白名单 → StructuredConflict（结构化字段报出双方
task 与路径），不部分授予。共享白名单（contracts/ 前缀与常见 lockfile 精确名，
可配）走单 owner 串行——他方持有：默认排队（FIFO，release 时尽力晋升），
显式 queue=False 则拒绝并标 retryable=True 供调度方稍后重试。排队只是等待
不预留：串行不变式是"任一时刻每路径至多一个 active owner"；公平性尽力而为
——被共享路径阻塞的等待者可能被后到者越过（其要的路径当时空闲），但晋升
永远检查 active 持有，串行保证不打折。

- 台账在内存；store_path 给定时 JSON 原子落盘（write_json）并在构造时加载，
  进程重启后归属可恢复。
- 路径用 validation.relative_path 校验（相对 posix，拒绝敏感/排除目录）——
  .git、.env 等天然进不了台账。
- StructuredConflict 继承 CompanionError：既有 CompanionError 捕获面兼容。
"""
import time
from pathlib import Path
from typing import NamedTuple

from agents_kernel.atomicio import read_json, write_json
from agents_kernel.validation import CompanionError, relative_path, strings, text

OWNERSHIP_VERSION = 1

# 共享白名单（默认）：以 / 结尾为目录前缀，否则为精确路径。共享文件同样单 owner
# （TK09：冲突可处理——排队或显式拒绝，语义见 claim）。
DEFAULT_SHARED_WHITELIST = (
    "contracts/",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
    "poetry.lock", "Cargo.lock", "uv.lock", "Pipfile.lock", "composer.lock",
)


class StructuredConflict(CompanionError):
    """归属冲突：继承 CompanionError，另携带结构化字段供调度方程序化处理。

    shared=True 的冲突发生在共享白名单内（retryable 指示可稍后重试/排队）；
    shared=False 是非共享路径的越权重叠（终局拒绝，须先协调归属）。
    """

    def __init__(self, message, *, holder_task, requester_task, paths, shared, retryable):
        super().__init__(message)
        self.holder_task = holder_task
        self.requester_task = requester_task
        self.paths = list(paths)
        self.shared = shared
        self.retryable = retryable


class OwnershipClaim(NamedTuple):
    """一次声明后的归属快照；status 为 active（已持有）或 queued（排队等待）。"""
    task_id: str
    paths: tuple
    status: str
    claimed_at: float


def _safe_id(task_id):
    tid = text(task_id, "任务编号")
    if "/" in tid or "\\" in tid or tid in (".", ".."):
        raise CompanionError("任务编号不能作台账键：%s" % tid)
    return tid


class OwnershipRegistry:
    """写路径归属的 claim/release/state 台账（内存 + 可选 JSON 持久化）。"""

    def __init__(self, shared_whitelist=None, store_path=None, clock=time.time):
        self._shared = tuple(DEFAULT_SHARED_WHITELIST if shared_whitelist is None
                             else shared_whitelist)
        self._store = Path(store_path) if store_path is not None else None
        self._clock = clock
        self._claims = {}  # task_id -> {"paths": tuple, "status": str, "claimed_at": float}
        self._queue = []   # 排队中的 task_id，FIFO
        self._load()

    # ---- 查询 ----

    def is_shared(self, path):
        """路径是否落在共享白名单：/ 结尾规则作目录前缀，其余精确匹配。"""
        path = text(path, "文件路径")
        for rule in self._shared:
            if path.startswith(rule) if rule.endswith("/") else path == rule:
                return True
        return False

    def state(self):
        """只读快照：按领取时间稳定排序（审计与测试用）。"""
        ordered = sorted(self._claims.items(), key=lambda kv: (kv[1]["claimed_at"], kv[0]))
        return [{"task_id": tid, "paths": list(rec["paths"]), "status": rec["status"],
                 "claimed_at": rec["claimed_at"]} for tid, rec in ordered]

    # ---- 变更 ----

    def claim(self, task_id, paths, queue=True):
        """声明 paths 的写归属（全有或全无）。非共享重叠 → StructuredConflict
        （终局）；共享重叠：queue=True 排队（FIFO），queue=False 拒绝
        （retryable）。同任务重复声明 → 拒绝（先 release 再重新声明）。"""
        tid = _safe_id(task_id)
        checked = []
        for item in strings(paths, "声明路径", nonempty=True):
            path = relative_path(item)
            if path not in checked:
                checked.append(path)
        if tid in self._claims:
            raise CompanionError("任务 %s 已持有归属台账（先 release 再重新声明）" % tid)
        holders = {}  # path -> active 持有者（排队者不占路径，见模块 docstring）
        for other, rec in self._claims.items():
            if rec["status"] == "active":
                for path in rec["paths"]:
                    holders.setdefault(path, other)
        hard = sorted(path for path in checked
                      if path in holders and not self.is_shared(path))
        if hard:
            holder = holders[hard[0]]
            raise StructuredConflict(
                "路径归属冲突：任务 %s 已持有 %s，任务 %s 被拒绝（非共享白名单路径，"
                "不允许并行写）" % (holder, ",".join(hard), tid),
                holder_task=holder, requester_task=tid, paths=hard,
                shared=False, retryable=False)
        busy = sorted({path for path in checked
                       if path in holders and self.is_shared(path)})
        status = "active"
        if busy:
            if not queue:
                holder = holders[busy[0]]
                raise StructuredConflict(
                    "共享文件 %s 当前由任务 %s 持有（单 owner 串行），任务 %s 本次拒绝，"
                    "可稍后重试" % (",".join(busy), holder, tid),
                    holder_task=holder, requester_task=tid, paths=busy,
                    shared=True, retryable=True)
            status = "queued"
            self._queue.append(tid)
        self._claims[tid] = {"paths": tuple(checked), "status": status,
                             "claimed_at": self._clock()}
        self._save()
        return OwnershipClaim(tid, tuple(checked), status, self._claims[tid]["claimed_at"])

    def release(self, task_id):
        """释放归属：active 释放后按 FIFO 尽力晋升等待者（其全部路径当前无
        active 持有者即转 active，被阻塞者继续排队）；queued 释放仅出队。
        不在册拒绝。"""
        tid = _safe_id(task_id)
        rec = self._claims.get(tid)
        if rec is None:
            raise CompanionError("任务 %s 不在归属台账中，无法释放" % tid)
        was_active = rec["status"] == "active"
        del self._claims[tid]
        if tid in self._queue:
            self._queue.remove(tid)
        if was_active:
            self._promote()
        self._save()

    # ---- 内部 ----

    def _promote(self):
        for tid in list(self._queue):
            rec = self._claims.get(tid)
            if rec is None or rec["status"] != "queued":
                self._queue.remove(tid)
                continue
            held = {path for other, item in self._claims.items()
                    if other != tid and item["status"] == "active"
                    for path in item["paths"]}
            if not any(path in held for path in rec["paths"]):
                rec["status"] = "active"
                self._queue.remove(tid)

    def _save(self):
        if self._store is None:
            return
        payload = {"version": OWNERSHIP_VERSION,
                   "claims": {tid: {"paths": list(rec["paths"]), "status": rec["status"],
                                    "claimed_at": rec["claimed_at"]}
                              for tid, rec in self._claims.items()},
                   "queue": list(self._queue)}
        self._store.parent.mkdir(parents=True, exist_ok=True)
        write_json(self._store, payload)

    def _load(self):
        if self._store is None or not self._store.exists():
            return
        payload = read_json(self._store)
        if (not isinstance(payload, dict) or payload.get("version") != OWNERSHIP_VERSION
                or not isinstance(payload.get("claims"), dict)
                or not isinstance(payload.get("queue"), list)):
            raise CompanionError("归属台账版本或形状不符：%s" % self._store)
        for tid, rec in payload["claims"].items():
            self._claims[tid] = {"paths": tuple(rec["paths"]), "status": rec["status"],
                                 "claimed_at": rec["claimed_at"]}
        self._queue = [tid for tid in payload["queue"] if tid in self._claims]

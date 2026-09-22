"""有限重试退避与预算领取门：把 P3-03 Budget 纳入调度循环（stdlib only, Py3.9+）。

- RetryPolicy / attempt_delay：指数退避 + 抖动，纯计算——delay =
  min(base * multiplier**(n-1), max_delay)，抖动在 [delay*(1-jitter), delay]
  内收缩（只降不升，不抖出上限）；rng 可注入以便确定性测试。达 max_attempts
  即终局：task 停在 failed，绝不无限重试（TK08：attempt 计数不虚增完成度）。
- RetryTracker：把策略接到 TaskBoard——finish_attempt("failed") 后由它裁决
  failed → ready（重试，重开新 attempt）还是停在 failed。
- BudgetGate + BudgetedDispatcher：领取门——预算耗尽是显式 paused：不领取
  新任务、分文不扣；已持租约任务的 finish 收尾不经过领取门（不吞已领取的
  工作）。游标即台账里剩余 ready 集（任务状态在场即游标），补充预算后续跑
  从游标继续，不重不漏（budget.spend_batches 的批边界暂停语义在任务粒度重放）。

依赖方向：本模块组合 services.scheduler（ready 集/串行保护）与 execution.lease，
不改动它们的任何既有语义；旧 require_idle 串行保护在 BudgetedDispatcher 内
原样保留（同一时刻至多一个 running）。
"""
import random
from typing import NamedTuple

from agents_kernel.execution.lease import LeaseManager
from agents_kernel.services.scheduler import SerialExecutor, order_ready, ready_set
from agents_kernel.validation import CompanionError, text


class RetryPolicy:
    """重试纪律：max_attempts 次上限（含首次），超限终局 failed。"""

    def __init__(self, max_attempts=3, base_delay=1.0, multiplier=2.0,
                 max_delay=60.0, jitter=0.1):
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise CompanionError("max_attempts 必须是正整数")
        for name, value in (("base_delay", base_delay), ("multiplier", multiplier),
                            ("max_delay", max_delay)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise CompanionError("%s 必须是正数" % name)
        if isinstance(jitter, bool) or not isinstance(jitter, (int, float)) \
                or not 0 <= jitter <= 1:
            raise CompanionError("jitter 必须在 [0, 1]")
        self.max_attempts = max_attempts
        self.base_delay = float(base_delay)
        self.multiplier = float(multiplier)
        self.max_delay = float(max_delay)
        self.jitter = float(jitter)


def attempt_delay(policy, attempt_no, rng):
    """第 attempt_no 次 attempt 失败后的退避间隔（纯计算；rng 注入抖动）。"""
    if isinstance(attempt_no, bool) or not isinstance(attempt_no, int) or attempt_no < 1:
        raise CompanionError("attempt_no 必须是正整数")
    raw = min(policy.base_delay * (policy.multiplier ** (attempt_no - 1)), policy.max_delay)
    if policy.jitter <= 0:
        return raw
    return raw * (1.0 - policy.jitter * rng.random())


class RetryVerdict(NamedTuple):
    status: str   # "retry"（已转回 ready，delay 为下次尝试前退避）| "exhausted"（停在 failed）
    delay: float


class RetryTracker:
    """把 RetryPolicy 接到 TaskBoard 的失败裁决器。"""

    def __init__(self, board, policy, rng=None):
        self._board = board
        self._policy = policy
        self._rng = rng if rng is not None else random.Random()

    def record_failure(self, task_id):
        """attempt 结束为 failed 后裁决：未达上限 → 转回 ready 并给出退避间隔；
        达上限 → 什么都不改，task 停在 failed（不再重试、不再领取它）。"""
        attempts = self._board.attempt_count(task_id)
        if attempts >= self._policy.max_attempts:
            return RetryVerdict("exhausted", 0.0)
        self._board.transition_task(task_id, "ready")  # 白名单：failed → ready 重试
        return RetryVerdict("retry", attempt_delay(self._policy, attempts + 1, self._rng))


class BudgetGate:
    """领取门：新任务领取前整额试扣其预算（不足分文不扣）；收尾/续约不过门。"""

    def __init__(self, budget, cost_of=None):
        self._budget = budget
        self._cost_of = cost_of if cost_of is not None else (lambda task: 1)

    def try_admit(self, task):
        return self._budget.try_consume(int(self._cost_of(task)))


class DispatchOutcome(NamedTuple):
    status: str        # "claimed" | "paused" | "drained"
    task_id: str       # claimed 时的任务编号，其余为 None
    cursor: tuple      # paused/claimed 时的剩余 ready 任务编号（续跑游标）


class BudgetedDispatcher:
    """串行领取循环：ready 集 → 预算门 → 租约 → running（require_idle 语义保留）。"""

    def __init__(self, board, gate, leases, worker_id, ttl):
        if not isinstance(leases, LeaseManager):
            raise CompanionError("leases 必须是 LeaseManager")
        self._board = board
        self._executor = SerialExecutor(board)
        self._gate = gate
        self._leases = leases
        self._worker_id = text(worker_id, "worker 编号")
        self._ttl = ttl

    def start_next(self, started_at):
        """领取下一个 ready 任务。无 ready → drained；预算不足 → paused（不领取
        新任务、分文不扣、返回剩余游标）；成功 → claimed 并持有租约直到 finish。"""
        ready = order_ready(ready_set(self._board.tasks()))
        if not ready:
            return DispatchOutcome("drained", None, ())
        if self._executor.current() is not None:
            raise CompanionError("串行模式同一时刻至多一个运行中任务（require_idle 语义）")
        head = ready[0]
        if not self._gate.try_admit(head):
            return DispatchOutcome("paused", None, tuple(t["id"] for t in ready))
        task_id = head["id"]
        self._leases.acquire(task_id, self._worker_id, self._ttl)
        try:
            self._executor.start_next(started_at)
        except BaseException:
            self._leases.release(task_id, self._worker_id)  # 台账变更失败不留下了租约
            raise
        return DispatchOutcome("claimed", task_id, tuple(t["id"] for t in ready[1:]))

    def finish(self, outcome, failure_reason=None):
        """结束当前 running 任务（含失败传播）：收尾不经过领取门——预算耗尽
        不吞已领取工作；随后释放租约。返回任务编号。"""
        task_id = self._executor.finish(outcome, failure_reason=failure_reason)
        self._leases.release(task_id, self._worker_id)
        return task_id

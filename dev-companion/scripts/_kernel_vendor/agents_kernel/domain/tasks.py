"""Feature/Task/Attempt 三层任务模型与状态转换白名单（stdlib only，无 IO）。

06 计划 §1/§2 的最小落点：Feature 是业务功能（状态语义与 dev-companion 对齐：
draft 待确认 → confirmed 范围已确认 → in_progress → delivered → accepted 验收）；
Task 是功能下有明确产物的职责单元，带依赖（DAG）、优先级与状态机；Attempt 是
Task 的一次运行尝试——同任务重试开新 attempt，attempt 计数与 Task 最终状态
分开记（TK08：失败重试不虚增完成度）。

Z24 统一入口：凡进入 blocked/cancelled 的状态变更必须走 block()/cancel()，
两者都要求显式 reason 与阻断链（证据处理规则显式化），transition_task 拒绝
这两个目标；本轮只统一状态转换规则，证据存储联动归 P5-02。

本模块只建模型，不写库：对 P2-02 事件库仅提供 to_event_payload()/from_event()
纯函数桥接（持久化接线归 P5-02）；写隔离/租约归 P5-02/03；旧 require_idle
串行保护不受影响。P5-04：feature 的 review_required policy 开启时，impl/review
任务的 done 前置独立审查门（G-REVIEW），policy 缺省关闭，旧项目不受影响。
"""
from agents_kernel.domain.views import EVENT_FEATURE_STATUS, EVENT_TASK_STATUS
from agents_kernel.validation import CompanionError, relative_path, strings, text
from agents_kernel.validation import feature_id as valid_feature_id

# Feature 状态：与 dev-companion 的范围确认/交付/用户验收语义对齐。
FEATURE_STATUSES = ("draft", "confirmed", "in_progress", "delivered", "accepted")
# Task 状态：cancelled 为显式取消终态（区别于 failed——取消没有失败证据）。
TASK_STATUSES = ("pending", "ready", "running", "done", "failed", "blocked", "cancelled")
TASK_KINDS = ("design", "impl", "review", "integration", "release")
ATTEMPT_OUTCOMES = ("pending", "succeeded", "failed")

# 白名单（不含 blocked/cancelled 目标：它们只能经 block()/cancel() 统一入口进入）。
# blocked → ready：阻断解除（阻断链清空）；failed → ready：重试，重开新 attempt。
FEATURE_TRANSITIONS = {
    "draft": frozenset({"confirmed"}),
    "confirmed": frozenset({"in_progress", "draft"}),  # 范围修订退回 draft 待重新确认
    "in_progress": frozenset({"delivered"}),
    "delivered": frozenset({"accepted", "in_progress"}),  # 验收打回返工
    "accepted": frozenset(),
}
TASK_TRANSITIONS = {
    "pending": frozenset({"ready"}),
    "ready": frozenset({"running"}),
    "running": frozenset({"done", "failed"}),
    "done": frozenset(),
    "failed": frozenset({"ready"}),
    "blocked": frozenset({"ready"}),
    "cancelled": frozenset(),
}
# 允许被 block()/cancel() 的来源状态（终态与已完成不可再动）。
_BLOCKABLE_FROM = frozenset({"pending", "ready", "running", "blocked"})
_CANCELLABLE_FROM = frozenset({"pending", "ready", "running", "failed", "blocked"})


def check_transition(table, from_status, to_status, what):
    """白名单校验：非法转换结构化报错（CompanionError，含起止状态）。"""
    if to_status not in table:
        raise CompanionError("未知%s状态：%s" % (what, to_status))
    if from_status not in table:
        raise CompanionError("未知%s状态：%s" % (what, from_status))
    if to_status not in table[from_status]:
        raise CompanionError("非法%s状态转换：%s → %s（白名单外）" % (what, from_status, to_status))


def to_event_payload(entity):
    """Feature/Task 状态 → P2-02 事件 payload（纯函数，不写库）。

    Task 带 feature_id → task_status 事件；Feature → feature_status 事件。
    字段与 domain/views 折叠规则的最小集一一对应。
    """
    if "feature_id" in entity:
        return {"feature_id": entity["feature_id"], "status": entity["status"]}
    return {"title": entity["title"], "status": entity["status"]}


def from_event(event_type, entity_id, payload):
    """P2-02 事件 → 实体状态片段（纯函数）。状态词按本模块白名单收紧校验。"""
    if event_type == EVENT_TASK_STATUS:
        status = text(payload.get("status"), "任务状态")
        if status not in TASK_STATUSES:
            raise CompanionError("未知任务状态：%s" % status)
        return {"id": text(entity_id, "任务编号"), "feature_id": text(payload.get("feature_id"), "所属功能"),
                "status": status}
    if event_type == EVENT_FEATURE_STATUS:
        status = text(payload.get("status"), "功能状态")
        if status not in FEATURE_STATUSES:
            raise CompanionError("未知功能状态：%s" % status)
        return {"id": text(entity_id, "功能编号"), "title": payload.get("title", ""), "status": status}
    raise CompanionError("未知事件类型：%s" % event_type)


class TaskBoard:
    """Feature/Task/Attempt 台账（内存）。所有变更经白名单校验，读取返回副本。"""

    def __init__(self):
        self._features = {}
        self._tasks = {}
        self._attempts = {}  # task_id -> [attempt, ...]，attempt_no 从 1 递增
        # G-REVIEW done 门回调（P5-04：由 ReviewBoard.require_approval 接线）；
        # 仅当 feature 的 review_required policy 开启时被调用，None = 未接入审查台。
        self.review_guard = None

    # ---- Feature ----

    def add_feature(self, feature_id, title, allowed_paths=(), review_required=False):
        if not isinstance(review_required, bool):
            raise CompanionError("review_required 必须是布尔")
        fid = valid_feature_id(feature_id, self._features)
        entry = {"id": fid, "title": text(title, "功能标题"), "status": "draft",
                 "allowed_paths": [relative_path(p) for p in list(allowed_paths)],
                 "review_required": review_required}
        self._features[fid] = entry
        return dict(entry)

    def feature(self, fid):
        entry = self._features.get(text(fid, "功能编号"))
        if entry is None:
            raise CompanionError("功能不存在：%s" % fid)
        entry = dict(entry)
        entry["allowed_paths"] = list(entry["allowed_paths"])
        return entry

    def features(self):
        return [self.feature(fid) for fid in sorted(self._features)]

    def transition_feature(self, fid, to_status):
        entry = self._features[self.feature(fid)["id"]]
        check_transition(FEATURE_TRANSITIONS, entry["status"], to_status, "功能")
        entry["status"] = to_status
        return self.feature(fid)

    # ---- Task ----

    def add_task(self, task_id, feature_id, kind, depends_on=(), priority=0,
                 allowed_paths=()):
        tid = text(task_id, "任务编号")
        if tid in self._tasks:
            raise CompanionError("任务编号已存在：%s" % tid)
        self.feature(feature_id)  # 必须挂在已存在功能下
        if kind not in TASK_KINDS:
            raise CompanionError("未知任务类型：%s" % kind)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise CompanionError("优先级必须是整数")
        deps = strings(list(depends_on), "依赖任务")
        if len(set(deps)) != len(deps):
            raise CompanionError("依赖任务重复：%s" % tid)
        for dep in deps:
            if dep not in self._tasks:
                raise CompanionError("依赖任务不存在：%s → %s" % (tid, dep))
        paths = [relative_path(p) for p in list(allowed_paths)]
        entry = {"id": tid, "feature_id": text(feature_id, "所属功能"), "kind": kind,
                 "depends_on": deps, "priority": priority,
                 "allowed_paths": list(dict.fromkeys(paths)), "status": "pending",
                 "block_reason": None, "blocked_by": []}
        self._tasks[tid] = entry
        self._attempts[tid] = []
        return self.task(tid)

    def adopt_task(self, task_id, feature_id, kind, depends_on=(), priority=0,
                   allowed_paths=(), status="pending", block_reason=None,
                   blocked_by=()):
        """回放/迁移专用：按已落库的历史事实登记任务，不做白名单重审。

        门禁只约束新写入；事件库里既存状态（迁移导入的直接 done/running、无
        attempt 记录的历史事实）原样重建——否则历史无法重放进台账，门禁前置
        查询会把合法历史误判为违规。仅 storage.team 事件回放调用。
        """
        entry = self.add_task(task_id, feature_id, kind, depends_on=depends_on,
                              priority=priority, allowed_paths=allowed_paths)
        if status not in TASK_STATUSES:
            raise CompanionError("未知任务状态：%s" % status)
        raw = self._tasks[entry["id"]]
        raw["status"] = status
        raw["block_reason"] = block_reason
        raw["blocked_by"] = list(blocked_by)
        return self.task(entry["id"])

    def task(self, tid):
        entry = self._tasks.get(text(tid, "任务编号"))
        if entry is None:
            raise CompanionError("任务不存在：%s" % tid)
        entry = dict(entry)
        entry["blocked_by"] = list(entry["blocked_by"])
        entry["allowed_paths"] = list(entry["allowed_paths"])
        return entry

    def tasks(self):
        return [self.task(tid) for tid in sorted(self._tasks)]

    def transition_task(self, tid, to_status):
        entry = self._tasks[self.task(tid)["id"]]
        if to_status in ("blocked", "cancelled"):
            entry_name = {"blocked": "block", "cancelled": "cancel"}[to_status]
            raise CompanionError("进入 %s 必须经统一入口 %s()（Z24）" % (to_status, entry_name))
        check_transition(TASK_TRANSITIONS, entry["status"], to_status, "任务")
        if to_status == "done":
            self._gate_done(entry)  # G-REVIEW：done 前置门（仅 policy 开启时生效）
        entry["status"] = to_status
        entry["block_reason"] = None  # blocked → ready 等离开阻断态时清空阻断记录
        entry["blocked_by"] = []
        return self.task(tid)

    def block(self, tid, reason, blocked_by=()):
        """Z24 统一入口：任何进入 blocked 的变更都必须给出显式原因与阻断链。"""
        entry = self._tasks[self.task(tid)["id"]]
        if entry["status"] not in _BLOCKABLE_FROM:
            raise CompanionError("任务 %s 处于 %s，不可 block" % (tid, entry["status"]))
        reason = text(reason, "阻断原因")  # 先校验后变更：拒绝时不留半套状态
        chain = strings(list(blocked_by), "阻断链")
        entry["status"] = "blocked"
        entry["block_reason"] = reason
        entry["blocked_by"] = chain
        return self.task(tid)

    def cancel(self, tid, reason):
        """Z24 统一入口：显式取消（终态），必须给出原因。"""
        entry = self._tasks[self.task(tid)["id"]]
        if entry["status"] not in _CANCELLABLE_FROM:
            raise CompanionError("任务 %s 处于 %s，不可取消" % (tid, entry["status"]))
        reason = text(reason, "取消原因")  # 先校验后变更
        entry["status"] = "cancelled"
        entry["block_reason"] = reason
        entry["blocked_by"] = []
        return self.task(tid)

    def _gate_done(self, entry):
        """G-REVIEW（P5-04）：feature policy 开启时，impl/review 任务 done 前
        必须有有效独立审查批准（review_guard 由 ReviewBoard 接线）。
        policy 缺省关闭：未开启或非 impl/review 任务不经过此门（向后兼容）。"""
        if entry["kind"] not in ("impl", "review"):
            return
        feature = self._features[entry["feature_id"]]
        if not feature["review_required"]:
            return
        if self.review_guard is None:
            raise CompanionError("功能 %s 要求独立审查，但未接入审查台（G-REVIEW）" % feature["id"])
        self.review_guard(entry["id"])

    # ---- Attempt（TK08：attempt 计数与 Task 最终状态分开） ----

    def start_attempt(self, tid, started_at):
        entry = self._tasks[self.task(tid)["id"]]
        if entry["status"] != "running":
            raise CompanionError("任务 %s 未处于 running，不能开 attempt（当前 %s）"
                                 % (tid, entry["status"]))
        attempt = {"task_id": entry["id"], "attempt_no": len(self._attempts[tid]) + 1,
                   "started_at": text(started_at, "开始时间"),
                   "outcome": "pending", "failure_reason": None}
        self._attempts[tid].append(attempt)
        return dict(attempt)

    def finish_attempt(self, tid, outcome, failure_reason=None):
        entry = self._tasks[self.task(tid)["id"]]
        if outcome not in ("succeeded", "failed"):
            raise CompanionError("attempt 结局只能是 succeeded/failed：%s" % outcome)
        attempts = self._attempts[tid]
        if not attempts or attempts[-1]["outcome"] != "pending":
            raise CompanionError("任务 %s 没有进行中的 attempt" % tid)
        if outcome == "succeeded":
            self._gate_done(entry)  # 先过门再变更：拒绝时不留半套状态
        attempts[-1]["outcome"] = outcome
        attempts[-1]["failure_reason"] = text(failure_reason, "失败原因") if outcome == "failed" else None
        check_transition(TASK_TRANSITIONS, entry["status"], "done" if outcome == "succeeded" else "failed",
                         "任务")
        entry["status"] = "done" if outcome == "succeeded" else "failed"
        return dict(attempts[-1])

    def attempts(self, tid):
        return [dict(attempt) for attempt in self._attempts[self.task(tid)["id"]]]

    def attempt_count(self, tid):
        return len(self._attempts[self.task(tid)["id"]])

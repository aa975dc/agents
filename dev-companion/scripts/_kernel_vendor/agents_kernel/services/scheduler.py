"""纯逻辑 DAG 调度器（stdlib only，无 IO）：ready 集、环检测、传播、串行适配。

ready_set 只回答「现在能派发什么」：依赖全部 done 且自身 pending。建图即校验
（G-READY 的机器前置）：依赖必须存在；DFS 三色标记做环检测，有环整体拒绝并
报出完整环路径。失败沿依赖反向传播成 blocked 链（记录阻断链）；显式取消同理
传播到全部非终态传递依赖。

SerialExecutor 是旧 require_idle 串行保护的模型层等价物：任一时刻至多一个任务
running——轻量模式按它跑即可，不删除也不绕过 dev-companion 现有串行保护。
ParallelReady 只是返回 ready 集合，不构成并行执行承诺：写隔离、租约与独立
工作区归 P5-02/03，隔离未证明之前并行写一律不启用。
"""
from collections import deque

from agents_kernel.validation import CompanionError

_TERMINAL = frozenset({"done", "cancelled"})
_CANCEL_TARGETS = frozenset({"pending", "ready", "running", "blocked"})


def build_graph(tasks):
    """建依赖图：依赖必须存在；DFS 环检测，有环报完整环路径。返回 {task_id: [deps]}。"""
    graph = {}
    known = {task["id"] for task in tasks}
    for task in tasks:
        unknown = [dep for dep in task["depends_on"] if dep not in known]
        if unknown:
            raise CompanionError("依赖任务不存在：%s → %s" % (task["id"], ",".join(unknown)))
        graph[task["id"]] = list(task["depends_on"])

    state = {}  # 1=在当前 DFS 栈上，2=已完成
    path = []

    def visit(node):
        state[node] = 1
        path.append(node)
        for dep in graph[node]:
            if state.get(dep) == 1:
                cycle = path[path.index(dep):] + [dep]
                raise CompanionError("依赖图存在环：" + " → ".join(cycle))
            if state.get(dep) != 2:
                visit(dep)
        path.pop()
        state[node] = 2

    for node in sorted(graph):
        if state.get(node) != 2:
            visit(node)
    return graph


def ready_set(tasks, graph=None):
    """当前可派发集：自身 pending 且依赖全部 done。"""
    if graph is None:
        graph = build_graph(tasks)
    status = {task["id"]: task["status"] for task in tasks}
    return [task for task in tasks
            if task["status"] == "pending" and all(status[dep] == "done" for dep in graph[task["id"]])]


def order_ready(ready):
    """ready 内排序：优先级数值小者优先，再按所属功能分组，同组按任务编号稳定。"""
    return sorted(ready, key=lambda task: (task["priority"], task["feature_id"], task["id"]))


def propagate_failure(tasks, failed_task_id):
    """依赖失败传播：返回 {被阻断任务: 阻断链}（纯函数，不改输入）。

    从 failed 任务沿反向边传递闭包：pending/ready/blocked 的传递依赖被阻断，
    阻断链 = 其依赖中已 failed/blocked 或已在阻断计划中的任务（按依赖声明序）。
    done/failed/cancelled/running 的依赖保持原状（自有事实不被覆盖）。
    """
    by_id = {task["id"]: task for task in tasks}
    root = by_id.get(failed_task_id)
    if root is None:
        raise CompanionError("任务不存在：%s" % failed_task_id)
    if root["status"] != "failed":
        raise CompanionError("只有 failed 任务可传播失败：%s 当前 %s" % (failed_task_id, root["status"]))
    dependents = {}
    for task in tasks:
        for dep in task["depends_on"]:
            dependents.setdefault(dep, []).append(task["id"])
    plan = {}
    queue = deque([failed_task_id])
    while queue:
        current = queue.popleft()
        for child in sorted(dependents.get(current, [])):
            task = by_id[child]
            if task["status"] in _TERMINAL or task["status"] == "running":
                continue
            chain = [dep for dep in task["depends_on"]
                     if dep in plan or by_id[dep]["status"] in ("failed", "blocked")]
            plan[child] = chain
            queue.append(child)
    return plan


def propagate_cancel(tasks, cancelled_task_id):
    """显式取消传播：返回应随根任务一起取消的非终态传递依赖（BFS 序，纯函数）。

    done/failed/cancelled 不动：完成事实保留，failed 依赖保留自有失败证据。
    """
    by_id = {task["id"]: task for task in tasks}
    if cancelled_task_id not in by_id:
        raise CompanionError("任务不存在：%s" % cancelled_task_id)
    dependents = {}
    for task in tasks:
        for dep in task["depends_on"]:
            dependents.setdefault(dep, []).append(task["id"])
    order, seen = [], {cancelled_task_id}
    queue = deque([cancelled_task_id])
    while queue:
        current = queue.popleft()
        for child in sorted(dependents.get(current, [])):
            if child in seen:
                continue
            seen.add(child)
            queue.append(child)
            if by_id[child]["status"] in _CANCEL_TARGETS:
                order.append(child)
    return order


class SerialExecutor:
    """串行执行语义（无 IO）：任一时刻至多一个任务 running。

    与旧 require_idle 串行保护等价的模型层：start_next 前置检查当前无 running，
    违反即拒绝；结束当前任务（含失败传播）后才允许取下一个。
    """

    def __init__(self, board):
        self._board = board

    def current(self):
        for task in self._board.tasks():
            if task["status"] == "running":
                return task["id"]
        return None

    def start_next(self, started_at):
        """取优先级最高的 ready 任务转 running 并开新 attempt；无 ready 返回 None。"""
        if self.current() is not None:
            raise CompanionError("串行模式同一时刻至多一个运行中任务（require_idle 语义）")
        ready = order_ready(ready_set(self._board.tasks()))
        if not ready:
            return None
        task_id = ready[0]["id"]
        self._board.transition_task(task_id, "ready")  # 走白名单路径：pending → ready → running
        self._board.transition_task(task_id, "running")
        self._board.start_attempt(task_id, started_at)
        return task_id

    def finish(self, outcome, failure_reason=None):
        """结束当前 running 任务的 attempt；失败时按阻断链把传递依赖置 blocked。"""
        task_id = self.current()
        if task_id is None:
            raise CompanionError("当前没有运行中的任务")
        self._board.finish_attempt(task_id, outcome, failure_reason=failure_reason)
        if outcome == "failed":
            for blocked_id, chain in propagate_failure(self._board.tasks(), task_id).items():
                self._board.block(blocked_id, "上游任务失败：%s" % task_id, chain)
        return task_id


class ParallelReady:
    """并行就绪视图：只暴露 ready 集合，不承诺并行执行。

    写隔离、租约与独立工作区是 P5-02/03 的事；隔离未证明之前，拿到本集合的
    调用方仍必须串行派发或仅派只读任务，不得据此并行写同一工作区。
    """

    def __init__(self, board):
        self._board = board

    def ready(self):
        return order_ready(ready_set(self._board.tasks()))

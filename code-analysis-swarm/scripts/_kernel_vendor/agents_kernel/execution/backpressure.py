"""并发背压闸门：有界 permit、FIFO 排队、gated_run 助手（stdlib only, Py3.9+）。

03_CAPACITY_AND_INDEXING.md §7（C13 尾）：
- 默认模型并发 3、可配；超大规模通过增加持久队列长度消化，不通过无限
  Promise.all——把全部任务一次性扇出的无界并发被本闸门在结构上排除：
  超过 permit 数的请求在闸门前 FIFO 排队等待，而不是同时压向模型。
- FIFO：唤醒顺序严格等于排队顺序（队首未获permit不得越过队首），不依赖
  调度器唤醒次序。
- 调用约定：任何并发扇出必须经 Gate/gated_run；`asyncio.gather(*裸任务)`、
  `concurrent.futures` 裸池按约定禁用于模型调用路径（本模块提供的是
  线程侧的同步实现，异步侧沿用同一 permit 语义接入）。

边界：进程内闸门，不跨进程；与 execution.lease（跨进程互斥）、backoff
（重试节奏）正交，不改其既有语义。
"""
import threading

from agents_kernel.validation import CompanionError

DEFAULT_MAX_PERMITS = 3  # §7：默认模型并发 3


class Gate:
    """信号量式 permit 闸门：acquire 阻塞排队（FIFO），release 归还并唤醒队首。"""

    def __init__(self, max_permits=DEFAULT_MAX_PERMITS):
        if not isinstance(max_permits, int) or isinstance(max_permits, bool) or max_permits < 1:
            raise CompanionError("max_permits 必须是正整数")
        self._max = max_permits
        self._active = 0
        self._cv = threading.Condition()
        self._queue = []  # 排队中的 waiter（线程身份），队首最先获 permit

    @property
    def max_permits(self):
        return self._max

    @property
    def active(self):
        """当前持有 permit 的请求数（峰值并发观测用）。"""
        with self._cv:
            return self._active

    @property
    def waiting(self):
        """当前排队等待的请求数（背压深度观测用）。"""
        with self._cv:
            return len(self._queue)

    def acquire(self):
        """取一个 permit；无 permit 时 FIFO 排队阻塞，直到轮到队首且有空闲。

        并发上限由 active 计数把守（与持有者身份无关）；同一线程可持有多个
        permit（配对 acquire/release 由调用方保证，如主线程占位排空队列）。
        """
        me = threading.current_thread()
        with self._cv:
            self._queue.append(me)
            try:
                while self._queue[0] is not me or self._active >= self._max:
                    self._cv.wait()
                self._queue.pop(0)
                self._active += 1
                self._cv.notify_all()  # 授予改变了队首/余量，唤醒下一位符合条件的等待者
            except BaseException:
                # 等待中被中断：退出队列，避免占着队首堵死后续 waiter。
                if me in self._queue:
                    self._queue.remove(me)
                    self._cv.notify_all()
                raise
            return None

    def release(self):
        """归还 permit 并唤醒队首；无在持 permit 时拒绝。"""
        with self._cv:
            if self._active < 1:
                raise CompanionError("release 无持有的 permit")
            self._active -= 1
            self._cv.notify_all()


def gated_run(gate, fns):
    """有界并发执行零参可调用列表：峰值并发 ≤ gate.max_permits，结果按提交序返回。

    FIFO 承诺：第 k 个获准执行的是排队序第 k 个请求。列表中某个 fn 抛错时，
    等其所在线程结束后把第一个异常原样抛出（已开始的并发自然收尾，不泄漏 permit）。
    传生成器亦可；这就是"不用无限 Promise.all"的调用形态——扇出永远过闸门。
    """
    fns = list(fns)
    results = [None] * len(fns)
    errors = [None] * len(fns)

    def worker(i, fn):
        try:
            gate.acquire()
            try:
                results[i] = fn()
            finally:
                gate.release()
        except BaseException as exc:  # fn 或 acquire 的异常：记账后统一上抛
            errors[i] = exc

    threads = []
    for i, fn in enumerate(fns):
        thread = threading.Thread(target=worker, args=(i, fn), daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()
    for exc in errors:
        if exc is not None:
            raise exc
    return results

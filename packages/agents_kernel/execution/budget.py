"""活动预算：token 预算驱动的有界消费、暂停与检查点续跑（stdlib only, Py3.9+）。

03_CAPACITY_AND_INDEXING.md §3/§7、CV02 前半：
- 预算耗尽是显式 paused：返回续跑游标（已处理到的 batch_index/文件锚）。
  暂停不是降级——绝不隐式改成抽样、截断或 targeted 后谎报完成。
- try_consume 原子：剩余不足即拒且分文不扣；消费以批为单位（批内不拆分），
  每批有界，批数不限、全部覆盖。
- 检查点是独立 JSON 文件（atomicio.write_json 原子写），跨"活动"（进程/批次）
  持久化；resume 从游标继续，跨活动总处理量==全量、不重不漏。
- token 口径与 indexing.slicing 相同：est_tokens 是启发式估算，
  检查点恒标 estimation=true，不得据字符数宣称精确 token。
"""
from typing import NamedTuple

from agents_kernel.atomicio import read_json, write_json
from agents_kernel.validation import CompanionError

CHECKPOINT_VERSION = 1
STATUS_COMPLETE = "complete"
STATUS_PAUSED = "paused"


class Budget:
    """一次活动的 token 消耗账本：整额扣减、不足即停，不含任何抽样逻辑。"""

    def __init__(self, total_tokens, spent=0):
        if not isinstance(total_tokens, int) or isinstance(total_tokens, bool) or total_tokens < 0:
            raise CompanionError("total_tokens 必须是非负整数")
        if not isinstance(spent, int) or isinstance(spent, bool) or spent < 0:
            raise CompanionError("spent 必须是非负整数")
        if spent > total_tokens:
            raise CompanionError("spent %d 超过 total_tokens %d，检查点非法" % (spent, total_tokens))
        self._total = total_tokens
        self._spent = spent

    @property
    def total_tokens(self):
        return self._total

    @property
    def spent(self):
        return self._spent

    @property
    def remaining(self):
        return self._total - self._spent

    def try_consume(self, n):
        """整额扣减：剩余足够才扣并返回 True；不足返回 False 且分文不扣。"""
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            raise CompanionError("consume 量必须是非负整数")
        if n > self.remaining:
            return False
        self._spent += n
        return True

    def checkpoint(self):
        return {"version": CHECKPOINT_VERSION, "total_tokens": self._total,
                "spent": self._spent, "estimation": True}


class BudgetOutcome(NamedTuple):
    status: str      # "complete" | "paused"
    processed: int   # 本次活动完成的批数
    cursor: int      # 下一个未处理批的 batch_index；complete 时等于批总数
    spent: int       # 预算累计已消耗（含此前活动）


def spend_batches(batches, budget, start_cursor=0):
    """按 est_tokens 逐批扣预算消费 batches（indexing.slicing.slice_claims 的输出）。

    预算不足在批边界暂停：已处理的是严格前缀 [0, cursor)，批内不拆、不跳批、
    不抽样；配合 save_checkpoint/resume 可跨活动把全部批处理完（不重不漏）。
    """
    if not isinstance(start_cursor, int) or isinstance(start_cursor, bool) \
            or not 0 <= start_cursor <= len(batches):
        raise CompanionError("start_cursor 必须是批范围内的整数")
    processed = 0
    for index in range(start_cursor, len(batches)):
        if not budget.try_consume(batches[index][2]):
            return BudgetOutcome(STATUS_PAUSED, processed, index, budget.spent)
        processed += 1
    return BudgetOutcome(STATUS_COMPLETE, processed, len(batches), budget.spent)


def save_checkpoint(path, budget, cursor, extra=None):
    """原子写预算检查点；cursor 是续跑游标（batch_index 或文件锚等 JSON 值）。"""
    payload = budget.checkpoint()
    payload["cursor"] = cursor
    if extra is not None:
        payload["extra"] = extra
    write_json(path, payload)
    return payload


def load_checkpoint(path):
    data = read_json(path)
    if not isinstance(data, dict) or data.get("version") != CHECKPOINT_VERSION \
            or not isinstance(data.get("total_tokens"), int) \
            or not isinstance(data.get("spent"), int) \
            or "cursor" not in data:
        raise CompanionError("预算检查点字段不完整或版本不符：%s" % path)
    return data


def resume(path):
    """从检查点恢复 (Budget, cursor)；调用方从 cursor 继续，不重不漏。"""
    data = load_checkpoint(path)
    return Budget(data["total_tokens"], data["spent"]), data["cursor"]

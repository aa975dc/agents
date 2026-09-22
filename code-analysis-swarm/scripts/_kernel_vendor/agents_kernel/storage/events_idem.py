"""事件幂等消费辅助（只读，不改动 db.py/events.py 既有函数；stdlib only, Py3.9+）。

P2-02 的 append_event 对同幂等键重放直接返回 deduped=True，不比较内容；而
06_TASK_ISOLATION §4 要求"重复同内容返回既有结果，重复不同内容拒绝"。因此
消费方（回报导入等）拿到 deduped=True 后需要：按幂等键取回既有事件、用与
写入端同一套规范化口径核对内容。本模块只补这两块读侧能力：

- canonical_digest：规范化 JSON（ensure_ascii=False、sort_keys，与 append_event
  的序列化口径一致）的 SHA-256，是幂等"内容相同/不同"的唯一判定依据；
- event_by_idempotency_key：按幂等键读回既有事件（payload 已解析）。

绝不写库：本模块没有写入口，变更仍只能经 storage.events.append_event。
"""
import hashlib
import json

from agents_kernel.validation import CompanionError, text


def canonical_digest(payload):
    """规范化 JSON 摘要：幂等内容比较的唯一口径（写入与核对两侧同源）。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def event_by_idempotency_key(store, key):
    """按幂等键读回既有事件（含解析后的 payload）；无则返回 None。只读。"""
    key = text(key, "幂等键")
    row = store.query_one("SELECT * FROM events WHERE idempotency_key = ?", (key,))
    if row is None:
        return None
    item = dict(row)
    item["payload"] = json.loads(item["payload"])
    return item

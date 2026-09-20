"""回报幂等去重：同键同内容去重、同键异内容拒绝（stdlib only, Py3.9+）。

06 计划 §4：回报导入用 idempotency_key 防重复——重复同内容返回既有结果，
重复不同内容拒绝。接线 P2-02 事件幂等键语义：
- 键形状 report_key() = "attempt-report:<task_id>:<attempt_no>:<outcome>"，
  与 (task_id, attempt_no, outcome) 一一对应，不同 attempt 互不冲突；
- 内容口径 = storage.events_idem.canonical_digest（与 append_event 的
  sort_keys 规范化同源）：同键同 digest → 去重并返回首次登记（deduped=True）；
  同键不同 digest → CompanionError 拒绝（矛盾回报，须人工核查）。

边界：本模块用登记簿文件落地这套语义（登记文件原子写 + before_replace 复查）；
把回报事件接进 P2-02 事实库需在 domain.views 登记新事件类型与折叠规则
（domain 不在本任务范围），归 P5-05 持久化调度。租约层保证的是 at-least-once，
"恰好一次"由这里的幂等键去重达成，不靠租约本身声称。
"""
import json
from pathlib import Path

from agents_kernel.atomicio import write_atomic
from agents_kernel.storage.events_idem import canonical_digest
from agents_kernel.validation import CompanionError, text

REPORT_VERSION = 1
# 回报是终态：pending 不是可回报的结局（ATTEMPT_OUTCOMES 里仅后两者可上报）。
REPORT_OUTCOMES = ("succeeded", "failed")


def report_key(task_id, attempt_no, outcome):
    """回报幂等键：同 (task_id, attempt_no, outcome) 恒得同键。"""
    tid = _safe_id(task_id)
    if isinstance(attempt_no, bool) or not isinstance(attempt_no, int) or attempt_no < 1:
        raise CompanionError("attempt_no 必须是正整数")
    if outcome not in REPORT_OUTCOMES:
        raise CompanionError("回报结局只能是 succeeded/failed：%s" % (outcome,))
    return "attempt-report:%s:%d:%s" % (tid, attempt_no, outcome)


def _safe_id(task_id):
    tid = text(task_id, "任务编号")
    if "/" in tid or "\\" in tid or tid in (".", ".."):
        raise CompanionError("任务编号不能作登记文件名：%s" % tid)
    return tid


class ReportRegistry:
    """回报登记簿：每键一个 JSON 登记（原子写），去重与冲突检测的落点。"""

    def __init__(self, directory):
        self._dir = Path(directory)

    def submit(self, task_id, attempt_no, outcome, payload=None):
        """提交一次回报。首报 accepted=True；同键同内容 → deduped=True 并返回
        首次登记；同键异内容 → 拒绝。payload 是回报内容（证据摘要等）。"""
        key = report_key(task_id, attempt_no, outcome)
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise CompanionError("回报 payload 必须是对象")
        record = {"version": REPORT_VERSION, "key": key, "task_id": _safe_id(task_id),
                  "attempt_no": attempt_no, "outcome": outcome, "payload": payload,
                  "digest": canonical_digest(payload)}
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / ("%s.json" % key)
        if path.exists():
            existing = self._read(path)
            if existing["digest"] == record["digest"]:
                return {"accepted": False, "deduped": True, "report": existing}
            raise CompanionError(
                "回报冲突：幂等键 %s 已登记不同内容（拒绝迟到/矛盾回报）" % key)

        serialized = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")

        def guard():
            if path.exists() and self._read(path)["digest"] != record["digest"]:
                raise CompanionError("回报冲突：幂等键 %s 正被并发写入不同内容" % key)

        write_atomic(path, serialized, before_replace=guard)
        return {"accepted": True, "deduped": False, "report": record}

    @staticmethod
    def _read(path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CompanionError("回报登记损坏：%s" % path) from exc
        if not isinstance(data, dict) or "digest" not in data:
            raise CompanionError("回报登记字段不全：%s" % path)
        return data

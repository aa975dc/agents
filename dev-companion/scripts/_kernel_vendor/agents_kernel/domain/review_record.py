"""审查记录（ReviewRecord）：固定版本审查的最小值对象（stdlib only）。

P5-04 把 P4-05 联审门的"绑定 sha256"接到 P5-01 任务模型：被审 subject 固定为
attempt 产出（subject_type="attempt"，subject_ref 是 attempt_id）或登记表产物
（handoff/contract，subject_ref 是登记表编号）；审查者以 {role, attempt_id}
署名，审查台据此比对独立性——实现者 attempt 不得自审。记录只陈述"谁在何时对
哪个固定版本下了什么结论"；批准是否仍然有效（审后修改失效）由 ReviewBoard 按
当前 sha256 判定。

schema 校验复用 P4-05 风格：contracts.schemas.validate_review_record 返回结构化
错误列表，本模块包装成 CompanionError，拒绝不落账。
"""
from agents_kernel.contracts import schemas
from agents_kernel.validation import CompanionError, text

SUBJECT_TYPES = schemas.RECORD_SUBJECT_TYPES


def attempt_id(task_id, attempt_no):
    """attempt 的稳定引用（"任务编号#第几次"）：审查台用它指认被审/署名双方。"""
    tid = text(task_id, "任务编号")
    if isinstance(attempt_no, bool) or not isinstance(attempt_no, int) or attempt_no < 1:
        raise CompanionError("attempt_no 必须是正整数")
    return "%s#%d" % (tid, attempt_no)


class ReviewRecord:
    """一条独立审查结论：构造即校验的值对象，外部只拿到副本。"""

    def __init__(self, review_id, subject_type, subject_ref, subject_sha256,
                 reviewer, verdict, blockers=(), reviewed_at=None):
        record = {
            "review_id": review_id,
            "subject_type": subject_type,
            "subject_ref": subject_ref,
            "subject_sha256": subject_sha256,
            "reviewer": reviewer,
            "verdict": verdict,
            "blockers": list(blockers) if isinstance(blockers, (list, tuple)) else blockers,
            "reviewed_at": reviewed_at,
        }
        errors = schemas.validate_review_record(record)
        if errors:
            raise CompanionError("审查记录校验失败 %s：%s"
                                 % (record.get("review_id"), schemas.format_errors(errors)))
        self._record = record

    @classmethod
    def coerce(cls, value):
        """接受 dict 或 ReviewRecord，统一成校验过的 ReviewRecord。"""
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls(value.get("review_id"), value.get("subject_type"),
                       value.get("subject_ref"), value.get("subject_sha256"),
                       value.get("reviewer"), value.get("verdict"),
                       value.get("blockers") or (), value.get("reviewed_at"))
        raise CompanionError("审查记录必须是 JSON 对象或 ReviewRecord")

    def to_dict(self):
        record = dict(self._record)
        record["blockers"] = list(record["blockers"])
        record["reviewer"] = dict(record["reviewer"])
        return record

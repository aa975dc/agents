"""设计联审门（G-DESIGN/G-REVIEW 的最小机器形态，stdlib only）。

两条硬规则：实现者不得担任唯一独立审查者（不能自己唯一审查，TK06）；
approved 记录绑定被审产物的内容 sha256——产物在登记表换版（审后修改）后
批准自动失效，须重新审查。changes_requested 必须逐条写阻塞项，拒绝空泛否决。
记录存内存；接入台账事件属后续任务。
"""
from agents_kernel.contracts import schemas
from agents_kernel.validation import CompanionError, text


class ReviewGate:
    def __init__(self, registry):
        self._registry = registry
        self._records = []

    def submit(self, gate_id, subject_type, subject_id, subject_sha256, verdict,
               reviewer_role, blockers=(), implementer_role=None):
        """提交一条审查结论；通过校验与绑定核对后落记录，返回记录副本。"""
        record = {
            "gate_id": text(gate_id, "门记录编号"),
            "subject_type": text(subject_type, "被审产物类型"),
            "subject_id": text(subject_id, "被审产物编号"),
            "subject_sha256": subject_sha256,
            "verdict": verdict,
            "reviewer_role": text(reviewer_role, "审查者角色"),
            "blockers": list(blockers),
        }
        errors = schemas.validate_review_gate(record)
        if errors:
            raise CompanionError("联审记录校验失败 %s：%s"
                                 % (record["gate_id"], schemas.format_errors(errors)))
        if implementer_role is not None and record["reviewer_role"] == implementer_role:
            raise CompanionError("实现者不得担任唯一独立审查者：%s" % record["reviewer_role"])
        if any(existing["gate_id"] == record["gate_id"] for existing in self._records):
            raise CompanionError("联审记录编号已存在：%s" % record["gate_id"])
        # 审查对象必须是登记表中的当前版本：产物已变更则先重新登记，再重审。
        self._registry.require_current(record["subject_id"], record["subject_sha256"],
                                       record["subject_type"])
        self._records.append(record)
        return dict(record)

    def verdicts(self, subject_id):
        return [dict(record) for record in self._records if record["subject_id"] == subject_id]

    def is_approved(self, subject_id):
        """仅当存在 approved 且其绑定 sha256 等于登记表当前 sha256（审后修改自动失效）。"""
        entry = self._registry.get(subject_id)
        if entry is None:
            return False
        return any(record["verdict"] == "approved" and record["subject_sha256"] == entry["sha256"]
                   for record in self._records if record["subject_id"] == subject_id)

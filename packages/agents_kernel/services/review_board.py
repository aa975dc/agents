"""审查台（ReviewBoard）：固定版本审查的服务侧编排（stdlib only，内存）。

连接 P5-01 任务模型与 P4-05 登记/联审语义（06 计划 §3：审查以固定 SHA/manifest，
不审持续变化目录；实现者不能自行 approve）：

- submit_for_review(attempt, artifact_sha256)：实现者把任务最新 attempt 的产出以
  固定 sha256 提交待审——审查从此只针对这个版本；handoff/contract 类 subject 的
  固定版本核对委托 HandoffRegistry（复用登记表哈希逻辑，不重实现）；
- record(review_record)：落一条独立审查结论。硬规则：审查者 attempt 必须不是
  实现 attempt（自审拒绝）；结论绑定 sha256 与请求固定版本不一致（审查期间
  subject 已变化）拒绝；登记表类 subject 落账前再核对登记表当前版本（换版即拒）；
- current_approval(task_id)：任务最新 attempt 的有效批准。实现者重新提交固定
  版本、开新 attempt、或登记表产物换版，原批准立即失效并带 invalid_reason；
  changes_requested 附带逐条 blockers；
- require_approval(task_id)：TaskBoard 的 done 门回调——feature policy 开启时，
  impl/review 任务 done 前必须有有效批准，否则拒绝。policy 缺省关闭（向后兼容，
  旧项目不受影响）。

R03 跨进程持久化：submit/approval 记录本为内存态；现补检查点落盘点
（save_checkpoint/from_checkpoint，atomicio 原子写）。请求与结论都绑定
subject_sha256，落盘重读时逐条形状校验 + 结论经 ReviewRecord.coerce 复验，
损坏明确报错。与 IntegrationBoard 检查点配套，经 ResumeLedger（kind=integration）
登记定位。
"""
from agents_kernel import digest
from agents_kernel.atomicio import read_json, write_json
from agents_kernel.contracts import schemas
from agents_kernel.domain.review_record import ReviewRecord, attempt_id
from agents_kernel.validation import CompanionError, text

# 固定版本由登记表背书的 subject 类型；attempt 类由请求时 pin 的 sha256 背书。
_REGISTRY_SUBJECTS = ("handoff", "contract")

# R03 检查点。
CHECKPOINT_VERSION = 1
CHECKPOINT_KIND = "review_board"


def _check_sha256(value):
    if not isinstance(value, str) or len(value) != 64 \
            or any(ch not in "0123456789abcdef" for ch in value):
        raise CompanionError("subject_sha256 必须是 64 位小写十六进制：%r" % (value,))
    return value


class ReviewBoard:
    def __init__(self, board, registry=None):
        self._board = board
        self._registry = registry
        self._requests = []  # 按提交顺序保留；每任务取最新一条为"当前固定版本"
        self._records = []

    def submit_for_review(self, attempt, artifact_sha256, subject_type="attempt",
                          subject_ref=None):
        """实现者把任务 attempt 的产出以固定版本提交待审，返回请求副本。"""
        if not isinstance(attempt, dict):
            raise CompanionError("待审 attempt 必须是任务台账的 attempt 对象")
        tid = text(attempt.get("task_id"), "任务编号")
        no = attempt.get("attempt_no")
        ref = subject_ref if subject_ref is not None else attempt_id(tid, no)
        if subject_type not in schemas.RECORD_SUBJECT_TYPES:
            raise CompanionError("未知审查 subject 类型：%s" % subject_type)
        requests = self._board.attempts(tid)  # 任务不存在在此报错
        if not requests or requests[-1]["attempt_no"] != no:
            raise CompanionError("只能提交任务 %s 最新 attempt 待审（当前 #%d）"
                                 % (tid, requests[-1]["attempt_no"] if requests else 0))
        if subject_type in _REGISTRY_SUBJECTS:
            if self._registry is None:
                raise CompanionError("handoff/contract 类 subject 需要接入 HandoffRegistry")
            self._registry.require_current(ref, artifact_sha256)
        request = {"task_id": tid, "subject_type": subject_type, "subject_ref": ref,
                   "subject_sha256": artifact_sha256,
                   "implementer_attempt_id": attempt_id(tid, no)}
        self._requests.append(request)
        return dict(request)

    def record(self, review_record):
        """校验独立性、固定版本一致后落一条审查结论，返回记录副本。"""
        rec = ReviewRecord.coerce(review_record).to_dict()
        if any(existing["review_id"] == rec["review_id"] for existing in self._records):
            raise CompanionError("审查记录编号已存在：%s" % rec["review_id"])
        request = self._latest_request(rec["subject_ref"])
        if request is None:
            raise CompanionError("被审对象未提交审查请求：%s" % rec["subject_ref"])
        if rec["reviewer"]["attempt_id"] == request["implementer_attempt_id"]:
            raise CompanionError("实现者不得自审：%s" % rec["reviewer"]["attempt_id"])
        if rec["subject_sha256"] != request["subject_sha256"]:
            raise CompanionError("审查期间 subject 已变化：请求固定 sha256=%s，结论=%s"
                                 % (request["subject_sha256"], rec["subject_sha256"]))
        if request["subject_type"] in _REGISTRY_SUBJECTS:
            self._registry.require_current(rec["subject_ref"], rec["subject_sha256"])
        self._records.append(rec)
        return dict(rec)

    def current_approval(self, task_id):
        """任务最新 attempt 的有效批准。

        返回 dict：status=approved（含 subject/reviewer/reviewed_at）、
        changes_requested（附 blockers）或 none/invalid（附 invalid_reason）。
        审后修改（重提固定版本、登记表换版）使批准失效，返回 invalid_reason。
        """
        tid = text(task_id, "任务编号")
        request = None
        for candidate in reversed(self._requests):
            if candidate["task_id"] == tid:
                request = candidate
                break
        if request is None:
            return {"task_id": tid, "status": "none", "invalid_reason": "尚未提交审查"}
        if request["subject_type"] == "attempt":
            latest_no = self._board.attempts(tid)[-1]["attempt_no"]
            if request["implementer_attempt_id"] != attempt_id(tid, latest_no):
                return {"task_id": tid, "status": "none",
                        "invalid_reason": "最新 attempt #%d 尚未提交审查：既有批准属旧 attempt，不适用"
                                          % latest_no}
        ref = request["subject_ref"]
        verdicts = [rec for rec in self._records if rec["subject_ref"] == ref]
        if not verdicts:
            return {"task_id": tid, "status": "none", "invalid_reason": "已提交审查但尚无结论"}
        latest = verdicts[-1]  # 落账顺序即时间序：同 subject 多条结论取最新
        base = {"task_id": tid, "subject_ref": ref, "subject_sha256": latest["subject_sha256"],
                "reviewer": dict(latest["reviewer"]), "reviewed_at": latest["reviewed_at"]}
        if latest["verdict"] == "changes_requested":
            return dict(base, status="changes_requested", blockers=list(latest["blockers"]))
        if latest["subject_sha256"] != request["subject_sha256"]:
            return dict(base, status="invalid",
                        invalid_reason="审后 subject 已变化：批准只对固定版本 %s 有效，须重新审查"
                                       % request["subject_sha256"])
        if request["subject_type"] in _REGISTRY_SUBJECTS:
            try:
                self._registry.require_current(ref, latest["subject_sha256"])
            except CompanionError as error:
                return dict(base, status="invalid", invalid_reason=str(error))
        return dict(base, status="approved")

    def require_approval(self, task_id):
        """TaskBoard 的 done 门回调：无有效批准即拒绝 done（G-REVIEW）。"""
        verdict = self.current_approval(task_id)
        if verdict["status"] == "approved":
            return verdict
        if verdict["status"] == "changes_requested":
            reason = "changes_requested：%s" % "；".join(verdict["blockers"])
        else:
            reason = verdict["invalid_reason"]
        raise CompanionError("任务 %s 不能 done（缺少有效独立审查批准）：%s" % (task_id, reason))

    def _latest_request(self, subject_ref):
        for request in reversed(self._requests):
            if request["subject_ref"] == subject_ref:
                return request
        return None

    # ---- 检查点（R03：跨进程持久化 submit/approval 记录） ----

    def checkpoint(self):
        """内存记录 → 可序列化检查点（深副本）：审查请求 + 结论（均绑固定 sha256）。"""
        return {"kind": CHECKPOINT_KIND, "schema_version": CHECKPOINT_VERSION,
                "requests": [dict(request) for request in self._requests],
                "records": [dict(record) for record in self._records]}

    def save_checkpoint(self, path):
        """检查点原子落盘，返回内容摘要（与 integration 检查点摘要并列登记）。"""
        state = self.checkpoint()
        write_json(path, state)
        return digest.digest(state)

    @classmethod
    def from_checkpoint(cls, board, registry, path):
        """从检查点还原；结论逐条经 ReviewRecord.coerce 复验，请求形状收紧校验。"""
        state = read_json(path)
        if not isinstance(state, dict) or state.get("kind") != CHECKPOINT_KIND \
                or state.get("schema_version") != CHECKPOINT_VERSION \
                or not isinstance(state.get("requests"), list) \
                or not isinstance(state.get("records"), list):
            raise CompanionError("审查台检查点形状或版本不符：%s" % path)
        restored = cls(board, registry)
        for raw in state["requests"]:
            if not isinstance(raw, dict) or not isinstance(raw.get("task_id"), str) \
                    or raw.get("subject_type") not in schemas.RECORD_SUBJECT_TYPES \
                    or not isinstance(raw.get("subject_ref"), str) \
                    or not isinstance(raw.get("implementer_attempt_id"), str):
                raise CompanionError("审查台检查点请求条目损坏：%r" % (raw,))
            _check_sha256(raw.get("subject_sha256"))
            restored._requests.append(dict(raw))
        for raw in state["records"]:
            restored._records.append(ReviewRecord.coerce(raw).to_dict())
        return restored

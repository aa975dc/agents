"""交接产物登记表：版本 + 内容哈希绑定（stdlib only）。

04 计划 §4「所有关键输入/输出都绑定版本与 hash」的最小落点：设计/接口产物
登记时先过 contracts.schemas 校验（拒绝空泛交接），再算内容 sha256；下游任务
包引用的是 subject_sha256，产物修订（递增版本重登记）后旧引用立即失效——
"审后修改"必须重新登记、重新联审（TK06 前半）。内存实现，与台账解耦；
接入 storage 事件属后续任务。
"""
from agents_kernel import digest
from agents_kernel.contracts import schemas
from agents_kernel.validation import CompanionError, text

SUBJECT_TYPES = ("design_brief", "api_contract")

# 任务类型 → 必需的交接产物类型；实现与核对都按冻结设计和接口契约工作。
_REQUIRED_HANDOFFS = {
    "implement": ("design_brief", "api_contract"),
    "check": ("design_brief", "api_contract"),
}

_VALIDATORS = {
    "design_brief": schemas.validate_design_brief,
    "api_contract": schemas.validate_api_contract,
}


def require_for(task_type):
    """该任务类型必需的交接产物类型集合；未知任务类型直接拒绝派发。"""
    try:
        return _REQUIRED_HANDOFFS[task_type]
    except KeyError:
        raise CompanionError("未知任务类型：%s" % task_type)


def _check_version(version):
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise CompanionError("版本必须是递增正整数")
    return version


class HandoffRegistry:
    def __init__(self):
        self._entries = {}

    def register(self, subject_type, subject_id, content, version, upstream=()):
        """登记交接产物：schema 校验 → 版本递增 → 上游引用核对 → 计算内容 sha256。"""
        subject_type = text(subject_type, "交接产物类型")
        if subject_type not in _VALIDATORS:
            raise CompanionError("未知交接产物类型：%s" % subject_type)
        subject_id = text(subject_id, "交接产物编号")
        errors = _VALIDATORS[subject_type](content)
        if errors:
            raise CompanionError("交接产物校验失败 %s：%s"
                                 % (subject_id, schemas.format_errors(errors)))
        version = _check_version(version)
        previous = self._entries.get(subject_id)
        if previous is not None and version <= previous["version"]:
            raise CompanionError("版本必须递增：%s 当前 v%d，收到 v%d"
                                 % (subject_id, previous["version"], version))
        entry = {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "version": version,
            "sha256": digest.digest(content),
            "upstream": self._check_upstream(upstream),
        }
        self._entries[subject_id] = entry
        return dict(entry)

    def _check_upstream(self, upstream):
        refs = []
        for ref in upstream:
            if not isinstance(ref, dict):
                raise CompanionError("上游引用必须是 {subject_id, subject_sha256} 对象")
            self.require_current(ref.get("subject_id"), ref.get("subject_sha256"))
            refs.append({"subject_id": ref["subject_id"],
                         "subject_sha256": ref["subject_sha256"]})
        return refs

    def get(self, subject_id):
        entry = self._entries.get(subject_id)
        return dict(entry) if entry else None

    def require_current(self, subject_id, subject_sha256, subject_type=None):
        """下游引用校验：未登记、类型不符或哈希已变即拒绝（审后修改失效语义）。"""
        entry = self._entries.get(text(subject_id, "交接产物编号"))
        if entry is None:
            raise CompanionError("交接产物未登记：%s" % subject_id)
        if subject_type is not None and entry["subject_type"] != subject_type:
            raise CompanionError("交接产物 %s 类型不符：登记为 %s，引用为 %s"
                                 % (subject_id, entry["subject_type"], subject_type))
        if not isinstance(subject_sha256, str) or subject_sha256 != entry["sha256"]:
            raise CompanionError(
                "交接产物 %s 已变更（当前 v%d sha256=%s，引用=%s）：引用失效，须重新登记并联审"
                % (subject_id, entry["version"], entry["sha256"], subject_sha256))
        return dict(entry)

    def missing_for(self, task_type):
        present = {entry["subject_type"] for entry in self._entries.values()}
        return tuple(kind for kind in require_for(task_type) if kind not in present)

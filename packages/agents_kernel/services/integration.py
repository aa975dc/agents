"""固定候选集成（IntegrationBoard）：APPROVED 产出 → integration manifest + 两级回归门。

06 计划 §7（C06 后半/TK07/ST07 尾）：I1 只整合 APPROVED_SHA/manifest；集成后产生
新的 integration manifest，重新跑联调和受影响回归——分支各自通过不能直接视为
集成通过。语义边界：

- 候选（IntegrationCandidate）：若干 task 最新成功 attempt 的固定产出快照
  （task/attempt/subject_sha256 + 有效批准引用）。feature 的 review_required
  policy 开启时，入列必须有 ReviewBoard.current_approval 的有效批准（复用审查台
  语义，不重实现）；policy 关闭允许无批准入列（向后兼容）。批准存在时集成对象
  必须与批准固定 sha 一致——集成不准偷换未审内容。
- 版本（build_integration_version）：manifest 内容哈希只对候选清单整体计算
  （候选、任务、产出 sha 全部排序后 canonical JSON 哈希，顺序无关）——同候选集
  恒同版本号同哈希（幂等）；任一内容变更得到新哈希 → 递增新版本号。
- 两级回归：feature_level 逐功能记录 evidence_freshness 的 current/stale/unknown
  输入（只记录，不拦截——06 计划 §6：功能级缓存不能替代上线验证）；version_level
  全局结果由调用方注入（record_version_level）。complete() 要求 version_level 已
  通过：未记录或失败时 integration 保持 candidate，不得 completed（G-INTEGRATION）。
- 失效（对齐 P4-05 失效语义）：任一候选 task 的 subject sha256 变化——check_freshness
  显式核对，或重建时与旧版本内容比对——旧 integration version 标 superseded，
  需重建；已失效版本不再接受回归记录或完成标记。

P2-02 桥：to_event_payload/from_event 纯函数（事件只放类型化小 JSON：版本号、
manifest 哈希与状态，storage/events 约定）。跨进程持久化（R03）：检查点落盘走
JSON 检查点（save_checkpoint/from_checkpoint，atomicio 原子写）+ ResumeLedger 登记
（kind=integration，由调用方组合）——事件库镜像（integration_version 事件入
storage/events）需要 domain.views 注册新事件类型的折叠规则，views 不在本轮
改动范围，取舍为仅 JSON 检查点；事件桥保持纯函数现状。
"""
from agents_kernel import digest
from agents_kernel.atomicio import read_json, write_json
from agents_kernel.domain.review_record import attempt_id
from agents_kernel.validation import CompanionError, strings, text

# P2-02 事件类型（views 折叠与持久化接线归后续 CLI 任务）。
EVENT_INTEGRATION_VERSION = "integration_version"

VERSION_STATUSES = ("candidate", "completed", "superseded")
EVIDENCE_STATUSES = ("current", "stale", "unknown")

# R03 检查点（JSON 原子写；跨进程经 ResumeLedger 登记 kind=integration 定位）。
CHECKPOINT_VERSION = 1
CHECKPOINT_KIND = "integration_board"


def _require_sha256(value):
    if not isinstance(value, str) or len(value) != 64 \
            or any(ch not in "0123456789abcdef" for ch in value):
        raise CompanionError("清单 sha256 必须是 64 位小写十六进制：%r" % (value,))
    return value


def _attempt_no_of(value):
    """attempt 引用（"任务编号#第几次"）→ 序号；畸形引用明确报错，不猜。"""
    if not isinstance(value, str) or "#" not in value:
        raise CompanionError("attempt 引用非法：%r" % (value,))
    tail = value.rsplit("#", 1)[1]
    if not tail.isdigit() or int(tail) < 1:
        raise CompanionError("attempt 引用非法：%r" % (value,))
    return int(tail)


class IntegrationCandidate:
    """集成候选：若干 task 最新成功 attempt 的固定产出（task 按 id 排序的不可变快照）。

    每个 task 条目：{task_id, attempt_id, subject_sha256, approval}；approval 为
    None（policy 关闭且无批准）或 {status, subject_ref, reviewer, reviewed_at}。
    """

    def __init__(self, candidate_id, tasks):
        self.candidate_id = text(candidate_id, "候选编号")
        entries = [dict(task) for task in tasks]
        if not entries:
            raise CompanionError("候选 %s 至少包含一个任务产出" % self.candidate_id)
        self.tasks = tuple(sorted(entries, key=lambda task: task["task_id"]))

    def canonical(self):
        """参与内容哈希的规范形状（候选/任务均已排序：同集合同哈希）。"""
        return {"candidate_id": self.candidate_id,
                "tasks": [dict(task) for task in self.tasks]}

    def to_dict(self):
        return self.canonical()


class IntegrationBoard:
    """候选台账 + integration 版本登记（内存）。读取返回副本，绝不触碰事件库。"""

    def __init__(self, board, review_board=None):
        self._board = board
        self._review_board = review_board
        self._candidates = {}   # candidate_id -> IntegrationCandidate（同 id 再入列=重建替换）
        self._versions = []     # manifest dict，创建序；version_no = 下标 + 1
        self._by_content = {}   # manifest_sha256 -> integration_version（幂等索引）

    # ---- 候选 ----

    def add_candidate(self, candidate_id, artifacts):
        """artifacts: {task_id: subject_sha256}——集成对象按固定 sha 声明。

        每个 task 取最新成功 attempt；policy 开启须有有效批准，声明 sha 必须与
        批准固定 sha 一致。同 candidate_id 再次入列 = 变更重建后的候选快照替换。
        """
        if not isinstance(artifacts, dict) or not artifacts:
            raise CompanionError("artifacts 必须是非空 {task_id: sha256}")
        entries = []
        for tid in sorted(artifacts):
            sha = _require_sha256(artifacts[tid])
            self._board.task(tid)  # 任务不存在在此报错
            attempts = self._board.attempts(tid)
            if not attempts or attempts[-1]["outcome"] != "succeeded":
                raise CompanionError("任务 %s 最新 attempt 尚无成功产出，不能入列集成候选" % tid)
            verdict = None
            if self._review_board is not None:
                verdict = self._review_board.current_approval(tid)
            approved = verdict is not None and verdict["status"] == "approved"
            feature = self._board.feature(self._board.task(tid)["feature_id"])
            if feature["review_required"]:
                if self._review_board is None:
                    raise CompanionError("功能 %s 要求独立审查，但未接入审查台" % feature["id"])
                if not approved:
                    raise CompanionError("任务 %s 不能入列集成候选（缺少有效独立审查批准）：%s"
                                         % (tid, self._verdict_reason(verdict)))
            if approved and verdict["subject_sha256"] != sha:
                raise CompanionError("任务 %s 集成对象与批准固定版本不一致：批准 %s，声明 %s"
                                     % (tid, verdict["subject_sha256"], sha))
            approval = None
            if approved:
                approval = {"status": "approved", "subject_ref": verdict["subject_ref"],
                            "reviewer": dict(verdict["reviewer"]),
                            "reviewed_at": verdict["reviewed_at"]}
            entries.append({"task_id": tid,
                            "attempt_id": attempt_id(tid, attempts[-1]["attempt_no"]),
                            "subject_sha256": sha, "approval": approval})
        candidate = IntegrationCandidate(candidate_id, entries)
        previous = self._candidates.get(candidate.candidate_id)
        if previous is not None:
            self._reject_late_reports(candidate, previous)
        self._candidates[candidate.candidate_id] = candidate
        return candidate.to_dict()

    @staticmethod
    def _reject_late_reports(candidate, previous):
        """迟到回报围栏（R03）：同 candidate_id 再入列时，某任务回报的 attempt 序号
        严格旧于已固定候选 → 拒绝（旧 attempt 产出不能覆盖新候选）。同序号重提新
        固定版本是既有重建语义，放行（由审查门/失效语义把关）。"""
        recorded = {task["task_id"]: task for task in previous.tasks}
        for task in candidate.tasks:
            old = recorded.get(task["task_id"])
            if old is None:
                continue
            old_no, new_no = _attempt_no_of(old["attempt_id"]), _attempt_no_of(task["attempt_id"])
            if new_no < old_no:
                raise CompanionError(
                    "迟到回报被拒：任务 %s 回报 attempt #%d，候选 %s 已固定更新的 attempt #%d；"
                    "旧 attempt 产出不能覆盖新候选"
                    % (task["task_id"], new_no, candidate.candidate_id, old_no))

    @staticmethod
    def _verdict_reason(verdict):
        if verdict is None:
            return "未接入审查台"
        if verdict["status"] == "changes_requested":
            return "changes_requested：%s" % "；".join(verdict["blockers"])
        return verdict["invalid_reason"]

    # ---- 版本 ----

    def build_integration_version(self, candidate_ids):
        """候选清单 → integration manifest。

        幂等：同候选集（内容逐项一致）恒返回既有版本号与哈希；任一内容变更得到
        新哈希，递增新版本号，并使包含变化候选的旧版本 superseded（P4-05 失效）。
        回归结果不参与内容哈希（哈希只锚定候选清单内容）。
        """
        ids = sorted(set(strings(list(candidate_ids), "候选编号")))
        if not ids:
            raise CompanionError("集成版本至少需要一个候选")
        missing = [cid for cid in ids if cid not in self._candidates]
        if missing:
            raise CompanionError("候选不存在：%s" % missing[0])
        content = [self._candidates[cid].canonical() for cid in ids]
        content_sha = digest.digest(content)
        if content_sha in self._by_content:
            return self.version(self._by_content[content_sha])
        self._supersede_changed(content)
        manifest = {"integration_version": len(self._versions) + 1,
                    "manifest_sha256": content_sha, "candidates": content,
                    "status": "candidate",
                    "regression": {"feature_level": [], "version_level": None}}
        self._versions.append(manifest)
        self._by_content[content_sha] = manifest["integration_version"]
        return self.version(manifest["integration_version"])

    def _supersede_changed(self, content):
        """候选 task 的 sha 与旧版本清单不一致 → 旧版本标 superseded（需重建）。"""
        new_shas = {}
        for candidate in content:
            for task in candidate["tasks"]:
                new_shas[(candidate["candidate_id"], task["task_id"])] = task["subject_sha256"]
        for manifest in self._versions:
            changed = []
            for candidate in manifest["candidates"]:
                for task in candidate["tasks"]:
                    key = (candidate["candidate_id"], task["task_id"])
                    if key in new_shas and new_shas[key] != task["subject_sha256"]:
                        changed.append("/".join(key))
            if changed:
                self._mark_superseded(manifest, changed)

    def check_freshness(self, artifacts):
        """{task_id: 当前 sha} → 核对各版本候选；sha 变化的版本标 superseded（幂等）。

        返回本次被失效的版本号列表（升序）。对齐 P4-05：回归后变更使集成证据
        失效，不得沿用旧集成结论，须重建版本重新回归。
        """
        if not isinstance(artifacts, dict) or not artifacts:
            raise CompanionError("artifacts 必须是非空 {task_id: sha256}")
        current = {text(tid, "任务编号"): _require_sha256(sha) for tid, sha in artifacts.items()}
        superseded = []
        for manifest in self._versions:
            if manifest["status"] == "superseded":
                continue
            changed = sorted(task["task_id"]
                             for candidate in manifest["candidates"]
                             for task in candidate["tasks"]
                             if task["task_id"] in current
                             and current[task["task_id"]] != task["subject_sha256"])
            if changed and self._mark_superseded(manifest, changed):
                superseded.append(manifest["integration_version"])
        return superseded

    @staticmethod
    def _mark_superseded(manifest, changed):
        if manifest["status"] == "superseded":
            return False
        manifest["status"] = "superseded"
        manifest["superseded_reason"] = ("候选 subject sha256 已变化（%s），集成版本作废须重建"
                                         % "、".join(changed))
        return True

    # ---- 两级回归 ----

    def record_feature_level(self, integration_version, statuses):
        """逐功能证据新鲜度记录（EvidenceFreshness.assess 输出：EvidenceStatus 或等价 dict）。

        只记录不拦截：功能级 stale/unknown 不阻断集成完成——版本级全局回归才是
        集成门（06 计划 §6：功能级缓存不能替代上线验证）。
        """
        manifest = self._require_open(integration_version, "记录功能级回归")
        normalized = []
        for item in statuses:
            if hasattr(item, "_asdict"):  # EvidenceStatus 等 NamedTuple
                item = item._asdict()
            if not isinstance(item, dict):
                raise CompanionError("功能级回归记录必须是 EvidenceStatus 或等价 dict")
            status = text(item.get("status"), "证据状态")
            if status not in EVIDENCE_STATUSES:
                raise CompanionError("未知证据状态：%s" % status)
            normalized.append({"feature_id": text(item.get("feature_id"), "功能编号"),
                               "status": status,
                               "stale_files": list(item.get("stale_files", ())),
                               "hit_modules": list(item.get("hit_modules", ())),
                               "transitive": bool(item.get("transitive", False)),
                               "conservative": bool(item.get("conservative", False))})
        normalized.sort(key=lambda record: record["feature_id"])
        manifest["regression"]["feature_level"] = normalized
        return [dict(record) for record in normalized]

    def record_version_level(self, integration_version, passed, detail=""):
        """注入全局（版本级）回归结果：passed 布尔 + 说明；再次记录覆盖（重跑回归）。"""
        if not isinstance(passed, bool):
            raise CompanionError("passed 必须是布尔")
        if detail:
            detail = text(detail, "回归说明")
        manifest = self._require_open(integration_version, "记录版本级回归")
        manifest["regression"]["version_level"] = {
            "status": "passed" if passed else "failed", "detail": detail}
        return dict(manifest["regression"]["version_level"])

    def complete(self, integration_version):
        """集成完成门：version_level 已通过才允许 candidate → completed。

        未记录或未通过时拒绝，integration 保持 candidate（G-INTEGRATION：分支
        各自通过不能直接视为集成通过，须版本级全局回归通过）。
        """
        manifest = self._require_open(integration_version, "标记集成完成")
        level = manifest["regression"]["version_level"]
        if level is None:
            raise CompanionError("integration 版本 %d 的 version_level 回归未记录，保持 candidate"
                                 % integration_version)
        if level["status"] != "passed":
            raise CompanionError("integration 版本 %d 的 version_level 回归未通过（%s），保持 candidate"
                                 % (integration_version, level["status"]))
        manifest["status"] = "completed"
        return self.version(integration_version)

    def _require_open(self, integration_version, action):
        manifest = self._require_version(integration_version)
        if manifest["status"] == "superseded":
            raise CompanionError("integration 版本 %d 已失效，不能%s（须重建）" % (integration_version, action))
        if manifest["status"] == "completed":
            raise CompanionError("integration 版本 %d 已完成，不能%s（再变更走失效重建）"
                                 % (integration_version, action))
        return manifest

    # ---- 读取 ----

    def candidate(self, candidate_id):
        candidate = self._candidates.get(text(candidate_id, "候选编号"))
        if candidate is None:
            raise CompanionError("候选不存在：%s" % candidate_id)
        return candidate.to_dict()

    def version(self, integration_version):
        manifest = self._require_version(integration_version)
        copied = dict(manifest)
        copied["candidates"] = [{"candidate_id": candidate["candidate_id"],
                                 "tasks": [dict(task) for task in candidate["tasks"]]}
                                for candidate in manifest["candidates"]]
        regression = manifest["regression"]
        copied["regression"] = {
            "feature_level": [dict(record) for record in regression["feature_level"]],
            "version_level": dict(regression["version_level"]) if regression["version_level"] else None}
        return copied

    def versions(self):
        return [self.version(manifest["integration_version"]) for manifest in self._versions]

    def _require_version(self, integration_version):
        if isinstance(integration_version, bool) or not isinstance(integration_version, int) \
                or not 1 <= integration_version <= len(self._versions):
            raise CompanionError("integration 版本不存在：%r" % (integration_version,))
        return self._versions[integration_version - 1]

    # ---- 检查点（R03：跨进程持久化，JSON 原子写；不另造第二套状态机） ----

    def checkpoint(self):
        """内存台账 → 可序列化检查点（深副本）：候选集 + 版本清单（回归/superseded 随附）。"""
        return {"kind": CHECKPOINT_KIND, "schema_version": CHECKPOINT_VERSION,
                "candidates": [candidate.canonical()
                               for _, candidate in sorted(self._candidates.items())],
                "versions": [_checkpoint_manifest(manifest) for manifest in self._versions]}

    def save_checkpoint(self, path):
        """检查点原子落盘，返回内容摘要（作 ResumeLedger 的 source_anchor）。"""
        state = self.checkpoint()
        write_json(path, state)
        return digest.digest(state)

    @classmethod
    def from_checkpoint(cls, board, review_board, path):
        """从检查点还原（新进程只读持久记录）；形状/内容哈希不符明确报错，不猜。"""
        state = read_json(path)
        if not isinstance(state, dict) or state.get("kind") != CHECKPOINT_KIND \
                or state.get("schema_version") != CHECKPOINT_VERSION \
                or not isinstance(state.get("candidates"), list) \
                or not isinstance(state.get("versions"), list):
            raise CompanionError("integration 检查点形状或版本不符：%s" % path)
        restored = cls(board, review_board)
        for entry in state["candidates"]:
            candidate = _restore_candidate(entry)
            restored._candidates[candidate.candidate_id] = candidate
        for raw in state["versions"]:
            manifest = _restore_manifest(raw, len(restored._versions) + 1, path)
            restored._versions.append(manifest)
            restored._by_content[manifest["manifest_sha256"]] = manifest["integration_version"]
        return restored


def _checkpoint_manifest(manifest):
    copied = dict(manifest)
    copied["candidates"] = [{"candidate_id": candidate["candidate_id"],
                             "tasks": [dict(task) for task in candidate["tasks"]]}
                            for candidate in manifest["candidates"]]
    regression = manifest["regression"]
    copied["regression"] = {
        "feature_level": [dict(record) for record in regression["feature_level"]],
        "version_level": dict(regression["version_level"]) if regression["version_level"] else None}
    return copied


def _restore_candidate(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get("tasks"), list) \
            or not entry["tasks"]:
        raise CompanionError("integration 检查点候选条目损坏：%r" % (entry,))
    tasks = []
    for task in entry["tasks"]:
        if not isinstance(task, dict) or not isinstance(task.get("task_id"), str) \
                or "approval" not in task:
            raise CompanionError("integration 检查点任务条目损坏：%r" % (task,))
        _attempt_no_of(task.get("attempt_id"))
        _require_sha256(task.get("subject_sha256"))
        tasks.append(dict(task))
    return IntegrationCandidate(text(entry.get("candidate_id"), "候选编号"), tasks)


def _restore_manifest(raw, expected_version, path):
    if not isinstance(raw, dict) or raw.get("integration_version") != expected_version \
            or not isinstance(raw.get("candidates"), list) \
            or not isinstance(raw.get("regression"), dict):
        raise CompanionError("integration 检查点版本条目损坏（期望版本号 %d）：%s"
                             % (expected_version, path))
    status = text(raw.get("status"), "integration 状态")
    if status not in VERSION_STATUSES:
        raise CompanionError("integration 检查点状态无法识别：%s" % status)
    content = [{"candidate_id": candidate["candidate_id"],
                "tasks": [dict(task) for task in candidate["tasks"]]}
               for candidate in raw["candidates"]]
    content_sha = digest.digest(content)
    if content_sha != raw.get("manifest_sha256"):
        raise CompanionError(
            "integration 检查点内容哈希不符（manifest_sha256=%s，重算=%s）：%s"
            % (raw.get("manifest_sha256"), content_sha, path))
    regression = raw["regression"]
    feature_level = regression.get("feature_level")
    version_level = regression.get("version_level")
    if not isinstance(feature_level, list) or \
            (version_level is not None and
             (not isinstance(version_level, dict) or
              version_level.get("status") not in ("passed", "failed"))):
        raise CompanionError("integration 检查点回归记录损坏：%s" % path)
    manifest = {"integration_version": expected_version, "manifest_sha256": content_sha,
                "candidates": content, "status": status,
                "regression": {"feature_level": [dict(record) for record in feature_level],
                               "version_level": dict(version_level) if version_level else None}}
    if raw.get("superseded_reason") is not None:
        manifest["superseded_reason"] = text(raw.get("superseded_reason"), "失效原因")
    return manifest


def to_event_payload(manifest):
    """integration manifest → P2-02 事件 payload（纯函数，不写库）。

    事件只放类型化小 JSON（storage/events 约定）：版本号、manifest 哈希与状态；
    候选清单重物经 manifest_sha256 按哈希引用。append_event/视图折叠的持久化
    接线归后续 CLI 任务。
    """
    if not isinstance(manifest, dict):
        raise CompanionError("需要 integration manifest dict")
    version_no = manifest.get("integration_version")
    if isinstance(version_no, bool) or not isinstance(version_no, int) or version_no < 1:
        raise CompanionError("integration_version 必须是正整数")
    status = text(manifest.get("status"), "integration 状态")
    if status not in VERSION_STATUSES:
        raise CompanionError("未知 integration 状态：%s" % status)
    return {"integration_version": version_no,
            "manifest_sha256": _require_sha256(manifest.get("manifest_sha256")),
            "status": status}


def from_event(entity_id, payload):
    """P2-02 事件 → integration 版本状态片段（纯函数），词表白名单收紧校验。"""
    if not isinstance(payload, dict):
        raise CompanionError("事件 payload 必须是对象")
    version_no = payload.get("integration_version")
    if isinstance(version_no, bool) or not isinstance(version_no, int) or version_no < 1:
        raise CompanionError("integration_version 必须是正整数")
    status = text(payload.get("status"), "integration 状态")
    if status not in VERSION_STATUSES:
        raise CompanionError("未知 integration 状态：%s" % status)
    return {"id": text(entity_id, "实体标识"), "integration_version": version_no,
            "manifest_sha256": _require_sha256(payload.get("manifest_sha256")),
            "status": status}

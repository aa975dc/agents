"""交接产物最小 schema 与手写校验器（stdlib only，不引 jsonschema）。

守护 references/design-handoff.md 与 references/api-contract.md 约定的交接产物
与联审记录：校验失败返回结构化错误列表（{"path": 字段路径, "reason": 原因}），
不抛裸异常，调用方（登记表/联审门）据此拒绝空泛交接。契约文档改结构必须
先改这里——机器校验为准；tests/agents/test_handoff_contracts.py 用真实 fixture
与文档样例双重跑本文件，防三方漂移。
"""
import json
import re

FIVE_STATES = ("loading", "empty", "error", "partial", "success")
KINDS = ("http", "local")
ERROR_EXIT_CODES = (2, 3)
# 错误码闭合枚举的机器形态（api-contract.md：新码先改本表再用于契约）。
CLOSED_ERROR_CODES = frozenset(("E_PARAM", "E_PROJECT", "E_STATE", "E_AUTH", "E_CHECK"))
IDEMPOTENCY_MODES = ("read_only", "revision_cas", "idempotency_key")
VERDICTS = ("approved", "changes_requested")

_ERROR_CODE_RE = re.compile(r"^E_[A-Z_]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# 可断言验收措辞：写明可核验的观察，或用含 expected 的结构化期望；"界面友好"之类拒绝。
ASSERTABLE_RE = re.compile(r"出现|不出现|等于|不等于|包含|不包含|可见|隐藏|不低于|不超过|至少")


def format_errors(errors):
    """把校验错误列表渲染成一行摘要，供 CompanionError 携带。"""
    return "；".join("%s: %s" % (e["path"] or "(根)", e["reason"]) for e in errors)


def _add(errors, path, reason):
    errors.append({"path": path, "reason": reason})


def _check_text(value, path, errors, label):
    if not isinstance(value, str) or not value.strip():
        _add(errors, path, label + "必须是非空字符串")


def _check_version(value, path, errors):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _add(errors, path, "版本必须是递增正整数")


def _check_error_codes(codes, path, errors):
    if not isinstance(codes, list) or not codes:
        _add(errors, path, "错误码表必须是非空列表")
        return
    seen = set()
    for index, entry in enumerate(codes):
        where = "%s[%d]" % (path, index)
        if not isinstance(entry, dict):
            _add(errors, where, "错误码条目必须是对象")
            continue
        code = entry.get("code")
        if not isinstance(code, str) or not _ERROR_CODE_RE.match(code):
            _add(errors, where + ".code", "错误码必须是 E_ 前缀枚举")
        elif code not in CLOSED_ERROR_CODES:
            _add(errors, where + ".code",
                 "错误码不在闭合枚举内（先改 schemas.CLOSED_ERROR_CODES 再用）：" + code)
        elif code in seen:
            _add(errors, where + ".code", "错误码重复：" + code)
        seen.add(code)
        if entry.get("exit_code") not in ERROR_EXIT_CODES:
            _add(errors, where + ".exit_code", "退出码必须是 2（参数/状态/权限）或 3（检查失败）")
        _check_text(entry.get("when"), where + ".when", errors, "触发条件")


def _check_idempotency(idem, path, errors):
    if not isinstance(idem, dict):
        _add(errors, path, "幂等声明必须是对象")
        return
    mode = idem.get("mode")
    if mode not in IDEMPOTENCY_MODES:
        _add(errors, path + ".mode",
             "幂等模式必须是三模式之一：" + "/".join(IDEMPOTENCY_MODES))
    if "key" not in idem:
        _add(errors, path + ".key", "幂等键声明缺失（只读允许显式 null）")
    elif mode == "idempotency_key":
        _check_text(idem.get("key"), path + ".key", errors, "幂等键构造规则")
    elif idem["key"] is not None:
        _add(errors, path + ".key", "非幂等键模式 key 应为 null 并在 note 说明")


def validate_design_brief(brief):
    """设计 brief（references/design-handoff.md）：五态矩阵 + 可断言验收，缺一即错。"""
    errors = []
    if not isinstance(brief, dict):
        _add(errors, "", "设计 brief 必须是 JSON 对象")
        return errors
    _check_text(brief.get("brief_id"), "brief_id", errors, "设计版本标识")
    _check_text(brief.get("feature_id"), "feature_id", errors, "功能编号")
    _check_version(brief.get("brief_version"), "brief_version", errors)
    _check_text(brief.get("based_on"), "based_on", errors, "复用声明")
    screens = brief.get("screens")
    if not isinstance(screens, list) or not screens:
        _add(errors, "screens", "screens 必须是非空列表")
    else:
        for index, screen in enumerate(screens):
            _check_screen(screen, "screens[%d]" % index, errors)
    goals = brief.get("non_goals")
    if not isinstance(goals, list) or not goals or not all(
            isinstance(goal, str) and goal.strip() for goal in goals):
        _add(errors, "non_goals", "non_goals 必须是非空字符串列表")
    return errors


def _check_screen(screen, path, errors):
    if not isinstance(screen, dict):
        _add(errors, path, "screen 必须是对象")
        return
    _check_text(screen.get("screen_id"), path + ".screen_id", errors, "页面编号")
    states = screen.get("states")
    if not isinstance(states, dict):
        _add(errors, path + ".states", "状态矩阵必须是对象")
    else:
        for state in FIVE_STATES:
            entry = states.get(state)
            where = "%s.states.%s" % (path, state)
            if not isinstance(entry, dict):
                _add(errors, where, "五态缺一不可，缺状态：" + state)
                continue
            _check_text(entry.get("data"), where + ".data", errors, state + " 的数据形态")
            _check_text(entry.get("copy"), where + ".copy", errors, state + " 的文案")
            shown = entry.get("shown")
            if not isinstance(shown, list) or not shown:
                _add(errors, where + ".shown", "未写该状态下出现的组件")
    criteria = screen.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        _add(errors, path + ".acceptance_criteria", "设计验收标准不能为空")
        return
    for index, criterion in enumerate(criteria):
        where = "%s.acceptance_criteria[%d]" % (path, index)
        if isinstance(criterion, dict):
            _check_text(criterion.get("expected"), where + ".expected", errors, "结构化期望值")
        elif isinstance(criterion, str) and criterion.strip():
            if not ASSERTABLE_RE.search(criterion):
                _add(errors, where, "验收条不可断言（缺出现/等于/不低于等可核验措辞）：" + criterion)
        else:
            _add(errors, where, "验收条必须是非空字符串或含 expected 的结构化期望")


def validate_api_contract(doc):
    """接口契约两种形态：references/api-contract.md 的单契约文档（fixture 即此形态），
    或带 contract_version/endpoints 的交接包形态；版本号在交接包形态必填。"""
    errors = []
    if not isinstance(doc, dict):
        _add(errors, "", "接口契约必须是 JSON 对象")
        return errors
    _check_text(doc.get("contract_id"), "contract_id", errors, "契约版本标识")
    if "contract_version" in doc or "endpoints" in doc:
        return _validate_handoff_form(doc, errors)
    return _validate_doc_form(doc, errors)


def _validate_handoff_form(doc, errors):
    _check_version(doc.get("contract_version"), "contract_version", errors)
    endpoints = doc.get("endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        _add(errors, "endpoints", "endpoints 必须是非空列表")
        return errors
    for index, endpoint in enumerate(endpoints):
        where = "endpoints[%d]" % index
        if not isinstance(endpoint, dict):
            _add(errors, where, "端点必须是对象")
            continue
        _check_text(endpoint.get("method"), where + ".method", errors, "调用方法")
        _check_text(endpoint.get("path"), where + ".path", errors, "路径或本地入口")
        example = endpoint.get("request_example")
        if isinstance(example, str):
            try:
                example = json.loads(example)
            except ValueError:
                _add(errors, where + ".request_example", "request_example 字符串必须可解析为 JSON")
                example = None
        if not isinstance(example, dict):
            _add(errors, where + ".request_example", "请求例子必须是可解析的 JSON 对象")
        _check_error_codes(endpoint.get("error_codes"), where + ".error_codes", errors)
        _check_idempotency(endpoint.get("idempotency"), where + ".idempotency", errors)
    notes = doc.get("migration_notes")
    if isinstance(notes, str):
        _check_text(notes, "migration_notes", errors, "迁移注记")
    elif not (isinstance(notes, list) and notes
              and all(isinstance(note, str) and note.strip() for note in notes)):
        _add(errors, "migration_notes", "迁移注记必须是非空字符串或非空字符串列表")
    return errors


def _validate_doc_form(doc, errors):
    _check_text(doc.get("feature_id"), "feature_id", errors, "功能编号")
    if doc.get("kind") not in KINDS:
        _add(errors, "kind", "调用形态必须是 http 或 local")
    _check_text(doc.get("entry"), "entry", errors, "方法路径或本地入口")
    _check_text(doc.get("auth"), "auth", errors, "权限声明")
    if not isinstance(doc.get("request"), dict):
        _add(errors, "request", "请求例子必须是可解析的 JSON 对象")
    _check_error_codes(doc.get("error_codes"), "error_codes", errors)
    _check_idempotency(doc.get("idempotency"), "idempotency", errors)
    migration = doc.get("migration")
    if not isinstance(migration, dict):
        _add(errors, "migration", "迁移注记必须是对象")
        return errors
    if not isinstance(migration.get("breaking"), bool):
        _add(errors, "migration.breaking", "breaking 必须是布尔")
    _check_text(migration.get("coexistence"), "migration.coexistence", errors, "旧数据并存期")
    _check_text(migration.get("rollback"), "migration.rollback", errors, "回退方式")
    return errors


def validate_review_gate(record):
    """联审门记录：绑定被审产物的内容 sha256；approved 不带阻塞项，
    changes_requested 必须逐条写阻塞项（拒绝空泛否决）。"""
    errors = []
    if not isinstance(record, dict):
        _add(errors, "", "联审记录必须是 JSON 对象")
        return errors
    for field, label in (("gate_id", "门记录编号"), ("subject_type", "被审产物类型"),
                         ("subject_id", "被审产物编号"), ("reviewer_role", "审查者角色")):
        _check_text(record.get(field), field, errors, label)
    sha = record.get("subject_sha256")
    if not isinstance(sha, str) or not _SHA256_RE.match(sha):
        _add(errors, "subject_sha256", "必须是被审产物内容的 64 位小写 sha256")
    if record.get("verdict") not in VERDICTS:
        _add(errors, "verdict", "结论必须是 approved 或 changes_requested")
    blockers = record.get("blockers")
    if not isinstance(blockers, list) or not all(
            isinstance(blocker, str) and blocker.strip() for blocker in blockers):
        _add(errors, "blockers", "blockers 必须是非空字符串列表（可为空列表）")
    elif record.get("verdict") == "approved" and blockers:
        _add(errors, "blockers", "approved 不得携带阻塞项")
    elif record.get("verdict") == "changes_requested" and not blockers:
        _add(errors, "blockers", "changes_requested 必须逐条写明阻塞项")
    return errors

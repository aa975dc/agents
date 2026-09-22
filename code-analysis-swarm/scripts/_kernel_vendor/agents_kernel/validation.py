"""Pure validation rules shared by core and journey (stdlib only)."""
from pathlib import PurePosixPath

from agents_kernel.paths import EXCLUDED_DIRS, sensitive


class CompanionError(ValueError):
    pass


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise CompanionError(label + "不能为空")
    return value.strip()


def strings(value, label, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise CompanionError(label + "必须是%s列表" % ("非空" if nonempty else ""))
    return [text(item, label) for item in value]


def feature_id(identifier, seen):
    """功能编号规则——原 core.validate_scope 与 journey.product_scope 各写一份，现合一。

    严格度无差异（两边规则相同）；可选项的宽严差仍在各自调用点用 complete/键存在性表达。
    """
    if not all(c.isascii() and (c.isalnum() or c in "-_") for c in identifier) or identifier in seen:
        raise CompanionError("功能编号须唯一且仅含英文字母、数字、下划线或短横线")
    return identifier


def relative_path(value):
    value = text(value, "文件路径")
    path = PurePosixPath(value)
    if (path.is_absolute() or "\\" in value or ":" in value or
            any(p in {"", ".", ".."} for p in value.split("/")) or
            any(p in EXCLUDED_DIRS for p in path.parts) or any(sensitive(p) for p in path.parts)):
        raise CompanionError("文件路径不在支持的普通项目文件范围：" + value)
    return value


def safe_file(project, name, exists=False):
    name = relative_path(name)
    path = project
    for part in PurePosixPath(name).parts:
        path = path / part
        if path.is_symlink():
            raise CompanionError("文件链接不在支持范围：" + name)
    if path.exists() and not path.is_file():
        raise CompanionError("需要普通文件：" + name)
    if exists and not path.is_file():
        raise CompanionError("找不到产物文件：" + name)
    return path


def validate_scope(raw):
    if not isinstance(raw, dict):
        raise CompanionError("需求必须是 JSON 对象")
    scope = {key: text(raw.get(key), key) for key in ("title", "goal", "audience", "scenario")}
    for key in ("out_of_scope", "assumptions"):
        scope[key] = strings(raw.get(key, []), key)
    features = raw.get("features")
    if not isinstance(features, list) or not features:
        raise CompanionError("首版至少需要一项功能")
    scope["features"] = []
    seen = set()
    for item in features:
        if not isinstance(item, dict):
            raise CompanionError("功能必须是对象")
        identifier = text(item.get("id"), "功能编号")
        feature_id(identifier, seen)
        seen.add(identifier)
        paths = strings(item.get("allowed_paths"), "可修改文件", True)
        paths = list(dict.fromkeys(relative_path(p) for p in paths))
        commands = item.get("check_commands", [])
        if not isinstance(commands, list):
            raise CompanionError("检查命令必须为参数数组的列表")
        commands = [strings(c, "检查命令参数", True) for c in commands]
        needs_user = item.get("requires_user_acceptance", True)
        if type(needs_user) is not bool:
            raise CompanionError("requires_user_acceptance 必须为布尔值")
        scope["features"].append({"id": identifier, "title": text(item.get("title"), "功能名称"),
                                  "acceptance_criteria": strings(item.get("acceptance_criteria"), "验收条件", True),
                                  "allowed_paths": paths, "check_commands": commands,
                                  "requires_user_acceptance": needs_user})
    return scope

"""功能级证据新鲜度：变更闭包 → 每功能验收证据 current/stale/unknown 标注。

06_TASKS_ISOLATION_AND_GATES.md §6（C08 后半）：allowed_paths 不是完整依赖集；
功能验收证据在其模块依赖闭包内的文件变更后应标 stale——不能再以 accepted 面孔
示人。语义边界（ST07，硬规则）：
- 本服务只计算与标注，绝不写库、绝不改变任何已存储的验收状态；失效只降级展示
  与阻断复用，恢复 accepted 必须走真实重新验收（unknown/stale 不能变 accepted）。
- 集成/版本级证据（release 阶段记录）不参与功能级失效：它恒由全局版本回归重新
  核验（功能级缓存不能替代上线验证）。本服务只产出功能级标注，不读取、不产出、
  不改写任何 release/集成记录；功能级 stale/unknown 也不代表集成证据失效。
- 映射缺失（功能没有 allowed_paths 且无显式映射，或映射为空）时按 unknown 保守
  处理：无法证明无关时宁可标未知，不瞎缩小失效范围。

功能→模块映射规则：缺省用 ModuleMapper.module_of 对 allowed_paths 逐个推导
（规则前缀最长优先，否则首段目录，顶层散文件归 _root），与索引建模块表同口径；
调用方可用 feature_modules 显式覆盖（{feature_id: module_id 集合}，可只覆盖部分
功能）。mapper 必须与计算闭包所用规则一致，否则直接命中与传导命中的口径对不上。

输入输出均为纯数据：assess() 不触碰任何存储。
"""
from typing import NamedTuple

from agents_kernel.services.closure import ClosureResult
from agents_kernel.validation import CompanionError


class EvidenceStatus(NamedTuple):
    feature_id: str
    status: str          # current | stale | unknown
    stale_files: tuple   # 相关变更文件（排序）：直接命中=落入功能模块的文件；
                         # 传导命中=驱动闭包的全部变更文件；current/unknown 为空
    hit_modules: tuple   # 被波及的功能模块（排序；恒为功能模块与闭包的交集）
    transitive: bool     # True=无变更文件直接落入功能模块，经依赖闭包传导命中
    conservative: bool   # 所用闭包是否为 unknown 保守扩展（闭包不完整可信）

    def reason(self):
        """stale/unknown 的一句人读摘要；current 返回空串。"""
        if self.status == "current":
            return ""
        if self.status == "unknown":
            return "证据→模块映射缺失，保守按未知处理，需重新确认验收依据"
        files = "、".join(self.stale_files[:3]) + ("…" if len(self.stale_files) > 3 else "")
        via = "（经依赖闭包传导）" if self.transitive else ""
        cons = "；闭包含未知依赖，已按保守扩展处理" if self.conservative else ""
        return "依赖闭包内变更命中：%s%s%s" % (files, via, cons)


class EvidenceFreshness:
    """评估器：持有 mapper，assess() 产出 {feature_id: EvidenceStatus}。"""

    CURRENT = "current"
    STALE = "stale"
    UNKNOWN = "unknown"

    def __init__(self, mapper=None):
        if mapper is None:
            from agents_kernel.indexing.modules import ModuleMapper
            mapper = ModuleMapper()
        self._mapper = mapper

    def assess(self, changed_files, closure_result, features, feature_modules=None):
        """对一组功能逐一判定证据新鲜度；features 只读，返回全新数据。"""
        if isinstance(changed_files, str) or not changed_files:
            raise CompanionError("changed_files 必须是非空文件路径集合")
        if not isinstance(closure_result, ClosureResult):
            raise CompanionError("closure_result 必须是 closure.affected_closure 的结果")
        changed = sorted(set(changed_files))
        file_modules = {path: self._mapper.module_of(path) for path in changed}
        closure_set = set(closure_result.closure)
        outside = [path for path in changed if file_modules[path] not in closure_set]
        if outside:
            raise CompanionError("变更文件 %s 不在给定闭包内：闭包与变更集不匹配" % (outside[0],))
        given = feature_modules or {}
        result = {}
        for feature in features:
            feature_id = feature.get("id") if isinstance(feature, dict) else None
            if not isinstance(feature_id, str) or not feature_id:
                raise CompanionError("每个功能必须有非空 id")
            modules = self._modules_of(feature, given.get(feature_id))
            result[feature_id] = self._judge(feature_id, modules, changed,
                                             file_modules, closure_set, closure_result)
        return result

    def _modules_of(self, feature, explicit):
        """功能 → 模块集合；无法归属返回 None（映射缺失，保守未知）。"""
        if explicit is not None:
            if isinstance(explicit, str):
                raise CompanionError("显式映射必须是 module_id 非空字符串集合")
            explicit = tuple(explicit)
            if not all(isinstance(m, str) and m for m in explicit):
                raise CompanionError("显式映射必须是 module_id 非空字符串集合")
            return frozenset(explicit) or None
        allowed = feature.get("allowed_paths")
        if not allowed:
            return None
        return frozenset(self._mapper.module_of(path) for path in allowed)

    def _judge(self, feature_id, modules, changed, file_modules, closure_set, closure_result):
        if modules is None:
            return EvidenceStatus(feature_id, self.UNKNOWN, (), (), False,
                                  closure_result.conservative)
        hits = tuple(path for path in changed if file_modules[path] in modules)
        hit_modules = tuple(sorted(modules & closure_set))
        if hits or hit_modules:
            # 直接命中列直接落入功能模块的文件；传导命中时列驱动闭包的全部变更文件
            stale_files = hits or tuple(changed)
            return EvidenceStatus(feature_id, self.STALE, stale_files, hit_modules,
                                  not hits, closure_result.conservative)
        return EvidenceStatus(feature_id, self.CURRENT, (), (), False,
                              closure_result.conservative)

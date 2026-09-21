"""分维度覆盖账（C11）：索引/深读/独立复核各自的分母、已覆盖、generation 与缺口。

03_CAPACITY §2/C11：每个维度必须有分母与缺口清单，禁止"只取前 N 项"冒充完成；
unknown 不等于通过。口径：
- 三个维度固定：index_files（L0/L1 普查与结构索引）、semantics_deep（L2 语义深读）、
  independent_review（L3 独立复核/送验结论）。
- 分母未知（denominator=None）时该维度不完整，不得声称完成。
- covered 超过分母视为记账错误，直接拒绝，不静默钳制。
- 维度 complete = 分母已知 且 covered == 分母 且无缺口；总账 complete 要求全部维度完整。

当前为最小实现：纯内存账本，to_account() 产出 coverage_account.json 的 schema 字典，
落盘由调用方负责。DWF 接入方式见 code-analysis-swarm/workflow/code-analysis.dwf.ts
的"C11 覆盖账接入点"注释（经 world.run 调 python3 helper 或直接构造 JSON）。
"""

DIMENSIONS = ("index_files", "semantics_deep", "independent_review")


class CoverageLedger:
    """按维度记录分母/已覆盖/缺口的覆盖账本；只记账，不读盘不写盘。"""

    def __init__(self, run_id, generation=None):
        self.run_id = run_id
        self.generation = generation
        self._dimensions = {
            name: {"denominator": None, "covered": 0, "gaps": []}
            for name in DIMENSIONS
        }

    def set_denominator(self, dimension, denominator):
        """登记某维度的分母（如 source_files 总数、送验结论总数）；分母可更新但不得低于已覆盖数。"""
        dim = self._require(dimension)
        if not self._is_count(denominator):
            raise ValueError("%s 分母必须是非负整数" % dimension)
        if dim["covered"] > denominator:
            raise ValueError("%s 已覆盖 %d 超过新分母 %d" % (dimension, dim["covered"], denominator))
        dim["denominator"] = denominator

    def record_covered(self, dimension, covered, gaps=()):
        """登记某维度的已覆盖数与当前缺口清单；重复调用以最后一次为准。"""
        dim = self._require(dimension)
        if not self._is_count(covered):
            raise ValueError("%s 已覆盖数必须是非负整数" % dimension)
        denominator = dim["denominator"]
        if denominator is not None and covered > denominator:
            raise ValueError("%s 已覆盖 %d 超过分母 %d" % (dimension, covered, denominator))
        dim["covered"] = covered
        dim["gaps"] = list(gaps)

    def complete(self, dimension):
        """该维度是否闭合：分母已知、covered==分母、无缺口；分母未知一律 False。"""
        dim = self._require(dimension)
        return dim["denominator"] is not None and dim["covered"] == dim["denominator"] and not dim["gaps"]

    def to_account(self):
        """产出 coverage_account.json schema 字典（JSON 可序列化）；complete 为总账结论。"""
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "generation": self.generation,
            "complete": all(self.complete(name) for name in DIMENSIONS),
            "dimensions": {
                name: {
                    "denominator": dim["denominator"],
                    "covered": dim["covered"],
                    "complete": self.complete(name),
                    "gaps": list(dim["gaps"]),
                }
                for name, dim in self._dimensions.items()
            },
        }

    @staticmethod
    def _is_count(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def _require(self, dimension):
        """校验维度名并返回该维度的记账条目；未知维度直接拒绝。"""
        if dimension not in self._dimensions:
            raise ValueError("未知覆盖维度：%s（必须是 %s）" % (dimension, "/".join(DIMENSIONS)))
        return self._dimensions[dimension]

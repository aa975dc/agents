"""深读活动账本（full_deep campaign）：固定清单逐段深读、预算暂停、严格前缀续跑。

03_CAPACITY_AND_INDEXING.md §3 两种深读范围/CV02 分母完整性（C10 后半/C13 尾/CV05）：
- full_deep：对 manifest 圈定的固定清单（每项含 sha256 与 generation 锚定）逐段
  深读并独立复核。清单没读完就没有 complete 的 coverage account——暂停不是降级，
  绝不隐式改成抽样/targeted 后谎报完成。
- 预算预扣：next_batch 按游标取一批，复用 indexing.slicing 估算 token 并整批
  try_consume；不足→paused，游标不动、分文不扣（批内不拆分；调用方可显式调小
  batch_size 或追加预算重试，绝不静默缩小清单）。
- read_partial 是明确记录的事件：部分读必须记录原因，计入 coverage 缺口——
  部分读≠抽样，缺口如实入账。
- 锚定失效：领取时核对文件 sha256，与清单不符即旧锚作废、按新内容重锚并
  记录 generation 变化，重读在新锚下进行（源版本变化需新 generation，§3）。
- 严格前缀：results 恒为清单 [0, cursor) 的连续前缀，逐项与 manifest 锚一致；
  resume 时断言该性质（防跳项/重项）。领取不落盘——崩溃后批次在内存中作废重领
  （预算按估算重扣，at-least-once）；落盘点为 open/record_result/top_up/paused，
  每个落盘点都满足 cursor==len(results)，不存在跨落盘的半批状态。

边界：账本只管清单/游标/预算/结果记账；真实模型深读的 token 消耗实测归
P6-05。est_tokens 是 slicing 的启发式估算（estimation=true），不是精确 token。
coverage_account 只填 semantics_deep 维度，另两维度留给调用方合并。
"""
from pathlib import Path
from typing import NamedTuple

from agents_kernel.atomicio import read_json, write_json
from agents_kernel.execution.budget import Budget
from agents_kernel.indexing import slicing
from agents_kernel.services.coverage import CoverageLedger
from agents_kernel.validation import CompanionError, relative_path, text

CHECKPOINT_VERSION = 1
CAMPAIGN_KIND = "deep_read_campaign"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
OUTCOME_FULL = "read_fully"
OUTCOME_PARTIAL = "read_partial"
OUTCOME_FAILED = "failed"
OUTCOMES = (OUTCOME_FULL, OUTCOME_PARTIAL, OUTCOME_FAILED)
BATCH_ACQUIRED = "acquired"  # BatchResult 状态：整批预扣成功；区别于活动状态 running
DEFAULT_SLICE_TOKENS = 16000  # §7：首批正文按 16k tokens 目标实验，超出由 slicing 拆片


class CampaignItem(NamedTuple):
    """next_batch 领取到的单个深读项：清单锚 + 该文件的全部切片。"""
    index: int
    path: str
    sha256: str
    generation: int
    slices: list      # slicing.slice_file 的切片（含锚与正文），深读材料
    est_tokens: int


class BatchResult(NamedTuple):
    status: str       # "acquired" | "paused"
    items: list
    est_tokens: int   # 整批预扣的估算 token；paused 时为 0



def _require_sha256(value):
    if not isinstance(value, str) or len(value) != 64 \
            or any(ch not in "0123456789abcdef" for ch in value):
        raise CompanionError("清单 sha256 必须是 64 位小写十六进制：%r" % (value,))
    return value


def _require_generation(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CompanionError("generation 必须是正整数")
    return value


class DeepReadCampaign:
    """一次 full_deep 深读活动的状态机：running/paused/completed + 严格前缀账本。"""

    def __init__(self, campaign_id, generation, manifest, cursor, status,
                 budget, results, invalidations, root, slice_tokens, path):
        self.campaign_id = campaign_id
        self.generation = generation
        self.manifest = manifest          # [{"path","sha256","generation"}] 固定顺序
        self.cursor = cursor              # 下一未记账项下标；恒 == len(results)
        self.status = status
        self.budget = budget              # execution.budget.Budget
        self.results = results            # 严格前缀 [0, cursor) 的记账
        self.invalidations = invalidations  # 锚定失效（generation 变化）事件
        self.root = Path(root)
        self.slice_tokens = slice_tokens
        self.path = Path(path)            # 活动账本 JSON 落盘位置
        self._acquired = {}               # index -> est_tokens（已领取未记账）
        self._by_path = {entry["path"]: i for i, entry in enumerate(manifest)}

    # ---- 构造与恢复 -----------------------------------------------------

    @classmethod
    def open(cls, manifest, root, total_tokens, run_dir, campaign_id, generation,
             slice_tokens=DEFAULT_SLICE_TOKENS):
        """按固定清单开活动：manifest=[{"path","sha256"}]，锚定 generation 起点 1。"""
        campaign_id = text(campaign_id, "campaign_id")
        if "/" in campaign_id or "\\" in campaign_id or campaign_id in (".", ".."):
            raise CompanionError("campaign_id 不能作文件名：%s" % campaign_id)
        generation = text(generation, "generation")
        if not manifest:
            raise CompanionError("full_deep 清单为空：没有分母就不存在全量深读")
        entries = []
        seen = set()
        for item in manifest:
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                raise CompanionError("清单项必须是 {path, sha256}：%r" % (item,))
            path = relative_path(item["path"])
            if path in seen:
                raise CompanionError("清单路径重复：%s" % path)
            seen.add(path)
            entries.append({"path": path, "sha256": _require_sha256(item["sha256"]),
                            "generation": 1})
        entries.sort(key=lambda e: e["path"])  # 固定顺序与物理枚举解耦，锚定唯一游标序
        if not isinstance(slice_tokens, int) or isinstance(slice_tokens, bool) or slice_tokens < 1:
            raise CompanionError("slice_tokens 必须是正整数")
        path = Path(run_dir) / ("%s.campaign.json" % campaign_id)
        if path.exists():
            raise CompanionError("活动已存在，续跑请用 resume：%s" % path)
        self = cls(campaign_id, generation, entries, 0, STATUS_RUNNING,
                   Budget(total_tokens, 0), [], [], root, slice_tokens, path)
        self._save()
        return self

    @classmethod
    def resume(cls, run_dir, campaign_id):
        """从检查点恢复，并断言严格前缀性质：results 恒为 [0, cursor) 的连续前缀，
        逐项 path/sha256/generation 与清单锚一致；任何断裂（跳项/重项/篡改）拒绝续跑。"""
        path = Path(run_dir) / ("%s.campaign.json" % text(campaign_id, "campaign_id"))
        data = read_json(path)
        if not isinstance(data, dict) or data.get("version") != CHECKPOINT_VERSION \
                or data.get("kind") != CAMPAIGN_KIND \
                or not isinstance(data.get("manifest"), list) \
                or not isinstance(data.get("results"), list) \
                or not isinstance(data.get("invalidations"), list) \
                or not isinstance(data.get("budget"), dict) \
                or not isinstance(data.get("root"), str):
            raise CompanionError("活动检查点字段不完整或版本不符：%s" % path)
        for entry in data["manifest"]:
            if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "generation"}:
                raise CompanionError("检查点清单项形状非法：%r" % (entry,))
            _require_sha256(entry["sha256"])
            _require_generation(entry["generation"])
        self = cls(
            data["campaign_id"], data["generation"], data["manifest"], data["cursor"],
            data["status"], Budget(data["budget"]["total_tokens"], data["budget"]["spent"]),
            data["results"], data["invalidations"], data["root"], data["slice_tokens"], path)
        count = len(self.manifest)
        if not 0 <= self.cursor <= count or len(self.results) != self.cursor:
            raise CompanionError("检查点前缀断裂：cursor=%d results=%d 清单=%d"
                                 % (self.cursor, len(self.results), count))
        for i, record in enumerate(self.results):
            entry = self.manifest[i]
            if record["index"] != i or record["path"] != entry["path"] \
                    or record["sha256"] != entry["sha256"] \
                    or record["generation"] != entry["generation"]:
                raise CompanionError("检查点前缀断裂于第 %d 项：记账锚与清单锚不一致" % i)
        if (self.status == STATUS_COMPLETED) != (self.cursor == count):
            raise CompanionError("检查点状态与游标不一致：%s cursor=%d" % (self.status, self.cursor))
        if self.status not in (STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED):
            raise CompanionError("未知活动状态：%s" % self.status)
        self._by_path = {entry["path"]: i for i, entry in enumerate(self.manifest)}
        return self

    # ---- 领取与记账 -----------------------------------------------------

    def next_batch(self, batch_size):
        """按游标领取下一批：核锚→slicing 估算→整批预扣；不足→paused（游标不动分文不扣）。

        OSError（文件缺失/不可读）直接抛出，活动状态不变（不领取、不扣减）。
        """
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise CompanionError("batch_size 必须是正整数")
        if self.status == STATUS_COMPLETED:
            raise CompanionError("活动已完成，无批可领")
        if self._acquired:
            raise CompanionError("上一批尚有 %d 项未记账，不得领取下一批" % len(self._acquired))
        take = min(batch_size, len(self.manifest) - self.cursor)
        items = []
        total_est = 0
        for offset in range(take):
            index = self.cursor + offset
            entry = self.manifest[index]
            full = self.root / entry["path"]
            sha, _ = self._hash_file(full)
            if sha != entry["sha256"]:
                # 锚定失效：旧锚作废、按新内容重锚并记录 generation 变化，重读在新锚下进行。
                self.invalidations.append({"index": index, "path": entry["path"],
                                           "old_sha256": entry["sha256"], "new_sha256": sha,
                                           "generation": entry["generation"] + 1})
                entry["sha256"] = sha
                entry["generation"] += 1
            slices = [{"anchor": s.anchor, "start_line": s.start_line, "end_line": s.end_line,
                       "est_tokens": s.est_tokens, "truncated_line": s.truncated_line,
                       "text": s.text}
                      for s in slicing.slice_file(str(full), self.slice_tokens)]
            est = sum(s["est_tokens"] for s in slices)
            items.append(CampaignItem(index, entry["path"], entry["sha256"],
                                      entry["generation"], slices, est))
            total_est += est
        if not self.budget.try_consume(total_est):
            self.status = STATUS_PAUSED
            self._save()
            return BatchResult(STATUS_PAUSED, [], 0)
        for item in items:
            self._acquired[item.index] = item.est_tokens
        self.status = STATUS_RUNNING  # 内存态；落盘等首个 record_result（避免半批检查点）
        return BatchResult(BATCH_ACQUIRED, items, total_est)

    def record_result(self, item, outcome, reason=None, notes=None):
        """记账下一项的显式结论。read_partial/failed 必须带 reason；read_fully 不带。

        只能按清单顺序记账（严格前缀）；部分读是明确记录的事件并计入缺口，不是抽样。
        """
        index = self._resolve(item)
        if index != len(self.results):
            raise CompanionError("必须按清单顺序记账：下一项是 %d，收到 %d"
                                 % (len(self.results), index))
        if outcome not in OUTCOMES:
            raise CompanionError("outcome 必须是 %s 之一：%r" % ("/".join(OUTCOMES), outcome))
        if outcome == OUTCOME_FULL and reason is not None:
            raise CompanionError("read_fully 不携带 reason；部分读/失败才需要原因")
        if outcome in (OUTCOME_PARTIAL, OUTCOME_FAILED):
            if not isinstance(reason, str) or not reason.strip():
                raise CompanionError("%s 必须记录原因（部分读≠抽样，缺口要如实）" % outcome)
            reason = reason.strip()
        if notes is not None and not isinstance(notes, str):
            raise CompanionError("notes 必须是字符串或 None")
        entry = self.manifest[index]
        est = self._acquired.pop(index, None)
        if est is None:
            raise CompanionError("第 %d 项未领取，不能记账" % index)
        self.results.append({"index": index, "path": entry["path"], "sha256": entry["sha256"],
                             "generation": entry["generation"], "outcome": outcome,
                             "reason": reason, "notes": notes, "est_tokens": est})
        self.cursor += 1
        if self.cursor == len(self.manifest):
            self.status = STATUS_COMPLETED
        self._save()

    def top_up(self, tokens):
        """显式追加预算（暂停后续跑的入口之一）；追加是显式决定，绝不隐式重置。"""
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 1:
            raise CompanionError("追加预算必须是正整数")
        self.budget = Budget(self.budget.total_tokens + tokens, self.budget.spent)
        self._save()

    # ---- 覆盖出口 -------------------------------------------------------

    def coverage_account(self):
        """completed 时产出 coverage_account（复用 CoverageLedger）：分母=清单全集，
        已读= outcome∈read_fully 的项，partial/failed 如实列缺口。清单没读完不产生 account。
        """
        if self.status != STATUS_COMPLETED:
            raise CompanionError("清单未读完（%d/%d）不产生 coverage account——暂停不是完成"
                                 % (self.cursor, len(self.manifest)))
        ledger = CoverageLedger(run_id=self.campaign_id, generation=self.generation)
        ledger.set_denominator("semantics_deep", len(self.manifest))
        covered = sum(1 for r in self.results if r["outcome"] == OUTCOME_FULL)
        gaps = [{"index": r["index"], "path": r["path"], "outcome": r["outcome"],
                 "reason": r["reason"]}
                for r in self.results if r["outcome"] != OUTCOME_FULL]
        ledger.record_covered("semantics_deep", covered, gaps)
        return ledger.to_account()

    # ---- 内部 -----------------------------------------------------------

    def _resolve(self, item):
        if isinstance(item, CampaignItem):
            index, path = item.index, item.path
        elif isinstance(item, dict):
            if not isinstance(item.get("index"), int) or isinstance(item["index"], bool):
                raise CompanionError("领取项缺少合法 index：%r" % (item,))
            index, path = item["index"], item.get("path")
        elif isinstance(item, str):
            index, path = None, relative_path(item)
        else:
            raise CompanionError("item 必须是清单路径、领取项或路径字符串：%r" % (item,))
        manifest_path = self.manifest[index]["path"] if index is not None else None
        if index is not None:
            if not 0 <= index < len(self.manifest) or manifest_path != path:
                raise CompanionError("领取项与清单不一致：%r" % (item,))
            return index
        found = self._by_path.get(path)
        if found is None:
            raise CompanionError("清单中没有该路径：%s" % path)
        return found

    def _hash_file(self, full):
        from agents_kernel import digest
        return digest.sha256_file(str(full))

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.path, {
            "version": CHECKPOINT_VERSION, "kind": CAMPAIGN_KIND,
            "campaign_id": self.campaign_id, "generation": self.generation,
            "slice_tokens": self.slice_tokens, "estimation": True,
            "manifest": self.manifest, "cursor": self.cursor, "status": self.status,
            "budget": self.budget.checkpoint(), "results": self.results,
            "invalidations": self.invalidations, "root": str(self.root),
        })

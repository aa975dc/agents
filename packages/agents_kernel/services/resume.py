"""跨会话续接台账（ResumeLedger）：从持久事实恢复"有哪些活动、各自状态、下一步"。

06 计划 §4/§5、07 计划 §2（C12 尾/SC08/TK03 尾/TK04 尾/IX08 尾/ST04 尾）：
- 续接的依据是持久事实（run_root/.code-analysis-resume.json），不是聊天记忆。
  新会话/新进程打开台账即可知道：有哪些活动（campaign 深读/index_scan 预算扫描/
  integration 集成/custom）、各自状态、检查点在哪、下一步 API 调用是什么。
- 旧写者围栏（TK03 尾；SR-03 修订）：接管+登记+心跳+完结都是"读当前状态→校验
  持有者身份→分配/变更→落盘提交"的完整序列，整体处于 run_root 级跨进程互斥
  临界区（atomicio.exclusive_lock 独占锁文件）内——读取/比较/替换不再有互斥窗口。
  接管身份 = (writer_epoch, writer_owner) 二元组：epoch 单调递增，owner_id 是
  每个台账对象唯一的进程绑定 id（pid+随机后缀）；写路径在临界区内复查磁盘上的
  (epoch, owner)，任一不符即拒——两个接管者拿到相同数字 epoch 也无法互覆，
  旧持有者的心跳/登记/回报一律失效。变更以磁盘最新活动为基（读-改-写同界），
  已成功登记的活动不会被旧内存快照覆盖丢失。接管只围栏写入，不宣称旧进程已
  停止："超时不等于旧进程已停止"（§4）。强杀恢复：锁文件按持有者 pid 存活检测
  接管（陈旧锁不留死锁），台账是单文件 JSON 事实，无半写（原子替换）。
- 源版本更换策略（IX08 尾）：登记时记录 source_anchor（被分析仓库 HEAD 或
  manifest sha）；resume 时调用方提供当前锚，锚变了该项标 stale_anchor——
  游标绑定的是旧 generation，续跑会把不同 generation 拼成"完整结果"，因此
  不自动续跑，需按新锚重建。
- 检查点文件缺失/损坏→该项标 broken：如实呈现，不猜游标、不造空状态。
- 幂等与防重（TK04 尾）：台账只记"下一步怎么做"，真实副作用在各自服务的
  检查点续跑入口（campaign.resume / budget.resume）；台账不代替它们，重复
  续跑由各服务自己的严格前缀/幂等检查兜底。

边界（如实声明）：台账是被动的持久事实，不能自动唤醒宿主或调度器——"新会话
续接"由人/新进程显式发起；本模块只保证发起时拿到的续接清单真实可信。
integration 的服务面尚未合入工作树：kind 允许登记，但只做检查点存在性/游标
摘要的通用处理，不引入 integration 专属语义。
"""
import json
import os
import time
from pathlib import Path

from agents_kernel.atomicio import exclusive_lock, write_json
from agents_kernel.validation import CompanionError, text

LEDGER_VERSION = 1
LEDGER_KIND = "resume_ledger"
LEDGER_FILENAME = ".code-analysis-resume.json"
LOCK_FILENAME = LEDGER_FILENAME + ".lock"
TAKEOVER_LOG = "resume-takeovers.jsonl"
KIND_CAMPAIGN = "campaign"
KIND_INDEX_SCAN = "index_scan"
KIND_INTEGRATION = "integration"
KIND_CUSTOM = "custom"
KINDS = (KIND_CAMPAIGN, KIND_INDEX_SCAN, KIND_INTEGRATION, KIND_CUSTOM)
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
CONDITION_OK = "ok"
CONDITION_BROKEN = "broken"
CONDITION_STALE_ANCHOR = "stale_anchor"
CAMPAIGN_SUFFIX = ".campaign.json"


def _schema_version(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CompanionError("schema_version 必须是正整数")
    return value


def _optional_anchor(value):
    if value is None:
        return None
    return text(value, "source_anchor")


class ResumeLedger:
    """run_root 级活动台账：登记/心跳/完结 + 续接清单 + (epoch, owner) 围栏。

    打开即接管：在互斥临界区内读台账 → writer_epoch+1 + 新 owner_id 并记接管
    事件；此后旧 (epoch, owner) 持有者的 register/heartbeat/complete/fail 一律
    被拒（临界区内复查磁盘身份）。时钟 clock 可注入（默认 time.time 墙钟，
    跨进程可比）。owner_id 可注入（测试用），缺省进程唯一（pid+随机后缀）。
    """

    def __init__(self, path, writer_id, epoch, taken_from, activities, clock,
                 owner_id=None):
        self.path = Path(path)
        self.writer_id = writer_id
        self.epoch = epoch
        self._taken_from = taken_from     # 接管来源 epoch（新建为 None）：审计信息
        self._taken_from_state = None     # 接管时观察到的 (epoch, owner)：落盘守护放行用
        self._activities = activities      # {(kind, id): row dict}（最近一次临界区内的磁盘基线）
        self._clock = clock
        self.owner_id = owner_id if owner_id is not None else _new_owner_id()

    @classmethod
    def open(cls, run_root, writer_id, clock=time.time):
        """打开（或创建）run_root 的续接台账；已有台账则在互斥临界区内接管。"""
        writer = text(writer_id, "writer_id")
        path = Path(run_root) / LEDGER_FILENAME
        with exclusive_lock(path.parent / LOCK_FILENAME):
            previous = cls._read(path)
            if previous is None:
                ledger = cls(path, writer, 1, None, {}, clock)
                ledger._save()
                return ledger
            epoch = int(previous["writer_epoch"]) + 1
            ledger = cls(path, writer, epoch, int(previous["writer_epoch"]),
                         dict(previous["activities"]), clock)
            # 记录接管来源身份：本次落盘就是把"来源状态"原子替换为"我的状态"，
            # 守护据此放行（epoch+owner 成对核对，两个接管者不可能共享同一 owner）
            ledger._taken_from_state = (int(previous["writer_epoch"]),
                                        previous["writer_owner"])
            ledger._save()
            cls._record_takeover(path.parent, previous, writer, epoch,
                                 ledger.owner_id, clock())
            return ledger

    # ---- 登记与更新（互斥临界区内读-改-写，全部带 (epoch, owner) 围栏） ----

    def register(self, kind, activity_id, checkpoint_path, schema_version,
                 source_anchor=None):
        """登记一个活动（或同 id 重登为新一次尝试）：status 重置为 running。"""
        checkpoint = str(Path(text(checkpoint_path, "checkpoint_path")))
        version = _schema_version(schema_version)
        anchor = _optional_anchor(source_anchor)

        def update():
            row = self._row(kind, activity_id)
            now = self._clock()
            row.update({
                "checkpoint_path": checkpoint,
                "schema_version": version,
                "source_anchor": anchor,
                "status": STATUS_RUNNING,
                "registered_at": now, "heartbeat_at": now,
                "completed_at": None, "failed_at": None, "fail_reason": None,
            })
            return dict(row)

        return self._mutate(update)

    def heartbeat(self, kind, activity_id, note=None):
        """心跳：只刷新 heartbeat_at（可附 note）；活动仍由各自服务推进。"""
        if note is not None and not isinstance(note, str):
            raise CompanionError("note 必须是字符串或 None")

        def update():
            row = self._row(kind, activity_id)
            row["heartbeat_at"] = self._clock()
            row["note"] = note

        self._mutate(update)

    def complete(self, kind, activity_id):
        """完结：status=completed；resume_plan 不再列出该活动。"""

        def update():
            row = self._row(kind, activity_id)
            row["status"] = STATUS_COMPLETED
            row["completed_at"] = self._clock()

        self._mutate(update)

    def fail(self, kind, activity_id, reason):
        """失败：如实记录原因；续接清单会列出它并附原因，不静默消失。"""
        reason = text(reason, "失败原因")

        def update():
            row = self._row(kind, activity_id)
            row["status"] = STATUS_FAILED
            row["failed_at"] = self._clock()
            row["fail_reason"] = reason

        self._mutate(update)

    # ---- 续接清单 --------------------------------------------------------

    def resume_plan(self, current_anchor=None):
        """未完成活动的续接动作清单：核对检查点与锚，给出游标摘要与建议调用。

        - completed 不列出；failed 列出并附原因。
        - 检查点缺失/损坏→condition=broken，不猜游标。
        - current_anchor 给定且与登记锚不符→condition=stale_anchor，
          不给续接调用（不同 generation 不拼完整结果，需按新锚重建）。
        """
        current_anchor = _optional_anchor(current_anchor)
        plan = []
        for key in sorted(self._activities):
            row = self._activities[key]
            if row["status"] == STATUS_COMPLETED:
                continue
            item = {
                "kind": row["kind"], "id": row["id"],
                "checkpoint_path": row["checkpoint_path"],
                "ledger_status": row["status"],
                "condition": CONDITION_OK,
                "cursor_summary": None,
                "resume_call": None,
                "detail": row["fail_reason"],
            }
            data = self._read_checkpoint(row["checkpoint_path"])
            if data is None:
                item["condition"] = CONDITION_BROKEN
                item["detail"] = "检查点缺失或损坏，无法给出可信游标：%s" \
                    % row["checkpoint_path"]
                plan.append(item)
                continue
            if current_anchor is not None and row["source_anchor"] is not None \
                    and row["source_anchor"] != current_anchor:
                item["condition"] = CONDITION_STALE_ANCHOR
                item["detail"] = ("源锚已变化（登记=%s 当前=%s）：游标绑定旧 generation，"
                                  "不可信，需按新锚重建，不自动续跑"
                                  % (row["source_anchor"], current_anchor))
                plan.append(item)
                continue
            item["cursor_summary"] = _cursor_summary(data)
            item["resume_call"] = _resume_call(row)
            plan.append(item)
        return plan

    def activities(self):
        """全部登记行的只读快照（含 completed/failed），按 (kind, id) 排序。"""
        return [dict(self._activities[key]) for key in sorted(self._activities)]

    # ---- 内部：互斥临界区与 (epoch, owner) 身份围栏 ----------------------

    def _mutate(self, update):
        """跨进程互斥临界区内的读-改-写：读盘 → 校验持有者身份 → 以磁盘最新
        活动为基变更 → 落盘提交，全部同界。旧持有者/旧内存快照无法覆盖他人
        已提交的活动；另一接管者在本临界区内必然看到前者已提交的新 epoch。
        返回 update 的返回值（如 register 的行快照）。
        """
        with exclusive_lock(self.path.parent / LOCK_FILENAME):
            current = self._read(self.path)
            if current is not None:
                self._require_identity(current)
                self._activities = current["activities"]
            result = update()
            self._save()
            return result

    def _row(self, kind, activity_id):
        """定位（或新建）活动行；只应在 _mutate 的临界区内被调用。"""
        if kind not in KINDS:
            raise CompanionError("kind 必须是 %s 之一：%r" % ("/".join(KINDS), kind))
        aid = text(activity_id, "activity_id")
        row = self._activities.get((kind, aid))
        if row is None:
            row = {"kind": kind, "id": aid, "writer_epoch": self.epoch,
                   "note": None}
            self._activities[(kind, aid)] = row
        return row

    def _identity_is_mine(self, current):
        """持有者身份：epoch 与 owner_id 同时匹配——数字相同不构成身份。"""
        return (current["writer_epoch"] == self.epoch
                and current["writer_owner"] == self.owner_id)

    def _require_identity(self, current):
        if self._identity_is_mine(current):
            return
        raise CompanionError(
            "更新被拒：台账已被接管（当前 epoch=%d writer=%s owner=%s；本持有者 "
            "epoch=%d owner=%s）——旧持有者不得再写"
            % (current["writer_epoch"], current["writer_id"],
               current["writer_owner"], self.epoch, self.owner_id))

    def _save(self):
        payload = {"version": LEDGER_VERSION, "kind": LEDGER_KIND,
                   "writer_id": self.writer_id, "writer_epoch": self.epoch,
                   "writer_owner": self.owner_id,
                   "activities": [self._activities[key]
                                  for key in sorted(self._activities)]}

        def guard():
            # 纵深防御：临界区内正常只会看到自己的身份，或（接管首次落盘时）
            # 自己刚读走的来源状态；其余一律视为并发接管，拒绝覆盖。
            current = self._read(self.path)
            if current is None or self._identity_is_mine(current):
                return
            if (self._taken_from_state is not None
                    and (int(current["writer_epoch"]), current["writer_owner"])
                    == self._taken_from_state):
                return
            raise CompanionError(
                "落盘被拒：台账已被并发接管（本持有者 epoch=%d owner=%s）：%s"
                % (self.epoch, self.owner_id, self.path))

        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.path, payload, before_replace=guard)

    @staticmethod
    def _read(path):
        """读台账：不存在 → None；损坏/字段不全 → CompanionError（不造空台账掩盖）。"""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise CompanionError("续接台账损坏：%s" % path) from exc
        if (not isinstance(data, dict) or data.get("version") != LEDGER_VERSION
                or data.get("kind") != LEDGER_KIND
                or not isinstance(data.get("activities"), list)
                or not isinstance(data.get("writer_epoch"), int)
                or isinstance(data.get("writer_epoch"), bool)
                or not isinstance(data.get("writer_id"), str)):
            raise CompanionError("续接台账字段不完整或版本不符：%s" % path)
        owner = data.get("writer_owner")
        if owner is not None and not isinstance(owner, str):
            raise CompanionError("续接台账字段不完整或版本不符：%s" % path)
        activities = {}
        for row in data["activities"]:
            if (not isinstance(row, dict) or row.get("kind") not in KINDS
                    or not isinstance(row.get("id"), str)):
                raise CompanionError("续接台账活动行形状非法：%r" % (row,))
            activities[(row["kind"], row["id"])] = row
        return {"writer_id": data["writer_id"], "writer_epoch": data["writer_epoch"],
                "writer_owner": owner,  # 旧格式台账无此字段：None，首个新接管者起必有
                "activities": activities}

    @staticmethod
    def _read_checkpoint(checkpoint_path):
        """检查点存在且为合法 JSON → 返回解析值；缺失/损坏 → None（broken，不抛）。"""
        try:
            return json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _record_takeover(directory, previous, writer, epoch, owner_id, at):
        record = {"type": "resume_takeover", "ledger": LEDGER_FILENAME,
                  "from": {"writer_id": previous["writer_id"],
                           "epoch": int(previous["writer_epoch"]),
                           "writer_owner": previous["writer_owner"]},
                  "to": {"writer_id": writer, "epoch": epoch,
                         "writer_owner": owner_id}, "at": at}
        line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n") \
            .encode("utf-8")
        fd = os.open(str(Path(directory) / TAKEOVER_LOG),
                     os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)


def _new_owner_id():
    """接管者身份 id：进程唯一（pid + 随机后缀，同进程多对象/跨时pid复用也唯一）。"""
    return "%d-%s" % (os.getpid(), os.urandom(8).hex())


def _cursor_summary(data):
    """从检查点提取游标摘要；结构未知则返回 None（如实说明，不猜）。"""
    if not isinstance(data, dict):
        return None
    if data.get("kind") == "deep_read_campaign" and isinstance(data.get("cursor"), int) \
            and isinstance(data.get("manifest"), list):
        return "cursor=%d/%d status=%s" % (data["cursor"], len(data["manifest"]),
                                           data.get("status"))
    if "cursor" in data:
        return "cursor=%s" % json.dumps(data["cursor"], ensure_ascii=False)
    return None


def _resume_call(row):
    """按 kind 给出建议的续接 API 调用字符串；无已知入口 → None（人工检查）。"""
    path = Path(row["checkpoint_path"])
    if row["kind"] == KIND_CAMPAIGN:
        name = path.name
        if not name.endswith(CAMPAIGN_SUFFIX):
            return None
        campaign_id = name[: -len(CAMPAIGN_SUFFIX)]
        return "DeepReadCampaign.resume(run_dir=%r, campaign_id=%r)" \
            % (str(path.parent), campaign_id)
    if row["kind"] == KIND_INDEX_SCAN:
        return "budget.resume(%r)" % str(path)
    return None  # integration / custom：无统一入口，按检查点 JSON 人工核对

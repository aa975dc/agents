"""工作区隔离：git worktree / 受控副本、精确变更检查与在册清理（stdlib only, Py3.9+）。

C07 后半/TK02/FS03：worktree 不等于沙箱——本模块解决其中确定性的目录隔离部分：
"两个写任务不共目录"。派发前 dry-run（将创建的路径与大小估算）；创建时先在
manifest 登记（status="creating"）再动手，成功后转 ready——中断残留因此在册
可识别，下次 sweep() 接管清理；回报时给出精确变更清单；收尾只清理 manifest
在册的工作区，不在册目录一律拒绝（防误删用户目录）。

- git 仓库（source 下存在 .git）：subprocess `git worktree add --detach <path>
  [<base_commit>]`。路径先校验：task_id 不得含路径分隔符；目标必须落在 root
  内、与 source 互不嵌套。--detach 固定基线，不制造分支名冲突。变更检查用
  `git status --porcelain`（含未跟踪），三态归一 added/modified/deleted
  （rename 记到新路径并按 modified 计——门禁只关心"改了哪些路径"）。
- 非 git：受控副本 copytree（符号链接按链接本身复制），排除清单可配（默认
  复用 paths.EXCLUDED_DIRS 并补 vendor/.hg/.svn）。副本建成后立即对目标目录
  生成基线快照 {posix 相对路径: sha256}（1MiB 分块流式，复用 kernel.digest；
  符号链接不入快照——链接目标在副本外的变化不构成工作区变更）。变更检查=
  按创建时冻结的排除清单重走目标目录，与快照做三态差异。
- manifest：root/manifest.json，write_atomic 落盘，单协调写者假设（与租约/
  幂等登记同风格的进程内面）。快照内联在各自条目里——大目录的 manifest 差异
  本就是台账的一部分；拆分存储属优化而非本层语义。

并行写正确性不在本层声称：独立工作区只保证不共目录，未证明合并正确前回报
集成仍须串行（06 计划 §3）。prepare_dispatch 是最小派发门：ownership claim
成功 + workspace 创建成功才算就绪，任一失败干净回滚已建部分（task 不启动、
无残留）。
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import NamedTuple

from agents_kernel import digest
from agents_kernel.atomicio import write_json
from agents_kernel.paths import EXCLUDED_DIRS, inside
from agents_kernel.validation import CompanionError, text

WORKSPACE_VERSION = 1
MANIFEST_FILE = "manifest.json"
DEFAULT_EXCLUDES = frozenset(EXCLUDED_DIRS | {"vendor", ".hg", ".svn"})


class WorkspaceInfo(NamedTuple):
    """一次成功创建后的工作区快照。"""
    task_id: str
    mode: str        # git-worktree / copy
    path: str
    status: str


def _safe_name(task_id):
    tid = text(task_id, "任务编号")
    if "/" in tid or "\\" in tid or tid in (".", ".."):
        raise CompanionError("任务编号不能作工作区目录名：%s" % tid)
    return tid


def _triage(status):
    """porcelain 状态位 → 三态：D 删除优先，A/?? 新增，其余按修改计。"""
    if "D" in status:
        return "deleted"
    if "A" in status or status == "??":
        return "added"
    return "modified"


class WorkspaceManager:
    """每任务隔离工作区的 plan/create/changes/cleanup/sweep 面。"""

    def __init__(self, root, source, excludes=None, clock=time.time):
        self._root = Path(root).resolve()
        self._source = Path(source).resolve()
        if not self._source.is_dir():
            raise CompanionError("工作区来源目录不存在：%s" % self._source)
        self._excludes = frozenset(excludes) if excludes is not None else DEFAULT_EXCLUDES
        for name in self._excludes:
            if "/" in text(name, "排除项") or "\\" in name:
                raise CompanionError("排除项必须是目录/文件名而不是路径：%s" % name)
        self._clock = clock

    # ---- 公开入口 ----

    def plan(self, task_id):
        """dry-run：将创建的模式/路径与大小估算；不创建目录、不写 manifest。
        目标已在册或路径已被占用时与 create 同步拒绝（前置检查前移）。"""
        tid = _safe_name(task_id)
        if tid in self._load()["workspaces"]:
            raise CompanionError("任务 %s 的工作区已在册" % tid)
        target = self._root / tid
        if target.exists():
            raise CompanionError("目标路径已存在且不在册（疑似用户目录）：%s" % target)
        files, total = self._estimate(self._source, self._excludes)
        return {"task_id": tid,
                "mode": "git-worktree" if (self._source / ".git").exists() else "copy",
                "path": str(target), "estimated_files": files, "estimated_bytes": total,
                "excludes": sorted(self._excludes)}

    def create(self, task_id, base_commit=None):
        """派发隔离工作区：git 来源建 detached worktree，否则受控副本。
        先写 creating 在册标记再动手（中断可被 sweep 接管），成功转 ready；
        失败回滚本调用创建的目录与在册条目，不留残根。"""
        tid = _safe_name(task_id)
        manifest = self._load()
        if tid in manifest["workspaces"]:
            raise CompanionError("任务 %s 的工作区已在册，先清理再重建" % tid)
        mode = "git-worktree" if (self._source / ".git").exists() else "copy"
        target = self._root / tid
        if target.exists():
            raise CompanionError("目标路径已存在且不在册（疑似用户目录），拒绝覆盖：%s" % target)
        if inside(target, self._source) or inside(self._source, target):
            raise CompanionError("工作区目标与来源互相嵌套：%s / %s" % (target, self._source))
        entry = {"version": WORKSPACE_VERSION, "task_id": tid, "mode": mode,
                 "path": str(target), "source": str(self._source),
                 "created_at": self._clock(), "status": "creating",
                 "excludes": sorted(self._excludes)}
        manifest["workspaces"][tid] = entry
        self._save(manifest)  # 在册标记先落盘：之后任一步中断都可被 sweep 接管
        try:
            if mode == "git-worktree":
                args = ["-C", str(self._source), "worktree", "add", "--detach", str(target)]
                if base_commit is not None:
                    args.append(text(base_commit, "基线提交"))
                self._git(args)
            else:
                if base_commit is not None:
                    raise CompanionError("基线提交仅对 git 工作区有意义")
                shutil.copytree(self._source, target, symlinks=True,
                                ignore=shutil.ignore_patterns(*sorted(self._excludes)))
                entry["snapshot"] = self._snapshot(target, self._excludes)
            entry["status"] = "ready"
            self._save(manifest)
        except BaseException:
            # 失败回滚：目录与在册条目都是本调用创建的，清掉不留残根
            shutil.rmtree(target, ignore_errors=True)
            manifest["workspaces"].pop(tid, None)
            try:
                self._save(manifest)
            except CompanionError:
                pass
            raise
        return WorkspaceInfo(tid, mode, str(target), "ready")

    def changes(self, task_id):
        """精确变更清单：git 用 status --porcelain；副本与创建时基线快照做
        三态差异（added/modified/deleted，按路径稳定排序）。不在册拒绝。"""
        tid = _safe_name(task_id)
        entry = self._entry(self._load(), tid)
        if entry["mode"] == "git-worktree":
            items = []
            for line in self._git(["-C", entry["path"], "status", "--porcelain"]).splitlines():
                if not line.strip():
                    continue
                rest = line[3:]
                if " -> " in rest:  # rename：记新路径（含转义引号的罕见形态不展开）
                    rest = rest.rsplit(" -> ", 1)[1]
                items.append({"path": rest, "change": _triage(line[:2])})
            return sorted(items, key=lambda item: item["path"])
        excludes = frozenset(entry.get("excludes", self._excludes))
        current = self._snapshot(Path(entry["path"]), excludes)
        baseline = entry.get("snapshot", {})
        items = ([{"path": p, "change": "added"} for p in current if p not in baseline]
                 + [{"path": p, "change": "modified"} for p in current
                    if p in baseline and current[p] != baseline[p]]
                 + [{"path": p, "change": "deleted"} for p in baseline if p not in current])
        return sorted(items, key=lambda item: item["path"])

    def cleanup(self, task_id):
        """清理：只删 manifest 在册且路径仍落在 root 内的工作区；不在册一律
        拒绝（防误删用户目录）。git 工作区删目录后对源仓库做 worktree prune。"""
        tid = _safe_name(task_id)
        manifest = self._load()
        entry = self._entry(manifest, tid)
        target = Path(entry["path"])
        if not inside(target, self._root):
            raise CompanionError("工作区路径逃逸 root，拒绝删除：%s" % target)
        shutil.rmtree(target, ignore_errors=True)
        if entry["mode"] == "git-worktree":
            try:  # 源仓库迁移等导致 prune 失败可忽略：目录已删，元数据无属主危害
                self._git(["-C", entry["source"], "worktree", "prune"])
            except CompanionError:
                pass
        manifest["workspaces"].pop(tid, None)
        self._save(manifest)
        return {"task_id": tid, "removed": str(target)}

    def sweep(self):
        """接管清理中断残留：只动 status="creating" 的在册条目（create 落盘
        标记后中断留下的）；ready 在册与不在册目录一律不碰。返回已接管任务列表。"""
        adopted = []
        for tid, entry in sorted(self._load()["workspaces"].items()):
            if entry.get("status") == "creating":
                adopted.append(self.cleanup(tid)["task_id"])
        return adopted

    def entries(self):
        """只读台账（剥离快照本体、附 snapshotted_files 计数），审计与测试用。"""
        out = []
        for tid, entry in sorted(self._load()["workspaces"].items()):
            item = {k: v for k, v in entry.items() if k != "snapshot"}
            if "snapshot" in entry:
                item["snapshotted_files"] = len(entry["snapshot"])
            out.append(item)
        return out

    # ---- 内部 ----

    @staticmethod
    def _entry(manifest, tid):
        entry = manifest["workspaces"].get(tid)
        if entry is None:
            raise CompanionError("任务 %s 的工作区不在册（只管理本工具创建的工作区）" % tid)
        return entry

    def _manifest_path(self):
        return self._root / MANIFEST_FILE

    def _load(self):
        try:
            data = json.loads(self._manifest_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": WORKSPACE_VERSION, "workspaces": {}}
        except (OSError, ValueError) as exc:
            raise CompanionError("manifest 无法读取：%s" % self._manifest_path()) from exc
        if (not isinstance(data, dict) or data.get("version") != WORKSPACE_VERSION
                or not isinstance(data.get("workspaces"), dict)):
            raise CompanionError("manifest 版本或形状不符：%s" % self._manifest_path())
        return data

    def _save(self, manifest):
        self._root.mkdir(parents=True, exist_ok=True)
        write_json(self._manifest_path(), manifest)

    @staticmethod
    def _walk_files(root_dir, excludes):
        """按排除清单遍历目录：跳过排除名与符号链接（不跟链接），产出绝对路径。"""
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = [d for d in dirnames if d not in excludes
                           and not os.path.islink(os.path.join(dirpath, d))]
            for name in filenames:
                full = os.path.join(dirpath, name)
                if not os.path.islink(full):
                    yield full

    @classmethod
    def _snapshot(cls, root_dir, excludes):
        """基线快照 {posix 相对路径: sha256}；1MiB 分块流式（kernel.digest）。"""
        return {os.path.relpath(full, root_dir).replace(os.sep, "/"):
                digest.sha256_file(full)[0]
                for full in cls._walk_files(root_dir, excludes)}

    @classmethod
    def _estimate(cls, source, excludes):
        files = total = 0
        for full in cls._walk_files(source, excludes):
            files += 1
            total += os.stat(full).st_size
        return files, total

    @staticmethod
    def _git(args):
        try:
            proc = subprocess.run(["git"] + args, capture_output=True, text=True)
        except FileNotFoundError:
            raise CompanionError("git 不可用（未安装或不在 PATH）") from None
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise CompanionError("git %s 失败：%s" % (args[0], detail))
        return proc.stdout


def prepare_dispatch(task_id, paths, ownership, workspace):
    """派发前置门（最小集成）：ownership 声明成功 + 工作区创建成功，任务才允许启动。

    claim 走 queue=False：共享路径他方持有时显式拒绝（retryable，调度方稍后
    重试），排队等待语义由调用方自选（ownership.claim(queue=True)）。任一步
    失败→回滚已建部分（claim 释放、在册条目删除、半成品目录删除）并抛错：
    task 不启动、无残留。
    """
    ownership.claim(task_id, paths, queue=False)
    try:
        info = workspace.create(task_id)
    except BaseException:
        ownership.release(task_id)  # 回滚自有 claim；release 异常不吞 create 的原始错误
        raise
    return info

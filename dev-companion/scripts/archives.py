"""Explicit-scope local file snapshots. No Git, database, or whole-disk backup.

Callers must hold the project's exclusive lock for every mutating operation,
including preview_restore. Stop editors and agents before restoring files.
"""

import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
from datetime import datetime, timezone

import kernel_bootstrap  # noqa: F401 — P2-05 单处引导：优先本目录 _kernel_vendor，回退仓库 packages/


from agents_kernel.atomicio import read_json, write_atomic
from agents_kernel.digest import canonical_bytes, sha256_bytes
from agents_kernel.paths import SENSITIVE_NAMES, SENSITIVE_PREFIXES, SENSITIVE_SUFFIXES, absolute, realpath


MAX_FILES = 1000
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
BACKUP_NOTICE = "仅保护显式纳管的普通文件及其缺失状态；不包含 Git、数据库、线上服务或整盘内容。"


class ArchiveError(ValueError):
    """A rejected or failed snapshot operation, optionally with a safety archive."""

    def __init__(self, message, safety_archive_id=None):
        super().__init__(message)
        self.safety_archive_id = safety_archive_id


def pending_restore(project_root):
    """供 core/CLI 探测未完成恢复的公共 API（外部不得硬编码存档内部布局常量）。

    返回 None 或 {"safety_archive_id": ...}。路径异常抛 ArchiveError（由调用方转译为
    自身的错误类型）；元数据损坏时由共享 read_json 抛 CompanionError，与 core 侧
    旧探测行为一致。
    """
    archive_root = realpath(project_root) / ".dev-companion" / "archives"
    pending = archive_root / "restore-pending.json"
    if archive_root.is_symlink() or pending.is_symlink():
        raise ArchiveError("存档恢复记录路径异常，请先核查")
    if not pending.exists():
        return None
    record = read_json(pending)
    return {"safety_archive_id": record.get("safety_archive_id") if isinstance(record, dict) else None}


class ArchiveStore:
    def __init__(self, project):
        candidate = absolute(project)
        if candidate.is_symlink() or not candidate.is_dir():
            raise ArchiveError("项目必须是现有普通目录，不能是符号链接。")
        self.project = realpath(candidate)
        self.root = self.project / ".dev-companion" / "archives"

    def _internal_path(self, path):
        """Refuse linked directories and metadata, including a linked store root."""
        relative = path.relative_to(self.project)
        current = self.project
        for part in relative.parts:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                raise ArchiveError("存档内部路径不允许符号链接。")
            if current != path and not stat.S_ISDIR(info.st_mode):
                raise ArchiveError("存档目录路径被普通文件占用。")
        return path

    def _read_bytes(self, path, limit):
        self._internal_path(path)
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ArchiveError("存档内容必须是普通文件。")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ArchiveError("存档内容必须是普通文件。")
                data = source.read(limit + 1)
        except OSError as exc:
            raise ArchiveError("无法读取存档内容：" + str(exc)) from exc
        if len(data) > limit:
            raise ArchiveError("存档内容超过允许大小。")
        return data

    def _read_json(self, path):
        try:
            return json.loads(self._read_bytes(path, MAX_METADATA_BYTES))
        except (ValueError, UnicodeError) as exc:
            raise ArchiveError("存档元数据损坏：" + str(path.name)) from exc

    def _atomic_json(self, path, value):
        self._internal_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = canonical_bytes(value)
        if len(data) > MAX_METADATA_BYTES:
            raise ArchiveError("存档元数据超过允许大小。")
        write_atomic(path, data, prefix=".pending-")

    def _catalog(self):
        path = self._internal_path(self.root / "catalog.json")
        if not path.exists():
            return {"version": 1, "managed_paths": [], "archives": []}
        catalog = self._read_json(path)
        if not isinstance(catalog, dict) or catalog.get("version") != 1:
            raise ArchiveError("不支持或损坏的存档目录。")
        paths = catalog.get("managed_paths")
        records = catalog.get("archives")
        if not isinstance(paths, list) or not isinstance(records, list):
            raise ArchiveError("存档目录缺少范围或历史。")
        self._validate_paths(paths)
        if len(paths) != len(set(paths)):
            raise ArchiveError("存档目录包含重复范围。")
        seen = set()
        for record in records:
            if not isinstance(record, dict):
                raise ArchiveError("存档历史损坏。")
            archive_id = record.get("archive_id", "")
            if not isinstance(archive_id, str) or not re.fullmatch(r"[0-9TZ-]+[a-f0-9]{16}", archive_id):
                raise ArchiveError("存档编号无效。")
            if archive_id in seen or not re.fullmatch(r"[a-f0-9]{64}", str(record.get("manifest_sha256", ""))):
                raise ArchiveError("存档历史重复或校验信息损坏。")
            seen.add(archive_id)
        return catalog

    def _assert_writable(self):
        pending_path = self._internal_path(self.root / "restore-pending.json")
        if pending_path.exists():
            pending = self._read_json(pending_path)
            safety = pending.get("safety_archive_id") if isinstance(pending, dict) else None
            raise ArchiveError(
                "上次恢复未完成，已暂停写入。请根据 restore-pending.json 和保护存档人工核对；不得直接重试。保护存档：" + str(safety),
                safety,
            )

    def _validate_path(self, name):
        if not isinstance(name, str) or not name or len(name) > 1024:
            raise ArchiveError("文件路径必须是非空项目相对路径。")
        if name.startswith("/") or "\\" in name or ":" in name or "\x00" in name:
            raise ArchiveError("禁止绝对路径或跨平台歧义路径：" + name)
        parts = name.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ArchiveError("禁止空路径段、. 或 ..：" + name)
        for part in parts:
            lower = part.lower()
            # 统一敏感清单（agents_kernel.paths）= 本模块旧清单 ∪ core 旧清单；
            # core 独有条目均已被前缀规则覆盖，此处拒绝范围与旧实现完全一致。
            if lower in SENSITIVE_NAMES:
                raise ArchiveError("不能纳入内部目录或凭据目录：" + name)
            if (lower.startswith(SENSITIVE_PREFIXES) or lower.endswith(SENSITIVE_SUFFIXES)):
                raise ArchiveError("不能纳入可能包含凭据或私钥的路径：" + name)
        return name

    def _validate_paths(self, paths):
        if not isinstance(paths, list) or len(paths) > MAX_FILES:
            raise ArchiveError("每次最多纳管 1000 个文件。")
        for name in paths:
            self._validate_path(name)
        names = set(paths)
        for name in paths:
            parts = name.split("/")
            if any("/".join(parts[:index]) in names for index in range(1, len(parts))):
                raise ArchiveError("文件范围存在父子路径冲突。")

    def _project_path(self, name):
        self._validate_path(name)
        path = self.project
        for part in name.split("/"):
            path /= part
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                raise ArchiveError("不能保存或恢复符号链接：" + name)
            if path != self.project / name and not stat.S_ISDIR(info.st_mode):
                raise ArchiveError("文件父目录被非目录占用：" + name)
            if path == self.project / name and not stat.S_ISREG(info.st_mode):
                raise ArchiveError("只能纳入普通文件，不能纳入目录或特殊文件：" + name)
        return path

    def _capture(self, paths):
        self._validate_paths(paths)
        entries, blobs, total = {}, {}, 0
        for name in sorted(set(paths)):
            path = self._project_path(name)
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except FileNotFoundError:
                entries[name] = None
                continue
            except OSError as exc:
                raise ArchiveError("无法读取项目文件：" + name) from exc
            with os.fdopen(fd, "rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ArchiveError("只能保存普通文件：" + name)
                if before.st_size > MAX_FILE_BYTES:
                    raise ArchiveError("单文件上限为 10 MiB：" + name)
                data = source.read(MAX_FILE_BYTES + 1)
                after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ArchiveError("读取时文件发生变化，请停止写入后重试：" + name)
            total += len(data)
            if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise ArchiveError("单文件上限为 10 MiB，每次存档上限为 50 MiB。")
            digest = sha256_bytes(data)
            entries[name] = {"sha256": digest, "size": len(data), "mode": stat.S_IMODE(before.st_mode) & 0o777}
            blobs[digest] = data
        return entries, blobs

    def _load_archive(self, archive_id, catalog):
        record = next((item for item in catalog["archives"] if item["archive_id"] == archive_id), None)
        if record is None:
            raise ArchiveError("找不到存档：" + str(archive_id))
        directory = self.root / archive_id
        data = self._read_bytes(directory / "manifest.json", MAX_METADATA_BYTES)
        if sha256_bytes(data) != record["manifest_sha256"]:
            raise ArchiveError("存档清单校验失败，拒绝恢复。")
        try:
            manifest = json.loads(data)
            entries = manifest["entries"]
            if manifest["archive_id"] != archive_id or not isinstance(entries, dict):
                raise ValueError("manifest identity")
        except (ValueError, TypeError, KeyError) as exc:
            raise ArchiveError("存档清单损坏。") from exc
        self._validate_paths(list(entries))
        if not set(entries).issubset(catalog["managed_paths"]):
            raise ArchiveError("存档范围不属于当前显式纳管范围。")
        blobs, total = {}, 0
        for name, entry in entries.items():
            if entry is None:
                continue
            if (not isinstance(entry, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("sha256", "")))
                    or type(entry.get("size")) is not int or not 0 <= entry["size"] <= MAX_FILE_BYTES
                    or type(entry.get("mode")) is not int or not 0 <= entry["mode"] <= 0o777):
                raise ArchiveError("存档文件记录损坏：" + name)
            digest = entry["sha256"]
            blob = self._read_bytes(directory / "files" / digest, MAX_FILE_BYTES)
            total += len(blob)
            if len(blob) != entry["size"] or sha256_bytes(blob) != digest or total > MAX_TOTAL_BYTES:
                raise ArchiveError("存档内容校验失败或超过大小上限：" + name)
            blobs[digest] = blob
        return record, entries, blobs

    def _save_snapshot(self, catalog, paths, summary, kind):
        entries, blobs = self._capture(paths)
        archive_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(8)
        created_at = datetime.now(timezone.utc).isoformat()
        manifest = {"version": 1, "archive_id": archive_id, "created_at": created_at,
                    "summary": summary, "kind": kind, "entries": entries}
        self._internal_path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".building-", dir=self.root))
        destination = self.root / archive_id
        try:
            (temporary / "files").mkdir()
            for digest, data in blobs.items():
                with (temporary / "files" / digest).open("wb") as out:
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
            manifest_data = canonical_bytes(manifest)
            with (temporary / "manifest.json").open("wb") as out:
                out.write(manifest_data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, destination)
            record = {"archive_id": archive_id, "created_at": created_at, "summary": summary,
                      "kind": kind, "file_count": sum(entry is not None for entry in entries.values()),
                      "scope_count": len(entries), "manifest_sha256": sha256_bytes(manifest_data)}
            updated = {"version": 1, "managed_paths": sorted(paths), "archives": catalog["archives"] + [record]}
            self._load_archive(archive_id, updated)
            self._atomic_json(self.root / "catalog.json", updated)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return record, updated, entries

    def save(self, paths, summary):
        """Explicitly adopt paths; snapshot all adopted files and missing paths."""
        self._assert_writable()
        self._validate_paths(paths)
        if not paths:
            raise ArchiveError("请明确列出需要纳入存档的文件。")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
            raise ArchiveError("请提供 1 至 2000 字的存档说明。")
        catalog = self._catalog()
        managed = sorted(set(catalog["managed_paths"]) | set(paths))
        record, _, _ = self._save_snapshot(catalog, managed, summary, "manual")
        return dict(record, managed_paths=managed, notice=BACKUP_NOTICE)

    def history(self):
        catalog = self._catalog()
        result = []
        for record in reversed(catalog["archives"]):
            try:
                self._load_archive(record["archive_id"], catalog)
                result.append(dict(record, integrity="verified"))
            except ArchiveError as exc:
                result.append(dict(record, integrity="failed", error=str(exc)))
        return result

    def _restore_context(self, archive_id):
        catalog = self._catalog()
        record, entries, blobs = self._load_archive(archive_id, catalog)
        paths = sorted(catalog["managed_paths"])
        current, _ = self._capture(paths)
        desired = {name: entries.get(name) for name in paths}
        fingerprint = sha256_bytes(canonical_bytes({"archive_id": archive_id, "manifest_sha256": record["manifest_sha256"],
                                                    "scope": paths, "current": current}))
        return catalog, current, desired, blobs, fingerprint

    def preview_restore(self, archive_id):
        self._assert_writable()
        catalog, current, desired, _, fingerprint = self._restore_context(archive_id)
        changes = []
        for name in sorted(desired):
            if current[name] != desired[name]:
                action = "create" if current[name] is None else "delete" if desired[name] is None else "modify"
                changes.append({"path": name, "action": action})
        token = secrets.token_urlsafe(32)
        self._atomic_json(self.root / "restore-preview.json", {"archive_id": archive_id,
                          "token_sha256": sha256_bytes(token.encode()), "fingerprint": fingerprint})
        return {"archive_id": archive_id, "token": token, "changes": changes,
                "managed_paths": catalog["managed_paths"], "notice": BACKUP_NOTICE,
                "warning": "请暂停其他智能体和编辑器写入。旧存档中没有、但之后显式纳管的文件会被删除；恢复前会保存当前完整纳管范围。"}

    def _apply_entry(self, name, entry, blobs):
        path = self._project_path(name)
        if entry is None:
            if path.exists():
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._project_path(name)
        temporary = None
        try:
            # 保留原实现而非并入 kernel 原子写：需要在 replace 前 fchmod 还原文件模式。
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".companion-restore-", delete=False) as out:
                temporary = Path(out.name)
                out.write(blobs[entry["sha256"]])
                out.flush()
                os.fsync(out.fileno())
                os.fchmod(out.fileno(), entry["mode"])
            self._project_path(name)
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def restore(self, archive_id, token):
        self._assert_writable()
        preview_path = self._internal_path(self.root / "restore-preview.json")
        if not preview_path.exists():
            raise ArchiveError("请先预览恢复范围，再使用该次预览 token。")
        preview = self._read_json(preview_path)
        if (not isinstance(preview, dict) or preview.get("archive_id") != archive_id or not isinstance(token, str)
                or not secrets.compare_digest(str(preview.get("token_sha256", "")), sha256_bytes(token.encode()))):
            raise ArchiveError("恢复 token 无效或与目标不匹配，请重新预览。")
        catalog, current, desired, blobs, fingerprint = self._restore_context(archive_id)
        if preview.get("fingerprint") != fingerprint:
            raise ArchiveError("预览后文件、范围或目标发生变化，必须重新预览。")
        safety, _, safety_entries = self._save_snapshot(
            catalog, sorted(desired), "恢复 " + archive_id + " 之前的自动保护", "safety")
        safety_id = safety["archive_id"]
        pending = {"target_archive_id": archive_id, "safety_archive_id": safety_id,
                   "managed_paths": sorted(desired), "status": "applying", "applied_paths": []}
        pending_path = self.root / "restore-pending.json"
        try:
            # The safety snapshot itself may take time. Recheck before any overwrite.
            now, _ = self._capture(sorted(desired))
            if safety_entries != current or now != current:
                raise ArchiveError("生成保护存档期间文件发生变化，请重新预览。", safety_id)
            self._atomic_json(pending_path, pending)
            preview_path.unlink()  # Each confirmed preview is single-use.
            for name, entry in desired.items():
                actual, _ = self._capture([name])
                if actual[name] != current[name]:
                    raise ArchiveError("恢复时文件被其他程序修改：" + name, safety_id)
                if current[name] != entry:
                    self._apply_entry(name, entry, blobs)
                    pending["applied_paths"].append(name)
                    self._atomic_json(pending_path, pending)
            actual, _ = self._capture(sorted(desired))
            if actual != desired:
                raise ArchiveError("恢复后校验不一致。", safety_id)
            pending_path.unlink()
        except Exception as exc:
            if pending_path.exists():
                pending.update(status="failed", error=str(exc))
                try:
                    self._atomic_json(pending_path, pending)
                except Exception:
                    pass  # Keep the original journal if even metadata writes fail.
            raise ArchiveError("恢复未完成：" + str(exc) + "。保护存档：" + safety_id, safety_id) from exc
        return {"archive_id": archive_id, "safety_archive_id": safety_id, "status": "restored",
                "changed_paths": pending["applied_paths"], "notice": BACKUP_NOTICE}


def export_compat(project_dir, out_dir):
    """把已导入 SQLite 事实库的项目导出为旧版兼容 JSON 草稿（P2-04，CLI 不可见纯函数）。

    只读旁路：委托 agents_kernel.storage.migration.fallback_export，导出目录由调用方
    指定，绝不覆盖项目 .dev-companion 下的原 state/journey/release.json；与存档/恢复
    逻辑（ArchiveStore）完全无关，不读取也不写入 archives/。
    """
    from agents_kernel.storage import migration
    return migration.fallback_export(project_dir, out_dir)

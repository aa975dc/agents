"""SQLite 事实库：建库、schema 版本化迁移、单协调写者（stdlib only, Python 3.9+）。

- 库文件属于数据目录而非源码树；路径由调用方传入（P2-04 接入时由上层定位）。
- WAL 模式 + synchronous=NORMAL：进程崩溃/强杀安全（未提交事务整体回滚、已提交
  事务完整）。耐久性边界：NORMAL 下断电可能丢最近若干已提交事务（不会损坏库）；
  需要更强断电保证时把 synchronous 提到 FULL，代价是每次提交 fsync。
- schema_version 表驱动版本化迁移：每个版本单事务应用，失败整体回滚，绝不半升级。
- 单协调写者（ST04）：进程间互斥用 <db>.writer 锁文件，O_CREAT|O_EXCL 原子创建
  （与 kernel.atomicio 同思路：显式创建、崩溃安全清理；陈旧锁按 pid 存活检测接管，
  pid 复用属已知边界，最终由 epoch 兜底）。epoch 持久化在 store_meta，且每次写事务
  内重新校验——断电/强杀后旧写者的任何写入一律被拒，不能覆盖新写者。
- 读入口 query_all/query_one 只执行 SELECT，无副作用；写入口必须携带 WriterSession
  颁发的 epoch，worker 拿不到 epoch 即无法改台账。
"""
import contextlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from agents_kernel.validation import CompanionError

SCHEMA_VERSION = 1
WRITER_EPOCH_KEY = "writer_epoch"
HEAD_SEQ_KEY = "view_head_seq"

_SCHEMA_V1 = (
    """CREATE TABLE IF NOT EXISTS store_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload TEXT NOT NULL,
        idempotency_key TEXT UNIQUE,
        writer_epoch INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS events_by_entity ON events (entity_type, entity_id, seq)",
    """CREATE TABLE IF NOT EXISTS feature_view (
        feature_id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'unknown',
        scope_version INTEGER NOT NULL DEFAULT 0,
        scope_json TEXT NOT NULL DEFAULT '[]',
        updated_seq INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS task_view (
        task_id TEXT PRIMARY KEY,
        feature_id TEXT NOT NULL,
        status TEXT NOT NULL,
        updated_seq INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS task_view_by_feature ON task_view (feature_id, task_id)",
    """CREATE TABLE IF NOT EXISTS evidence_view (
        evidence_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        result TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        updated_seq INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS evidence_by_subject ON evidence_view (kind, subject_id)",
    """CREATE TABLE IF NOT EXISTS release_view (
        release_id TEXT PRIMARY KEY,
        feature_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        updated_seq INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS release_view_by_feature ON release_view (feature_id, release_id)",
)

_MIGRATIONS = {1: _SCHEMA_V1}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None else default


def set_meta(conn, key, value):
    conn.execute(
        """INSERT INTO store_meta (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, str(value)))


def require_epoch(conn, epoch):
    """写事务内的 epoch 门：与 store_meta 中当前 epoch 不符即拒（旧写者保护）。"""
    if not isinstance(epoch, int) or isinstance(epoch, bool):
        raise CompanionError("写入必须携带协调者颁发的写者 epoch")
    current = int(get_meta(conn, WRITER_EPOCH_KEY, "0"))
    if epoch != current:
        raise CompanionError("旧写者写入被拒：epoch=%d，当前 epoch=%d" % (epoch, current))
    return current


def _pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # 无权探测（他人进程）等情形保守视为存活
    return True


def _read_lock(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # 空文件/损坏内容按陈旧锁处理


class WriterSession:
    """一次协调写者会话：持有锁文件与 epoch；close 幂等。"""

    def __init__(self, store, lock_path, epoch):
        self._store = store
        self._lock_path = lock_path
        self.epoch = epoch
        self._closed = False

    def close(self):
        if self._closed:
            return
        self._closed = True
        info = _read_lock(self._lock_path)
        if info is None or info.get("pid") == os.getpid():
            try:
                self._lock_path.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


def acquire_writer(store, *, stale_retries=3):
    """获取单协调写者会话：文件锁互斥 + epoch 单调递增（持久化在 store_meta）。

    锁已被活进程持有时拒绝；锁文件存在但记录的 pid 已死（崩溃遗留）时接管。
    """
    if store._conn is None:
        raise CompanionError("存储未打开，无法获取写者锁")
    lock_path = Path(str(store.path) + ".writer")
    created = False
    for _ in range(stale_retries + 1):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            info = _read_lock(lock_path)
            if info is not None and _pid_alive(info.get("pid")):
                raise CompanionError(
                    "已有活跃协调写者（pid=%s）持有 %s" % (info.get("pid"), lock_path))
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            continue
        created = True
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "acquired_at": utcnow()}, handle)
        except OSError:
            try:
                lock_path.unlink()
            except OSError:
                pass
            raise
        break
    if not created:
        raise CompanionError("获取写者锁失败（重试 %d 次）：%s" % (stale_retries, lock_path))
    try:
        with store.transaction() as conn:
            epoch = int(get_meta(conn, WRITER_EPOCH_KEY, "0")) + 1
            set_meta(conn, WRITER_EPOCH_KEY, epoch)
    except BaseException:
        try:
            lock_path.unlink()
        except OSError:
            pass
        raise
    return WriterSession(store, lock_path, epoch)


def _migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )""")
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row is not None and row[0] is not None else 0
    for version in sorted(_MIGRATIONS):
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _MIGRATIONS[version]:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                         (version, utcnow()))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


class Store:
    """事实库连接面：打开/迁移/读查询/写事务边界。"""

    def __init__(self, path):
        self.path = Path(path)
        self._conn = None
        self._in_transaction = False
        self._readonly = False

    def open(self):
        if self._conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._conn = conn
            _migrate(conn)
        except BaseException:
            self._conn = None
            conn.close()
            raise

    def open_readonly(self):
        """只读打开既有库（FIX-01，SR-02 剩余加固）：mode=ro URI，SQLite 层面禁止写入。

        不建目录、不建库、不迁移 schema、不产生/改写 journal/WAL；调用方须先做
        普通文件类型检查（本方法对非普通文件再次拒绝，绝不 open）。-wal 不存在时
        主文件必为完整事实（SQLite 原子性：未合并的提交只可能在 -wal 里），用
        immutable=1 避免只读连接尝试创建 -shm 失败；此时若恰有并发写者开始首个
        提交，读到的可能是上一份快照（status 轻微滞后，无写入副作用）。
        """
        if self._conn is not None:
            return
        if not self.path.is_file():
            raise CompanionError("事实库不是普通文件或不存在：%s" % self.path)
        immutable = "" if Path(str(self.path) + "-wal").exists() else "&immutable=1"
        conn = sqlite3.connect("file:%s?mode=ro%s" % (quote(str(self.path)), immutable),
                               uri=True, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        self._readonly = True

    def close(self):
        if self._conn is None:
            return
        self._conn.close()
        self._conn = None
        self._in_transaction = False

    def _ensure_open(self):
        if self._conn is None:
            raise CompanionError("存储未打开：%s" % self.path)

    def query_all(self, sql, params=()):
        self._ensure_open()
        return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        self._ensure_open()
        return self._conn.execute(sql, params).fetchone()

    def checkpoint(self):
        self._ensure_open()
        if self._readonly:
            raise CompanionError("只读连接不可执行 checkpoint：%s" % self.path)
        return self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

    @contextlib.contextmanager
    def transaction(self):
        """写事务边界：BEGIN IMMEDIATE → 提交；任一异常整体回滚。不可嵌套。"""
        self._ensure_open()
        if self._readonly:
            raise CompanionError("只读连接不可写：%s" % self.path)
        if self._in_transaction:
            raise CompanionError("存储写事务不可嵌套")
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield conn
            self._commit(conn)
        except BaseException:
            self._in_transaction = False
            conn.rollback()
            raise
        self._in_transaction = False

    def _commit(self, conn):
        """提交注入点：测试用它模拟"CAS 落盘后、提交前"崩溃（tests/test_storage.py）。"""
        conn.commit()

# -*- coding: utf-8 -*-
"""FIX-01（SR-02 剩余加固）：team-status 只读入口与文件类型边界。

复核报告 §5：SR-02 的 symlink 逃逸已被 36621cc 堵住，但打开路径仍会初始化/迁移
schema（sqlite 连接即建表），且未覆盖非普通文件类型与损坏库。本文件全部经真实
subprocess 调 companion.py CLI，逐项锁定：

- team.db 为指向外部 sentinel 库的 symlink → 拒绝，外库哈希/表结构不变（负向断言，
  对照复核探针 team_status_follows_external_db_symlink）；
- team.db 位置放着"只有 sentinel 表的普通 SQLite 库"（SR-02 残余场景：旧实现
  sqlite 连接即建 schema_version/events 等表）→ 结构化报错且不建任何表、字节不变；
- team.db 为目录 / fifo → 打开前 stat 检查明确拒绝（fifo 不 open，不阻塞）；
- 库不存在 → team-status 报"未初始化"且不产生任何文件；
- 损坏文件（随机字节）→ 结构化错误且字节不变（不写 journal/WAL、不迁移）；
- schema 版本高于工具支持 → 结构化报错且字节不变；
- 正常项目读写不回归，且 team-status 前后目录清单与库哈希不变（只读铁证）。
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "dev-companion" / "scripts"
COMPANION = SCRIPTS / "companion.py"


def cli(project, *argv, timeout=120):
    return subprocess.run([sys.executable, str(COMPANION), "--project", str(project), *argv],
                          capture_output=True, text=True, timeout=timeout)


def cli_json(project, *argv, **kwargs):
    result = cli(project, *argv, **kwargs)
    assert result.returncode == 0, "CLI 失败(%d)：%s%s" % (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def table_names(path):
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        return sorted(row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"))
    finally:
        conn.close()


class Fix01ReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # 与 core/legacy 相同的 realpath 口径（macOS /var → /private/var）
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "proj"
        self.project.mkdir(parents=True)
        self.data = self.project / ".dev-companion"
        self.db = self.data / "team.db"

    def seed_store(self):
        """正常初始化一个 team 库并写入一条任务事实（写入口既有语义）。"""
        cli_json(self.project, "team-init", "--feature", "login", "登录功能")
        cli_json(self.project, "team-task", "--set", "t1",
                 "--feature", "login", "--status", "running")

    def test_symlink_to_external_sentinel_db_rejected_external_untouched(self):
        """(a) 复核探针场景：team.db → 外部 sentinel 库；status/task/init 全拒绝，
        外库哈希与表结构不变（负向断言：sentinel 之外一个表都不许多）。"""
        victim = self.base / "victim"
        victim.mkdir()
        ext_db = victim / "external.db"
        conn = sqlite3.connect(str(ext_db))
        conn.execute("CREATE TABLE sentinel (x TEXT)")
        conn.execute("INSERT INTO sentinel VALUES ('keep')")
        conn.commit()
        conn.close()
        tables_before = table_names(ext_db)
        hash_before = sha256(ext_db)
        self.seed_store()
        self.db.unlink()
        self.db.symlink_to(ext_db)
        for argv in (("team-status",),
                     ("team-task", "--set", "x1", "--feature", "login", "--status", "running"),
                     ("team-init", "--feature", "spy", "窥探功能")):
            failed = cli(self.project, *argv)
            self.assertEqual(failed.returncode, 2, argv)
            self.assertIn("不能是文件链接", failed.stderr)
        self.assertEqual(sha256(ext_db), hash_before, "外部库被越界改写")
        self.assertEqual(table_names(ext_db), tables_before, "外部库表结构被改写")
        self.assertEqual([r[0] for r in sqlite3.connect(str(ext_db)).execute(
            "SELECT x FROM sentinel")], ["keep"], "sentinel 数据丢失")

    def test_non_team_sqlite_file_rejected_without_creating_schema(self):
        """(a-残余) team.db 位置是"只有 sentinel 表的有效 SQLite 库"：旧实现经
        sqlite 连接即建 schema_version/events 等表（SR-02 复现内核）；现在结构化
        报错且不建表、不写字节、不留 -wal/-shm。"""
        self.data.mkdir()
        conn = sqlite3.connect(str(self.db))
        conn.execute("CREATE TABLE sentinel (x TEXT)")
        conn.commit()
        conn.close()
        tables_before = table_names(self.db)
        hash_before = sha256(self.db)
        failed = cli(self.project, "team-status")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("损坏", failed.stderr)
        self.assertEqual(table_names(self.db), tables_before, "只读查询不得建表")
        self.assertEqual(sha256(self.db), hash_before)
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ["team.db"],
                         "查询不得留下 journal/WAL/锁文件")

    def test_directory_as_team_db_rejected(self):
        """(b) team.db 是目录 → 打开前类型检查明确拒绝（读/写/初始化入口同拒）。"""
        self.data.mkdir()
        self.db.mkdir()
        for argv in (("team-status",),
                     ("team-init", "--feature", "login", "登录功能"),
                     ("team-task", "--set", "t1", "--feature", "login", "--status", "running")):
            failed = cli(self.project, *argv)
            self.assertEqual(failed.returncode, 2, argv)
            self.assertIn("不是普通文件", failed.stderr)
        self.assertTrue(self.db.is_dir(), "目录不得被改写成库文件")

    def test_fifo_as_team_db_rejected_without_opening(self):
        """(c) team.db 是 fifo → stat 检查在 open 之前拒绝，进程不阻塞。"""
        self.data.mkdir()
        os.mkfifo(str(self.db))
        # 若实现误 open fifo，这里会永久阻塞；timeout 使测试失败而非挂死
        failed = cli(self.project, "team-status", timeout=60)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("不是普通文件", failed.stderr)
        import stat as stat_module
        self.assertTrue(stat_module.S_ISFIFO(os.stat(str(self.db)).st_mode), "fifo 不得被改动")

    def test_missing_db_reports_uninitialized_and_creates_nothing(self):
        """(d) 库不存在 → team-status 报"未初始化"（引导 team-init），不产生任何文件。"""
        failed = cli(self.project, "team-status")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("team-init", failed.stderr)
        self.assertIn("不存在", failed.stderr)
        self.assertEqual(sorted(p.name for p in self.project.iterdir()), [],
                         "状态查询不得创建记录目录或库文件")

    def test_corrupt_file_rejected_without_writing(self):
        """(e) team.db 是随机字节 → 结构化错误；不写 journal/WAL、不做迁移、字节不变。"""
        self.data.mkdir()
        payload = os.urandom(512)
        self.db.write_bytes(payload)
        failed = cli(self.project, "team-status")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("损坏", failed.stderr)
        self.assertEqual(self.db.read_bytes(), payload, "损坏文件被改写")
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ["team.db"])

    def test_unknown_schema_version_rejected_without_migrating(self):
        """(e-变体) schema 版本高于工具支持 → 结构化报错，不降级迁移、字节不变。"""
        self.seed_store()
        conn = sqlite3.connect(str(self.db))
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (99, 'test')")
        conn.commit()
        conn.close()
        hash_before = sha256(self.db)
        failed = cli(self.project, "team-status")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("schema 版本", failed.stderr)
        self.assertEqual(sha256(self.db), hash_before, "未知版本库被改写")

    def test_normal_read_write_unaffected_and_status_leaves_no_trace(self):
        """(f) 正常项目读写不回归；team-status 前后目录清单与库哈希不变（只读铁证）。"""
        self.seed_store()
        view_before = cli_json(self.project, "team-status")
        self.assertEqual([(t["task_id"], t["status"]) for t in view_before["tasks"]["items"]],
                         [("t1", "running")])
        inventory = sorted(p.name for p in self.data.iterdir())
        hash_before = sha256(self.db)
        # 读两次（覆盖 -wal/-shm 在场与 immutable 两条只读路径）
        cli_json(self.project, "team-status")
        view_after = cli_json(self.project, "team-status")
        self.assertEqual(view_after, view_before)
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), inventory,
                         "team-status 改动了记录目录（journal/WAL/锁残留）")
        self.assertEqual(sha256(self.db), hash_before, "team-status 改写了库文件")
        # 写入口既有语义保持：新任务照常落库并被只读 status 读到
        cli_json(self.project, "team-task", "--set", "t2",
                 "--feature", "login", "--status", "done")
        view = cli_json(self.project, "team-status")
        self.assertEqual([(t["task_id"], t["status"]) for t in view["tasks"]["items"]],
                         [("t1", "running"), ("t2", "done")])
        self.assertEqual(view["generation"], view_before["generation"] + 1)


if __name__ == "__main__":
    unittest.main()

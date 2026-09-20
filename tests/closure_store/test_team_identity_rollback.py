# -*- coding: utf-8 -*-
"""候选交付定点核对（核对项 2+3，team 域）：别名身份一致性 / symlink 越界拒绝 / 回退分级。

全部经真实 subprocess 调 companion.py CLI（每步独立进程）+ 真实文件系统符号链接，
先以探针确证当前行为再定性（不以"未 realpath"直接预设结论）：

核对项 2 探针结论（2026-09-21，修复前实测）：
- 绝对/相对(不同 cwd)/末尾斜杠/经 symlink 四种别名：OS 逐段解析后都落到同一个
  team.db（同 inode），事实源未分裂；但报告的 store 串随命令行原样路径变化，
  与 core.Project 的 realpath 口径不一致；
- team.db 为指向"已存在外部有效库"的 symlink：team-status 读到项目外事实、
  team-task 越界改写外部文件（sha256 变化）——真实逃逸，已修复为拒绝；
- team.db 为悬空 symlink：误报"不存在"（碰巧未逃逸），修复后明确拒绝；
- .dev-companion 为外链 symlink：core.Project 构造即拒（core.py:61），本就安全；
- 项目根本身是 symlink：合法别名，库落到真实目录；修复后 store 串归一到真实目录。

核对项 3（回退分级）：撤销空初始化/迁移后无新写入直接回退；有任何会丢失的活动
事实时拒绝或强制 --export-first 先导出（草稿 + 全量事件日志）再回退。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "dev-companion" / "scripts"
COMPANION = SCRIPTS / "companion.py"

LEGACY_JSON = ("state.json", "journey.json", "release.json")
SCOPE = {"title": "记账工具", "goal": "算出总金额", "audience": "自己", "scenario": "记账",
         "out_of_scope": ["云同步"], "assumptions": [],
         "features": [
             {"id": "sum", "title": "计算合计", "acceptance_criteria": ["20+30=50"],
              "allowed_paths": ["ledger.py"], "check_commands": [["python3", "-c", "pass"]]},
             {"id": "export", "title": "导出账单", "acceptance_criteria": ["能导出"],
              "allowed_paths": ["export.py"], "check_commands": [["python3", "-c", "pass"]]}]}


def cli(project, *argv, cwd=None):
    """跑一次真实 companion.py 进程（可指定 cwd 以测相对路径别名），不抛。"""
    return subprocess.run([sys.executable, str(COMPANION), "--project", str(project), *argv],
                          capture_output=True, text=True, cwd=None if cwd is None else str(cwd))


def cli_json(project, *argv, **kwargs):
    result = cli(project, *argv, **kwargs)
    assert result.returncode == 0, "CLI 失败(%d)：%s%s" % (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def db_inode(path):
    return os.stat(path).st_ino


class TeamIdentityTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # 与 core/legacy 相同的 realpath 口径（macOS /var → /private/var）
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "proj"
        self.project.mkdir(parents=True)
        self.data = self.project / ".dev-companion"


class AliasIdentityTests(TeamIdentityTestBase):
    def test_four_aliases_land_on_single_fact_source(self):
        """绝对/相对(不同 cwd)/末尾斜杠/经 symlink 四种别名 → 同一 team.db、同一事实。"""
        init = cli_json(self.project, "team-init", "--feature", "login", "登录功能")
        self.assertTrue(init["applied"])
        real_db = self.data / "team.db"
        inode_before = db_inode(real_db)
        link = self.base / "link-to-proj"
        link.symlink_to(self.project)
        (self.base / "sub").mkdir()
        aliases = [
            ("绝对路径", self.project, None),
            ("相对路径", "proj", self.base),
            ("相对上跳", Path("..") / "proj", self.base / "sub"),
            ("末尾斜杠", str(self.project) + "/", None),
            ("symlink别名", link, None),
        ]
        expected_store = str(real_db)
        for label, target, cwd in aliases:
            view = cli_json(target, "team-status", cwd=cwd)
            self.assertEqual(view["store"], expected_store, "%s：store 应归一到真实路径" % label)
            self.assertEqual([f["feature_id"] for f in view["features"]["items"]], ["login"], label)
            written = cli_json(target, "team-task", "--set", "t1", "--feature", "login",
                               "--status", "running", cwd=cwd)
            self.assertTrue(written["applied"], label)
            self.assertEqual(written["generation"], view["generation"] + 1,
                             "%s：写入应落在同一事实源（generation 单调）" % label)
        # 五次写入都落在同一个库：generation 单调且最终=6，事实源唯一
        final = cli_json(self.project, "team-status")
        self.assertEqual(final["generation"], 6)
        self.assertEqual([(t["task_id"], t["status"]) for t in final["tasks"]["items"]],
                         [("t1", "running")])
        # 全程库文件同一个 inode（任何别名都未造出第二个库）
        self.assertEqual(db_inode(real_db), inode_before)
        inventories = sorted(p.name for p in self.data.iterdir())
        self.assertTrue(all(name.startswith("team.db") for name in inventories),
                        "出现 team.db 之外的文件：%s" % inventories)

    def test_symlinked_team_db_rejected_and_external_file_untouched(self):
        """team.db 是指向外部有效库的 symlink → 读写都明确拒绝，外部文件字节不变。"""
        victim = self.base / "victim"
        victim.mkdir()
        cli_json(victim, "team-init", "--feature", "secret", "机密功能")
        ext_db = victim / ".dev-companion" / "team.db"
        before = sha256(ext_db)
        cli_json(self.project, "team-init", "--feature", "login", "登录功能")
        (self.data / "team.db").unlink()
        (self.data / "team.db").symlink_to(ext_db)
        for argv in (("team-status",), ("team-task", "--set", "x1", "--feature", "login",
                                       "--status", "running")):
            failed = cli(self.project, *argv)
            self.assertEqual(failed.returncode, 2)
            self.assertIn("不能是文件链接", failed.stderr)
            self.assertIn(str(ext_db), failed.stderr, "错误应指明链接目标便于排查")
        self.assertEqual(sha256(ext_db), before, "外部库被越界改写")
        # 悬空 symlink 同样拒绝（而非误报"不存在"或静默创建目标文件）
        (self.data / "team.db").unlink()
        (self.data / "team.db").symlink_to(self.base / "nowhere" / "ext.db")
        failed = cli(self.project, "team-status")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("不能是文件链接", failed.stderr)
        self.assertFalse((self.base / "nowhere").exists(), "悬空目标不得被创建")

    def test_symlinked_dev_companion_rejected(self):
        """.dev-companion 是外链 symlink → 拒绝，外部目录不被写入（core 即拒，锁定行为）。"""
        outside = self.base / "outside-dir"
        outside.mkdir()
        (self.project / ".dev-companion").symlink_to(outside)
        for argv in (("team-init", "--feature", "login", "登录功能"), ("team-status",)):
            failed = cli(self.project, *argv)
            self.assertEqual(failed.returncode, 2)
            self.assertIn("不能是文件链接", failed.stderr)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), [], "外部目录被写入")

    def test_project_root_symlink_is_legal_alias_unified_to_real_dir(self):
        """项目根本身是 symlink → 合法别名：realpath 归一到真实目录的同一个库。"""
        real_root = self.base / "real-root"
        real_root.mkdir()
        root_link = self.base / "proj-link"
        root_link.symlink_to(real_root)
        cli_json(root_link, "team-init", "--feature", "login", "登录功能")
        view = cli_json(real_root, "team-status")
        self.assertEqual(view["store"], str(real_root / ".dev-companion" / "team.db"),
                         "store 应归一到真实目录，不出现 symlink 路径")
        self.assertEqual(view["generation"], 1)
        self.assertTrue((real_root / ".dev-companion" / "team.db").is_file())
        written = cli_json(root_link, "team-task", "--set", "t1",
                           "--feature", "login", "--status", "running")
        self.assertTrue(written["applied"])
        self.assertEqual(cli_json(real_root, "team-status")["tasks"]["items"][0]["status"],
                         "running")


class RollbackTestBase(TeamIdentityTestBase):
    def build_legacy(self):
        scope_path = self.project / "scope.json"
        scope_path.write_text(json.dumps(SCOPE, ensure_ascii=False), encoding="utf-8")
        cli_json(self.project, "init", "--input", str(scope_path))
        cli_json(self.project, "confirm", "--revision", "1")

    def legacy_hashes(self):
        return {name: sha256(self.data / name)
                for name in LEGACY_JSON if (self.data / name).exists()}

    def team_files(self):
        return sorted(p.name for p in self.data.iterdir() if p.name.startswith("team.db"))

    def migrate(self):
        report = cli_json(self.project, "team-migrate", "--from-json")
        self.assertEqual(report["status"], "imported")
        return report


class EmptyInitRollbackTests(RollbackTestBase):
    def test_empty_init_rollback_leaves_clean_directory(self):
        cli_json(self.project, "team-init", "--feature", "login", "登录功能")
        report = cli_json(self.project, "team-rollback")
        self.assertEqual(report["status"], "rolled_back")
        self.assertEqual(report["mode"], "empty_init")
        self.assertEqual(self.team_files(), [], "team.db 及伴随文件应全部删除")
        failed = cli(self.project, "team-status")
        self.assertEqual(failed.returncode, 2, "回退后 team 命令明确报错，不静默重建")
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), [], "目录应干净")

    def test_rollback_refused_while_writer_lock_exists(self):
        cli_json(self.project, "team-init", "--feature", "login", "登录功能")
        (self.data / "team.db.writer").write_text("{}", encoding="utf-8")
        failed = cli(self.project, "team-rollback")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("写者锁", failed.stderr)
        self.assertTrue((self.data / "team.db").exists(), "拒绝时库必须原样保留")


class MigratedRollbackTests(RollbackTestBase):
    def test_rollback_after_migration_without_new_writes_keeps_legacy_intact(self):
        self.build_legacy()
        before = self.legacy_hashes()
        self.migrate()
        report = cli_json(self.project, "team-rollback")
        self.assertEqual(report["status"], "rolled_back")
        self.assertEqual(report["mode"], "migrated")
        # 核对信息：manifest 导入计数与库内实数一致，新写入为 0
        self.assertEqual(sum(report["manifest_counts"].values()), report["imported_events"])
        self.assertEqual(report["at_risk_events"], 0)
        self.assertEqual(self.team_files(), [])
        self.assertEqual(self.legacy_hashes(), before, "legacy JSON 哈希必须不变")
        status = cli_json(self.project, "status", "--format", "json")
        self.assertEqual(status["revision"], 2, "回退后 legacy 照常")
        failed = cli(self.project, "team-status")
        self.assertEqual(failed.returncode, 2)

    def test_legacy_lifecycle_still_works_after_rollback(self):
        self.build_legacy()
        self.migrate()
        cli_json(self.project, "team-rollback")
        # 回退后 legacy 全生命周期照常：修订 → 确认 → 制作回报 → 检查 → 验收，全程真实 CLI
        scope_path = self.project / "scope2.json"
        scope_path.write_text(json.dumps(SCOPE, ensure_ascii=False), encoding="utf-8")
        revise = cli_json(self.project, "scope", "--input", str(scope_path), "--revision", "2")
        self.assertFalse(revise["confirmed"])
        cli_json(self.project, "confirm", "--revision", str(revise["revision"]))
        packet = cli_json(self.project, "packet", "--feature", "sum")
        (self.project / "ledger.py").write_text("def total(values): return sum(values)\n",
                                                encoding="utf-8")
        receipt = {"feature_id": "sum", "scope_version": packet["scope_version"],
                   "run_id": packet["run_id"], "status": "implemented", "summary": "完成合计",
                   "changed_files": ["ledger.py"], "evidence_files": []}
        receipt_path = self.base / "receipt.json"
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        cli_json(self.project, "receipt", "--input", str(receipt_path))
        check = cli_json(self.project, "check", "--feature", "sum")
        self.assertTrue(check["passed"])
        accept = cli_json(self.project, "accept", "--feature", "sum", "--note", "核对通过",
                          "--user-confirmed")
        self.assertEqual(accept["status"], "accepted")
        view = cli_json(self.project, "status", "--format", "json")
        self.assertEqual(view["revision"], accept["revision"])

    def test_rollback_with_new_writes_requires_export_then_round_trips(self):
        self.build_legacy()
        before = self.legacy_hashes()
        self.migrate()
        cli_json(self.project, "team-init", "--feature", "extra", "迁移后新增功能")
        cli_json(self.project, "team-task", "--set", "x1",
                 "--feature", "extra", "--status", "running")
        # 无导出 → 拒绝并报出新事实条数（2 条新事件），库原样保留
        failed = cli(self.project, "team-rollback")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("2 条", failed.stderr)
        self.assertIn("--export-first", failed.stderr)
        self.assertTrue((self.data / "team.db").exists())
        # --export-first → 先导出全量事实再回退
        out_dir = self.base / "facts-export"
        report = cli_json(self.project, "team-rollback", "--export-first", str(out_dir))
        self.assertEqual(report["status"], "rolled_back")
        self.assertEqual(report["at_risk_events"], 2)
        self.assertEqual(self.legacy_hashes(), before, "回退不得改动 legacy JSON")
        self.assertEqual(self.team_files(), [])
        # 导出含新增任务：全量事件日志里有 extra 的 feature_status/task_status 事件
        log = json.loads((out_dir / "team_events.json").read_text(encoding="utf-8"))
        entities = [(e["event_type"], e["entity_id"]) for e in log["events"]]
        self.assertEqual(len(log["events"]), report["events_total"])
        self.assertIn(("feature_status", "extra"), entities)
        self.assertIn(("task_status", "x1"), entities)
        # round-trip：导出的旧 schema 草稿可被 team-migrate 再次读入
        # （草稿恢复迁移时点事实；新增任务以事件日志保真，见上方 extra 断言）
        present = sorted(n for n in LEGACY_JSON if (self.data / n).exists())
        self.assertEqual(sorted(report["export"]["drafts"]), present)
        backup = self.base / "legacy-backup"
        backup.mkdir()
        for name in present:
            (backup / name).write_bytes((self.data / name).read_bytes())
            (self.data / name).unlink()
            (self.data / name).write_bytes((out_dir / name).read_bytes())
        remigrate = cli_json(self.project, "team-migrate", "--from-json")
        self.assertEqual(remigrate["status"], "imported")
        view = cli_json(self.project, "team-status")
        self.assertEqual({f["feature_id"] for f in view["features"]["items"]},
                         {"sum", "export"}, "草稿恢复迁移时点事实")
        # 还原 legacy 原件并复核哈希（草稿 round-trip 只在副本上验证）
        for name in present:
            (self.data / name).unlink()
            (self.data / name).write_bytes((backup / name).read_bytes())
        self.assertEqual(self.legacy_hashes(), before)


if __name__ == "__main__":
    unittest.main()

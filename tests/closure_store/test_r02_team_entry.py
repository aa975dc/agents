# -*- coding: utf-8 -*-
"""R02：team 事实库的真实用户入口（tests/closure_store）。

全部经真实 subprocess 调 companion.py CLI（每一步都是独立进程），锁定：
- scratch 新 team 项目 team-init → team-task → 新进程 team-status 读到同一事实，
  事实源唯一（只有 team.db 变化，无任何 legacy JSON 状态文件产生）；
- legacy 项目 team-migrate：dry-run 零写入 → 导入 → team-status 可见 → 重复导入
  去重 → 删 team.db 回退 → legacy 命令照常且三 JSON 字节未动；
- legacy 命令在有 team.db 的项目上输出与从前逐字节一致（无双主写入）；
- 旧 JSON 损坏时 team-migrate 明确报错且不建库不写任何数据。
"""
import json
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


def cli(*argv):
    """跑一次真实 companion.py 进程，返回 CompletedProcess（不抛）。"""
    return subprocess.run([sys.executable, str(COMPANION), *argv],
                          capture_output=True, text=True)


def cli_json(*argv):
    result = cli(*argv)
    assert result.returncode == 0, "CLI 失败(%d)：%s%s" % (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


def strip_volatile(value):
    """去掉读取时刻的时间戳（observed_at 每次读取都变），保留其余全部事实。"""
    if isinstance(value, dict):
        return {key: strip_volatile(item) for key, item in value.items()
                if key not in ("observed_at", "updated_at")}
    if isinstance(value, list):
        return [strip_volatile(item) for item in value]
    return value


class TeamEntryTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # 与 core/legacy 相同的 realpath 口径（macOS /var → /private/var）
        self.project = (Path(self.temp.name) / "proj").resolve()
        self.project.mkdir(parents=True)
        self.data = self.project / ".dev-companion"

    def build_legacy(self):
        """真实 CLI 建 legacy 项目：init + confirm（各一次独立进程）。"""
        scope_path = self.project / "scope.json"
        scope_path.write_text(json.dumps(SCOPE, ensure_ascii=False), encoding="utf-8")
        cli_json("--project", str(self.project), "init", "--input", str(scope_path))
        cli_json("--project", str(self.project), "confirm", "--revision", "1")

    def legacy_bytes(self):
        return {name: (self.data / name).read_bytes()
                for name in LEGACY_JSON if (self.data / name).exists()}

    def fact_files(self):
        return sorted(p.name for p in self.data.iterdir())

    def drop_team_db(self):
        for suffix in ("", "-wal", "-shm", ".writer"):
            path = Path(str(self.data / "team.db") + suffix)
            if path.exists():
                path.unlink()


class NewTeamProjectTests(TeamEntryTestBase):
    def test_facts_roundtrip_across_processes_and_single_source(self):
        """scratch 项目：init→add→task→新进程 status 读到同一事实；事实源唯一。

        FIX-04 更新说明：team-task 不再从任意状态凭空造任务（SR-01 门禁），
        先经 team-task-add 显式创建（pending），再按白名单 ready→running；
        generation 相应从 3 变 5。
        """
        report = cli_json("--project", str(self.project), "team-init", "--feature", "login", "登录功能")
        self.assertTrue(report["applied"])
        cli_json("--project", str(self.project), "team-init", "--feature", "report", "报表功能")
        cli_json("--project", str(self.project), "team-task-add", "--set", "t1",
                 "--feature", "login", "--kind", "impl")
        cli_json("--project", str(self.project), "team-task",
                 "--set", "t1", "--feature", "login", "--status", "ready")
        set_report = cli_json("--project", str(self.project), "team-task",
                              "--set", "t1", "--feature", "login", "--status", "running")
        self.assertTrue(set_report["applied"])
        # 全新的 Python 进程只从 team.db 读回事实
        view = cli_json("--project", str(self.project), "team-status")
        self.assertEqual(view["store"], str(self.data / "team.db"))
        self.assertEqual(view["generation"], 5)
        self.assertEqual([(f["feature_id"], f["status"]) for f in view["features"]["items"]],
                         [("login", "draft"), ("report", "draft")])
        self.assertEqual([(t["task_id"], t["status"]) for t in view["tasks"]["items"]],
                         [("t1", "running")])
        # 事实源唯一：项目记录目录只有 team.db（及 SQLite 伴随文件），无任何 JSON 状态文件
        names = self.fact_files()
        self.assertTrue(all(name.startswith("team.db") for name in names),
                        "出现 team.db 之外的事实文件：%s" % names)
        # 分页形状：limit 生效且带 has_more（LIMIT/OFFSET 语义）
        paged = cli_json("--project", str(self.project), "team-status", "--limit", "1")
        self.assertEqual([f["feature_id"] for f in paged["features"]["items"]], ["login"])
        self.assertTrue(paged["features"]["has_more"])
        self.assertEqual(cli_json("--project", str(self.project), "team-status")["features"]["has_more"],
                         False)

    def test_reinit_is_idempotent_and_status_without_init_errors(self):
        cli_json("--project", str(self.project), "team-init", "--feature", "login", "登录功能")
        again = cli_json("--project", str(self.project), "team-init", "--feature", "login", "登录功能")
        self.assertFalse(again["applied"], "重复 init 应幂等去重")
        view = cli_json("--project", str(self.project), "team-status")
        self.assertEqual(len(view["features"]["items"]), 1)
        # 未初始化的 team-task / team-status 明确报错（exit 2），不静默建空库
        scratch = self.project.parent / "scratch-empty"
        scratch.mkdir()
        failed = cli("--project", str(scratch), "team-status")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("team-init", failed.stderr)
        self.assertFalse((scratch / ".dev-companion" / "team.db").exists())

    def test_task_cas_conflict_rejected_and_unknown_feature_refused(self):
        """CAS 冲突与未登记功能拒绝（FIX-04 更新：done 现在走门禁，改用 ready 触发
        CAS——pending→ready 是合法转换，门禁放行后由 CAS 拒绝；done 的门禁拒绝
        另有专项断言，见 test_fix04_gates.py）。"""
        cli_json("--project", str(self.project), "team-init", "--feature", "login", "登录功能")
        view = cli_json("--project", str(self.project), "team-status")
        stale = view["generation"]
        cli_json("--project", str(self.project), "team-task-add", "--set", "t1",
                 "--feature", "login", "--kind", "impl")
        # 用 add 之前的旧 generation 写 ready → 门禁放行但 CAS 冲突，exit 2 且事实不变
        failed = cli("--project", str(self.project), "team-task", "--set", "t1",
                     "--feature", "login", "--status", "ready", "--expect-seq", str(stale))
        self.assertEqual(failed.returncode, 2)
        self.assertIn("CAS", failed.stderr)
        view = cli_json("--project", str(self.project), "team-status")
        self.assertEqual(view["tasks"]["items"][0]["status"], "pending")
        # 未登记的 feature 拒绝挂任务（创建入口校验功能存在）
        failed = cli("--project", str(self.project), "team-task-add", "--set", "t2",
                     "--feature", "nope", "--kind", "impl")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("team-init", failed.stderr)


class MigrationTests(TeamEntryTestBase):
    def test_migrate_dry_run_import_idempotent_and_rollback(self):
        self.build_legacy()
        before = self.legacy_bytes()
        # dry-run：计数正确、零写入（连 team.db 都不建），原 JSON 不动
        dry = cli_json("--project", str(self.project), "team-migrate", "--from-json", "--dry-run")
        self.assertEqual(dry["status"], "planned")
        self.assertGreater(dry["planned_total"], 0)
        self.assertNotIn("team.db", self.fact_files())
        self.assertEqual(self.legacy_bytes(), before)
        # 正式导入：计数一致，team-status 可见迁移事实
        report = cli_json("--project", str(self.project), "team-migrate", "--from-json")
        self.assertEqual(report["status"], "imported")
        self.assertEqual(report["applied"], dry["planned_total"])
        view = cli_json("--project", str(self.project), "team-status")
        imported = {f["feature_id"]: f["status"] for f in view["features"]["items"]}
        self.assertEqual(imported, {"sum": "pending", "export": "pending"})
        self.assertEqual(len(view["tasks"]["items"]), 2)
        # 重复导入：幂等去重，零新写入
        repeat = cli_json("--project", str(self.project), "team-migrate", "--from-json")
        self.assertEqual(repeat["status"], "deduped")
        self.assertEqual(repeat["applied"], 0)
        self.assertEqual(self.legacy_bytes(), before)
        # 回退：删 team.db（含伴随文件）→ legacy 命令照常，原 JSON 字节未动
        self.drop_team_db()
        status_after = cli_json("--project", str(self.project), "status", "--format", "json")
        self.assertEqual(status_after["revision"], 2)
        self.assertEqual(self.legacy_bytes(), before)
        failed = cli("--project", str(self.project), "team-status")
        self.assertEqual(failed.returncode, 2, "回退后 team 命令明确报错，不静默重建")

    def test_corrupt_legacy_json_rejected_without_writing(self):
        self.build_legacy()
        (self.data / "state.json").write_text("{不是 JSON", encoding="utf-8")
        failed = cli("--project", str(self.project), "team-migrate", "--from-json")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("JSON", failed.stderr)
        self.assertFalse((self.data / "team.db").exists(), "损坏来源不得建库写数据")

    def test_migrate_requires_from_json_flag(self):
        failed = cli("--project", str(self.project), "team-migrate")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("--from-json", failed.stderr)


class NoDualMasterTests(TeamEntryTestBase):
    def test_legacy_commands_unchanged_when_team_db_exists(self):
        """legacy 命令完全不感知 team.db：输出与无 team.db 时逐字节一致。"""
        self.build_legacy()
        status_before = cli("--project", str(self.project), "status", "--format", "json")
        doctor_before = cli("--project", str(self.project), "doctor")
        self.assertEqual(status_before.returncode, 0)
        cli_json("--project", str(self.project), "team-init", "--feature", "extra", "并行新功能")
        # FIX-04 更新说明：任务先经 team-task-add 显式创建，ready→running 才合法（SR-01 门禁）
        cli_json("--project", str(self.project), "team-task-add", "--set", "x1",
                 "--feature", "extra", "--kind", "impl")
        cli_json("--project", str(self.project), "team-task", "--set", "x1",
                 "--feature", "extra", "--status", "ready")
        cli_json("--project", str(self.project), "team-task", "--set", "x1",
                 "--feature", "extra", "--status", "running")
        status_after = cli("--project", str(self.project), "status", "--format", "json")
        doctor_after = cli("--project", str(self.project), "doctor")
        self.assertEqual(status_after.returncode, 0)
        # status 视图含读取时刻时间戳，去掉后逐字段比对（等价于逐字节一致的稳定部分）
        self.assertEqual(strip_volatile(json.loads(status_after.stdout)),
                         strip_volatile(json.loads(status_before.stdout)),
                         "legacy status 输出被 team.db 改变（双主写入）")
        self.assertEqual(doctor_after.stdout, doctor_before.stdout)
        # legacy 的 scope 修订也照常工作
        scope_path = self.project / "scope2.json"
        scope_path.write_text(json.dumps(SCOPE, ensure_ascii=False), encoding="utf-8")
        revise = cli_json("--project", str(self.project), "scope", "--input", str(scope_path),
                          "--revision", "2")
        self.assertFalse(revise["confirmed"])


if __name__ == "__main__":
    unittest.main()

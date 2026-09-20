# -*- coding: utf-8 -*-
"""FIX-03（SR-04）：迁移后回退不丢事实 + 旧格式真实可读。

复核报告 §7 场景完整重放（evidence migration_after_new_facts）：legacy 项目 →
team-migrate → 新增子任务 → 原任务 done → 回退导出。修复前：new_task_missing_in_export
=true、warnings 空、done 原样写入旧 JSON 致真实 Project.load 报"无法识别任务状态"。

修复后锁定：
- fallback_export 从事件日志全量折叠：新增任务进 sidecar（旧 schema 每功能仅一个
  同 id 任务，子任务无法进草稿——不静默丢弃），manifest 计数与库内事件一致；
- 不可映射事实（review 等异形证据、team-init 功能）逐条列入 warnings + sidecar；
- 状态映射显式：done→awaiting_review（不升级 accepted），ready/running/cancelled→
  pending，failed→blocked；
- 生成的旧格式草稿必须过真实 legacy core.Project.load（非 json.loads）；
- 回退失败不删活动库、不改原 JSON。

review 证据经 kernel events API 直接登记（CLI 到审查板的接线属 FIX-04/SR-05，
当前无公共入口可产生该事实类型；此处只测导出折叠，不模拟任何 E2E 完成）。
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
sys.path.insert(0, str(REPO_ROOT / "packages"))
sys.path.insert(0, str(SCRIPTS))

from core import Project  # 真实 legacy 读取器（复核报告要求：不只 json.loads）


def cli(project, *argv, timeout=120):
    return subprocess.run([sys.executable, str(COMPANION), "--project", str(project), *argv],
                          capture_output=True, text=True, timeout=timeout)


def cli_json(project, *argv, **kwargs):
    result = cli(project, *argv, **kwargs)
    assert result.returncode == 0, "CLI 失败(%d)：%s%s" % (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


def sha256(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


SCOPE = {"title": "记账工具", "goal": "算出总金额", "audience": "自己", "scenario": "记账",
         "out_of_scope": ["云同步"], "assumptions": [],
         "features": [
             {"id": "sum", "title": "计算合计", "acceptance_criteria": ["20+30=50"],
              "allowed_paths": ["ledger.py"], "check_commands": [["python3", "-c", "pass"]]},
             {"id": "export", "title": "导出账单", "acceptance_criteria": ["能导出"],
              "allowed_paths": ["export.py"], "check_commands": [["python3", "-c", "pass"]]}]}


class Fix03RollbackBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "proj"
        self.project.mkdir(parents=True)
        self.data = self.project / ".dev-companion"

    def build_legacy(self):
        scope_path = self.project / "scope.json"
        scope_path.write_text(json.dumps(SCOPE, ensure_ascii=False), encoding="utf-8")
        cli_json(self.project, "init", "--input", str(scope_path))
        cli_json(self.project, "confirm", "--revision", "1")

    def legacy_hashes(self):
        return {name: sha256(self.data / name)
                for name in ("state.json", "journey.json", "release.json")
                if (self.data / name).exists()}

    def register_review_evidence(self, task_id, evidence_id):
        """经 kernel events API 登记一条 review 证据（旧 schema 无此形状）。"""
        from agents_kernel.storage import db, events
        store = db.Store(self.data / "team.db")
        store.open()
        try:
            writer = db.acquire_writer(store)
            try:
                events.append_event(
                    store, writer.epoch, event_type="evidence_registered",
                    entity_id=evidence_id,
                    payload={"kind": "check", "role": "review", "subject_id": task_id,
                             "result": "passed", "detail": "独立审查通过",
                             "review": {"id": evidence_id, "passed": True}},
                    idempotency_key="test:" + evidence_id)
            finally:
                writer.close()
        finally:
            store.close()

    def load_draft_with_real_legacy(self, out_dir):
        """把草稿放入项目原位，用真实 core.Project.load 读取（读后还原原件）。"""
        original = self.legacy_hashes()
        moved = self.data / "state.json"
        backup = self.base / "state.json.bak"
        moved.rename(backup)
        try:
            (self.data / "state.json").write_bytes((Path(out_dir) / "state.json").read_bytes())
            state = Project(self.project).load()  # 旧格式不合法会在此抛 CompanionError
        finally:
            (self.data / "state.json").unlink()
            backup.rename(moved)
        self.assertEqual(self.legacy_hashes(), original, "草稿校验不得改动 legacy 原件")
        return state


class Sr04ReplayTests(Fix03RollbackBase):
    def test_rollback_after_new_facts_exports_everything_and_draft_loads(self):
        """SR-04 场景完整重放：legacy→migrate→新增子任务→原任务 done→rollback
        --export-first，逐项断言复核报告的四个缺陷全部闭合。

        FIX-04 更新说明：新增子任务经 team-task-add 创建（任意 upsert 已被 SR-01
        门禁拒绝）；原任务 sum（迁移导入为 pending）按真实门禁链推到 done——
        复核报告 §7 预言"修复 SR-01 后合法完成的任务仍会产生 done"，这里顺带
        验证该路径的导出映射。新增的团队事实事件数随之变化，回退 at-risk 计数
        不在断言内（由 test_rollback_with_new_writes 专项覆盖）。
        """
        self.build_legacy()
        before = self.legacy_hashes()
        cli_json(self.project, "team-migrate", "--from-json")
        # 迁移后新增子任务（挂在已有功能 sum 下）+ 原任务推进为 done + 一条 review 证据
        cli_json(self.project, "team-task-add", "--set", "new-after-migration",
                 "--feature", "sum", "--kind", "impl")
        cli_json(self.project, "team-task", "--set", "new-after-migration",
                 "--feature", "sum", "--status", "ready")
        cli_json(self.project, "team-task", "--set", "sum", "--feature", "sum", "--status", "ready")
        cli_json(self.project, "team-task", "--set", "sum", "--feature", "sum", "--status", "running")
        import hashlib
        sha = hashlib.sha256(b"sum-deliverable").hexdigest()
        cli_json(self.project, "team-report", "--task", "sum", "--outcome", "succeeded",
                 "--summary", "迁移前功能合法完成", "--changed-files", "ledger.py",
                 "--artifact-sha256", sha)
        cli_json(self.project, "team-task", "--set", "sum", "--feature", "sum", "--status", "done")
        self.register_review_evidence("sum", "review:sum")

        # 无导出 → 拒绝回退，活动库与 legacy 原件原样保留（断言 e）
        failed = cli(self.project, "team-rollback")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("--export-first", failed.stderr)
        self.assertTrue((self.data / "team.db").exists(), "失败回退不得删活动库")
        self.assertEqual(self.legacy_hashes(), before, "失败回退不得改原 JSON")
        # 导出目录在记录目录内 → 拒绝，同样不删库
        failed = cli(self.project, "team-rollback", "--export-first", str(self.data / "exp"))
        self.assertEqual(failed.returncode, 2)
        self.assertIn("记录目录", failed.stderr)
        self.assertTrue((self.data / "team.db").exists())

        out_dir = self.base / "facts-export"
        report = cli_json(self.project, "team-rollback", "--export-first", str(out_dir))
        self.assertEqual(report["status"], "rolled_back")
        export = report["export"]

        # (a) 导出含新增任务：sidecar 完整记录 + 全量事件日志一致 + manifest 计数核对
        sidecar = json.loads((out_dir / "team_sidecar.json").read_text(encoding="utf-8"))
        self.assertEqual([t for t in sidecar["tasks"] if t["task_id"] == "new-after-migration"],
                         [{"feature_id": "sum", "status": "ready",
                           "task_id": "new-after-migration"}], "新增任务未进导出")
        self.assertEqual(sidecar["tasks"][0]["status"], "ready", "sidecar 应保真团队原始状态")
        log = json.loads((out_dir / "team_events.json").read_text(encoding="utf-8"))["events"]
        self.assertEqual(len(log), report["events_total"], "事件日志与库内总数不一致")
        manifest = json.loads((out_dir / "export_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["counts"]["events_total"], report["events_total"])
        self.assertEqual(export["counts"]["events_total"], report["events_total"])

        # (b) warnings 逐条列出不可映射项（新增子任务 + review 证据 + done 降级）
        warnings = "\n".join(export["warnings"])
        self.assertIn("new-after-migration", warnings)
        self.assertIn("review:sum", warnings)
        self.assertIn("awaiting_review", warnings)
        self.assertIn("done", warnings)

        # (d) sidecar 含审查等不可映射事实原件（FIX-04 更新：sum 的合法回报证据
        # report:sum#1 同样不可映射，按事件顺序一并入 sidecar）
        self.assertEqual([e["evidence_id"] for e in sidecar["evidence"]],
                         ["report:sum#1", "review:sum"])
        self.assertEqual(sidecar["evidence"][-1]["review"]["passed"], True)

        # (c) 旧格式草稿过真实 legacy Project.load；done 未变成 accepted
        state = self.load_draft_with_real_legacy(out_dir)
        self.assertEqual(set(state["tasks"]), {"sum", "export"})
        self.assertEqual(state["tasks"]["sum"]["status"], "awaiting_review")
        self.assertNotEqual(state["tasks"]["sum"]["status"], "accepted",
                            "done 不得升级成 accepted")
        self.assertEqual(state["tasks"]["export"]["status"], "pending")
        self.assertEqual(self.legacy_hashes(), before, "回退不得改动 legacy 原件")

    def test_status_mapping_table_covers_all_team_statuses(self):
        """映射决策表端到端：每个功能一种团队状态，导出草稿逐一断言 legacy 状态与警告。

        FIX-04 更新说明：p1..p7 已随迁移导入为 pending 任务，不能再 add（编号已存在）；
        各状态改经合法动作链构造——p2=ready、p3=ready+running、p4=完整链到 done
        （合法完成仍产生 done，导出映射不变）、p5=running+failed(--reason)、
        p6=blocked(--reason)、p7=cancelled(--reason)。导出映射表与警告断言原样保留。
        """
        statuses = {"p1": "pending", "p2": "ready", "p3": "running", "p4": "done",
                    "p5": "failed", "p6": "blocked", "p7": "cancelled"}
        scope = {"title": "映射表", "goal": "验证映射", "audience": "测试", "scenario": "单测",
                 "out_of_scope": [], "assumptions": [],
                 "features": [{"id": fid, "title": fid, "acceptance_criteria": ["c"],
                               "allowed_paths": ["a.txt"], "check_commands": [["python3", "-c", "pass"]]}
                              for fid in sorted(statuses)]}
        scope_path = self.project / "scope.json"
        scope_path.write_text(json.dumps(scope, ensure_ascii=False), encoding="utf-8")
        cli_json(self.project, "init", "--input", str(scope_path))
        cli_json(self.project, "confirm", "--revision", "1")
        cli_json(self.project, "team-migrate", "--from-json")
        cli_json(self.project, "team-task", "--set", "p2", "--feature", "p2", "--status", "ready")
        cli_json(self.project, "team-task", "--set", "p3", "--feature", "p3", "--status", "ready")
        cli_json(self.project, "team-task", "--set", "p3", "--feature", "p3", "--status", "running")
        cli_json(self.project, "team-task", "--set", "p4", "--feature", "p4", "--status", "ready")
        cli_json(self.project, "team-task", "--set", "p4", "--feature", "p4", "--status", "running")
        import hashlib
        sha = hashlib.sha256(b"p4-deliverable").hexdigest()
        cli_json(self.project, "team-report", "--task", "p4", "--outcome", "succeeded",
                 "--summary", "完成", "--changed-files", "a.txt", "--artifact-sha256", sha)
        cli_json(self.project, "team-task", "--set", "p4", "--feature", "p4", "--status", "done")
        cli_json(self.project, "team-task", "--set", "p5", "--feature", "p5", "--status", "ready")
        cli_json(self.project, "team-task", "--set", "p5", "--feature", "p5", "--status", "running")
        cli_json(self.project, "team-task", "--set", "p5", "--feature", "p5",
                 "--status", "failed", "--reason", "回归失败")
        cli_json(self.project, "team-task", "--set", "p6", "--feature", "p6",
                 "--status", "blocked", "--reason", "等待上游")
        cli_json(self.project, "team-task", "--set", "p7", "--feature", "p7",
                 "--status", "cancelled", "--reason", "范围裁剪")
        out_dir = self.base / "mapping-export"
        report = cli_json(self.project, "team-rollback", "--export-first", str(out_dir))
        state = self.load_draft_with_real_legacy(out_dir)
        expected = {"p1": "pending", "p2": "pending", "p3": "pending", "p4": "awaiting_review",
                    "p5": "blocked", "p6": "blocked", "p7": "pending"}
        self.assertEqual({fid: t["status"] for fid, t in state["tasks"].items()}, expected)
        self.assertNotIn("accepted", expected.values(), "任何团队状态都不得映射为 accepted")
        warnings = "\n".join(report["export"]["warnings"])
        for fid in ("p2", "p3", "p4", "p5", "p7"):  # 非恒等映射逐条有警告
            self.assertIn(fid, warnings)
        # FIX-04 更新说明：p4 经 team-report 回报后完成，回报证据（kind=check）旧
        # schema 无法表达 → 进 sidecar（不静默丢弃）；同 id 任务状态仍全部可映射
        sidecar = json.loads((out_dir / "team_sidecar.json").read_text(encoding="utf-8"))
        self.assertEqual([e["evidence_id"] for e in sidecar["evidence"]],
                         ["report:p4#1"])


if __name__ == "__main__":
    unittest.main()

"""R05 收尾回归：真实 CLI 子进程验证 status 读取语义（09 §1 B 层证据）。

覆盖（对应 docs/verification/r05-status-l-tier-2026-09-20.md 的插桩取证结论）：
- 低于容量阈值：status 走快照路径，源码树内容读取发生（对照语义，如实断言）。
- 超容量阈值：status exit 0 降级（paused/unavailable），写路径仍硬拒绝。
- 历史验收展示：超限后 accepted 不计数、状态降 awaiting_review、验收记录与
  accepted 事件仍展示（不冒充当前验收）。
- team-status：对存在源码树的项目零源码树读取，事实库经 sqlite3.connect 读取。
插桩经 tests/closure_r05/instrument.py 注入子进程环境，不改产品代码。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from instrument import build_rules, run_counted  # noqa: E402

REPO = HERE.parents[1]
COMPANION = REPO / "dev-companion" / "scripts" / "companion.py"
PLUGIN_ROOTS = [REPO / "dev-companion" / "scripts", REPO / "packages"]

SCOPE = {"title": "R05 回归项目", "goal": "验证读取语义", "audience": "回归",
         "scenario": "单元回归", "out_of_scope": [], "assumptions": [],
         "features": [{"id": "f1", "title": "代表功能", "acceptance_criteria": ["可用"],
                       "allowed_paths": ["app.py"], "requires_user_acceptance": False,
                       "check_commands": [["/usr/bin/true"]]}]}


def cli(project, *argv):
    return [sys.executable, str(COMPANION), "--project", str(project), *argv]


def run_ok(argv):
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    if proc.returncode != 0:
        raise AssertionError("命令失败 exit=%d：%s" % (proc.returncode, proc.stderr.decode()[:300]))
    return json.loads(proc.stdout)


class CliProjectCase(unittest.TestCase):
    def make_project(self, file_count=12):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for index in range(file_count):
            leaf = root / ("pkg_%02d" % (index // 4)) / ("sub_%02d" % (index % 4))
            leaf.mkdir(parents=True, exist_ok=True)
            (leaf / ("mod_%03d.py" % index)).write_text("VALUE = %d\n" % index)
        run_ok(cli(root, "init", "--input", self.write_json("scope.json", SCOPE)))
        run_ok(cli(root, "confirm", "--revision", "1"))
        return root

    def write_json(self, name, payload):
        path = Path(self.make_temp()) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    @staticmethod
    def make_temp():
        return tempfile.mkdtemp(prefix="r05-test-")

    def instrumented_status(self, project):
        argv = cli(project, "status", "--format", "json")
        proc, counts, _ = run_counted(argv, build_rules(project, PLUGIN_ROOTS))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[:300])
        return json.loads(proc.stdout), counts


class SnapshotPathReadsSourceTree(CliProjectCase):
    def test_below_threshold_status_reads_source_content(self):
        project = self.make_project(file_count=12)
        view, counts = self.instrumented_status(project)
        self.assertIsNone(view.get("capacity_status"), "低于阈值不得标 paused")
        self.assertGreater(counts["source_tree"]["open_calls"], 0, "快照路径必须读源码树（对照语义）")
        self.assertGreater(counts["source_tree"]["read_bytes"], 0)
        # 2 次 = state.json + capacity-verdict.json（R05 修复新增的容量判定缓存，同属事实库合法读取）
        self.assertEqual(counts["facts"]["open_calls"], 2, "事实库读取另计（state + 容量判定缓存）")
        self.assertEqual(view["counts"]["pending"], 1)


class OverCapacityDegradation(CliProjectCase):
    def prepare_accepted_then_oversize(self):
        project = self.make_project(file_count=12)
        packet = run_ok(cli(project, "packet", "--feature", "f1"))
        (project / "app.py").write_text("def total(values):\n    return sum(values)\n")
        receipt = {"feature_id": "f1", "run_id": packet["run_id"],
                   "scope_version": packet["scope_version"], "status": "implemented",
                   "summary": "合计可用", "changed_files": ["app.py"], "evidence_files": []}
        run_ok(cli(project, "receipt", "--input", self.write_json("receipt.json", receipt)))
        run_ok(cli(project, "check", "--feature", "f1"))
        run_ok(cli(project, "accept", "--feature", "f1", "--note", "真实检查通过"))
        (project / "big.bin").write_bytes(b"\0" * (21 * 1024 * 1024))
        return project

    def test_status_degrades_and_write_path_hard_fails(self):
        project = self.prepare_accepted_then_oversize()
        view, counts = self.instrumented_status(project)
        self.assertEqual(view["capacity_status"], "paused")
        self.assertEqual(view["fingerprint_status"], "unavailable")
        self.assertIsNone(view["source_fingerprint"])
        self.assertIsNone(view["overall_percent"])
        proc = subprocess.run(cli(project, "packet", "--feature", "f1"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        self.assertEqual(proc.returncode, 2, "写路径必须硬拒绝")
        self.assertIn("超出", proc.stderr.decode())

    def test_historical_acceptance_still_displayed_not_counted(self):
        project = self.prepare_accepted_then_oversize()
        view, _ = self.instrumented_status(project)
        self.assertEqual(view["counts"]["accepted"], 0, "无法核验的验收不得继续算 accepted")
        feature = view["features"][0]
        self.assertEqual(feature["status"], "awaiting_review")
        self.assertTrue(feature["evidence_stale"])
        self.assertEqual((feature.get("acceptance") or {}).get("note"), "真实检查通过",
                         "历史验收记录仍须展示")
        self.assertIn("accepted", [event["kind"] for event in view["history"]],
                      "accepted 事件历史不被删除")


class TeamStatusReadsOnlyFactStore(CliProjectCase):
    def test_team_status_zero_source_reads_and_paginates(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        project = Path(temp.name)
        for index in range(6):
            (project / ("src_%d.py" % index)).write_text("X = %d\n" % index)
        run_ok(cli(project, "team-init", "--feature", "core", "核心功能"))
        for index in range(5):
            # FIX-04 更新说明：ready 前须显式 team-task-add（SR-01 门禁：任务存在才可派发）
            run_ok(cli(project, "team-task-add", "--set", "t%d" % index,
                       "--feature", "core", "--kind", "impl"))
            run_ok(cli(project, "team-task", "--set", "t%d" % index,
                       "--feature", "core", "--status", "ready"))
        argv = cli(project, "team-status", "--offset", "0", "--limit", "3")
        proc, counts, _ = run_counted(argv, build_rules(project, PLUGIN_ROOTS))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[:300])
        view = json.loads(proc.stdout)
        self.assertEqual(len(view["tasks"]["items"]), 3)
        self.assertTrue(view["tasks"]["has_more"])
        self.assertEqual(counts["source_tree"]["open_calls"], 0, "不得读取目标源码内容")
        self.assertEqual(counts["source_tree"]["scandir_calls"], 0, "不得枚举目标源码树")
        self.assertEqual(counts["source_tree"]["read_bytes"], 0)
        self.assertGreaterEqual(counts["facts"]["sqlite_connects"], 1, "事实库经 sqlite3.connect")


class InstrumentAttribution(CliProjectCase):
    def test_rules_order_facts_before_source_and_cover_unresolved_path(self):
        """SR-07：改用可控临时 symlink 场景断言 realpath 双拼写语义，不再依赖
        macOS /var→/private/var 特例——core 对项目根 realpath、team 模块不解析
        symlink，两类拼写都必须被插桩规则覆盖（Linux/CI 同样成立）。"""
        real_root = Path(self.make_temp()) / "real-proj"
        real_root.mkdir(parents=True)
        link_root = real_root.parent / "link-proj"
        link_root.symlink_to(real_root, target_is_directory=True)
        rules = build_rules(str(link_root), [str(REPO / "packages")])
        facts_prefixes = [rule["prefix"] for rule in rules if rule["category"] == "facts"]
        self.assertTrue(facts_prefixes and facts_prefixes[0].endswith("/.dev-companion"),
                        "facts（.dev-companion）必须排最前")
        self.assertEqual(rules[0]["category"], "facts")
        self.assertTrue(any(rule["category"] == "source_tree" for rule in rules))
        resolved = os.path.realpath(str(link_root))
        self.assertTrue(any(rule["prefix"].startswith(resolved) for rule in rules),
                        "core realpath 解析后的拼写必须被覆盖")
        self.assertTrue(any(rule["prefix"].startswith(str(link_root)) for rule in rules),
                        "team 模块不解析 symlink 的原始拼写必须被覆盖")

    @unittest.skipUnless(sys.platform == "darwin",
                         "macOS 特例断言：/var→/private/var realpath 拼写只在 darwin 成立；"
                         "其余平台跳过（同一 realpath 语义已由上一用例的可控 symlink 场景覆盖）")
    def test_darwin_var_realpath_spelling_covered(self):
        """原 macOS 口径保留在本平台条件用例中：/var/folders 路径必须同时覆盖
        /private/var（core realpath）与 /var/（team 原始拼写）两类前缀。"""
        rules = build_rules("/var/folders/x/proj", ["/repo/pkg"])
        self.assertTrue(any("/private/var" in rule["prefix"] for rule in rules),
                        "core realpath 拼写（/private/var）必须被覆盖")
        self.assertTrue(any(rule["prefix"].startswith("/var/") for rule in rules),
                         "team 模块不解析 symlink 的原始拼写必须被覆盖")


if __name__ == "__main__":
    unittest.main()

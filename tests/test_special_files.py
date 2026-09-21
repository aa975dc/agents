"""Special files outside management are skipped with a reason; managed ones block;
a missing state.json with a surviving release.json is diagnosed, never auto-rebuilt."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "dev-companion" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from core import CompanionError, Project
from releases import ReleaseStore

HAS_FIFO = hasattr(os, "mkfifo")


class SpecialFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = Project(self.root)
        self.scope = {"title": "本地工具", "goal": "验证合计", "audience": "自己", "scenario": "本机使用",
                      "features": [{"id": "sum", "title": "合计", "acceptance_criteria": ["20+30=50"],
                                    "allowed_paths": ["ledger.py"], "requires_user_acceptance": False,
                                    "check_commands": [[sys.executable, "-c", "from ledger import total; assert total([20,30]) == 50"]]}]}
        self.config = {"version": "1.0.0", "target": "临时本地文件", "environment": "local",
                       "summary": "发布合计工具", "rollback_plan": "恢复临时目标到旧版本",
                       "verification_notes": "读取实际部署版本",
                       "deploy_commands": [[sys.executable, "-c", "from pathlib import Path; Path('.dev-companion/deployed.txt').write_text('1.0.0')"]],
                       "verify_commands": [[sys.executable, "-c", "from pathlib import Path; assert Path('.dev-companion/deployed.txt').read_text() == '1.0.0'"]],
                       "rollback_commands": [[sys.executable, "-c", "from pathlib import Path; Path('.dev-companion/deployed.txt').write_text('0.9.0')"]]}

    def ready(self):
        self.project.init(self.scope)
        self.project.confirm(self.project.load()["revision"])
        packet = self.project.packet("sum")
        (self.root / "ledger.py").write_text("def total(values): return sum(values)\n")
        self.project.receipt({"feature_id": "sum", "run_id": packet["run_id"], "scope_version": packet["scope_version"],
                              "status": "implemented", "summary": "合计可用", "changed_files": ["ledger.py"], "evidence_files": []})
        self.project.check("sum")
        self.project.accept("sum", "真实合计检查通过")

    @unittest.skipIf(not HAS_FIFO, "platform without os.mkfifo")
    def test_unmanaged_fifo_is_recorded_as_excluded_and_status_survives(self):
        self.ready()
        os.mkfifo(str(self.root / "events.pipe"))
        view = self.project.status()
        self.assertEqual(view["overall_percent"], 100)
        self.assertIn("events.pipe（特殊文件，未纳入项目检查）", view["excluded_paths"])
        self.assertNotIn("events.pipe", view["source_fingerprint"])

    @unittest.skipIf(not HAS_FIFO, "platform without os.mkfifo")
    def test_fifo_inside_allowed_paths_blocks_dispatch_with_clear_error(self):
        self.project.init(self.scope)
        self.project.confirm(self.project.load()["revision"])
        os.mkfifo(str(self.root / "ledger.py"))
        with self.assertRaisesRegex(CompanionError, "需要普通文件"):
            self.project.packet("sum")

    def test_missing_state_with_surviving_release_is_diagnosed_and_never_rebuilt(self):
        self.ready()
        self.store = ReleaseStore(self.project)
        prepared = self.store.prepare(self.config, 0)
        release_path = self.project.data / "release.json"
        original_release = release_path.read_text()
        self.project.state_path.unlink()
        for operation in (lambda: self.project.status(),
                          lambda: ReleaseStore(self.project).status(),
                          lambda: self.project.packet("sum"),
                          lambda: self.project.init(self.scope)):
            with self.subTest(operation=operation), self.assertRaisesRegex(CompanionError, "state.json 缺失"):
                operation()
        self.assertFalse(self.project.state_path.exists())
        self.assertEqual(release_path.read_text(), original_release)
        self.assertEqual(json.loads(original_release)["revision"], prepared["revision"])
        with self.assertRaisesRegex(CompanionError, "state.json 缺失"):
            ReleaseStore(self.project).prepare(self.config, prepared["revision"])


if __name__ == "__main__":
    unittest.main()

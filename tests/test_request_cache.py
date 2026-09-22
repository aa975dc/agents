"""P2-03：请求级缓存（Z13 性能半）——一次 status 链路只解析 Journey 一次，写路径不受缓存影响。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dev-companion" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from core import Project
from journey import Journey
from agents_kernel.atomicio import write_json
from agents_kernel.services import request_cache


class RequestCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = Project(self.root)
        self.scope = {"title": "记账工具", "goal": "算出总金额", "audience": "自己", "scenario": "记账",
                      "out_of_scope": [], "assumptions": [], "features": [
                          {"id": "sum", "title": "计算合计", "acceptance_criteria": ["20+30=50"],
                           "allowed_paths": ["ledger.py"], "requires_user_acceptance": True,
                           "check_commands": [[sys.executable, "-c", "pass"]]}]}
        self.project.init(self.scope)
        self.project.confirm(1)
        self.seed_release_record()

    def seed_release_record(self):
        # 一条合法 prepared 发布记录，让 status 走到发布核验的 Journey 复核（第 3 处解析）。
        record = {"schema_version": 1, "project": str(self.project.root), "revision": 1, "history": [],
                  "config": {"version": "1.0.0", "target": "本地目录", "environment": "local", "summary": "发布",
                             "rollback_plan": "恢复旧版本", "verification_notes": "读取实际部署版本",
                             "deploy_commands": [["true"]], "verify_commands": [["true"]],
                             "rollback_commands": [["true"]]},
                  "binding": {"scope_version": 1, "fingerprint": "a" * 64, "check_ids": {},
                              "integration_ids": {}, "journey": None},
                  "status": "prepared", "evidence": {}}
        write_json(self.project.data / "release.json", record)

    def count_loads(self):
        """替换 Journey._load 计数：status 链路三处解析（状态视图/规划指纹/发布核验）都经过它。"""
        real_load = Journey._load
        calls = []

        def counting(journey_self):
            calls.append(1)
            return real_load(journey_self)

        return mock.patch.object(Journey, "_load", counting), calls

    def test_one_status_parses_journey_once_even_with_release_review(self):
        patcher, calls = self.count_loads()
        with patcher:
            self.project.status()
        self.assertEqual(len(calls), 1, "status 链路（状态+指纹+发布核验）应只解析一次 Journey")

    def test_multiple_statuses_inside_one_scope_reuse_one_parse(self):
        patcher, calls = self.count_loads()
        with patcher, request_cache.RequestCache.with_scope():
            self.project.status()
            self.project.status()
            self.project.status()
        self.assertEqual(len(calls), 1, "同一请求作用域内复用一次解析")

    def test_outside_scope_every_status_reparses(self):
        patcher, calls = self.count_loads()
        with patcher:
            self.project.status()
            self.project.status()
            self.project.status()
        self.assertEqual(len(calls), 3, "作用域外等于新请求：必须现读，缓存不得跨请求泄漏")

    def test_get_without_scope_always_computes(self):
        counter = {"n": 0}

        def factory():
            counter["n"] += 1
            return counter["n"]

        self.assertEqual(request_cache.RequestCache.get("k", factory), 1)
        self.assertEqual(request_cache.RequestCache.get("k", factory), 2)
        self.assertFalse(request_cache.RequestCache.hit("k"))

    def test_scope_caches_by_key_and_reports_hits(self):
        counter = {"n": 0}

        def factory():
            counter["n"] += 1
            return counter["n"]

        with request_cache.RequestCache.with_scope():
            self.assertFalse(request_cache.RequestCache.hit("k"))
            self.assertEqual(request_cache.RequestCache.get("k", factory), 1)
            self.assertFalse(request_cache.RequestCache.hit("k"), "首次计算不算命中")
            self.assertEqual(request_cache.RequestCache.get("k", factory), 1)
            self.assertTrue(request_cache.RequestCache.hit("k"))

    def test_nested_scope_reuses_outer_scope(self):
        counter = {"n": 0}

        def factory():
            counter["n"] += 1
            return counter["n"]

        with request_cache.RequestCache.with_scope():
            request_cache.RequestCache.get("k", factory)
            with request_cache.RequestCache.with_scope():
                request_cache.RequestCache.get("k", factory)
        self.assertEqual(counter["n"], 1)


if __name__ == "__main__":
    unittest.main()

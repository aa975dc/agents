"""P2-03：状态分页视图与新鲜度（Z13 呈现半 + Z21 前半 + C14）。"""
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dev-companion" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from core import CapacityExceeded, CompanionError, Project, render_html, render_markdown
from agents_kernel.presentation import status_view
from agents_kernel.services import request_cache


def feature(identifier):
    return {"id": identifier, "title": "功能" + identifier, "acceptance_criteria": ["可用"],
            "allowed_paths": [identifier + ".py"], "requires_user_acceptance": False,
            "check_commands": [[sys.executable, "-c", "pass"]]}


class PaginateTests(unittest.TestCase):
    features = list(range(250))

    def test_default_page_size_100_and_capped(self):
        page = status_view.paginate(self.features)
        self.assertEqual((page["page"], page["page_size"]), (1, 100))
        self.assertEqual(page["total_estimated"], 250)
        self.assertEqual(len(page["items"]), 100)
        self.assertTrue(page["has_more"])

    def test_slices_and_has_more(self):
        last = status_view.paginate(self.features, page=3)
        self.assertEqual(last["items"], list(range(200, 250)))
        self.assertFalse(last["has_more"])
        middle = status_view.paginate(self.features, page=2)
        self.assertEqual(middle["items"], list(range(100, 200)))
        self.assertTrue(middle["has_more"])

    def test_page_size_over_100_is_truncated(self):
        self.assertEqual(status_view.paginate(self.features, page_size=250)["page_size"], 100)
        self.assertEqual(status_view.paginate(self.features, page_size=101)["items"], list(range(100)))

    def test_page_floor_is_one(self):
        self.assertEqual(status_view.paginate(self.features, page=0)["page"], 1)
        self.assertEqual(status_view.paginate(self.features, page=-3)["items"], self.features[:100])

    def test_page_params_must_be_integers(self):
        with self.assertRaises(CompanionError):
            status_view.paginate([], page="1")
        with self.assertRaises(CompanionError):
            status_view.paginate([], page_size=True)


class StatusPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = Project(self.root)
        self.scope = {"title": "多功能项目", "goal": "算总金额", "audience": "自己", "scenario": "记账",
                      "out_of_scope": [], "assumptions": [],
                      "features": [feature("f%d" % i) for i in range(1, 6)]}
        self.project.init(self.scope)
        self.project.confirm(1)

    def test_pages_carry_full_context_and_correct_slices(self):
        page1 = status_view.build_status_page(self.project, page=1, page_size=2)
        page2 = status_view.build_status_page(self.project, page=2, page_size=2)
        page3 = status_view.build_status_page(self.project, page=3, page_size=2)
        self.assertEqual([f["id"] for f in page1["items"]], ["f1", "f2"])
        self.assertEqual([f["id"] for f in page2["items"]], ["f3", "f4"])
        self.assertEqual([f["id"] for f in page3["items"]], ["f5"])
        self.assertTrue(page1["has_more"] and page2["has_more"])
        self.assertFalse(page3["has_more"])
        for page in (page1, page2, page3):
            self.assertEqual(page["total_estimated"], 5)
            self.assertEqual(page["counts"]["pending"], 5)
            self.assertEqual(page["overall_percent"], 0)
            self.assertTrue(page["generated_at"])

    def test_default_and_capped_page_size(self):
        self.assertEqual(status_view.build_status_page(self.project)["page_size"], 100)
        self.assertFalse(status_view.build_status_page(self.project)["has_more"])
        self.assertEqual(status_view.build_status_page(self.project, page_size=500)["page_size"], 100)

    def test_freshness_live_then_cached_inside_one_scope(self):
        with request_cache.RequestCache.with_scope():
            first = status_view.build_status_page(self.project)
            second = status_view.build_status_page(self.project, page=2)
        self.assertEqual(first["source"], "live")
        self.assertFalse(first["stale"])
        self.assertEqual(first["fingerprint_status"], "available")
        self.assertEqual(second["source"], "cached")
        self.assertTrue(second["stale"], "同作用域内复用缓存必须标 stale（C14）")
        self.assertTrue(second["generated_at"])

    def test_each_standalone_call_is_live(self):
        self.assertEqual(status_view.build_status_page(self.project)["source"], "live")
        self.assertEqual(status_view.build_status_page(self.project)["source"], "live")


class OverCapacityStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = Project(self.root)
        self.scope = {"title": "超限项目", "goal": "算总金额", "audience": "自己", "scenario": "记账",
                      "out_of_scope": [], "assumptions": [],
                      "features": [{"id": "sum", "title": "计算合计", "acceptance_criteria": ["20+30=50"],
                                    "allowed_paths": ["ledger.py"], "requires_user_acceptance": False,
                                    "check_commands": [[sys.executable, "-c", "assert True"]]}]}

    def big_file(self):
        (self.root / "big.bin").write_bytes(b"\0" * (21 * 1024 * 1024))

    def test_snapshot_still_hard_fails_for_write_paths(self):
        self.project.init(self.scope)
        self.project.confirm(1)
        self.big_file()
        with self.assertRaises(CapacityExceeded):
            self.project.snapshot()
        with self.assertRaises(CompanionError):
            self.project.snapshot()

    def test_status_degrades_without_raising_and_marks_unavailable(self):
        self.project.init(self.scope)
        self.project.confirm(1)
        self.big_file()
        view = self.project.status()  # 不抛错
        self.assertIsNone(view["source_fingerprint"])
        self.assertEqual(view["fingerprint_status"], "unavailable", "指纹不可核验必须显式标注")
        self.assertEqual(view["capacity_status"], "paused")
        self.assertIn("索引", view["next_step"])
        self.assertIn("超出", view["capacity_message"])
        self.assertIsNone(view["overall_percent"], "无法核验时不得冒充进度")
        self.assertIn("发布核验暂停", view["publication"])

    def test_accepted_is_not_counted_as_accepted_without_fingerprint(self):
        self.project.init(self.scope)
        self.project.confirm(1)
        packet = self.project.packet("sum")
        (self.root / "ledger.py").write_text("def total(values): return sum(values)\n")
        self.project.receipt({"feature_id": "sum", "run_id": packet["run_id"],
                              "scope_version": packet["scope_version"], "status": "implemented",
                              "summary": "合计可用", "changed_files": ["ledger.py"], "evidence_files": []})
        self.project.check("sum")
        self.project.accept("sum", "真实检查通过")
        self.assertEqual(self.project.status()["overall_percent"], 100)
        self.big_file()
        view = self.project.status()
        self.assertEqual(view["counts"]["accepted"], 0, "unknown/stale 不能继续算 accepted")
        self.assertEqual(view["features"][0]["status"], "awaiting_review")
        self.assertTrue(view["features"][0]["evidence_stale"])
        markdown = render_markdown(view)
        self.assertIn("索引未建", markdown)
        self.assertIn("无法核验", markdown)
        self.assertIn("索引未建", render_html(view))

    def test_paginated_degraded_view_comes_from_store_and_is_stale(self):
        self.project.init(self.scope)
        self.project.confirm(1)
        self.big_file()
        page = status_view.build_status_page(self.project)
        self.assertEqual(page["source"], "store")
        self.assertTrue(page["stale"])
        self.assertEqual(page["fingerprint_status"], "unavailable")
        self.assertEqual(page["counts"]["pending"], 1)


if __name__ == "__main__":
    unittest.main()

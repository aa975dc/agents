"""Backend/data/interface contract (C04): companion-backend stays isomorphic
with the four existing roles, its contract doc and machine-readable example
hold a minimal schema (error-code enum, idempotency, migration), and — the
point of the exercise — the documented example cannot drift from the real
implementation: field names, enum values and pagination constants are compared
against agents_kernel.presentation.status_view at runtime, so a field the code
does not produce fails these tests. Existing contract suites must keep passing."""
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO / "dev-companion"
ROLE_FILE = PLUGIN_ROOT / "agents" / "companion-backend.md"
CONTRACT_DOC = PLUGIN_ROOT / "references" / "api-contract.md"
FIXTURE = REPO / "tests" / "fixtures" / "design" / "api-example.json"

SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import kernel_bootstrap  # noqa: E402 — import 即完成 sys.path 注入，与 core 同款引导
from agents_kernel.presentation import status_view  # noqa: E402
from agents_kernel.services.request_cache import RequestCache  # noqa: E402
from agents_kernel.validation import CompanionError  # noqa: E402
from core import Project  # noqa: E402

ALL_ROLES = ("companion-developer", "companion-checker", "companion-product",
             "companion-design", "companion-backend")

REQUIRED_FRONTMATTER_KEYS = {"name", "description"}
REFERENCE_RE = re.compile(r"references/[A-Za-z0-9_-]+\.md")
JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)

ROLE_MARKERS = {
    "companion-backend": (
        "数据语义", "幂等", "错误码", "迁移", "measurement_required",
        "不写实现代码", "不再派发子智能体", "references/api-contract.md",
    ),
}

REPORT_BLOCK_KEYS = {"feature_id", "run_id", "summary", "backend_design", "open_questions", "blocker"}
CONTRACT_FIELDS = ("contract_id", "feature_id", "kind", "entry", "auth", "request",
                   "response", "error_codes", "idempotency", "migration")
ENDPOINT_FIELDS = ("contract_id", "kind", "entry", "request_example", "response_example",
                   "error_codes", "idempotency")
ERROR_CODE_RE = re.compile(r"^E_[A-Z_]+$")
ERROR_EXIT_CODES = (2, 3)
IDEMPOTENCY_MODES = ("read_only", "revision_cas", "idempotency_key")
COUNT_LABELS = ("pending", "running", "awaiting_review", "accepted", "blocked")
ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


def frontmatter(text):
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return None
    fields = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class BackendRoleContractTests(unittest.TestCase):
    def role_text(self, name):
        path = PLUGIN_ROOT / "agents" / f"{name}.md"
        self.assertTrue(path.is_file(), f"missing role file: {path}")
        return path.read_text(encoding="utf-8")

    def test_five_roles_frontmatter_isomorphic(self):
        for name in ALL_ROLES:
            with self.subTest(role=name):
                fields = frontmatter(self.role_text(name))
                self.assertIsNotNone(fields, f"{name}: frontmatter block not found")
                self.assertEqual(set(fields), REQUIRED_FRONTMATTER_KEYS,
                                 f"{name}: frontmatter keys diverge")
                self.assertEqual(fields["name"], name, f"{name}: frontmatter name mismatch")
                self.assertGreater(len(fields["description"]), 10, f"{name}: empty description")

    def test_reference_closure_from_roles_and_contract_docs(self):
        """Every references/*.md cited by any role or contract doc exists;
        cited docs are scanned transitively so the chain never dangles."""
        seen, queue = set(), ["agents/%s.md" % role for role in ALL_ROLES]
        queue.append("references/api-contract.md")
        while queue:
            item = queue.pop()
            if item in seen:
                continue
            seen.add(item)
            text = (PLUGIN_ROOT / item).read_text(encoding="utf-8")
            for reference in REFERENCE_RE.findall(text):
                self.assertTrue((PLUGIN_ROOT / reference).is_file(),
                                f"{item}: referenced doc missing: {reference}")
                queue.append(reference)

    def test_backend_role_markers_and_report_block(self):
        text = self.role_text("companion-backend")
        for marker in ROLE_MARKERS["companion-backend"]:
            self.assertIn(marker, text, f"companion-backend: substantive marker missing: {marker}")
        self.assertIn("回流 companion-product", text, "业务决策回流产品的红线缺失")
        match = JSON_BLOCK_RE.search(text)
        self.assertIsNotNone(match, "companion-backend: json report block missing")
        block = json.loads(match.group(1))
        self.assertEqual(set(block), REPORT_BLOCK_KEYS,
                         "report block keys diverge from existing roles' shape")
        design = block["backend_design"]
        for key in ("path", "contract_id", "entities", "endpoints"):
            self.assertIn(key, design, f"backend_design missing key: {key}")
        self.assertTrue(design["entities"] and design["endpoints"], "实体与端点不得为空")
        for key in ("name", "lifecycle", "invariants"):
            self.assertIn(key, design["entities"][0], f"entity missing key: {key}")
        for key in ENDPOINT_FIELDS:
            self.assertIn(key, design["endpoints"][0], f"endpoint missing key: {key}")


class ApiExampleSchemaTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(FIXTURE.is_file(), f"missing fixture: {FIXTURE}")
        self.example = load_fixture()
        self.doc_text = CONTRACT_DOC.read_text(encoding="utf-8")

    def test_required_top_level_fields(self):
        for key in CONTRACT_FIELDS + ("response_enums", "response_semantics",
                                      "pagination", "measurement_required"):
            self.assertIn(key, self.example, f"api example missing field: {key}")
        self.assertIn(self.example["kind"], ("http", "local"))
        self.assertTrue(self.example["entry"].strip() and self.example["auth"].strip())

    def test_error_codes_are_closed_enum_with_exit_codes(self):
        codes = self.example["error_codes"]
        self.assertTrue(codes, "error_codes must not be empty")
        seen = set()
        for entry in codes:
            with self.subTest(code=entry.get("code")):
                self.assertRegex(entry["code"], ERROR_CODE_RE, "错误码必须是 E_ 前缀枚举")
                self.assertNotIn(entry["code"], seen, "错误码重复")
                seen.add(entry["code"])
                self.assertIn(entry["exit_code"], ERROR_EXIT_CODES)
                self.assertTrue(entry["when"].strip(), "错误码缺触发条件")
                self.assertIn(entry["code"], self.doc_text,
                              f"{entry['code']} 未在 api-contract.md 枚举表中声明")

    def test_idempotency_declaration_present(self):
        idem = self.example["idempotency"]
        self.assertIn(idem["mode"], IDEMPOTENCY_MODES, "幂等模式必须是三模式之一")
        self.assertIn("key", idem, "幂等键声明缺失（只读允许显式 null）")
        if idem["mode"] == "idempotency_key":
            self.assertIsInstance(idem["key"], str)
            self.assertTrue(idem["key"].strip(), "幂等键模式必须给出键的构造规则")
        else:
            self.assertIsNone(idem["key"], "非幂等键模式 key 应为 null 并在 note 说明")

    def test_migration_and_measurement_annotations(self):
        migration = self.example["migration"]
        self.assertIsInstance(migration["breaking"], bool)
        self.assertTrue(migration["coexistence"].strip(), "缺旧数据并存期说明")
        self.assertTrue(migration["rollback"].strip(), "缺回退方式")
        self.assertIsInstance(self.example["measurement_required"], list)

    def test_pagination_declares_real_response_field(self):
        pagination = self.example["pagination"]
        self.assertIn(pagination["has_more_field"], self.example["response"],
                      "has_more_field 必须是 response 里真实存在的字段")

    def test_doc_example_matches_fixture(self):
        blocks = [json.loads(match.group(1)) for match in JSON_BLOCK_RE.finditer(self.doc_text)]
        self.assertTrue(blocks, "api-contract.md has no json example")
        self.assertIn(self.example, blocks,
                      "api-contract.md example drifted from tests/fixtures sample")


class ImplementationConsistencyTests(unittest.TestCase):
    """The fixture must describe what build_status_page actually returns:
    field sets are compared against a live call, enum values are exercised
    through all three freshness branches, so fabricated fields fail here."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.project = Project(Path(cls.temp.name))
        scope = {"title": "多功能项目", "goal": "算总金额", "audience": "自己", "scenario": "记账",
                 "out_of_scope": [], "assumptions": [],
                 "features": [{"id": "f%d" % i, "title": "功能f%d" % i, "acceptance_criteria": ["可用"],
                               "allowed_paths": ["f%d.py" % i], "requires_user_acceptance": False,
                               "check_commands": [[sys.executable, "-c", "pass"]]}
                              for i in range(1, 6)]}
        cls.project.init(scope)
        cls.project.confirm(1)
        cls.example = load_fixture()

    def test_response_field_set_matches_real_implementation(self):
        real = status_view.build_status_page(self.project, page=1, page_size=2)
        self.assertEqual(set(self.example["response"]), set(real),
                         "文档字段与实现返回字段不一致：文档出现实现没有的字段即失败")

    def test_example_values_come_from_real_call(self):
        real = status_view.build_status_page(self.project, page=1, page_size=2)
        expected = self.example["response"]
        self.assertRegex(real["generated_at"], ISO_UTC_RE, "generated_at 必须 UTC ISO-8601")
        for key in ("total_estimated", "page", "page_size", "has_more", "source", "stale",
                    "title", "revision", "counts", "overall_percent", "next_step",
                    "fingerprint_status"):
            self.assertEqual(real[key], expected[key], f"示例值与实现不符: {key}")

    def test_item_example_fields_really_exist(self):
        real_item = status_view.build_status_page(self.project, page=1, page_size=2)["items"][0]
        shown = self.example["response"]["items"][0]
        for key, value in shown.items():
            self.assertIn(key, real_item, f"items 示例出现实现没有的字段: {key}")
            self.assertEqual(real_item[key], value, f"items 示例值与实现不符: {key}")

    def test_counts_keys_fixed_five_labels(self):
        real = status_view.build_status_page(self.project)
        self.assertEqual(tuple(real["counts"]), COUNT_LABELS)
        self.assertEqual(tuple(self.example["response"]["counts"]), COUNT_LABELS)

    def test_param_rules_match_implementation(self):
        rule = self.example["request_rules"][0]
        with self.assertRaises(CompanionError) as ctx:
            status_view.paginate([], page="1")
        self.assertEqual(str(ctx.exception), "page必须是整数")
        self.assertIn(str(ctx.exception), rule, "request_rules 未引用实现真实报错原文")
        self.assertEqual(self.example["pagination"]["page_size_max"], status_view.MAX_PAGE_SIZE)

    def test_source_enum_exhaustive_over_all_freshness_branches(self):
        declared = set(self.example["response_enums"]["source"])
        standalone = status_view.build_status_page(self.project)
        with RequestCache.with_scope():
            status_view.build_status_page(self.project)
            cached = status_view.build_status_page(self.project, page=2)
        self.assertEqual(standalone["source"], "live")
        self.assertFalse(standalone["stale"])
        self.assertEqual(cached["source"], "cached")
        self.assertTrue(cached["stale"])
        (Path(self.temp.name) / "big.bin").write_bytes(b"\0" * (21 * 1024 * 1024))
        store = status_view.build_status_page(self.project)
        self.assertEqual(store["source"], "store")
        self.assertTrue(store["stale"])
        self.assertEqual(store["fingerprint_status"], "unavailable")
        self.assertEqual(declared, {"live", "cached", "store"}, "source 枚举与实现三态不符")
        self.assertEqual(set(self.example["response_enums"]["fingerprint_status"]),
                         {"available", "unavailable"})


class ExistingContractTestsNotRegressed(unittest.TestCase):
    def test_existing_contract_suites_still_pass(self):
        suite = unittest.TestSuite()
        for name in ("test_agent_contracts", "test_design_contract"):
            path = REPO / "tests" / "agents" / f"{name}.py"
            spec = importlib.util.spec_from_file_location(name + "_regression", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        self.assertGreater(result.testsRun, 0, "既有契约测试一个都没跑到")
        self.assertEqual(result.failures, [], "既有契约测试出现失败")
        self.assertEqual(result.errors, [], "既有契约测试出现错误")


if __name__ == "__main__":
    unittest.main()

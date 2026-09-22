"""Versioned handoff artifacts and the joint review gate (C05/Z28尾/Z10尾).

contracts.schemas is the machine line of defense for the three references
contracts: design briefs, api contracts and review-gate records validate against
it, and — the point of the exercise — the REAL fixture files and the JSON
examples inside the reference docs must both pass the kernel validators, so the
doc sample, the fixture and the validator cannot drift apart. The registry binds
subject sha256 + version, a revision invalidates downstream references and
approvals (TK06 前半), and the gate refuses the implementer as sole reviewer.
"""
import copy
import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO / "dev-companion"
FIXTURES = REPO / "tests" / "fixtures" / "design"
DESIGN_FIXTURE = FIXTURES / "board-status-example.json"
API_FIXTURE = FIXTURES / "api-example.json"
DESIGN_DOC = PLUGIN_ROOT / "references" / "design-handoff.md"
API_DOC = PLUGIN_ROOT / "references" / "api-contract.md"

SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import kernel_bootstrap  # noqa: E402 — import 即完成 sys.path 注入，与 core 同款引导
from agents_kernel import digest  # noqa: E402
from agents_kernel.contracts import schemas  # noqa: E402
from agents_kernel.domain.handoff import HandoffRegistry, require_for  # noqa: E402
from agents_kernel.domain.review_gate import ReviewGate  # noqa: E402
from agents_kernel.validation import CompanionError  # noqa: E402

JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
BRIEF_ID = "board-status-v1"
CONTRACT_ID = "status-page-v1"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def paths_of(errors):
    return [error["path"] for error in errors]


class KernelSchemaValidationTests(unittest.TestCase):
    """文档样例与真实 fixture 都必须通过 kernel 校验器（防三方漂移）。"""

    def test_real_fixtures_pass_kernel_validators(self):
        self.assertEqual(schemas.validate_design_brief(load(DESIGN_FIXTURE)), [])
        self.assertEqual(schemas.validate_api_contract(load(API_FIXTURE)), [])

    def test_reference_doc_examples_pass_kernel_validators(self):
        for doc, validator in ((DESIGN_DOC, schemas.validate_design_brief),
                               (API_DOC, schemas.validate_api_contract)):
            with self.subTest(doc=doc.name):
                blocks = [json.loads(match.group(1)) for match in JSON_BLOCK_RE.finditer(
                    doc.read_text(encoding="utf-8"))]
                self.assertTrue(blocks, "%s has no json example" % doc.name)
                for block in blocks:
                    self.assertEqual(validator(block), [],
                                     "%s example drifted from the validator" % doc.name)

    def test_missing_field_reports_structured_path(self):
        brief = load(DESIGN_FIXTURE)
        del brief["brief_id"]
        del brief["non_goals"]
        errors = schemas.validate_design_brief(brief)
        self.assertIn("brief_id", paths_of(errors))
        self.assertIn("non_goals", paths_of(errors))
        for error in errors:
            self.assertIn("reason", error, "结构化错误必须带字段路径与原因")

    def test_bad_version_rejected(self):
        for bad in (0, "1", True, 1.5, None):
            with self.subTest(version=bad):
                brief = load(DESIGN_FIXTURE)
                brief["brief_version"] = bad
                self.assertIn("brief_version", paths_of(schemas.validate_design_brief(brief)))

    def test_non_assertable_acceptance_rejected(self):
        brief = load(DESIGN_FIXTURE)
        brief["screens"][0]["acceptance_criteria"][0] = "界面友好，体验流畅"
        errors = schemas.validate_design_brief(brief)
        self.assertIn("screens[0].acceptance_criteria[0]", paths_of(errors))
        self.assertIn("不可断言", errors[0]["reason"])

    def test_structured_expected_value_counts_as_assertable(self):
        brief = load(DESIGN_FIXTURE)
        brief["screens"][0]["acceptance_criteria"][0] = {"action": "打开 board.html",
                                                         "expected": "出现『静态产物』字样"}
        self.assertEqual(schemas.validate_design_brief(brief), [])

    def test_state_matrix_below_five_states_rejected(self):
        brief = load(DESIGN_FIXTURE)
        del brief["screens"][0]["states"]["partial"]
        errors = schemas.validate_design_brief(brief)
        self.assertIn("screens[0].states.partial", paths_of(errors))
        self.assertIn("五态缺一不可", errors[0]["reason"])

    def test_api_error_codes_stay_inside_closed_enum(self):
        doc = load(API_FIXTURE)
        doc["error_codes"][0] = {"code": "E_MAGIC", "exit_code": 2, "when": "魔法失败"}
        errors = schemas.validate_api_contract(doc)
        self.assertIn("error_codes[0].code", paths_of(errors))
        self.assertIn("闭合枚举", errors[0]["reason"])
        doc["error_codes"][0] = {"code": "E_PARAM", "exit_code": 1, "when": "参数错误"}
        self.assertIn("error_codes[0].exit_code", paths_of(schemas.validate_api_contract(doc)))

    def test_api_idempotency_modes_enforced(self):
        doc = load(API_FIXTURE)
        doc["idempotency"]["mode"] = "retry"
        self.assertIn("idempotency.mode", paths_of(schemas.validate_api_contract(doc)))
        doc["idempotency"] = {"mode": "read_only", "key": "run_id"}
        self.assertIn("idempotency.key", paths_of(schemas.validate_api_contract(doc)))

    def test_api_handoff_form_requires_version_endpoints_migration(self):
        endpoint = {"method": "POST", "path": "/v1/orders",
                    "request_example": "{\"amount\": 1}",
                    "error_codes": [{"code": "E_PARAM", "exit_code": 2, "when": "参数错误"}],
                    "idempotency": {"mode": "idempotency_key", "key": "run_id", "note": ""}}
        doc = {"contract_id": "orders-v1", "contract_version": 2,
               "endpoints": [endpoint], "migration_notes": "新增端点，不破坏既有读法"}
        self.assertEqual(schemas.validate_api_contract(doc), [])
        doc["contract_version"] = 0
        self.assertIn("contract_version", paths_of(schemas.validate_api_contract(doc)))
        doc["contract_version"] = 1
        del doc["migration_notes"]
        self.assertIn("migration_notes", paths_of(schemas.validate_api_contract(doc)))

    def test_review_gate_record_schema(self):
        record = {"gate_id": "gate-1", "subject_type": "design_brief", "subject_id": BRIEF_ID,
                  "subject_sha256": "a" * 64, "verdict": "approved",
                  "reviewer_role": "companion-checker", "blockers": []}
        self.assertEqual(schemas.validate_review_gate(record), [])
        record["verdict"] = "passed"
        self.assertIn("verdict", paths_of(schemas.validate_review_gate(record)))
        record["verdict"] = "approved"
        record["subject_sha256"] = "XYZ"
        self.assertIn("subject_sha256", paths_of(schemas.validate_review_gate(record)))
        record["subject_sha256"] = "a" * 64
        record["blockers"] = ["有点问题"]
        self.assertIn("blockers", paths_of(schemas.validate_review_gate(record)))
        record["verdict"] = "changes_requested"
        record["blockers"] = []
        self.assertIn("blockers", paths_of(schemas.validate_review_gate(record)))


class HandoffRegistryTests(unittest.TestCase):
    def register_fixtures(self, registry):
        registry.register("design_brief", BRIEF_ID, load(DESIGN_FIXTURE), 1)
        registry.register("api_contract", CONTRACT_ID, load(API_FIXTURE), 1)

    def test_real_fixtures_register_with_content_hash(self):
        registry = HandoffRegistry()
        self.register_fixtures(registry)
        entry = registry.get(BRIEF_ID)
        self.assertEqual(entry["sha256"], digest.digest(load(DESIGN_FIXTURE)))
        self.assertEqual(entry["version"], 1)
        self.assertEqual(entry["subject_type"], "design_brief")
        registry.require_current(CONTRACT_ID, digest.digest(load(API_FIXTURE)), "api_contract")

    def test_unknown_or_stale_reference_rejected(self):
        registry = HandoffRegistry()
        with self.assertRaises(CompanionError) as ctx:
            registry.require_current(BRIEF_ID, "a" * 64)
        self.assertIn("未登记", str(ctx.exception))
        self.register_fixtures(registry)
        with self.assertRaises(CompanionError) as ctx:
            registry.require_current(BRIEF_ID, "b" * 64)
        self.assertIn("已变更", str(ctx.exception))
        self.assertIn("引用失效", str(ctx.exception))

    def test_revision_invalidates_downstream_reference(self):
        """契约字段/内容变更（AG03）使引用旧 sha256 的下游任务失效。"""
        registry = HandoffRegistry()
        registry.register("design_brief", BRIEF_ID, load(DESIGN_FIXTURE), 1)
        revised = load(DESIGN_FIXTURE)
        revised["non_goals"].append("不做语音播报")
        registry.register("design_brief", BRIEF_ID, revised, 2)
        with self.assertRaises(CompanionError) as ctx:
            registry.require_current(BRIEF_ID, digest.digest(load(DESIGN_FIXTURE)))
        self.assertIn("引用失效", str(ctx.exception))
        registry.require_current(BRIEF_ID, digest.digest(revised))

    def test_version_must_increase(self):
        registry = HandoffRegistry()
        self.register_fixtures(registry)
        with self.assertRaises(CompanionError) as ctx:
            registry.register("design_brief", BRIEF_ID, load(DESIGN_FIXTURE), 1)
        self.assertIn("版本必须递增", str(ctx.exception))
        with self.assertRaises(CompanionError):
            registry.register("design_brief", BRIEF_ID, load(DESIGN_FIXTURE), 0)
        with self.assertRaises(CompanionError):
            registry.register("design_brief", BRIEF_ID, load(DESIGN_FIXTURE), True)

    def test_vague_content_refused_with_structured_errors(self):
        registry = HandoffRegistry()
        vague = load(DESIGN_FIXTURE)
        vague["screens"][0]["acceptance_criteria"] = ["界面友好"]
        with self.assertRaises(CompanionError) as ctx:
            registry.register("design_brief", BRIEF_ID, vague, 1)
        self.assertIn("不可断言", str(ctx.exception))

    def test_require_for_and_missing_for(self):
        self.assertEqual(require_for("implement"), ("design_brief", "api_contract"))
        self.assertEqual(require_for("check"), ("design_brief", "api_contract"))
        with self.assertRaises(CompanionError):
            require_for("deploy")
        registry = HandoffRegistry()
        self.assertEqual(registry.missing_for("implement"), ("design_brief", "api_contract"))
        registry.register("api_contract", CONTRACT_ID, load(API_FIXTURE), 1)
        self.assertEqual(registry.missing_for("implement"), ("design_brief",))

    def test_upstream_reference_checked_at_registration(self):
        registry = HandoffRegistry()
        registry.register("design_brief", BRIEF_ID, load(DESIGN_FIXTURE), 1)
        brief_sha = digest.digest(load(DESIGN_FIXTURE))
        registry.register("api_contract", CONTRACT_ID, load(API_FIXTURE), 1,
                          upstream=[{"subject_id": BRIEF_ID, "subject_sha256": brief_sha}])
        self.assertEqual(registry.get(CONTRACT_ID)["upstream"],
                         [{"subject_id": BRIEF_ID, "subject_sha256": brief_sha}])
        revised = load(DESIGN_FIXTURE)
        revised["non_goals"].append("不做打印布局")
        registry.register("design_brief", BRIEF_ID, revised, 2)
        with self.assertRaises(CompanionError) as ctx:
            registry.register("api_contract", "orders-v1", load(API_FIXTURE), 1,
                              upstream=[{"subject_id": BRIEF_ID, "subject_sha256": brief_sha}])
        self.assertIn("引用失效", str(ctx.exception))


class ReviewGateTests(unittest.TestCase):
    IMPLEMENTER = "companion-developer"
    REVIEWER = "companion-checker"

    def setUp(self):
        self.registry = HandoffRegistry()
        self.registry.register("design_brief", BRIEF_ID, load(DESIGN_FIXTURE), 1)
        self.brief_sha = digest.digest(load(DESIGN_FIXTURE))
        self.gate = ReviewGate(self.registry)

    def approve(self, gate_id="gate-brief-1", sha=None, reviewer=REVIEWER):
        return self.gate.submit(gate_id, "design_brief", BRIEF_ID,
                                sha or self.brief_sha, "approved",
                                reviewer, implementer_role=self.IMPLEMENTER)

    def test_implementer_cannot_be_sole_reviewer(self):
        with self.assertRaises(CompanionError) as ctx:
            self.approve(reviewer=self.IMPLEMENTER)
        self.assertIn("实现者不得担任唯一独立审查者", str(ctx.exception))
        self.assertEqual(self.gate.verdicts(BRIEF_ID), [], "被拒记录不得落账")

    def test_approved_binds_subject_sha256(self):
        record = self.approve()
        self.assertEqual(record["verdict"], "approved")
        self.assertEqual(record["subject_sha256"], self.brief_sha)
        self.assertTrue(self.gate.is_approved(BRIEF_ID))

    def test_revision_invalidates_approval_until_rereviewed(self):
        """审查后修改使原批准失效；重新登记 + 独立重审后再次通过（TK06）。"""
        self.approve()
        revised = load(DESIGN_FIXTURE)
        revised["non_goals"].append("不做移动端专属布局")
        self.registry.register("design_brief", BRIEF_ID, revised, 2)
        self.assertFalse(self.gate.is_approved(BRIEF_ID), "审后修改必须使批准失效")
        with self.assertRaises(CompanionError):
            self.approve(gate_id="gate-brief-stale", sha=self.brief_sha)
        self.approve(gate_id="gate-brief-2", sha=digest.digest(revised))
        self.assertTrue(self.gate.is_approved(BRIEF_ID))

    def test_changes_requested_records_blockers(self):
        blockers = ["验收条不可断言：第 1 条缺可核验措辞"]
        record = self.gate.submit("gate-brief-1", "design_brief", BRIEF_ID, self.brief_sha,
                                  "changes_requested", self.REVIEWER, blockers,
                                  implementer_role=self.IMPLEMENTER)
        self.assertEqual(record["blockers"], blockers)
        self.assertFalse(self.gate.is_approved(BRIEF_ID))

    def test_duplicate_gate_id_refused(self):
        self.approve()
        with self.assertRaises(CompanionError) as ctx:
            self.approve()
        self.assertIn("已存在", str(ctx.exception))

    def test_subject_type_mismatch_refused(self):
        self.registry.register("api_contract", CONTRACT_ID, load(API_FIXTURE), 1)
        with self.assertRaises(CompanionError) as ctx:
            self.gate.submit("gate-1", "api_contract", BRIEF_ID, self.brief_sha,
                             "approved", self.REVIEWER, implementer_role=self.IMPLEMENTER)
        self.assertIn("类型不符", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

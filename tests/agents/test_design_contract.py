"""Design handoff contract (C03): the companion-design role stays isomorphic
with the existing roles, every reference it cites exists, the sample brief
passes a minimal schema (five-state matrix, assertable acceptance criteria),
and the doc example cannot drift from the machine-readable fixture."""
import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO / "dev-companion"
ROLE_FILE = PLUGIN_ROOT / "agents" / "companion-design.md"
HANDOFF_DOC = PLUGIN_ROOT / "references" / "design-handoff.md"
FIXTURE = REPO / "tests" / "fixtures" / "design" / "board-status-example.json"

REQUIRED_FRONTMATTER_KEYS = {"name", "description"}
REFERENCE_RE = re.compile(r"references/[A-Za-z0-9_-]+\.md")
JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
FIVE_STATES = ("loading", "empty", "error", "partial", "success")
SCREEN_FIELDS = ("screen_id", "title", "user_flow", "states", "components",
                 "responsive", "accessibility", "acceptance_criteria")
REPORT_BLOCK_KEYS = ("feature_id", "run_id", "summary", "design_brief", "open_questions", "blocker")
# A design acceptance criterion is assertable only when it states an observable
# expectation with a concrete check wording; vague praise ("界面友好") is rejected.
ASSERTABLE_RE = re.compile(r"出现|不出现|等于|不等于|包含|不包含|可见|隐藏|不低于|不超过|至少")


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


class DesignRoleContractTests(unittest.TestCase):
    def load_role(self):
        self.assertTrue(ROLE_FILE.is_file(), f"missing role file: {ROLE_FILE}")
        return ROLE_FILE.read_text(encoding="utf-8")

    def test_frontmatter_isomorphic_with_existing_roles(self):
        fields = frontmatter(self.load_role())
        self.assertIsNotNone(fields, "companion-design: frontmatter block not found")
        self.assertEqual(set(fields), REQUIRED_FRONTMATTER_KEYS)
        self.assertEqual(fields["name"], "companion-design")
        self.assertGreater(len(fields["description"]), 10)
        for other in ("companion-developer", "companion-checker", "companion-product"):
            other_text = (PLUGIN_ROOT / "agents" / f"{other}.md").read_text(encoding="utf-8")
            self.assertEqual(set(fields), set(frontmatter(other_text) or {}),
                             f"frontmatter keys diverge from {other}")

    def test_cited_references_exist(self):
        for reference in REFERENCE_RE.findall(self.load_role()):
            self.assertTrue((PLUGIN_ROOT / reference).is_file(),
                            f"companion-design: referenced doc missing: {reference}")

    def test_role_has_five_states_red_lines_and_json_report_block(self):
        text = self.load_role()
        for state in FIVE_STATES:
            self.assertIn(state, text, f"role file missing state: {state}")
        for red_line in ("不写实现代码", "不替用户拍板", "不再派发子智能体"):
            self.assertIn(red_line, text, f"role file missing red line: {red_line}")
        match = JSON_BLOCK_RE.search(text)
        self.assertIsNotNone(match, "companion-design: json report block missing")
        block = json.loads(match.group(1))
        self.assertEqual(set(block), set(REPORT_BLOCK_KEYS),
                         "report block keys diverge from existing roles' shape")
        screens = block["design_brief"]["screens"]
        self.assertTrue(screens)
        for key in ("screen_id", "states_covered", "acceptance_criteria"):
            self.assertIn(key, screens[0], f"report screen missing key: {key}")


class DesignBriefSchemaTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(FIXTURE.is_file(), f"missing fixture: {FIXTURE}")
        self.brief = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_required_top_level_fields(self):
        for key in ("brief_id", "feature_id", "brief_version", "based_on", "screens", "non_goals"):
            self.assertIn(key, self.brief, f"brief missing field: {key}")
        self.assertTrue(self.brief["screens"], "screens must not be empty")
        self.assertTrue(self.brief["non_goals"], "non_goals must not be empty")

    def test_every_screen_has_five_state_matrix(self):
        for screen in self.brief["screens"]:
            with self.subTest(screen=screen["screen_id"]):
                for key in SCREEN_FIELDS:
                    self.assertIn(key, screen, f"screen missing field: {key}")
                for state in FIVE_STATES:
                    self.assertIn(state, screen["states"], f"state matrix missing: {state}")
                    entry = screen["states"][state]
                    self.assertTrue(entry.get("data"), f"{state}: data shape empty")
                    self.assertTrue(entry.get("copy"), f"{state}: copy empty")
                    self.assertTrue(entry.get("shown"), f"{state}: shown components empty")
                self.assertTrue(screen["components"], "components must not be empty")
                for component in screen["components"]:
                    self.assertTrue(component.get("name") and component.get("reuse"),
                                    "component must map to an existing vocabulary entry")

    def test_acceptance_criteria_are_assertable(self):
        for screen in self.brief["screens"]:
            criteria = screen["acceptance_criteria"]
            self.assertTrue(criteria, f"{screen['screen_id']}: acceptance criteria empty")
            for criterion in criteria:
                self.assertIsInstance(criterion, str)
                self.assertTrue(criterion.strip(), "acceptance criterion is blank")
                self.assertRegex(criterion, ASSERTABLE_RE,
                                 f"criterion not assertable: {criterion}")

    def test_handoff_doc_example_matches_fixture(self):
        text = HANDOFF_DOC.read_text(encoding="utf-8")
        blocks = [json.loads(match.group(1)) for match in JSON_BLOCK_RE.finditer(text)]
        self.assertTrue(blocks, "design-handoff.md has no json example")
        self.assertIn(self.brief, blocks,
                      "design-handoff.md example drifted from tests/fixtures sample")


if __name__ == "__main__":
    unittest.main()

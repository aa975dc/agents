"""Agent role contracts: frontmatter stays isomorphic across roles, referenced
contract docs under references/ exist, and substantive duty/red-line markers
remain searchable (empty shells fail). The same invariants cover the two
existing roles so they cannot regress while new roles are added."""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO / "dev-companion"
AGENTS_DIR = PLUGIN_ROOT / "agents"

EXISTING_ROLES = ("companion-developer", "companion-checker")
NEW_ROLES = ("companion-product", "companion-design")
NEW_ROLE = "companion-product"
ALL_ROLES = EXISTING_ROLES + NEW_ROLES

REQUIRED_FRONTMATTER_KEYS = {"name", "description"}

REFERENCE_RE = re.compile(r"references/[A-Za-z0-9_-]+\.md")

# Substantive markers: duties and red lines that must stay searchable so a
# hollow file with the right frontmatter cannot pass for a real role.
ROLE_MARKERS = {
    "companion-developer": ("allowed_paths", "changed_files", "不再派发子智能体"),
    "companion-checker": ("独立检查者", "未验证", "不再派发子智能体"),
    "companion-product": (
        "investigation_required",
        "验收标准",
        "非目标",
        "用户故事",
        "不再派发子智能体",
        "references/product-inputs.md",
    ),
    "companion-design": (
        "状态矩阵",
        "组件清单",
        "不写实现代码",
        "不再派发子智能体",
        "references/design-handoff.md",
    ),
}

PRODUCT_INPUTS_CONTRACT = PLUGIN_ROOT / "references" / "product-inputs.md"
PRODUCT_INPUTS_SECTIONS = ("用户故事", "验收标准", "非目标", "调查项", "investigation_required")


def role_path(name):
    return AGENTS_DIR / f"{name}.md"


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


class AgentContractTests(unittest.TestCase):
    def assertRoleContract(self, name):
        path = role_path(name)
        self.assertTrue(path.is_file(), f"missing role file: {path}")
        text = path.read_text(encoding="utf-8")
        fields = frontmatter(text)
        self.assertIsNotNone(fields, f"{name}: frontmatter block not found")
        self.assertEqual(set(fields), REQUIRED_FRONTMATTER_KEYS,
                         f"{name}: frontmatter keys diverge from existing roles")
        self.assertEqual(fields["name"], name, f"{name}: frontmatter name mismatch")
        self.assertGreater(len(fields["description"]), 10, f"{name}: empty description")
        for reference in REFERENCE_RE.findall(text):
            self.assertTrue((PLUGIN_ROOT / reference).is_file(),
                            f"{name}: referenced contract doc missing: {reference}")
        for marker in ROLE_MARKERS[name]:
            self.assertIn(marker, text, f"{name}: substantive marker missing: {marker}")
        return text

    def test_all_role_files_present(self):
        for name in ALL_ROLES:
            self.assertTrue(role_path(name).is_file(), f"missing role file for {name}")

    def test_frontmatter_isomorphic_and_references_resolved(self):
        for name in ALL_ROLES:
            with self.subTest(role=name):
                self.assertRoleContract(name)

    def test_product_role_declares_investigation_red_lines(self):
        text = self.assertRoleContract(NEW_ROLE)
        self.assertIn("不虚构 API", text)
        self.assertIn("不替用户拍板", text)
        self.assertRegex(text, r"confirmed \| refuted \| investigation_required")

    def test_existing_role_invariants_not_regressed(self):
        for name in EXISTING_ROLES:
            with self.subTest(role=name):
                self.assertRoleContract(name)

    def test_product_inputs_contract_is_real_and_complete(self):
        self.assertTrue(PRODUCT_INPUTS_CONTRACT.is_file())
        text = PRODUCT_INPUTS_CONTRACT.read_text(encoding="utf-8")
        for section in PRODUCT_INPUTS_SECTIONS:
            self.assertIn(section, text, f"product-inputs.md missing section: {section}")


if __name__ == "__main__":
    unittest.main()

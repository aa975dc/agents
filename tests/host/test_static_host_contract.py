"""静态宿主契约：插件清单与磁盘闭合、五角色 frontmatter 与引用闭合、
命令/技能中的角色引用不悬空、轻量模式文本自洽。

只做静态检查。新角色在真实宿主的动态加载（发现、按名派发、全文转交，
AG01/AG05/H03 动态半）显式 NOT_RUN：宿主缓存仍是插件 0.2.0，新角色的
真实加载必须等待插件安装/升级授权，授权前不得宣称动态验证已通过。
"""
import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO / "dev-companion"
AGENTS_DIR = PLUGIN_ROOT / "agents"
COMMANDS_DIR = PLUGIN_ROOT / "commands"
SKILL_PATH = PLUGIN_ROOT / "skills" / "dev-companion" / "SKILL.md"
PLUGIN_MANIFEST = PLUGIN_ROOT / ".zcode-plugin" / "plugin.json"

ROLES = (
    "companion-developer",
    "companion-checker",
    "companion-product",
    "companion-design",
    "companion-backend",
)
NEW_ROLES = ("companion-product", "companion-design", "companion-backend")
COMMAND_STEMS = (
    "companion-archive",
    "companion-check",
    "companion-progress",
    "companion-release",
    "companion-resume",
    "companion-start",
    "companion-work",
)

REQUIRED_FRONTMATTER_KEYS = {"name", "description"}
REFERENCE_RE = re.compile(r"references/[A-Za-z0-9_-]+\.md")
COMPANION_NAME_RE = re.compile(r"\bcompanion-[a-z0-9-]+\b")

# 新角色宿主动态加载验证状态（AG01/AG05/H03 动态半）。完成真实宿主验证后须
# 连同宿主证据一起改写；在授权完成前改成任何“已验证”值都属伪造。
DYNAMIC_HOST_LOAD_STATUS = "NOT_RUN"

ROLE_CONTRACT_FILES = {
    "companion-product": "references/product-inputs.md",
    "companion-design": "references/design-handoff.md",
    "companion-backend": "references/api-contract.md",
}


def read(path):
    return path.read_text(encoding="utf-8")


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


class StaticHostContractTests(unittest.TestCase):
    def test_manifest_declared_dirs_close_over_disk(self):
        """清单声明的 agents/commands/skills 目录与磁盘闭合：新增角色文件
        被覆盖，孤儿文件（未列入期望集合）也会暴露。"""
        manifest = json.loads(read(PLUGIN_MANIFEST))
        self.assertEqual(manifest.get("name"), "dev-companion")
        expectations = {
            "agents": set(ROLES),
            "commands": set(COMMAND_STEMS),
            "skills": {"dev-companion"},
        }
        for key, expected in expectations.items():
            with self.subTest(dir=key):
                self.assertIn(key, manifest, f"manifest 未声明 {key} 目录")
                dir_path = PLUGIN_ROOT / manifest[key]
                self.assertTrue(dir_path.is_dir(), f"{key} 目录不存在: {dir_path}")
                if key == "skills":
                    on_disk = {p.name for p in dir_path.iterdir() if p.is_dir()}
                else:
                    on_disk = {p.stem for p in dir_path.glob("*.md")}
                self.assertEqual(
                    on_disk, expected,
                    f"{key}：清单目录与磁盘不闭合（磁盘 {sorted(on_disk)} vs 期望 {sorted(expected)}）")

    def test_five_roles_frontmatter_and_references_exist(self):
        for role in ROLES:
            with self.subTest(role=role):
                path = AGENTS_DIR / f"{role}.md"
                self.assertTrue(path.is_file(), f"missing role file: {path}")
                fields = frontmatter(read(path))
                self.assertIsNotNone(fields, f"{role}: frontmatter 缺失")
                self.assertTrue(REQUIRED_FRONTMATTER_KEYS <= set(fields),
                                f"{role}: frontmatter 缺必要字段")
                self.assertEqual(fields.get("name"), role, f"{role}: name 不一致")
                self.assertTrue(fields.get("description"), f"{role}: description 为空")
                for reference in REFERENCE_RE.findall(read(path)):
                    self.assertTrue((PLUGIN_ROOT / reference).is_file(),
                                    f"{role}: 引用的契约文件不存在: {reference}")

    def test_role_names_referenced_in_commands_and_skill_do_not_dangle(self):
        """命令与 SKILL 中出现的每个 companion-* 名字，要么是既有命令入口，
        要么必须有对应 agents/*.md，防止悬空派发引用。"""
        command_names = set(COMMAND_STEMS)
        agent_names = set(ROLES)
        texts = [(stem + ".md", read(COMMANDS_DIR / f"{stem}.md")) for stem in COMMAND_STEMS]
        texts.append(("SKILL.md", read(SKILL_PATH)))
        for source, text in texts:
            for name in set(COMPANION_NAME_RE.findall(text)):
                if name in command_names:
                    continue
                self.assertIn(name, agent_names,
                              f"{source}: 悬空角色引用 {name}（agents/ 无对应文件）")

    def test_wiring_new_roles_referenced_by_start_work_and_skill(self):
        skill = read(SKILL_PATH)
        for role in NEW_ROLES:
            with self.subTest(role=role):
                self.assertIn(role, read(COMMANDS_DIR / "companion-start.md"),
                              "companion-start 未引用该角色")
                self.assertIn(role, read(COMMANDS_DIR / "companion-work.md"),
                              "companion-work 未引用该角色")
                self.assertIn(role, skill, "SKILL.md 角色路由表未覆盖该角色")
                self.assertIn(ROLE_CONTRACT_FILES[role], skill,
                              "SKILL.md 路由行缺产出契约文件")
        for marker in ("design-handoff.md", "api-contract.md"):
            self.assertIn(marker, read(COMMANDS_DIR / "companion-start.md"))
            self.assertIn(marker, read(COMMANDS_DIR / "companion-work.md"))

    def test_check_command_verifies_versioned_design_and_contract(self):
        """TK06：版本化产物按 subject_sha256 核对当前版本，失效即报告。"""
        check = read(COMMANDS_DIR / "companion-check.md")
        for marker in ("subject_sha256", "自动失效", "不通过旧版本验收",
                       "不由产出者自审", "review_gate"):
            self.assertIn(marker, check, f"companion-check 缺版本核对语义: {marker}")

    def test_lightweight_mode_preserved(self):
        """AG05：developer＋独立检查者的旧两角色流程完全不受影响，
        新角色全部按需引用，无强制派发。"""
        skill = read(SKILL_PATH)
        self.assertIn("旧两角色流程完全不受影响", skill,
                      "SKILL.md 缺轻量模式保留的明文承诺")
        start = read(COMMANDS_DIR / "companion-start.md")
        work = read(COMMANDS_DIR / "companion-work.md")
        check = read(COMMANDS_DIR / "companion-check.md")
        for source, text in (("SKILL.md", skill), ("start", start), ("work", work)):
            self.assertIn("按需", text, f"{source}: 新角色引用缺“按需”限定")
        for source, text in (("start", start), ("work", work)):
            self.assertIn("不派发", text, f"{source}: 缺“无相应需求不派发”的非强制表述")
        # 旧两角色主链路仍在：developer 派发与独立检查引用不被移除
        self.assertIn("companion-developer", work)
        self.assertIn("companion-checker", work)
        self.assertIn("companion-checker", check)
        # check 的新增核对是条件式（“存在…时”），不向旧流程强制注入新角色
        self.assertIn("存在版本化", check)

    def test_dynamic_host_load_is_explicit_not_run(self):
        """新角色宿主动态加载显式 NOT_RUN：等待插件安装/升级授权。
        本断言防止把静态结果宣称成动态通过；真实宿主验证完成后连同证据改写。"""
        self.assertEqual(
            DYNAMIC_HOST_LOAD_STATUS, "NOT_RUN",
            "宿主缓存仍为插件 0.2.0，新角色动态加载未经授权验证；"
            "禁止在无真实宿主证据时把本状态改成已验证")


if __name__ == "__main__":
    unittest.main()

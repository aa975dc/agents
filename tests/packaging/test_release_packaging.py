# -*- coding: utf-8 -*-
"""P7-05 发布打包验证（PK01/PK02/PK06 + 版本单源）。

- 版本门禁：marketplace.json 与两份 plugin.json 三处一致，且等于本文件
  EXPECTED_VERSION（0.3.0）。发版时三处 + 此常量需有意同步改动——断言钉死
  具体版本是刻意的发布闸，防止"只改两处"静默漂移（tools/build_vendor.py 与
  CI packaging 作业只校三处互等，不校具体值，本测试补上具体值一档）。
- 独立产物验证：两插件目录复制到仓库外（tempfile）后各自成立：
  * plugin.json 可解析，manifest 声明的命令/角色/技能目录与磁盘闭合，
    每个 .md 非空且带 name/description frontmatter（简化版 static host
    contract 检查，tests/host/test_static_host_contract.py 为 dev-companion
    的完整版）；
  * dev-companion：_kernel_vendor 存在，MANIFEST.json 与副本逐文件
    sha256/字节数自洽（只对副本自检，不回查仓库源——源一致性由
    tests/packaging/test_vendor.py 负责）；
  * code-analysis-swarm：无 agents_kernel 依赖 → 无需 vendor（与
    tools/build_vendor.py 的 uses_kernel 判定同口径，.py 全文扫描）。

只做静态与文件级检查；真实宿主对 0.3.0 安装副本的发现与加载属升级安装
授权范围，显式 NOT_RUN（见 tests/host/test_static_host_contract.py 同款红线）。
"""
import hashlib
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

EXPECTED_VERSION = "0.3.0"
PLUGIN_NAMES = ("dev-companion", "code-analysis-swarm")

# 每插件应随包发布的清单文件（发布事实，与 manifest 声明闭合对照）。
# frontmatter 要求：命令文件名即命令名，只强制 description；角色与技能强制 name+description。
EXPECTED_FILES = {
    "dev-companion": {
        "commands": {f"companion-{n}.md" for n in
                     ("archive", "check", "progress", "release", "resume", "start", "work")},
        "agents": {f"companion-{n}.md" for n in
                   ("developer", "checker", "product", "design", "backend")},
        "skills": {"dev-companion/SKILL.md"},
    },
    "code-analysis-swarm": {
        "commands": {"swarm-analyze.md"},
        "agents": {f"a{i}-{n}.md" for i, n in enumerate(
            ("scout", "module-analyst", "architect", "dependency", "build", "verifier", "reporter"),
            start=1)},
    },
}
REQUIRED_FRONTMATTER = {
    "commands": ("description",),
    "agents": ("name", "description"),
    "skills": ("name", "description"),
}

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def has_frontmatter_fields(text, required=("name", "description")):
    match = FRONTMATTER_RE.match(text)
    if not match:
        return False
    keys = {line.partition(":")[0].strip() for line in match.group(1).splitlines()}
    return all(k in keys for k in required)


class ReleaseVersionTests(unittest.TestCase):
    """三处版本单源：发版闸，钉死 EXPECTED_VERSION。"""

    def test_marketplace_and_both_plugin_json_pin_expected_version(self):
        marketplace = read_json(REPO / "marketplace.json")
        entries = {p["name"]: p for p in marketplace["plugins"]}
        for name in PLUGIN_NAMES:
            self.assertIn(name, entries, "marketplace 缺少条目 " + name)
            entry = entries[name]
            self.assertEqual(entry["version"], EXPECTED_VERSION,
                             "%s: marketplace.json 版本未随发版更新" % name)
            source = REPO / entry["source"].lstrip("./")
            self.assertEqual(source, REPO / name, "source 路径与插件目录不符")
            manifest = read_json(source / ".zcode-plugin" / "plugin.json")
            self.assertEqual(manifest["name"], name)
            self.assertEqual(manifest["version"], EXPECTED_VERSION,
                             "%s: plugin.json 版本未随发版更新" % name)


class StandalonePackageTests(unittest.TestCase):
    """插件目录复制到仓库外后独立成立（安装产物只有插件目录）。"""

    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._temp.cleanup)
        cls.sandbox = Path(cls._temp.name)
        cls.copies = {}
        for name in PLUGIN_NAMES:
            copy = cls.sandbox / name
            shutil.copytree(REPO / name, copy,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            cls.copies[name] = copy

    def _manifest(self, name):
        return read_json(self.copies[name] / ".zcode-plugin" / "plugin.json")

    def test_plugin_json_parses_and_version_travels_with_copy(self):
        for name in PLUGIN_NAMES:
            manifest = self._manifest(name)
            self.assertEqual(manifest["name"], name)
            self.assertEqual(manifest["version"], EXPECTED_VERSION,
                             name + ": 副本 manifest 版本不符（复制源过期？）")

    def test_manifest_declared_dirs_close_over_copied_files(self):
        """声明目录存在、期望文件齐全，且目录内每个 .md 非空带 name/description。"""
        for name in PLUGIN_NAMES:
            manifest = self._manifest(name)
            copy = self.copies[name]
            for field, expected in EXPECTED_FILES[name].items():
                dir_name = manifest.get(field)
                self.assertTrue(dir_name, "%s: manifest 未声明 %s 目录" % (name, field))
                base = copy / dir_name
                self.assertTrue(base.is_dir(), "%s: 副本缺 %s/" % (name, dir_name))
                actual = {p.relative_to(base).as_posix() for p in base.rglob("*")
                          if p.is_file() and p.suffix == ".md"}
                self.assertEqual(actual, expected,
                                 "%s: %s/ 清单文件与发布期望不符" % (name, dir_name))
                for rel in actual:
                    text = (base / rel).read_text(encoding="utf-8")
                    self.assertTrue(text.strip(), "%s: %s 为空文件" % (name, rel))
                    self.assertTrue(has_frontmatter_fields(text, REQUIRED_FRONTMATTER[field]),
                                    "%s: %s 缺 frontmatter %s" % (name, rel, REQUIRED_FRONTMATTER[field]))

    def test_dev_companion_vendor_manifest_self_consistent_in_copy(self):
        copy = self.copies["dev-companion"]
        vendor = copy / "scripts" / "_kernel_vendor"
        self.assertTrue((vendor / "agents_kernel").is_dir(),
                        "dev-companion 副本缺 vendor 内核目录")
        manifest = read_json(vendor / "MANIFEST.json")
        self.assertEqual(manifest["manifest_version"], 1)
        self.assertEqual(manifest["target"]["plugin"], "dev-companion")
        self.assertTrue(manifest["source"]["commit"], "MANIFEST 应记录源 commit")
        listed = {entry["path"] for entry in manifest["files"]}
        actual = {p.relative_to(vendor).as_posix() for p in vendor.rglob("*")
                  if p.is_file() and p.name != "MANIFEST.json"}
        self.assertEqual(listed, actual, "MANIFEST 清单与 vendor 目录不闭合")
        for entry in manifest["files"]:
            data = (vendor / entry["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"], entry["path"])
            self.assertEqual(len(data), entry["bytes"], entry["path"])

    def test_swarm_needs_no_vendor_and_has_no_kernel_dependency(self):
        """swarm 无 agents_kernel 依赖：不打包 vendor，也不应出现运行时引用。"""
        copy = self.copies["code-analysis-swarm"]
        self.assertFalse((copy / "scripts" / "_kernel_vendor").exists(),
                         "swarm 不应携带 vendor 副本")
        offenders = [str(py.relative_to(copy)) for py in copy.rglob("*.py")
                     if "agents_kernel" in py.read_text(encoding="utf-8")]
        self.assertEqual(offenders, [], "swarm .py 出现 agents_kernel 引用，需重估 vendor 策略")


if __name__ == "__main__":
    unittest.main()

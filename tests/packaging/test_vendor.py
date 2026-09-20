"""P2-05 vendor 分发测试：插件目录仓外独立运行、MANIFEST 校验、幂等、版本门禁。

构建入口 tools/build_vendor.py 以子进程运行（与 CI/用户同一调用方式）；
"仓外独立运行"把 dev-companion 复制进 tempfile，从那里引导 scripts 模块，
断言内核解析到 _kernel_vendor 而非仓库 packages/。

SR-07 证据口径：Git checkout 构建验证与无 .git archive 复核拆成两组，互不冒充——
- VendorBuildTests：Git checkout 形态（仓库含 .git），commit 断言真实 HEAD SHA；
  archive 形态下整组 skip（如实注明）。
- ArchiveFormVendorTests：在临时去 .git 的源码树副本上真实构建，断言显式
  "archive:<标记>" + provenance=archive_no_git，逐文件哈希仍然校验；同时验证
  工具拒绝 SHA 形态的 AGENTS_SOURCE_COMMIT（无 .git 时不可验证，禁止伪造）。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / "tools" / "build_vendor.py"
PLUGIN = "dev-companion"
ARCHIVE_ENV = "AGENTS_SOURCE_COMMIT"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ARCHIVE_MARKER = "archive:simulated-test-copy"
ARCHIVE_PROVENANCE = "archive_no_git"


def in_git_checkout():
    """当前仓库是否 Git checkout 形态（archive 解包形态无 .git）。"""
    return (REPO / ".git").exists()


def run_build(root, env_extra=None):
    env = dict(os.environ)
    env.pop(ARCHIVE_ENV, None)          # 缺省剥离，Git checkout 不受外部变量影响
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(BUILD), "--root", str(root)],
                          capture_output=True, text=True, env=env)


def tree_hashes(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def verify_manifest_integrity(manifest, vendor_dir, source_root):
    """与形态无关的完整性口径：清单闭合 + 逐文件 sha256/bytes 与副本和源一致。"""
    listed = {entry["path"] for entry in manifest["files"]}
    actual = {path.relative_to(vendor_dir).as_posix() for path in vendor_dir.rglob("*")
              if path.is_file() and path.name != "MANIFEST.json"}
    assert listed == actual, "MANIFEST 清单与 vendor 目录不闭合"
    for entry in manifest["files"]:
        data = (vendor_dir / entry["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], entry["path"]
        assert len(data) == entry["bytes"], entry["path"]
        source = source_root / "packages" / entry["path"]
        assert hashlib.sha256(source.read_bytes()).hexdigest() == entry["sha256"], \
            entry["path"] + " 副本与源不一致"


CHILD = '''
import json, sys
from pathlib import Path

repo = Path(sys.argv[3])
sys.path.insert(0, str(Path(sys.argv[1]) / "scripts"))
import kernel_bootstrap
assert kernel_bootstrap.ensure_kernel() == "vendor"
assert not [p for p in sys.path if p and str(p).startswith(str(repo))], sys.path

import agents_kernel
vendor = (Path(sys.argv[1]) / "scripts" / "_kernel_vendor").resolve()
assert str(Path(agents_kernel.__file__).resolve()).startswith(str(vendor)), agents_kernel.__file__

from agents_kernel.atomicio import read_json, write_json
from agents_kernel.digest import digest

target = Path(sys.argv[2]) / "state.json"
write_json(target, {"title": "仓外", "features": []})
loaded = read_json(target)
assert loaded["title"] == "仓外"
assert digest(loaded) == digest({"title": "仓外", "features": []})
print("OK " + json.dumps({"kernel": str(agents_kernel.__file__)}))
'''


class VendorBuildTests(unittest.TestCase):
    """Git checkout 形态构建验证（原口径）。archive 形态（源码包无 .git）下本组
    整组 skip 并注明，由 ArchiveFormVendorTests 覆盖同一构建入口。"""

    @classmethod
    def setUpClass(cls):
        if not in_git_checkout():
            raise unittest.SkipTest(
                "archive 形态（无 .git）：Git checkout 构建断言（真实 HEAD SHA）不适用，"
                "archive 口径见 ArchiveFormVendorTests")
        result = run_build(REPO)
        cls.build_output = result.stdout + result.stderr
        if result.returncode != 0:
            raise AssertionError("构建失败：\n" + cls.build_output)
        cls.vendor = REPO / PLUGIN / "scripts" / "_kernel_vendor"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sandbox = Path(self.temp.name)

    def test_plugin_copy_works_outside_repo_and_bootstraps_vendor(self):
        """插件目录复制到仓库外后独立运行：内核来自 _kernel_vendor，仓库路径不可见。"""
        copy = self.sandbox / PLUGIN
        shutil.copytree(REPO / PLUGIN, copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        child = self.sandbox / "child.py"
        child.write_text(CHILD, encoding="utf-8")
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, str(child), str(copy), str(self.sandbox), str(REPO)],
                                capture_output=True, text=True, cwd=str(self.sandbox), env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout.split("OK ", 1)[1])
        self.assertIn("_kernel_vendor", payload["kernel"])
        self.assertNotIn("packages", payload["kernel"])

    def test_manifest_matches_vendored_and_source_files(self):
        manifest = json.loads((self.vendor / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], 1)
        self.assertEqual(manifest["target"]["plugin"], PLUGIN)
        self.assertEqual(manifest["target"]["vendor_dir"], PLUGIN + "/scripts/_kernel_vendor")
        self.assertRegex(manifest["source"]["commit"], SHA_RE,
                         "Git checkout 形态应记录真实 HEAD SHA（git rev-parse）")
        self.assertNotIn("provenance", manifest["source"],
                         "Git checkout 形态不得带 archive 标记")
        self.assertTrue(manifest["generated_at"])
        verify_manifest_integrity(manifest, self.vendor, REPO)

    def test_rerun_is_idempotent(self):
        before_files = tree_hashes(self.vendor / "agents_kernel")
        before_manifest = json.loads((self.vendor / "MANIFEST.json").read_text(encoding="utf-8"))
        result = run_build(REPO)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("与源一致，无变更", result.stdout)
        self.assertEqual(tree_hashes(self.vendor / "agents_kernel"), before_files)
        after_manifest = json.loads((self.vendor / "MANIFEST.json").read_text(encoding="utf-8"))
        del before_manifest["generated_at"], after_manifest["generated_at"]
        self.assertEqual(after_manifest, before_manifest)

    def test_drift_is_overwritten_and_stale_removed(self):
        target = self.vendor / "agents_kernel" / "digest.py"
        target.write_text("# 人手改动\n", encoding="utf-8")
        stale = self.vendor / "agents_kernel" / "stale.py"
        stale.write_text("stale = True\n", encoding="utf-8")
        result = run_build(REPO)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("agents_kernel/digest.py", result.stdout)
        self.assertIn("agents_kernel/stale.py", result.stdout)
        self.assertFalse(stale.exists())
        self.assertEqual(target.read_bytes(),
                         (REPO / "packages" / "agents_kernel" / "digest.py").read_bytes())

    def test_swarm_kernel_dependency_gets_vendor(self):
        """SR-06：precheck.py 的 index/g2/resume/coverage 子命令引导 agents_kernel，
        uses_kernel 判定命中即自动生成 swarm vendor（既有规则补触发，同 dev-companion）。"""
        self.assertIn("code-analysis-swarm: vendor 就绪", self.build_output)
        swarm_vendor = REPO / "code-analysis-swarm" / "scripts" / "_kernel_vendor"
        self.assertTrue((swarm_vendor / "agents_kernel").is_dir())
        manifest = json.loads((swarm_vendor / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["target"]["plugin"], "code-analysis-swarm")
        verify_manifest_integrity(manifest, swarm_vendor, REPO)

    def test_version_mismatch_rejects_build(self):
        root = self.sandbox / "repo"
        (root / "packages" / "agents_kernel").mkdir(parents=True)
        (root / "packages" / "agents_kernel" / "__init__.py").write_text("", encoding="utf-8")
        (root / "marketplace.json").write_text(json.dumps({
            "name": "fake", "description": "fake",
            "plugins": [
                {"name": "dev-companion", "version": "1.0.0", "source": "./dev-companion",
                 "description": "d", "category": "developer-tools"},
                {"name": "code-analysis-swarm", "version": "1.0.0", "source": "./code-analysis-swarm",
                 "description": "d", "category": "developer-tools"},
            ]}, ensure_ascii=False), encoding="utf-8")
        for name, version in (("dev-companion", "0.0.1"), ("code-analysis-swarm", "1.0.0")):
            manifest_dir = root / name / ".zcode-plugin"
            manifest_dir.mkdir(parents=True)
            (manifest_dir / "plugin.json").write_text(
                json.dumps({"name": name, "version": version}), encoding="utf-8")
        result = run_build(root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("版本不一致", result.stderr)
        self.assertIn("dev-companion: 版本不一致 marketplace=1.0.0 plugin.json=0.0.1", result.stderr)
        self.assertFalse((root / PLUGIN / "scripts" / "_kernel_vendor").exists())


class ArchiveFormVendorTests(unittest.TestCase):
    """无 .git archive 形态复核（SR-07）：在临时构造的"源码包解包树"（无 .git）上
    经同一 tools/build_vendor.py 入口真实构建。证据只认逐文件 sha256 与显式
    "archive:<标记>"，不要求 git SHA；并验证工具拒绝 SHA 形态声明（禁伪造）。"""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name) / "archive-src"
        root.mkdir()
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "_kernel_vendor")
        shutil.copy2(REPO / "tools" / "build_vendor.py", root / "build_vendor.py")
        shutil.copy2(REPO / "marketplace.json", root / "marketplace.json")
        (root / "packages").mkdir()
        shutil.copytree(REPO / "packages" / "agents_kernel", root / "packages" / "agents_kernel",
                        ignore=ignore)
        for plugin in ("dev-companion", "code-analysis-swarm"):
            shutil.copytree(REPO / plugin, root / plugin, ignore=ignore)
        cls.root = root
        cls.vendor = root / PLUGIN / "scripts" / "_kernel_vendor"

    def run_archive_build(self, marker):
        """以 archive 树自带的 tools 脚本构建一次（用户解包后运行的真实方式）；
        marker=None 表示不设 AGENTS_SOURCE_COMMIT（缺省口径）。"""
        env = dict(os.environ)
        env.pop(ARCHIVE_ENV, None)
        if marker is not None:
            env[ARCHIVE_ENV] = marker
        return subprocess.run([sys.executable, str(self.root / "build_vendor.py"),
                               "--root", str(self.root)],
                              capture_output=True, text=True, env=env)

    def build_archive(self, marker):
        result = self.run_archive_build(marker)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("archive_no_git", result.stdout, "构建输出应注明 archive provenance")
        return json.loads((self.vendor / "MANIFEST.json").read_text(encoding="utf-8"))

    def test_archive_manifest_honest_marker_and_hashes_still_verified(self):
        manifest = self.build_archive(ARCHIVE_MARKER)
        self.assertEqual(manifest["source"]["commit"], ARCHIVE_MARKER,
                         "archive 形态 commit 必须是显式传入的诚实标记")
        self.assertEqual(manifest["source"]["provenance"], ARCHIVE_PROVENANCE)
        self.assertFalse(SHA_RE.match(manifest["source"]["commit"]),
                         "archive 形态不得出现具体 SHA（无 .git 不可验证）")
        verify_manifest_integrity(manifest, self.vendor, REPO)

    def test_archive_without_env_defaults_to_archive_none(self):
        manifest = self.build_archive(None)
        self.assertEqual(manifest["source"]["commit"], "archive:none")
        self.assertEqual(manifest["source"]["provenance"], ARCHIVE_PROVENANCE)

    def test_archive_rejects_sha_shaped_claim(self):
        """禁伪造保证：无 .git 时传入 40 位 SHA（哪怕是真实候选 SHA）一律拒绝构建，
        任何位置都不得落出含具体 SHA 的 MANIFEST。"""
        result = self.run_archive_build("1a7bc6986b9f24109381eb8e31c60cb65a3459a9")
        self.assertNotEqual(result.returncode, 0, "SHA 形态声明必须被拒绝")
        self.assertIn("archive:", result.stderr)
        for manifest_path in self.root.rglob("MANIFEST.json"):
            commit = json.loads(manifest_path.read_text(encoding="utf-8"))["source"]["commit"]
            self.assertFalse(SHA_RE.match(commit),
                             "拒绝路径不得落出含具体 SHA 的 MANIFEST：%s" % manifest_path)


if __name__ == "__main__":
    unittest.main()

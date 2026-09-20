"""P2-05 vendor 分发测试：插件目录仓外独立运行、MANIFEST 校验、幂等、版本门禁。

构建入口 tools/build_vendor.py 以子进程运行（与 CI/用户同一调用方式）；
"仓外独立运行"把 dev-companion 复制进 tempfile，从那里引导 scripts 模块，
断言内核解析到 _kernel_vendor 而非仓库 packages/。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / "tools" / "build_vendor.py"
PLUGIN = "dev-companion"


def run_build(root):
    return subprocess.run([sys.executable, str(BUILD), "--root", str(root)],
                          capture_output=True, text=True)


def tree_hashes(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


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
    @classmethod
    def setUpClass(cls):
        result = subprocess.run([sys.executable, str(BUILD)], capture_output=True, text=True, cwd=str(REPO))
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
        self.assertTrue(manifest["source"]["commit"], "应记录源 commit（git rev-parse HEAD）")
        self.assertTrue(manifest["generated_at"])
        listed = {entry["path"] for entry in manifest["files"]}
        actual = {path.relative_to(self.vendor).as_posix() for path in self.vendor.rglob("*")
                  if path.is_file() and path.name != "MANIFEST.json"}
        self.assertEqual(listed, actual)
        for entry in manifest["files"]:
            data = (self.vendor / entry["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"], entry["path"])
            self.assertEqual(len(data), entry["bytes"], entry["path"])
            source = REPO / "packages" / entry["path"]
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), entry["sha256"],
                             entry["path"] + " 副本与源不一致")

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

    def test_swarm_without_kernel_dependency_is_skipped(self):
        self.assertIn("跳过 code-analysis-swarm", self.build_output)
        self.assertFalse((REPO / "code-analysis-swarm" / "scripts" / "_kernel_vendor").exists())

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


if __name__ == "__main__":
    unittest.main()

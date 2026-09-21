# -*- coding: utf-8 -*-
"""文档一致性守卫（P7-02：Z18 单一命令注册表 / Z29 插件 README / Z27 退出码文档抽查）。

覆盖：
- command registry 生成器幂等，--check 对现文件通过、对缺失/篡改文件拒绝；
- 根 README `sh` 代码块中的测试命令真实可执行。验证强度分级（不完全执行）：
  * demo-project unittest / node --test / registry --check 真实执行（快，无副作用）；
  * 主套件 unittest 只做 discover 收集（collect-only 级，不执行用例——套件内
    套件会造成自递归）；
  * build_vendor.py 与 L_TIER 长跑基准只验证目标存在（前者会刷新 vendor 清单
    时间戳，后者生成 10 万条目 fixture，均不在文档测试中执行）；
- 三份 README 的 markdown 链接与「仓库结构」树中重建出的路径真实存在；
- 退出码 0/2/3 在 companion.py 源码、根 README（中/英）、cli-contract.md、
  command-registry.md 之间的一致性抽查。
"""
import importlib.util
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
COMPANION_README = REPO_ROOT / "dev-companion" / "README.md"
SWARM_README = REPO_ROOT / "code-analysis-swarm" / "README.md"
REGISTRY_DOC = REPO_ROOT / "docs" / "command-registry.md"
REGISTRY_TOOL = REPO_ROOT / "tools" / "command_registry.py"
CLI_CONTRACT = REPO_ROOT / "dev-companion" / "references" / "cli-contract.md"
COMPANION_CLI = REPO_ROOT / "dev-companion" / "scripts" / "companion.py"

GITHUB_BLOB = "https://github.com/aa975dc/agents/blob/main/"


def load_registry_module():
    spec = importlib.util.spec_from_file_location("command_registry", REGISTRY_TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_registry(*argv):
    return subprocess.run(
        [sys.executable, str(REGISTRY_TOOL), *argv],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )


def sh_block_commands(text):
    """提取 ```sh 围栏块里的命令行，去掉行内 # 注释。其余围栏（mermaid/纯代码块）
    不以 ```sh 开头，一律不收。"""
    commands, in_block = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "```sh":
            in_block = True
            continue
        if stripped.startswith("```"):
            in_block = False
            continue
        if in_block and stripped and not stripped.startswith("#"):
            commands.append(stripped.split(" # ")[0].strip())
    return commands


class CommandRegistryTests(unittest.TestCase):
    """Z18：注册表是唯一命令清单，生成幂等，--check 可作 CI 门。"""

    def test_render_is_idempotent_and_covers_all_declared_commands(self):
        module = load_registry_module()
        rows, problems = module.collect_rows()
        self.assertEqual(problems, [])
        first, second = module.render(rows), module.render(rows)
        self.assertEqual(first, second)
        self.assertEqual(REGISTRY_DOC.read_text(encoding="utf-8"), first)

        # 每条 manifest 声明目录里的命令都入表且源文件存在
        names = [row["name"] for row in rows]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(rows), 8)  # 7 companion-* + swarm-analyze
        for row in rows:
            self.assertTrue((REPO_ROOT / row["plugin"].rsplit(" ", 1)[0] / row["source"]).is_file(), row["source"])

    def test_check_mode_passes_for_committed_file(self):
        proc = run_registry("--check")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_check_mode_detects_missing_and_tampered_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "command-registry.md"
            missing = run_registry("--check", "--output", str(target))
            self.assertEqual(missing.returncode, 1, missing.stdout + missing.stderr)

            generated = run_registry("--output", str(target))
            self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
            ok = run_registry("--check", "--output", str(target))
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)

            target.write_text(target.read_text(encoding="utf-8").replace("companion", "tampered"), encoding="utf-8")
            drift = run_registry("--check", "--output", str(target))
            self.assertEqual(drift.returncode, 1, drift.stdout + drift.stderr)


class ReadmeCommandTests(unittest.TestCase):
    """根 README 的 sh 命令块逐条轻验证；执行强度分级，见模块 docstring。"""

    @classmethod
    def setUpClass(cls):
        cls.commands = sh_block_commands(README.read_text(encoding="utf-8"))
        half = len(cls.commands) // 2
        cls.zh_commands = cls.commands[:half]

    def test_zh_and_en_command_blocks_match(self):
        """中英两节各一份，命令集合应一致且非空。"""
        self.assertGreaterEqual(len(self.commands), 6, "README sh 命令块解析为空")
        half = len(self.commands) // 2
        self.assertEqual(self.commands[:half], self.commands[half:])

    def test_python_discover_tests_dir_collects_suite_without_running(self):
        """collect-only 级验证：能发现主套件（>500 项）但不执行任何用例。"""
        loader = unittest.TestLoader()
        suite = loader.discover(str(REPO_ROOT / "tests"), top_level_dir=str(REPO_ROOT / "tests"))
        self.assertGreater(suite.countTestCases(), 500)

    def test_python_discover_demo_project_actually_runs(self):
        target = "dev-companion/examples/demo-project"
        matched = [c for c in self.zh_commands if target in c]
        self.assertEqual(len(matched), 1)
        proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", target, "-q"],
                              capture_output=True, text=True, cwd=str(REPO_ROOT))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OK", proc.stderr)

    def test_node_test_command_runs_both_files(self):
        matched = [c for c in self.zh_commands if c.startswith("node --test")]
        self.assertEqual(len(matched), 1)
        files = [REPO_ROOT / part for part in matched[0].split() if part.endswith(".mjs")]
        self.assertEqual(len(files), 2)
        for path in files:
            self.assertTrue(path.is_file(), str(path))
        if shutil.which("node") is None:
            self.skipTest("node 不可用")
        proc = subprocess.run(matched[0].split(), capture_output=True, text=True, cwd=str(REPO_ROOT))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # FIX-05 后工作流测试 63 项 + 宿主契约 3 项 = 66（README 的 57 待文档轮同步，此处跟随实际套件输出）
        self.assertIn("pass 66", proc.stdout)

    def test_registry_check_command_runs(self):
        proc = run_registry("--check")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_build_vendor_only_verified_not_executed(self):
        """不执行（会刷新 vendor MANIFEST 时间戳）；验证存在且可编译。"""
        self.assertTrue((REPO_ROOT / "tools" / "build_vendor.py").is_file())
        py_compile.compile(str(REPO_ROOT / "tools" / "build_vendor.py"), doraise=True)

    def test_l_tier_benchmark_only_verified_not_executed(self):
        """不执行（10 万条目长跑基准）；验证入口文件与门控存在。"""
        self.assertTrue((REPO_ROOT / "tests" / "benchmarks" / "test_l_tier.py").is_file())
        source = (REPO_ROOT / "tests" / "benchmarks" / "test_l_tier.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("L_TIER") == "1"', source)


class ReadmePathTests(unittest.TestCase):
    """三份 README 的链接与「仓库结构」树提到的路径必须真实存在（Z15/Z29）。"""

    def _assert_links_resolve(self, readme_path, text):
        broken = []
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if target.startswith("#"):
                continue
            if target.startswith(GITHUB_BLOB):
                resolved = REPO_ROOT / target[len(GITHUB_BLOB):]
            elif target.startswith(("http://", "https://")):
                continue  # 外部链接不在此验证
            else:
                resolved = readme_path.parent / target
            if not resolved.exists():
                broken.append(target)
        self.assertEqual(broken, [], "%s 存在失效链接" % readme_path)

    def test_markdown_links_resolve_in_three_readmes(self):
        for readme in (README, COMPANION_README, SWARM_README):
            self.assertTrue(readme.is_file(), str(readme))
            self._assert_links_resolve(readme, readme.read_text(encoding="utf-8"))

    def _tree_paths(self, text, heading):
        """从「仓库结构」标题后的第一个围栏块（裸 ``` 开启）重建路径清单。"""
        lines = text.splitlines()
        start = next((i for i, line in enumerate(lines) if line.strip() == heading), None)
        if start is None:
            return set()
        i = start + 1
        while i < len(lines) and not lines[i].strip().startswith("```"):
            i += 1
        i += 1  # 进入围栏块内容
        paths, stack = set(), []
        while i < len(lines) and not lines[i].strip().startswith("```"):
            body = lines[i].strip().split("#", 1)[0].strip()
            if body:
                marker = re.search(r"(├──|└──)\s*(.+)$", body)
                if marker is None:  # 根行，如 "agents/"
                    stack = [body.rstrip("/")]
                    paths.add("/".join(stack))
                else:
                    # 层深 = 行首制图符/空格前缀长度 // 4（"│   " 每层 4 字符）
                    depth = (len(lines[i]) - len(lines[i].lstrip("│ "))) // 4
                    name = marker.group(2).strip().rstrip("/")
                    stack = stack[:depth] + [name]
                    paths.add("/".join(stack))
            i += 1
        return paths

    def test_repository_structure_tree_paths_exist(self):
        text = README.read_text(encoding="utf-8")
        found = self._tree_paths(text, "### 仓库结构") | self._tree_paths(text, "### Repository layout")
        for key in ("marketplace.json",
                    "code-analysis-swarm/README.md",
                    "code-analysis-swarm/command/swarm-analyze.md",
                    "code-analysis-swarm/scripts/precheck.py",
                    "dev-companion/commands",
                    "dev-companion/agents",
                    "packages/agents_kernel",
                    "tools",
                    "docs",
                    "tests"):
            self.assertIn(key, found, "仓库结构树缺少 %s" % key)
        # 树根（"agents/"）代表仓库根本身，不是仓库内的可检查路径
        found.discard("agents")
        nonexistent = sorted(p for p in found if not (REPO_ROOT / p).exists())
        self.assertEqual(nonexistent, [], "仓库结构树提到的路径不存在")


class ExitCodeDocTests(unittest.TestCase):
    """Z27 抽查：退出码 0/2/3 文档与实现一致。"""

    def test_cli_source_implements_the_documented_codes(self):
        source = COMPANION_CLI.read_text(encoding="utf-8")
        self.assertIn('result.get("passed") is False', source)
        self.assertIn("return 3", source)
        self.assertIn("return 2", source)
        self.assertIn("sys.exit(main())", source)

    def test_exit_codes_documented_once_per_doc(self):
        expectations = {
            README: "退出码：`0` 成功；`2` 参数 / 状态 / 权限错误；`3` 实际检查或发布命令失败",
            COMPANION_README: None,  # 指向 cli-contract，不重复三值定义，下面单独断言
            CLI_CONTRACT: "成功返回 0，参数/状态/权限错误返回 2，实际检查或发布命令失败返回 3",
            REGISTRY_DOC: "`0`/`2`/`3`",
        }
        for path, needle in expectations.items():
            text = path.read_text(encoding="utf-8")
            if needle is not None:
                self.assertIn(needle, text, "%s 退出码表述漂移" % path)
        # 英文节与中文节口径一致
        text = README.read_text(encoding="utf-8")
        self.assertIn("Exit codes: `0` success; `2` argument / status / permission error; "
                      "`3` a real check or release command failed", text)
        # 插件 README 只引用不另立口径
        companion_text = COMPANION_README.read_text(encoding="utf-8")
        self.assertIn("references/cli-contract.md", companion_text)


class SwarmReadmeTests(unittest.TestCase):
    """Z29 关闭守卫：swarm 有独立 README，且 Z16 口径写清。"""

    def test_swarm_readme_documents_manifest_declared_command_dir(self):
        text = SWARM_README.read_text(encoding="utf-8")
        self.assertIn('"commands": "command"', text)
        self.assertIn("显式声明", text)
        self.assertIn("不是命名缺陷", text)
        self.assertIn("[DESIGN.md](DESIGN.md)", text)
        self.assertIn("tests/swarm_workflow.test.mjs", text)


if __name__ == "__main__":
    unittest.main()

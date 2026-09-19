# -*- coding: utf-8 -*-
"""scripts/precheck.py 确定性预检 helper 单测（Z02/Z09，全部真实执行）。

覆盖：排他创建成功/冲突 exit 3、run_root 含于 source_root 拒绝、敏感路径 exit 4、
中文与空格路径、--json-file 输入、默认 run_root、read-report 回执与退出码。
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRECHECK = os.path.join(REPO_ROOT, "code-analysis-swarm", "scripts", "precheck.py")
ROLE_FILES = [
    "a1-scout.md", "a2-module-analyst.md", "a3-architect.md", "a4-dependency.md",
    "a5-build.md", "a6-verifier.md", "a7-reporter.md",
]


def run_helper(sub, payload=None, json_file=None, cwd=None):
    argv = [sys.executable, PRECHECK, sub]
    if json_file is not None:
        argv += ["--json-file", json_file]
    elif payload is not None:
        argv += ["--json", json.dumps(payload, ensure_ascii=False)]
    proc = subprocess.run(argv, capture_output=True, text=True, cwd=cwd)
    return proc.returncode, proc.stdout, proc.stderr


class PrecheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="precheck-test-")
        base = self.tmp.name
        self.source = os.path.join(base, "源码 仓库")
        self.team = os.path.join(base, "team 插件")
        self.workspace = os.path.join(base, "workspace 输出")
        os.makedirs(self.source)
        os.makedirs(os.path.join(self.team, "agents"))
        os.makedirs(self.workspace)
        for role in ROLE_FILES:
            with open(os.path.join(self.team, "agents", role), "w", encoding="utf-8") as fh:
                fh.write("role\n")
        self.parent = os.path.join(self.workspace, "报告 根")

    def tearDown(self):
        self.tmp.cleanup()

    def base_payload(self, **override):
        payload = {
            "source_root": self.source,
            "team_root": self.team,
            "run_root_parent": self.parent,
        }
        payload.update(override)
        return payload

    def stdout_json(self, out):
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, "stdout 应为单行 JSON")
        return json.loads(lines[0])

    def test_plan_ok_and_complete_fields(self):
        code, out, _ = run_helper("plan", self.base_payload(run_id="run-abc"))
        self.assertEqual(code, 0)
        data = self.stdout_json(out)
        self.assertTrue(data["ok"])
        self.assertEqual(data["run_id"], "run-abc")
        roots = data["roots"]
        self.assertEqual(set(roots), {"source_root", "team_root", "host_workspace_root", "run_root"})
        self.assertEqual(roots["source_root"]["realpath"], os.path.realpath(self.source))
        self.assertEqual(roots["source_root"]["lstat_kind"], "directory")
        self.assertEqual(roots["run_root"]["realpath"], os.path.realpath(os.path.join(self.parent, "run-abc")))
        self.assertFalse(data["run_root_exists"])
        checks = data["checks"]
        self.assertTrue(checks["source_root_is_dir"])
        self.assertTrue(checks["run_root_outside_source_root"])
        self.assertTrue(checks["run_root_outside_team_root"])
        self.assertTrue(checks["sensitive_paths_clear"])
        self.assertTrue(checks["team_role_files_present"])

    def test_plan_generates_run_id_with_secrets(self):
        code, out, _ = run_helper("plan", self.base_payload())
        self.assertEqual(code, 0)
        run_id = self.stdout_json(out)["run_id"]
        self.assertRegex(run_id, r"^run-[0-9a-f]{24}$")

    def test_plan_missing_source_root_exit_2(self):
        payload = self.base_payload()
        payload["source_root"] = os.path.join(self.tmp.name, "no-such-dir")
        code, out, _ = run_helper("plan", payload)
        self.assertEqual(code, 2)
        self.assertEqual(self.stdout_json(out)["kind"], "argument")

    def test_plan_source_is_file_exit_2(self):
        file_path = os.path.join(self.tmp.name, "a-file")
        with open(file_path, "w", encoding="utf-8") as fh:
            fh.write("x")
        code, _, _ = run_helper("plan", self.base_payload(source_root=file_path))
        self.assertEqual(code, 2)

    def test_plan_missing_role_file_exit_2(self):
        os.remove(os.path.join(self.team, "agents", "a6-verifier.md"))
        code, out, _ = run_helper("plan", self.base_payload())
        self.assertEqual(code, 2)
        self.assertIn("a6-verifier.md", self.stdout_json(out)["error"])

    def test_plan_run_root_inside_source_exit_3(self):
        payload = self.base_payload(run_root_parent=os.path.join(self.source, "out"))
        code, out, _ = run_helper("plan", payload)
        self.assertEqual(code, 3)
        self.assertEqual(self.stdout_json(out)["kind"], "conflict")

    def test_plan_run_root_inside_team_exit_3(self):
        payload = self.base_payload(run_root_parent=os.path.join(self.team, "runs"))
        code, _, _ = run_helper("plan", payload)
        self.assertEqual(code, 3)

    def test_plan_default_run_root_under_workspace(self):
        code, out, _ = run_helper("plan", {"source_root": self.source, "run_id": "run-x"}, cwd=self.workspace)
        self.assertEqual(code, 0)
        data = self.stdout_json(out)
        expected = os.path.join(os.path.realpath(self.workspace), ".code-analysis-swarm-runs", "run-x")
        self.assertEqual(data["roots"]["run_root"]["realpath"], expected)
        self.assertEqual(data["roots"]["run_root"]["publish_relpath"], os.path.join(".code-analysis-swarm-runs", "run-x"))

    def test_acquire_exclusive_success_and_receipt(self):
        code, out, _ = run_helper("acquire", self.base_payload(run_id="run-ok"))
        self.assertEqual(code, 0)
        receipt = self.stdout_json(out)
        run_root = os.path.join(self.parent, "run-ok")
        self.assertTrue(os.path.isdir(run_root))
        self.assertEqual(receipt["mode"], "mkdir_exclusive")
        self.assertEqual(receipt["run_id"], "run-ok")
        self.assertRegex(receipt["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertEqual(receipt["roots"]["run_root"]["realpath"], os.path.realpath(run_root))
        self.assertEqual(receipt["roots"]["host_workspace_root"]["realpath"], os.path.realpath(os.getcwd()))
        receipt_file = os.path.join(run_root, "precheck.json")
        with open(receipt_file, "r", encoding="utf-8") as fh:
            written = json.loads(fh.read())
        self.assertEqual(written, receipt)

    def test_acquire_conflict_exit_3_keeps_existing(self):
        first = run_helper("acquire", self.base_payload(run_id="run-dup"))
        self.assertEqual(first[0], 0)
        marker = os.path.join(self.parent, "run-dup", "marker.txt")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("keep")
        code, out, _ = run_helper("acquire", self.base_payload(run_id="run-dup"))
        self.assertEqual(code, 3)
        data = self.stdout_json(out)
        self.assertFalse(data["ok"])
        self.assertEqual(data["kind"], "conflict")
        with open(marker, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "keep")

    def test_acquire_json_file_input(self):
        input_path = os.path.join(self.tmp.name, "输入 plan.json")
        with open(input_path, "w", encoding="utf-8") as fh:
            json.dump(self.base_payload(run_id="run-file"), fh, ensure_ascii=False)
        code, out, _ = run_helper("acquire", json_file=input_path)
        self.assertEqual(code, 0)
        self.assertEqual(self.stdout_json(out)["run_id"], "run-file")

    def test_sensitive_run_root_exit_4(self):
        ssh_parent = os.path.join(self.tmp.name, "home", ".ssh")
        os.makedirs(ssh_parent)
        for sub in ("plan", "acquire"):
            code, out, _ = run_helper(sub, self.base_payload(run_root_parent=ssh_parent))
            self.assertEqual(code, 4, sub)
            self.assertEqual(self.stdout_json(out)["kind"], "sensitive")

    def test_sensitive_source_root_exit_4(self):
        gnupg_source = os.path.join(self.tmp.name, "home", ".gnupg", "keys")
        os.makedirs(gnupg_source)
        code, out, _ = run_helper("plan", self.base_payload(source_root=gnupg_source))
        self.assertEqual(code, 4)

    def test_verify_rejects_run_root_inside_source(self):
        code, out, _ = run_helper("verify", {"source_root": self.source, "run_root": os.path.join(self.source, "runs")})
        self.assertEqual(code, 3)
        self.assertEqual(self.stdout_json(out)["kind"], "conflict")

    def test_verify_rejects_output_outside_run_root(self):
        run_root = os.path.join(self.parent, "run-v")
        code, _, _ = run_helper("verify", {"source_root": self.source, "run_root": run_root, "outputs": [os.path.join(self.source, "x.md")]})
        self.assertEqual(code, 3)
        code, _, _ = run_helper("verify", {"source_root": self.source, "run_root": run_root, "outputs": [os.path.join(run_root, "report", "r.md")]})
        self.assertEqual(code, 0)

    def test_read_report_returns_hash_body_and_relpath(self):
        run_root = os.path.join(self.parent, "run-r")
        os.makedirs(os.path.join(run_root, "report"))
        body = "# 分析报告\n正文内容（中文与空格）\n"
        report = os.path.join(run_root, "report", "analysis-report.md")
        raw = body.encode("utf-8")
        with open(report, "wb") as fh:
            fh.write(raw)
        code, out, _ = run_helper("read-report", {"path": report, "within_root": run_root}, cwd=self.workspace)
        self.assertEqual(code, 0)
        data = self.stdout_json(out)
        self.assertTrue(data["ok"])
        self.assertEqual(data["size"], len(raw))
        self.assertEqual(data["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(data["body"], body)
        self.assertEqual(data["publish_relpath"], os.path.join("报告 根", "run-r", "report", "analysis-report.md"))

    def test_read_report_rejects_escape_and_missing_and_oversize(self):
        run_root = os.path.join(self.parent, "run-x")
        os.makedirs(run_root)
        outside = os.path.join(self.source, "README.md")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("x")
        code, _, _ = run_helper("read-report", {"path": outside, "within_root": run_root})
        self.assertEqual(code, 3)
        code, _, _ = run_helper("read-report", {"path": os.path.join(run_root, "absent.md"), "within_root": run_root})
        self.assertEqual(code, 3)
        big = os.path.join(run_root, "big.md")
        with open(big, "wb") as fh:
            fh.write(b"a" * 200_001)
        code, out, _ = run_helper("read-report", {"path": big, "within_root": run_root})
        self.assertEqual(code, 2)
        self.assertEqual(self.stdout_json(out)["kind"], "argument")

    def test_invalid_json_and_usage_exit_2(self):
        code, _, _ = run_helper("plan", json_file=os.path.join(self.tmp.name, "absent.json"))
        self.assertEqual(code, 2)
        proc = subprocess.run([sys.executable, PRECHECK, "nonsense"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("用法", proc.stdout)

    def test_unicode_and_space_paths_end_to_end(self):
        # 中文与空格出现在 source/team/workspace/报告路径与 JSON 输入文件名中；
        # run_id 本身按契约仅限 ASCII 字母数字下划线连字符。
        payload = self.base_payload(run_id="run-space-ok")
        code, out, _ = run_helper("plan", payload)
        self.assertEqual(code, 0)
        self.assertTrue(self.stdout_json(out)["ok"])
        code, out, _ = run_helper("acquire", payload)
        self.assertEqual(code, 0)
        run_root = os.path.join(self.parent, "run-space-ok")
        report = os.path.join(run_root, "report", "分析 报告.md")
        os.makedirs(os.path.dirname(report))
        with open(report, "wb") as fh:
            fh.write("# 中文 正文\n".encode("utf-8"))
        code, out, _ = run_helper("read-report", {"path": report, "within_root": run_root}, cwd=self.workspace)
        self.assertEqual(code, 0)
        self.assertIn("中文 正文", self.stdout_json(out)["body"])


if __name__ == "__main__":
    unittest.main()

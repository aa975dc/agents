#!/usr/bin/env python3
"""Run the real CLI lifecycle in an empty disposable project; retain all evidence."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
CLI = HERE.parent / "scripts" / "companion.py"
ASSETS = HERE / "lifecycle-demo"


def run_demo(root):
    data = root / ".dev-companion"
    inputs, evidence = data / "inputs", data / "demo-evidence"
    inputs.mkdir(parents=True)
    evidence.mkdir()
    summary = {"project": str(root), "human_trial": False, "product_confirmation": "自动化模拟用户确认，仅用于演示",
               "publication": "未验证", "steps": [], "passed": False}

    def call(*args, payload=None, expected=0):
        number = len(summary["steps"]) + 1
        argv = [sys.executable, "-B", str(CLI), "--project", str(root), *args]
        if payload is not None:
            path = inputs / ("%02d-%s.json" % (number, args[0]))
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            argv.extend(["--input", str(path)])
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
            receipt = {"argv": argv, "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        except subprocess.TimeoutExpired as exc:
            receipt = {"argv": argv, "exit_code": None, "error": "CLI 超时", "stdout": str(exc.stdout), "stderr": str(exc.stderr)}
        receipt_path = evidence / ("%02d-%s.json" % (number, args[0]))
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        summary["steps"].append({"command": args[0], "exit_code": receipt["exit_code"], "receipt": str(receipt_path)})
        if receipt["exit_code"] != expected:
            raise RuntimeError("步骤 %s 预期退出码 %s，实际 %s；见 %s" % (args[0], expected, receipt["exit_code"], receipt_path))
        return json.loads(receipt["stdout"])

    try:
        for name in ("verify.py", "release_demo.py", "prototype.md"):
            shutil.copy2(ASSETS / name, root / name)
        product = {"title": "支出合计演示", "goal": "知道本次支出合计", "audience": "本地演示使用者", "scenario": "输入金额并看到合计",
                   "out_of_scope": ["账户、保存记录、互联网部署"], "assumptions": ["产品确认由自动化模拟；human_trial=false"],
                   "features": [{"id": "sum", "title": "计算合计", "acceptance_criteria": ["20,30 显示 50", "非法和负数金额返回 4xx"], "requires_user_acceptance": False}]}
        scope = copy.deepcopy(product)
        scope["features"][0].update(allowed_paths=["app.py", "index.html"], check_commands=[[sys.executable, "-B", "verify.py", "unit"]])
        call("plan", "--stage", "concept", "--revision", "0", payload={"summary": "想算出支出合计", "details": {}})
        assert call("status", "--format", "json")["overall_percent"] is None
        details = {
            "concept": {"audience": "本地演示使用者", "problem": "不知道总共花费", "scenario": "输入支出", "outcome": "得到合计"},
            "requirements": {"constraints": "Python 标准库，本地演示，不保存数据", "priorities": "正确合计并解释非法输入"},
            "product": {"positioning": "一个能走通全生命周期的支出合计示例"},
            "flow": {"main_path": "输入20,30并点击计算，服务返回50", "alternatives": "非法金额显示解释并允许重试", "data_changes": "仅请求传值，不持久化"},
            "prototype": {"screens": "prototype.md中的单页草图", "states": "等待、加载、成功、失败", "walkthrough": "按静态草图走查输入20,30到合计50；自动化模拟，真实用户未试用"},
            "technical": {"architecture": "HTML 页面 + ThreadingHTTPServer", "data_model": "非负有限金额数组", "release_target": "隔离目录内本地副本"}}
        for revision, (stage, stage_details) in enumerate(details.items(), start=1):
            raw = {"summary": stage + " 演示成果", "details": stage_details, "open_questions": [],
                   "decisions": [{"question": "演示是否经过真实用户确认？", "answer": "没有。自动化模拟产品确认；human_trial=false", "source": "assumption"}]}
            flags = ["--complete"]
            if stage == "product":
                raw["scope"] = product
                flags.append("--user-confirmed")
            if stage in ("flow", "prototype"):
                raw["feature_ids"] = ["sum"]
            if stage == "prototype":
                raw["artifacts"] = ["prototype.md"]
            if stage == "technical":
                raw.update(scope=scope, interfaces=[{"feature_id": "sum", "kind": "http", "contract": "POST /api/sum {values:[number]} -> {total:number}; 非法/负数422；GET /api/version -> VERSION", "check_commands": [[sys.executable, "-B", "verify.py", "integration"]]}])
            call("plan", "--stage", stage, "--revision", str(revision), *flags, payload=raw)
        state = call("init", payload=scope)
        call("confirm", "--revision", str(state["revision"]))
        for broken in (True, False):
            packet = call("packet", "--feature", "sum")
            code = (ASSETS / "app.py").read_text()
            (root / "app.py").write_text(code.replace("result = sum(values)", "result = 0") if broken else code)
            if broken:
                shutil.copy2(ASSETS / "index.html", root / "index.html")
            call("receipt", payload={"feature_id": "sum", "scope_version": packet["scope_version"], "run_id": packet["run_id"], "status": "implemented", "summary": "故意错误版用于检验失败路径" if broken else "修复合计逻辑", "changed_files": ["app.py", "index.html"] if broken else ["app.py"], "evidence_files": []})
            checked = call("check", "--feature", "sum", expected=3 if broken else 0)
            assert checked["passed"] is (not broken)
            if broken:
                summary["expected_failure_observed"] = True
                call("feedback", "--feature", "sum", "--kind", "defect", "--note", "真实检查发现20+30错误为0，修复后重新检查")
        call("check", "--feature", "sum", "--kind", "integration")
        call("accept", "--feature", "sum", "--note", "功能与真实HTTP联调通过；不要求人工验收的自动化演示，human_trial=false")
        config = {"version": "0.2.0-demo", "target": str(data / "local-release"), "environment": "local", "summary": "已授权的隔离本地演示副本", "rollback_plan": "仅撤回此隔离目录的app.py/index.html副本", "verification_notes": "对部署副本启动临时端口并检查实际版本、页面及核心API；不是浏览器验收", "deploy_commands": [[sys.executable, "-B", "release_demo.py", "deploy"]], "verify_commands": [[sys.executable, "-B", "verify.py", "deployed"]], "rollback_commands": [[sys.executable, "-B", "release_demo.py", "rollback"]]}
        release = call("release-prepare", "--revision", "0", payload=config)
        release = call("release-run", "--action", "deploy", "--revision", str(release["revision"]), "--authorized")
        release = call("release-run", "--action", "verify", "--revision", str(release["revision"]), "--authorized")
        assert release["status"] == "local_verified"
        board = call("status", "--format", "html", "--out", str(data / "board.html"))
        summary.update(passed=True, publication=release["status"], board=board["output"],
                       start_command=[sys.executable, "-B", str(root / "app.py"), "--port", "8765"])
    except Exception as exc:
        summary["error"] = str(exc)
        raise
    finally:
        (data / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", help="不存在或空的隔离测试目录；默认创建并保留临时目录")
    args = parser.parse_args()
    root = Path(args.project).absolute() if args.project else Path(tempfile.mkdtemp(prefix="dev-companion-lifecycle-")).resolve()
    if any(parent.is_symlink() for parent in (root, *root.parents)):
        parser.error("隔离演示目录及其父目录不能是文件链接")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        parser.error("演示仅接受不存在或空目录，不覆盖现有文件")
    root.mkdir(parents=True, exist_ok=True)
    try:
        result = run_demo(root)
        print(json.dumps({key: result[key] for key in ("project", "passed", "publication", "human_trial", "board", "start_command")}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"passed": False, "error": str(exc), "project": str(root), "evidence": str(root / ".dev-companion")}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

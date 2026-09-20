#!/usr/bin/env python3
"""R05 L 档真实 CLI 实测入口（手动运行；不在常规 discover 收集范围）。

按 09_TEST_AND_BENCHMARK_ACCEPTANCE.md §1/§3/§5 与 02_BOUNDED_CLOSURE_TASKS.md R05：
全部走真实 `python3 companion.py` 子进程；计时运行不注入插桩，插桩仅用于单次取证。

用法：
    python3 tests/closure_r05/r05_l_tier.py --output /path/evidence.json [--workdir DIR]

步骤：
1. fixture（复用 tests/benchmarks/fixture_gen.py，先 --dry-run 后实跑；全部位于
   tempfile.gettempdir() 下 benchmark-fixture- 前缀目录）：L=--files 100000；
   small=--files 48（盘上 50 文件含 marker+manifest，对照档）；team=--files 48。
2. L 项目：CLI init/confirm → 插桩 status 取证 1 次 → 预热 3 次 → 30 次温热
   完整子进程计时（墙钟含解释器启动；RSS=子进程 ru_maxrss，macOS 单位字节）。
3. 启动对照：`python3 -c pass` 30 次（分解解释器启动占比）。
4. 小项目：init/confirm → packet/写 allowed 文件/receipt/check/accept（历史验收）
   → 插桩 status（快照路径对照，源码树读取应发生）→ 写入 21MiB big.bin 超限 →
   插桩 status（降级视图 + 历史验收展示断言）。
5. team 项目：team-init + 120 次 team-task → CLI 分页断言（offset/limit/has_more）
   → 插桩 team-status 取证 → 预热 3 次 → 30 次温热计时 --limit 100。

输出单份 JSON 证据（argv、起止、退出码、样本原始序列、p50/p95、RSS、插桩计数、
fixture manifest hash、HEAD SHA、逐项断言 PASS/FAIL）。本脚本不改产品代码；
发现与验收不符的行为如实记 FAIL，由证据文档汇总。
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
from instrument import TimedRunner, build_rules, percentile, run_counted  # noqa: E402

COMPANION = REPO / "dev-companion" / "scripts" / "companion.py"
FIXTURE_GEN = REPO / "tests" / "benchmarks" / "fixture_gen.py"
WARMUP = 3
SAMPLES = 30
TEAM_TASKS = 120


def cli(project, *argv):
    return [sys.executable, str(COMPANION), "--project", str(project), *argv]


def run(argv, timeout=600):
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    ended = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {"argv": argv, "started_at": started, "ended_at": ended,
            "exit": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def fixture(files, workdir, tag):
    target = workdir / ("benchmark-fixture-r05" + tag)
    gen = [sys.executable, str(FIXTURE_GEN), str(target), "--files", str(files)]
    dry = run(gen + ["--dry-run"])
    if dry["exit"] != 0 or "DRY-RUN OK" not in dry["stdout"].decode():
        raise RuntimeError("fixture dry-run 未通过：" + dry["stderr"].decode()[:300])
    manifest_path = target / "manifest.json"
    reused = False
    if manifest_path.is_file():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        reused = existing.get("requested_files") == files and (target / "BENCHMARK_FIXTURE").is_file()
    if not reused:
        real = run(gen)
        if real["exit"] != 0:
            raise RuntimeError("fixture 实跑失败：" + real["stderr"].decode()[:300])
    return target, {"tag": tag, "files": files, "reused_existing": reused,
                    "dir": str(target),
                    "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                    "manifest": json.loads(manifest_path.read_text(encoding="utf-8")),
                    "dry_run_line": dry["stdout"].decode().strip()}


def reset_scratch(project):
    """清掉本套件上次运行在同一 fixture 内留下的项目状态（仅限带 marker 的自有 scratch）。"""
    if not (project / "BENCHMARK_FIXTURE").is_file() or not (project / "manifest.json").is_file():
        raise RuntimeError("缺少 BENCHMARK_FIXTURE marker/manifest，拒绝改动：" + str(project))
    shutil.rmtree(project / ".dev-companion", ignore_errors=True)
    for leftover in ("app.py", "big.bin"):
        target = project / leftover
        if target.exists():
            target.unlink()


def scope_json(identifier, allowed_paths):
    return {"title": "R05 验收项目", "goal": "验证大项目 status 降级语义",
            "audience": "验收执行者", "scenario": "容量验收",
            "out_of_scope": ["XL 以上档位"], "assumptions": ["温热页缓存"],
            "features": [{"id": identifier, "title": "代表功能",
                          "acceptance_criteria": ["status 可运行"],
                          "allowed_paths": allowed_paths,
                          "requires_user_acceptance": False,
                          "check_commands": [["/usr/bin/true"]]}]}


def timed_samples(runner, argv):
    """预热 WARMUP 次后采样 SAMPLES 次；每次校验退出码后取 runner 报告。"""
    for _ in range(WARMUP):
        out = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        if out.returncode != 0:
            raise RuntimeError("预热运行失败 exit=%d %s" % (out.returncode, out.stderr.decode()[:300]))
    samples = []
    for _ in range(SAMPLES):
        sample = runner.sample(argv)
        if sample["exit"] != 0:
            raise RuntimeError("计时样本运行失败 exit=%d" % sample["exit"])
        samples.append(sample)
    return samples


def summarize(samples):
    durations = sorted(s["seconds"] for s in samples)
    rss = max(s["rss_peak"] for s in samples)
    return {"n": len(samples), "p50_s": percentile(durations, 0.50),
            "p95_s": percentile(durations, 0.95), "min_s": durations[0],
            "max_s": durations[-1], "mean_s": round(sum(durations) / len(durations), 6),
            "rss_peak_max_bytes": rss, "raw": samples}


def env_facts(tmp_root):
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO), capture_output=True)
    mem = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True)
    return {
        "head_sha": git.stdout.decode().strip() if git.returncode == 0 else None,
        "branch": "optimization-v1",
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "ram_bytes": int(mem.stdout) if mem.returncode == 0 else None,
        "tmp_volume_free_bytes": shutil.disk_usage(tmp_root).free,
        "cpu_count": os.cpu_count(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="R05 L 档真实 CLI 实测")
    parser.add_argument("--output", required=True, help="证据 JSON 输出路径")
    parser.add_argument("--workdir", default=None, help="fixture 根（默认 tempfile.gettempdir()）")
    args = parser.parse_args(argv)

    tmp_root = Path(tempfile.gettempdir())
    workdir = Path(args.workdir) if args.workdir else tmp_root
    evidence = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "suite": "R05 status L 档真实 CLI 实测", "env": env_facts(tmp_root),
                "checks": []}

    def check(identifier, expected, observed, passed):
        evidence["checks"].append({"id": identifier, "expected": expected,
                                   "observed": observed, "result": "PASS" if passed else "FAIL"})

    # ── 1. fixture ────────────────────────────────────────────────────────
    big_project, big_fixture = fixture(100000, workdir, "L")
    small_project, small_fixture = fixture(48, workdir, "small")
    team_project, team_fixture = fixture(48, workdir, "team")
    evidence["fixtures"] = [big_fixture, small_fixture, team_fixture]
    for project in (big_project, small_project, team_project):
        reset_scratch(project)

    # ── 2. L 项目初始化与插桩取证 ─────────────────────────────────────────
    scope_path = workdir / "r05-evidence-scope.json"
    scope_path.write_text(json.dumps(scope_json("f1", ["pkg_00/sub_00/mod_000000.py"]),
                                     ensure_ascii=False), encoding="utf-8")
    init = run(cli(big_project, "init", "--input", str(scope_path)))
    confirm = run(cli(big_project, "confirm", "--revision", "1"))
    check("L-init-confirm-exit0", "init/confirm 在 10 万文件项目上可用（不扫源码）",
          {"init_exit": init["exit"], "confirm_exit": confirm["exit"]},
          init["exit"] == 0 and confirm["exit"] == 0)

    plugin_roots = [REPO / "dev-companion" / "scripts", REPO / "packages"]
    rules = build_rules(big_project, plugin_roots)
    status_argv = cli(big_project, "status", "--format", "json")
    instr, counts, tops = run_counted(status_argv, rules)
    view = json.loads(instr.stdout)
    check("L-status-exit0-paused-unavailable",
          "exit 0 且 capacity_status=paused、fingerprint_status=unavailable、进度不冒充",
          {"exit": instr.returncode,
           **{k: view.get(k) for k in ("capacity_status", "fingerprint_status",
                                       "source_fingerprint", "overall_percent")}},
          instr.returncode == 0 and view.get("capacity_status") == "paused"
          and view.get("fingerprint_status") == "unavailable"
          and view.get("source_fingerprint") is None
          and view.get("overall_percent") is None)
    evidence["l_instrumented_status"] = {
        "argv": status_argv, "exit": instr.returncode,
        "counts": counts, "walk_tops_detail": tops,
        "view_fields": {k: view.get(k) for k in
                        ("capacity_status", "fingerprint_status", "source_fingerprint",
                         "overall_percent", "publication", "next_step", "counts", "revision")}}
    st = counts["source_tree"]
    check("L-status-zero-source-reads",
          "源码树 open/scandir 计数=0、内容读取=0（R05 验收）",
          {"open": st["open_calls"], "scandir": st["scandir_calls"],
           "walk": st["walk_calls"], "read_bytes": st["read_bytes"]},
          False if (st["open_calls"] or st["scandir_calls"] or st["read_bytes"]) else True)
    packet_l = run(cli(big_project, "packet", "--feature", "f1"))
    check("L-write-path-still-hard-fails",
          "超限项目写路径仍硬拒绝（降级只作用于只读展示，不伪通过）",
          {"exit": packet_l["exit"], "stderr_head": packet_l["stderr"].decode()[:160]},
          packet_l["exit"] == 2 and "超出" in packet_l["stderr"].decode())

    # ── 3. 30 次温热计时 + 启动对照 ───────────────────────────────────────
    runner = TimedRunner()
    evidence["l_status_timings"] = summarize(timed_samples(runner, status_argv))
    evidence["l_status_timings"]["argv"] = status_argv
    startup_argv = [sys.executable, "-c", "pass"]
    evidence["startup_control"] = summarize(timed_samples(runner, startup_argv))
    evidence["startup_control"]["argv"] = startup_argv

    # ── 4. 小项目对照与历史验收展示 ───────────────────────────────────────
    small_scope = workdir / "r05-evidence-scope-small.json"
    small_scope.write_text(json.dumps(scope_json("f1", ["app.py"]), ensure_ascii=False),
                           encoding="utf-8")
    for step in (run(cli(small_project, "init", "--input", str(small_scope))),
                 run(cli(small_project, "confirm", "--revision", "1")),
                 run(cli(small_project, "packet", "--feature", "f1"))):
        if step["exit"] != 0:
            raise RuntimeError("小项目流程失败：" + step["stderr"].decode()[:300])
    packet = json.loads(step["stdout"])
    (small_project / "app.py").write_text("def total(values):\n    return sum(values)\n")
    receipt_payload = {"feature_id": "f1", "run_id": packet["run_id"],
                       "scope_version": packet["scope_version"], "status": "implemented",
                       "summary": "合计可用", "changed_files": ["app.py"], "evidence_files": []}
    receipt_path = workdir / "r05-evidence-receipt.json"
    receipt_path.write_text(json.dumps(receipt_payload), encoding="utf-8")
    for step in (run(cli(small_project, "receipt", "--input", str(receipt_path))),
                 run(cli(small_project, "check", "--feature", "f1")),
                 run(cli(small_project, "accept", "--feature", "f1", "--note", "真实检查通过"))):
        if step["exit"] != 0:
            raise RuntimeError("小项目验收流程失败：" + step["stderr"].decode()[:300])
    accepted_view = json.loads(run(cli(small_project, "status", "--format", "json"))["stdout"])
    check("small-accepted-normal-path", "低于阈值：accepted=1、progress=100（快照路径）",
          {"accepted": accepted_view["counts"]["accepted"],
           "overall_percent": accepted_view["overall_percent"]},
          accepted_view["counts"]["accepted"] == 1 and accepted_view["overall_percent"] == 100)
    instr_small, counts_small, _ = run_counted(
        cli(small_project, "status", "--format", "json"),
        build_rules(small_project, plugin_roots))
    evidence["small_instrumented_status_snapshot_path"] = {
        "argv": cli(small_project, "status", "--format", "json"),
        "exit": instr_small.returncode, "counts": counts_small}
    check("small-snapshot-path-source-reads-happen",
          "低于阈值走快照路径：源码树内容读取发生（如实记录阈值语义）",
          {"read_bytes": counts_small["source_tree"]["read_bytes"],
           "open": counts_small["source_tree"]["open_calls"]},
          counts_small["source_tree"]["open_calls"] > 0)
    (small_project / "big.bin").write_bytes(b"\0" * (21 * 1024 * 1024))
    instr_small2, counts_small2, _ = run_counted(
        cli(small_project, "status", "--format", "json"),
        build_rules(small_project, plugin_roots))
    degraded = json.loads(instr_small2.stdout)
    feature = degraded["features"][0]
    history_kinds = [event["kind"] for event in degraded.get("history", [])]
    evidence["small_degraded_after_accept"] = {
        "exit": instr_small2.returncode, "counts": counts_small2,
        "view_fields": {k: degraded.get(k) for k in
                        ("capacity_status", "fingerprint_status", "overall_percent")},
        "feature_status": feature["status"], "acceptance_retained": feature.get("acceptance"),
        "history_kinds": history_kinds}
    check("small-degraded-historical-acceptance-display",
          "超限后：accepted 不计数、状态降 awaiting_review、验收记录仍展示、历史事件保留",
          {"accepted_count": degraded["counts"]["accepted"],
           "feature_status": feature["status"],
           "acceptance_note": (feature.get("acceptance") or {}).get("note"),
           "has_accepted_event": "accepted" in history_kinds,
           "capacity_status": degraded.get("capacity_status")},
          degraded["counts"]["accepted"] == 0 and feature["status"] == "awaiting_review"
          and (feature.get("acceptance") or {}).get("note") == "真实检查通过"
          and "accepted" in history_kinds and degraded.get("capacity_status") == "paused")

    # ── 5. team 项目：team-status 分页、插桩与计时 ────────────────────────
    team_init = run(cli(team_project, "team-init", "--feature", "core", "核心功能"))
    if team_init["exit"] != 0:
        raise RuntimeError("team-init 失败：" + team_init["stderr"].decode()[:300])
    statuses = ("ready", "running", "done", "blocked")
    team_failures = []
    for index in range(TEAM_TASKS):
        out = run(cli(team_project, "team-task", "--set", "t%03d" % index,
                      "--feature", "core", "--status", statuses[index % len(statuses)]))
        if out["exit"] != 0:
            team_failures.append(out["stderr"].decode()[:120])
    check("team-task-120-writes", "120 次 team-task CLI 写入全部成功",
          {"failures": len(team_failures)}, not team_failures)
    page1 = json.loads(run(cli(team_project, "team-status", "--offset", "0",
                               "--limit", "100"))["stdout"])
    page2 = json.loads(run(cli(team_project, "team-status", "--offset", "100",
                               "--limit", "100"))["stdout"])
    check("team-status-pagination-cli",
          "CLI 分页：首页 100 条、has_more=True、次页 20 条且与首页不重叠",
          {"page1_tasks": len(page1["tasks"]["items"]), "page1_has_more": page1["tasks"]["has_more"],
           "page2_tasks": len(page2["tasks"]["items"])},
          len(page1["tasks"]["items"]) == 100 and page1["tasks"]["has_more"]
          and len(page2["tasks"]["items"]) == TEAM_TASKS - 100
          and not ({t["task_id"] for t in page1["tasks"]["items"]}
                   & {t["task_id"] for t in page2["tasks"]["items"]}))
    team_status_argv = cli(team_project, "team-status", "--offset", "0", "--limit", "100")
    instr_team, counts_team, _ = run_counted(team_status_argv,
                                             build_rules(team_project, plugin_roots))
    evidence["team_instrumented_status"] = {
        "argv": team_status_argv, "exit": instr_team.returncode, "counts": counts_team}
    check("team-status-zero-source-reads",
          "team-status 对存在源码树的项目零源码树读取（事实库经 sqlite3.connect）",
          {"source_open": counts_team["source_tree"]["open_calls"],
           "source_scandir": counts_team["source_tree"]["scandir_calls"],
           "source_read_bytes": counts_team["source_tree"]["read_bytes"],
           "facts_sqlite_connects": counts_team["facts"]["sqlite_connects"]},
          counts_team["source_tree"]["open_calls"] == 0
          and counts_team["source_tree"]["scandir_calls"] == 0
          and counts_team["source_tree"]["read_bytes"] == 0
          and counts_team["facts"]["sqlite_connects"] >= 1)
    evidence["team_status_timings"] = summarize(timed_samples(runner, team_status_argv))
    evidence["team_status_timings"]["argv"] = team_status_argv

    Path(args.output).write_text(json.dumps(evidence, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    failed = [c for c in evidence["checks"] if c["result"] == "FAIL"]
    print("EVIDENCE:", args.output)
    print("CHECKS: %d total, %d FAIL" % (len(evidence["checks"]), len(failed)))
    for item in failed:
        print("  FAIL", item["id"], json.dumps(item["observed"], ensure_ascii=False)[:300])
    print("L status p50=%.4fs p95=%.4fs RSS<=%dB | startup p50=%.4fs | team-status p50=%.4fs p95=%.4fs"
          % (evidence["l_status_timings"]["p50_s"], evidence["l_status_timings"]["p95_s"],
             evidence["l_status_timings"]["rss_peak_max_bytes"],
             evidence["startup_control"]["p50_s"],
             evidence["team_status_timings"]["p50_s"], evidence["team_status_timings"]["p95_s"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

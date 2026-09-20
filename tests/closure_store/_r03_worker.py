# -*- coding: utf-8 -*-
"""R03 跨进程 worker：每个 subprocess 调用是一个独立 Python 进程（禁止同对象内测恢复）。

仅操作持久事实：IntegrationBoard/ReviewBoard 的 JSON 检查点 + ResumeLedger
（kind=integration）。TaskBoard 是内存域模型，各进程按自己的视角重建（attempt
数与产出 sha 由调用方声明），跨进程一致性的唯一依据是检查点与台账。
输出一律单行 JSON 到 stdout；退出码 0=按预期完成（含"迟到回报被拒"这类预期失败）。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages"))

from agents_kernel.domain.review_record import attempt_id  # noqa: E402
from agents_kernel.domain.tasks import TaskBoard  # noqa: E402
from agents_kernel.services.integration import IntegrationBoard  # noqa: E402
from agents_kernel.services.review_board import ReviewBoard  # noqa: E402
from agents_kernel.services.resume import ResumeLedger, KIND_INTEGRATION  # noqa: E402
from agents_kernel.validation import CompanionError  # noqa: E402

T0 = "2026-09-20T10:00:00Z"
T1 = "2026-09-20T11:00:00Z"
REVIEWER = {"role": "companion-checker", "attempt_id": "CHK#1"}
CANDIDATE_ID = "C1"
TASK_ID = "T1"


def prepare(run_dir):
    run_dir = Path(run_dir)
    (run_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
    return run_dir


def checkpoint_paths(run_dir):
    checkpoints = Path(run_dir) / "checkpoints"
    return checkpoints / "integration_board.json", checkpoints / "review_board.json"


def build_stack(attempts, sha):
    """内存域模型重建：attempts=1 → 一次成功 attempt；attempts=2 → 首败后重建成功。"""
    board = TaskBoard()
    board.add_feature("f1", "示例功能", review_required=False)
    board.add_task(TASK_ID, "f1", "impl")
    board.transition_task(TASK_ID, "ready")
    board.transition_task(TASK_ID, "running")
    first = board.start_attempt(TASK_ID, T0)
    if attempts <= 1:
        review = ReviewBoard(board)
        review.submit_for_review(first, sha)
        board.finish_attempt(TASK_ID, "succeeded")
        return board, review, IntegrationBoard(board, review)
    board.finish_attempt(TASK_ID, "failed", failure_reason="首版联调失败")
    board.transition_task(TASK_ID, "ready")
    board.transition_task(TASK_ID, "running")
    second = board.start_attempt(TASK_ID, T1)
    review = ReviewBoard(board)
    review.submit_for_review(second, sha)
    board.finish_attempt(TASK_ID, "succeeded")
    return board, review, IntegrationBoard(board, review)


def open_ledger(run_dir, writer, integration_checkpoint, anchor=None):
    ledger = ResumeLedger.open(run_dir, writer)
    ledger.register(KIND_INTEGRATION, "board-1", str(integration_checkpoint), 1, source_anchor=anchor)
    return ledger


def save_all(run_dir, integration, review):
    integration_path, review_path = checkpoint_paths(run_dir)
    integration_sha = integration.save_checkpoint(integration_path)
    review_sha = review.save_checkpoint(review_path)
    return {"integration_sha256": integration_sha, "review_sha256": review_sha,
            "integration_checkpoint": str(integration_path), "review_checkpoint": str(review_path)}


def cmd_full(args):
    """建候选 → build 版本 →（可选回归/审批）→ 分阶段保存；--exit-hard 在审批后强退。"""
    board, review, integration = build_stack(1, args.sha)
    attempt = board.attempts(TASK_ID)[-1]
    candidate = integration.add_candidate(CANDIDATE_ID, {TASK_ID: args.sha})
    version = integration.build_integration_version([CANDIDATE_ID])
    integration_path, _ = checkpoint_paths(args.run_dir)
    ledger = open_ledger(args.run_dir, "r03-full", integration_path,
                         anchor=version["manifest_sha256"])
    saved = {}
    if args.save_candidate:
        saved = save_all(args.run_dir, integration, review)
    regression = None
    if args.regression:
        integration.record_feature_level(1, [{"feature_id": "f1", "status": "current"}])
        regression = integration.record_version_level(1, True, detail="跨进程全量回归通过")
    approval = None
    if args.approve:
        approval = review.record({"review_id": "rev-1", "subject_type": "attempt",
                                  "subject_ref": attempt_id(TASK_ID, attempt["attempt_no"]),
                                  "subject_sha256": args.sha, "reviewer": REVIEWER,
                                  "verdict": "approved", "blockers": [], "reviewed_at": T1})
        if args.exit_hard:
            print(json.dumps({"phase": "approved-but-unsaved"}))
            sys.stdout.flush()
            os._exit(9)
    if args.save_final:
        saved = save_all(args.run_dir, integration, review)
        ledger.complete(KIND_INTEGRATION, "board-1")
    return {"candidate": candidate, "version": version, "approval": approval,
            "regression": regression, "saved": saved}


def cmd_inspect(args):
    """只读持久记录（不写任何文件），并做一次还原探测（from_checkpoint 重建语义面）。"""
    integration_path, review_path = checkpoint_paths(args.run_dir)
    integration_state = json.loads(integration_path.read_text(encoding="utf-8"))
    review_state = json.loads(review_path.read_text(encoding="utf-8"))
    ledger_state = json.loads((Path(args.run_dir) / ".code-analysis-resume.json")
                              .read_text(encoding="utf-8"))
    board, review, _ = build_stack(1, "0" * 64)
    integration = IntegrationBoard.from_checkpoint(board, review, integration_path)
    review_restored = ReviewBoard.from_checkpoint(board, None, review_path)
    approvals = [dict(record) for record in review_state["records"]]
    approval = review_restored.current_approval(TASK_ID)
    return {"candidate": integration.candidate(CANDIDATE_ID),
            "versions": integration.versions(),
            "approvals": approvals,
            "current_approval": approval,
            "ledger_activities": ledger_state["activities"],
            "checkpoint_kinds": [integration_state["kind"], review_state["kind"]]}


def cmd_approve_redo(args):
    """中断后恢复：加载检查点，重做审批并落盘。"""
    integration_path, review_path = checkpoint_paths(args.run_dir)
    board, review, integration = build_stack(1, args.sha)
    review = ReviewBoard.from_checkpoint(board, None, review_path)
    integration = IntegrationBoard.from_checkpoint(board, review, integration_path)
    attempt = board.attempts(TASK_ID)[-1]
    record = review.record({"review_id": args.review_id, "subject_type": "attempt",
                            "subject_ref": attempt_id(TASK_ID, attempt["attempt_no"]),
                            "subject_sha256": args.sha, "reviewer": REVIEWER,
                            "verdict": "approved", "blockers": [], "reviewed_at": T1})
    saved = save_all(args.run_dir, integration, review)
    return {"record": record, "saved": saved}


def cmd_resubmit(args):
    """进程 B：按新 attempt（#2）固定新 sha 重入列候选（重建语义）；只写 integration 检查点。"""
    integration_path, _ = checkpoint_paths(args.run_dir)
    board, review, integration = build_stack(2, args.sha)
    integration = IntegrationBoard.from_checkpoint(board, review, integration_path)
    candidate = integration.add_candidate(CANDIDATE_ID, {TASK_ID: args.sha})
    integration.save_checkpoint(integration_path)
    return {"candidate": candidate}


def cmd_freshness(args):
    """进程 C：check_freshness 显式核对 → 校验出 superseded 并落盘。"""
    integration_path, _ = checkpoint_paths(args.run_dir)
    board, _, integration = build_stack(1, args.sha)
    integration = IntegrationBoard.from_checkpoint(board, None, integration_path)
    superseded = integration.check_freshness({TASK_ID: args.sha})
    integration.save_checkpoint(integration_path)
    return {"superseded": superseded}


def cmd_rebuild(args):
    """进程 D：重建新版本 → 版本级回归 → completed；台账锚与当前主体不符按 stale_anchor 呈现。"""
    integration_path, _ = checkpoint_paths(args.run_dir)
    ledger = ResumeLedger.open(args.run_dir, "r03-rebuild")
    stale = ledger.resume_plan(current_anchor=args.current_anchor)
    board, review, integration = build_stack(2, args.sha)
    integration = IntegrationBoard.from_checkpoint(board, review, integration_path)
    version = integration.build_integration_version([CANDIDATE_ID])
    integration.record_feature_level(version["integration_version"],
                                     [{"feature_id": "f1", "status": "current"}])
    integration.record_version_level(version["integration_version"], True, detail="重建后回归通过")
    completed = integration.complete(version["integration_version"])
    integration.save_checkpoint(integration_path)
    ledger.register(KIND_INTEGRATION, "board-1", str(integration_path), 1,
                    source_anchor=version["manifest_sha256"])
    ledger.complete(KIND_INTEGRATION, "board-1")
    return {"version": version, "completed": completed,
            "stale_anchor_conditions": [item["condition"] for item in stale]}


def cmd_late_report(args):
    """迟到回报：旧视角（attempt #1）的旧 sha 想覆盖已固定的新候选 → 必须被拒。"""
    integration_path, _ = checkpoint_paths(args.run_dir)
    board, review, integration = build_stack(1, args.sha)
    integration = IntegrationBoard.from_checkpoint(board, review, integration_path)
    try:
        integration.add_candidate(CANDIDATE_ID, {TASK_ID: args.sha})
    except CompanionError as error:
        return {"rejected": True, "error": str(error)}
    return {"rejected": False}


def cmd_duplicate(args):
    """重复回报：同内容候选重报 → 同版本号同哈希（幂等），不产生新版本。"""
    integration_path, review_path = checkpoint_paths(args.run_dir)
    before = json.loads(integration_path.read_text(encoding="utf-8"))
    board, review, integration = build_stack(1, args.sha)
    integration = IntegrationBoard.from_checkpoint(board, review, integration_path)
    integration.add_candidate(CANDIDATE_ID, {TASK_ID: args.sha})
    version = integration.build_integration_version([CANDIDATE_ID])
    integration.save_checkpoint(integration_path)
    after = json.loads(integration_path.read_text(encoding="utf-8"))
    return {"version_no": version["integration_version"],
            "manifest_sha256": version["manifest_sha256"],
            "versions_count": len(integration.versions()),
            "checkpoint_unchanged": before == after}


def cmd_ledger_hold(args):
    """持有旧 epoch 的进程：登记后等待，被接管后尝试心跳（预期被拒）。"""
    integration_path, _ = checkpoint_paths(args.run_dir)
    integration_path.write_text("{}", encoding="utf-8")  # 检查点占位（登记行存在性核对用）
    ledger = open_ledger(args.run_dir, args.writer, integration_path)
    signal = Path(args.run_dir) / ".hold-ready"
    stop = Path(args.run_dir) / ".hold-stop"
    signal.write_text("ready", encoding="utf-8")
    deadline = time.time() + 20
    while not stop.exists() and time.time() < deadline:
        time.sleep(0.05)
    if not stop.exists():
        return {"heartbeat_rejected": False, "error": "等待超时"}
    try:
        ledger.heartbeat(KIND_INTEGRATION, "board-1", note="迟到的心跳")
    except CompanionError as error:
        return {"heartbeat_rejected": True, "epoch": ledger.epoch, "error": str(error)}
    return {"heartbeat_rejected": False, "epoch": ledger.epoch}


def cmd_ledger_open(args):
    ledger = ResumeLedger.open(args.run_dir, args.writer)
    return {"epoch": ledger.epoch}


def main():
    parser = argparse.ArgumentParser()
    child = parser.add_subparsers(dest="command", required=True)
    spec = child.add_parser("full")
    spec.add_argument("--run-dir", required=True)
    spec.add_argument("--sha", required=True)
    for flag in ("save-candidate", "regression", "approve", "save-final", "exit-hard"):
        spec.add_argument("--" + flag, action="store_true", dest=flag.replace("-", "_"))
    spec.set_defaults(handler=cmd_full)
    for name, handler in (("inspect", cmd_inspect), ("approve-redo", cmd_approve_redo),
                          ("resubmit", cmd_resubmit), ("freshness", cmd_freshness),
                          ("rebuild", cmd_rebuild), ("late-report", cmd_late_report),
                          ("duplicate", cmd_duplicate), ("ledger-hold", cmd_ledger_hold),
                          ("ledger-open", cmd_ledger_open)):
        spec = child.add_parser(name)
        spec.add_argument("--run-dir", required=True)
        if name in ("approve-redo", "resubmit", "freshness", "duplicate", "late-report"):
            spec.add_argument("--sha", required=True)
        if name == "approve-redo":
            spec.add_argument("--review-id", default="rev-1")
        if name == "rebuild":
            spec.add_argument("--sha", required=True)
            spec.add_argument("--current-anchor", required=True, dest="current_anchor")
        if name.startswith("ledger-"):
            spec.add_argument("--writer", required=True)
        spec.set_defaults(handler=handler)
    args = parser.parse_args()
    args.run_dir = prepare(args.run_dir)
    result = args.handler(args)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

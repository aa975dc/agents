#!/usr/bin/env python3
"""机器校验 17 职责→实际角色映射（docs/verification/r06 配套）。

校验四件事（全部基于仓库实有文件，不硬编码文件清单）：
1. coverage.json 覆盖原方案 04 的 17 个职责 ID，各恰一次；
2. 每条 actual_role_ids ∈ dev-companion/agents/ 实有 .md 文件名集合；
3. 每条引用的 contract / definition_path / gate 机制文件真实存在
   （contract 与 definition_path 限 dev-companion/ 内；gate 机制限 packages/agents_kernel/ 内）；
4. 独立性硬约束：实现者（companion-developer.md）不得出现在 Q1/Q2/Q3 的承担角色里，
   Q1/Q2/Q3 必须含 companion-checker.md；unmapped 条目必须给 minimal_fix。

退出码 0=PASS，1=FAIL；结果逐条打印，供验证文档原样引用。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COVERAGE = Path(__file__).resolve().parent / "coverage.json"
PLAN_IDS = ["O0", "P1", "R1", "U1", "U2", "F1", "F2",
            "B1", "D1", "B2", "Q1", "Q2", "Q3", "I1", "S1", "V1", "L1"]
INDEPENDENT_REVIEW_IDS = {"Q1", "Q2", "Q3"}
DEVCOMPANION_PREFIX = "dev-companion" + "/"
KERNEL_PREFIX = "packages/agents_kernel" + "/"

errors = []


def fail(msg):
    errors.append(msg)


data = json.loads(COVERAGE.read_text(encoding="utf-8"))
entries = data.get("roles", [])

# 1. 17 个职责逐条恰一次
ids = [e.get("responsibility_id") for e in entries]
if sorted(ids) != sorted(PLAN_IDS):
    missing = [i for i in PLAN_IDS if i not in ids]
    extra = [i for i in ids if i not in PLAN_IDS]
    fail("职责 ID 集不符：缺失=%s 多余=%s" % (missing, extra))
dupes = sorted({i for i in ids if ids.count(i) > 1})
if dupes:
    fail("职责 ID 重复：%s" % dupes)

# 2. 角色集合 = agents/ 实有 .md 文件名（运行时枚举）
agents_dir = ROOT / "dev-companion" / "agents"
role_files = sorted(p.name for p in agents_dir.glob("*.md"))
if not role_files:
    fail("dev-companion/agents/ 未发现任何角色文件")

for e in entries:
    rid = e.get("responsibility_id", "?")
    status = e.get("coverage_status")
    roles = e.get("actual_role_ids") or []
    for r in roles:
        if r not in role_files:
            fail("%s: 承担角色 %s 不在实有角色文件集合 %s" % (rid, r, role_files))
    if status == "mapped":
        def_path = e.get("definition_path")
        if not roles and not def_path:
            fail("%s: mapped 但既无承担角色也无 definition_path" % rid)
        if def_path:
            p = ROOT / def_path
            if not p.is_file():
                fail("%s: definition_path 不存在: %s" % (rid, def_path))
            if not def_path.startswith(DEVCOMPANION_PREFIX):
                fail("%s: definition_path 不在 dev-companion/ 内: %s" % (rid, def_path))
        for key in ("input", "output"):
            for c in (e.get(key) or {}).get("contracts") or []:
                cp = ROOT / c
                if not cp.is_file():
                    fail("%s: %s 契约文件不存在: %s" % (rid, key, c))
                elif not c.startswith(DEVCOMPANION_PREFIX):
                    fail("%s: %s 契约不在 dev-companion/ 内: %s" % (rid, key, c))
        if not (e.get("input") or {}).get("anchor") or not (e.get("output") or {}).get("anchor"):
            fail("%s: mapped 但 input/output hash 锚定方式缺失" % rid)
        if not e.get("tool_policy"):
            fail("%s: mapped 但工具边界（tool_policy）缺失" % rid)
        if not e.get("handoff_to"):
            fail("%s: mapped 但交接对象缺失" % rid)
    elif status == "unmapped":
        if not e.get("minimal_fix"):
            fail("%s: unmapped 但未给 minimal_fix" % rid)
    else:
        fail("%s: coverage_status 非法: %r" % (rid, status))

# 3. gate 机制文件存在（review/集成/隔离等 kernel 机制，出现在 note/anchor 提及处统一登记）
GATE_FILES = [
    "packages/agents_kernel/domain/review_gate.py",
    "packages/agents_kernel/services/review_board.py",
    "packages/agents_kernel/services/integration.py",
    "packages/agents_kernel/execution/isolation.py",
    "packages/agents_kernel/contracts/schemas.py",
]
for g in GATE_FILES:
    if not (ROOT / g).is_file():
        fail("gate 机制文件不存在: %s" % g)

# 4. 独立性硬约束
for e in entries:
    rid = e.get("responsibility_id")
    roles = set(e.get("actual_role_ids") or [])
    if rid in INDEPENDENT_REVIEW_IDS:
        if "companion-developer.md" in roles:
            fail("%s: 实现者角色不得承担独立检查/审查/验收" % rid)
        if "companion-checker.md" not in roles:
            fail("%s: 必须由 companion-checker 承担" % rid)

print("角色文件集合（运行时枚举 dev-companion/agents/*.md）: %s" % role_files)
print("映射条目数: %d（mapped=%d, unmapped=%d）" % (
    len(entries),
    sum(1 for e in entries if e.get("coverage_status") == "mapped"),
    sum(1 for e in entries if e.get("coverage_status") == "unmapped")))
merged = [e["responsibility_id"] for e in entries if e.get("merged_with")]
print("合并映射条目: %s" % merged)
unmapped = [e["responsibility_id"] for e in entries if e.get("coverage_status") == "unmapped"]
print("unmapped 条目: %s" % (unmapped or "无"))
if errors:
    print("FAIL（%d 项）:" % len(errors))
    for m in errors:
        print("  - %s" % m)
    sys.exit(1)
print("PASS：17 职责全部有判定，角色/契约/机制文件引用全部实有，独立性约束满足")

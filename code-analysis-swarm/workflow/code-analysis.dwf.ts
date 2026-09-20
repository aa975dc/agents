/* zcode-workflow
description: 只读代码分析；结构闸门通过后交付报告，独立复查状态逐条保留
args:
  target:
    type: string
    required: true
    description: 目标仓库绝对路径
  team_root:
    type: string
    required: true
    description: code-analysis-swarm 安装目录的绝对路径
  output_root:
    type: string
    required: false
    description: 运行目录父目录绝对路径（目标仓库之外）；缺省时 helper 在宿主 workspace 下排他创建 .code-analysis-swarm-runs/<run_id>
  run_id:
    type: string
    required: false
    description: 本次唯一标识，只含字母数字下划线和连字符，不得复用；缺省时由 helper 用 secrets 生成
*/

// H06 第 3 轮注：AmendWorkflow 不会继承上一轮 args，必须随提交显式传入 target/team_root
// （此前两轮 blocked 于参数检查即因漏传 args，脚本本身零改动）。

// 预检、报告核实与制品存在性核验走 scripts/precheck.py（标准库 helper，经 world.run
// 固定 argv 调用，回执为 stdout 单行 JSON）：realpath/lstat/目录关系/排他 mkdir/
// 敏感路径拒绝均为确定性检查，不再采信代理自述布尔（Z02/Z09）。四个根目录见 helper 文档字符串。
// 有限回流（Z03）：G2/G3/G4/G5 的可修复失败（schema 字段缺失/格式、覆盖缺项、模块名/边端点
// 不闭合、声明制品未落盘）携带具体失败原因重问同一代理实例，初次+2 次修复尝试；路径逃逸等
// 硬失败与同错复发直接 blocked。blocked 时已完成 G4 的 confirmed 结论保留在返回值并发布
// 部分完成报告，不返回空 findings 抹掉成果。A7 只引用经 helper 核验真实存在的制品（Z11）。
function diag(run: { stdout: string; stderr: string }): string {
  const text = (run.stderr || run.stdout || "").split("\n").map(s => s.trim()).find(s => s.length > 0) ?? "";
  return text.slice(0, 200);
}
interface Claim { id: string; claim: string; source_role: string; evidence_refs: string[] }
interface Finding { id: string; where: string; what: string; evidence: string; severity: "low" | "medium" | "high"; confidence: number }
interface Chunk { id: string; files: string[]; loc_est: number; neighbors: string[]; rationale: string }
interface CodebaseMap {
  meta: { target: string; generated_at: string; tool: string };
  scan_status: "complete" | "empty" | "unreadable" | "partial";
  scan_evidence: string;
  source_files: string[];
  excluded: { path: string; reason: string }[];
  languages: Record<string, number>; loc_total: number;
  entry_points: { path: string; why: string; evidence: string }[];
  build_files: { path: string; kind: string }[];
  tree_summary: string; chunks: Chunk[];
}
interface ModuleInfo {
  name: string; responsibility: string; entry_files: string[];
  public_interfaces: string[]; depends_on: string[]; patterns: string[];
  contract_path: string;
}
interface ModuleResult {
  meta: { chunk_id: string; author: string };
  modules: ModuleInfo[];
  edges: { from: string; to: string; kind: string; source: string; from_contract?: boolean }[];
  findings: Finding[];
  coverage: { files_claimed: number; files_analyzed: number; analyzed_files: string[]; gaps: string[] };
}
interface Specialty {
  summary: string; details: string[]; findings: Finding[]; claims: Claim[];
  module_refs: string[]; build_files_covered: string[]; not_covered: string[];
}
interface Verdict { claim_id: string; verdict: "confirmed" | "refuted" | "unverified"; note: string; own_evidence: string }
interface VerdictBundle { verdicts: Verdict[] }
interface ReportFile { path: string; summary: string; sections: number[]; claim_ids: string[] }
interface WorkflowReport {
  status: "complete" | "partial" | "blocked";
  conclusion: string;
  findings: (Finding & { status: "verified" | "refuted" | "unverified" })[];
  claim_verdicts?: { claim_id: string; verdict: Verdict["verdict"] }[];
  // gate_checks 是结构闸门（机械检查）的通过记录；闸门通过≠事实已核verified，
  // 事实层面的核验结果以 findings[].status 与 claim_verdicts（A6 独立 verdict）为准。
  gate_checks: string[];
  not_covered: string[];
  coverage?: { files: number; files_analyzed: number; chunks: number; verdicts: number; confirmed: number; refuted: number; unverified: number };
}

function requireThat(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}
// 可修复回流类失败（Z03 分类）：schema 字段缺失/格式、覆盖缺项、模块名/边端点不闭合等。
class RepairableIssue extends Error {}
function soft(condition: unknown, message: string): asserts condition {
  if (!condition) throw new RepairableIssue(message);
}
function nonempty(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}
function list(value: unknown, label: string): asserts value is unknown[] {
  soft(Array.isArray(value), `${label} 必须是数组`);
}
function strings(value: unknown, label: string): asserts value is string[] {
  list(value, label);
  soft(value.every(nonempty), `${label} 含空值或非字符串`);
}
function unique(values: string[], label: string) {
  soft(new Set(values).size === values.length, `${label} 重复`);
}
function sameSet(actual: string[], expected: string[], label: string) {
  unique(actual, label);
  soft(actual.length === expected.length && actual.every(v => expected.includes(v)), `${label} 不闭合`);
}
function absolute(value: unknown, label: string): string {
  requireThat(nonempty(value), `${label} 不能为空`);
  const path = value.replace(/\\/g, "/").replace(/\/+$/, "");
  requireThat(/^(\/[^/]|[A-Za-z]:\/)/.test(path), `${label} 必须是非根目录的绝对路径（POSIX 或 Windows 盘符）`);
  requireThat(!path.includes("//") && !path.split("/").some(v => v === "." || v === "..") && !/[\x00-\x1f]/.test(path), `${label} 含相对段或控制字符`);
  return path;
}
function pathKey(path: string) {
  const normalized = absolute(path, "路径");
  return /^[A-Za-z]:/.test(normalized) ? normalized.toLowerCase() : normalized;
}
function inside(child: string, parent: string) {
  const c = pathKey(child), p = pathKey(parent);
  return c === p || c.startsWith(`${p}/`);
}
function findingsValid(findings: Finding[], label: string) {
  list(findings, label);
  for (const f of findings) {
    soft(f && nonempty(f.id) && nonempty(f.where) && nonempty(f.what) && nonempty(f.evidence), `${label} 缺少 ID/位置/结论/证据`);
    soft(/:[1-9][0-9]*(?:-[1-9][0-9]*)?$/.test(f.where), `${label} 位置必须带真实行号引用`);
    soft(["low", "medium", "high"].includes(f.severity) && Number.isFinite(f.confidence) && f.confidence >= 0 && f.confidence <= 1, `${label} 严重度或置信度无效`);
  }
  unique(findings.map(f => f.id), `${label} ID`);
}
function claimsValid(claims: Claim[]) {
  list(claims, "claims");
  for (const c of claims) {
    soft(c && nonempty(c.id) && nonempty(c.claim) && nonempty(c.source_role), "结论缺少 ID/内容/来源角色");
    strings(c.evidence_refs, "evidence_refs");
    soft(c.evidence_refs.length > 0, "结论没有证据引用");
  }
  unique(claims.map(c => c.id), "结论 ID");
}

// —— 有限回流机制（Z03，05 修复包 §6）——
// MAX_REPAIRS=2：每个代理实例最多"初次 + 2 次修复尝试"。失败分类：
//   可修复（RepairableIssue）→ 携带具体失败原因重新 ask 同一实例（agent 名稳定，上下文续接）；
//   硬阻断（路径逃逸/权限/未知副作用等普通 Error，或与上一次完全相同的失败复发）→ 立即抛出 blocked。
// askBudget/lastFailure 按代理名全局记账：同一实例的修复尝试不因换闸门而重置额度。
const MAX_REPAIRS = 2;
// 宿主禁止对 agent 取引用（含 typeof），也禁止重定型为本地结构接口（逃逸站点标识）；
// 缓存必须以 facade 自己的 Agent 接口类型持有，ask 调用点才可被日志/重放定位。
const actorCache = new Map<string, Agent>();
const askBudget = new Map<string, number>();
const lastFailure = new Map<string, string>();
function actorFor(name: string) {
  if (!actorCache.has(name)) actorCache.set(name, agent(name));
  return actorCache.get(name)!;
}
// ask 的类型参数必须是本脚本内声明的具体可序列化接口——泛型 T 无法证明可序列化，
// 因此 askGate 只承接回流循环，真正的 ask 调用（带具体类型）由调用点以回调传入。
async function askGate<T>(name: string, doAsk: (feedback: string) => Node<T>, validate: (value: T) => void): Promise<T> {
  let feedback = "";
  for (;;) {
    const used = askBudget.get(name) ?? 0;
    requireThat(used < 1 + MAX_REPAIRS, `${name} 修复尝试次数已用尽（初次+${MAX_REPAIRS} 次后仍不闭合）`);
    askBudget.set(name, used + 1);
    const value = await doAsk(feedback);
    try {
      validate(value);
      return value;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if ((askBudget.get(name) ?? 0) >= 1 + MAX_REPAIRS || !(error instanceof RepairableIssue) || lastFailure.get(name) === message) throw error;
      lastFailure.set(name, message);
      feedback = feedback ? `${feedback}\n${message}` : message;
    }
  }
}
// —— 制品存在性核验（Z11）：经 precheck.py check-files 确认 run_root 内真实存在且为非空普通文件。
// 缺失按 owner 路由为可修复回流项（重写后复检）；owner 为 null（A1 上游/无回流对象）时硬阻断。
async function ensureArtifacts(withinRoot: string, helperPath: string, items: { path: string; owner: string | null }[], purpose: string, repairNote: (owner: string, missing: string[]) => string): Promise<void> {
  const paths = items.map(i => i.path);
  for (;;) {
    let missing: string[] = [];
    if (paths.length > 0) {
      const run = await world.run("python3", [helperPath, "check-files", "--json", JSON.stringify({ within_root: withinRoot, paths })]);
      if (run.exitCode !== 0) {
        let parsed: { missing?: unknown } = {};
        try { parsed = JSON.parse(run.stdout); } catch { parsed = {}; }
        if (run.exitCode !== 3 || !Array.isArray(parsed.missing)) throw new Error(`制品存在性核验失败（exit ${run.exitCode}）：${diag(run)}`);
        missing = parsed.missing.filter(nonempty);
      }
    }
    if (missing.length === 0) return;
    const byOwner = new Map<string | null, string[]>();
    for (const path of missing) {
      const owner = items.find(i => i.path === path)?.owner ?? null;
      byOwner.set(owner, [...(byOwner.get(owner) ?? []), path]);
    }
    const hard = byOwner.get(null) ?? [];
    if (hard.length > 0) throw new Error(`${purpose}以下制品缺失且无可修复回流对象（上游 A1 或硬失败）：${hard.join("、")}`);
    for (const [owner, list] of byOwner) {
      if (owner === null) continue;
      const message = `${purpose}以下制品未真实落盘：${list.join("、")}`;
      if (lastFailure.get(owner) === message) throw new RepairableIssue(message);
      const used = askBudget.get(owner) ?? 0;
      if (used >= 1 + MAX_REPAIRS) throw new RepairableIssue(`${owner} ${message}（修复尝试次数已用尽）`);
      askBudget.set(owner, used + 1);
      lastFailure.set(owner, message);
      await actorFor(owner).ask(`${repairNote(owner, list)}\n${message}\n实际写出上述文件（内容须符合角色契约）后仅回复"已写出"，不要返回 JSON。`);
    }
  }
}

let stage = "参数检查";
let repoName = "未知仓库";
const gateChecks: string[] = [];
// partial 保留（05 §6）：阻断/部分完成时已取得的结论不抹掉——G2/G3 后暂存未验证 findings，
// G4 后以 A6 verdict 定级；blocked 结局把它们留在返回值，并（已有 confirmed verdict 时）发布部分完成报告。
let salvageFindings: WorkflowReport["findings"] = [];
let salvageClaims: { claim_id: string; verdict: Verdict["verdict"] }[] = [];
let salvageCoverage: NonNullable<WorkflowReport["coverage"]> | null = null;
try {
  const target = absolute(args.target, "target");
  const teamRoot = absolute(args.team_root, "team_root");
  const helperPath = `${teamRoot}/scripts/precheck.py`;
  const ROLE = `${teamRoot}/agents`;
  repoName = target.split("/").pop() ?? target;
  const explicitOutput = args.output_root === undefined || args.output_root === null ? null : absolute(args.output_root, "output_root");
  requireThat(explicitOutput === null || !inside(explicitOutput, target), "output_root 不能位于目标仓库中");
  requireThat(args.run_id === undefined || args.run_id === null || (typeof args.run_id === "string" && /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(args.run_id)), "run_id 无效");
  const explicitRunId = typeof args.run_id === "string" ? args.run_id : null;

  // 确定性预检（Z02/Z09）：world.run 固定 argv 调标准库 helper，plan 计算
  // 四根并校验目录关系/敏感路径/角色文件，acquire 排他创建 run_root 并落回执。
  stage = "路径预检";
  phase("路径预检");
  const precheckInput = { source_root: target, team_root: teamRoot, ...(explicitOutput ? { run_root_parent: explicitOutput } : {}), ...(explicitRunId ? { run_id: explicitRunId } : {}) };
  const planRun = await world.run("python3", [helperPath, "plan", "--json", JSON.stringify(precheckInput)]);
  requireThat(planRun.exitCode === 0, `预检 plan 失败（exit ${planRun.exitCode}）：${diag(planRun)}`);
  const plan = JSON.parse(planRun.stdout);
  requireThat(plan?.ok === true && typeof plan.run_id === "string" && /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(plan.run_id), "预检回执缺少有效 run_id");
  const acquireInput = { ...precheckInput, run_id: plan.run_id };
  const acquireRun = await world.run("python3", [helperPath, "acquire", "--json", JSON.stringify(acquireInput)]);
  requireThat(acquireRun.exitCode === 0, `运行目录排他创建失败（exit ${acquireRun.exitCode}）：${diag(acquireRun)}`);
  const precheck = JSON.parse(acquireRun.stdout);
  requireThat(precheck?.ok === true && precheck.mode === "mkdir_exclusive" && precheck.run_id === plan.run_id && nonempty(precheck.created_at), "运行目录回执不完整");
  const board = absolute(precheck.roots?.run_root?.realpath, "运行目录真实路径");
  const sourceReal = absolute(plan.roots?.source_root?.realpath, "目标真实路径");
  const teamReal = absolute(precheck.roots?.team_root?.realpath, "团队真实路径");
  requireThat(!inside(board, sourceReal) && !inside(sourceReal, board) && !inside(board, teamReal), "真实路径存在目标/运行/团队目录重叠");
  gateChecks.push(`路径预检：helper 排他创建 ${board}（run_id=${plan.run_id}，回执含 realpath 与创建时间）`);

  artifact.board("chunks", {
    title: "分块分析进度", key: "id", status: "status", columns: ["待分析", "已检查制品"],
    cardTitle: "title", detail: [{ field: "files", label: "文件数" }],
  });
  stage = "G1 勘察闭合";
  phase("G1 勘察闭合");
  const map = await agent("勘察员·罗经纬").ask<CodebaseMap>([
    `先读 ${ROLE}/a1-scout.md。只读目标 ${JSON.stringify(target)}；黑板 ${JSON.stringify(board)}；不得写目标或插件目录。`,
    `manifest 写 ${board}/manifest.json；返回完整同构 JSON，统一 snake_case，严格遵守 DESIGN.md 6.1。`,
    "必须给出 scan_status、scan_evidence、source_files 完整枚举、excluded 及原因。读取失败不能当作空目录。每块 ≤5000 LOC 且 ≤150 文件，按实际需要分块，不截断。",
  ].join("\n"));
  requireThat(map && map.scan_status === "complete" && nonempty(map.scan_evidence), `勘察未闭合：${map?.scan_status ?? "无状态"}；不能声称空库或已验证`);
  requireThat(map.meta && pathKey(map.meta.target) === pathKey(target) && nonempty(map.meta.generated_at) && map.meta.tool === "A1", "manifest 元数据缺失或目标不一致");
  requireThat(map.languages && typeof map.languages === "object" && !Array.isArray(map.languages) && Object.values(map.languages).every(n => Number.isInteger(n) && n >= 0), "语言统计字段无效");
  strings(map.source_files, "源文件清单");
  requireThat(map.source_files.length > 0, "源文件清单为空；本次未分析");
  unique(map.source_files.map(pathKey), "源文件路径");
  requireThat(map.source_files.every(f => inside(f, target)), "源文件超出目标范围");
  list(map.excluded, "排除清单");
  requireThat(map.excluded.every(e => nonempty(e.path) && nonempty(e.reason)), "排除项缺少原因");
  requireThat(Number.isInteger(map.loc_total) && map.loc_total >= 0 && nonempty(map.tree_summary), "勘察缺少规模或摘要");
  list(map.entry_points, "入口清单"); list(map.build_files, "构建清单"); list(map.chunks, "分块");
  requireThat(map.entry_points.every(e => inside(e.path, target) && nonempty(e.why) && nonempty(e.evidence)), "入口判定无证据或越界");
  requireThat(map.build_files.every(f => inside(f.path, target) && nonempty(f.kind)), "构建文件无类型或越界");
  unique(map.build_files.map(f => pathKey(f.path)), "构建文件");
  unique(map.chunks.map(c => c.id), "块 ID");
  for (const c of map.chunks) {
    requireThat(/^chunk-[A-Za-z0-9_-]+$/.test(c.id), "块 ID 无效");
    requireThat(nonempty(c.rationale), "分块依据缺失");
    strings(c.files, "块文件"); strings(c.neighbors, "邻块");
    requireThat(c.files.length > 0 && c.files.length <= 150 && Number.isInteger(c.loc_est) && c.loc_est >= 0 && c.loc_est <= 5000, `块 ${c.id} 尺寸不合格`);
    requireThat(c.neighbors.every(id => id !== c.id && map.chunks.some(n => n.id === id)), "邻块引用不存在");
  }
  sameSet(map.chunks.flatMap(c => c.files).map(pathKey), map.source_files.map(pathKey), "分块覆盖");
  gateChecks.push(`G1：${map.source_files.length} 个清单源文件分块覆盖闭合（枚举真实性依赖宿主扫描证据）`);
  for (const c of map.chunks) report({ id: c.id, title: c.id, files: c.files.length, status: "待分析" }, "chunks");

  // G2：单块 schema/覆盖校验失败 → 可修复回流到同一 A2 实例（Z03）；
  // from_contract 为可选布尔（缺省 false），进入 G2 schema 校验（Z10）。
  stage = "G2 块结果合格";
  phase("G2 块结果合格");
  const a2Name = (id: string) => `模块深读员·${id}`;
  function validateChunkResult(r: ModuleResult, c: Chunk) {
    soft(r.meta?.chunk_id === c.id && nonempty(r.meta.author), "返回块 ID 错配或作者缺失");
    list(r.modules, "模块"); list(r.edges, "依赖边"); findingsValid(r.findings, "模块发现");
    soft(r.coverage && r.coverage.files_claimed === c.files.length && r.coverage.files_analyzed === c.files.length, `块 ${c.id} 文件计数不闭合`);
    strings(r.coverage.analyzed_files, "已分析文件"); list(r.coverage.gaps, "阅读缺口");
    sameSet(r.coverage.analyzed_files.map(pathKey), c.files.map(pathKey), `块 ${c.id} 阅读覆盖`);
    soft(r.coverage.gaps.length === 0, `块 ${c.id} 有阅读缺口`);
    for (const m of r.modules) {
      soft(nonempty(m.name) && nonempty(m.responsibility), "模块没有名称或职责");
      strings(m.entry_files, "模块入口"); strings(m.public_interfaces, "公共接口"); strings(m.depends_on, "模块依赖"); strings(m.patterns, "模式");
      soft(m.entry_files.every(f => c.files.map(pathKey).includes(pathKey(f))), "模块入口超出块闭集");
      soft(nonempty(m.contract_path), "模块契约路径缺失");
      requireThat(inside(m.contract_path, `${board}/interfaces/${c.id}`) && m.contract_path.endsWith(".md"), "模块契约路径越界");
    }
    for (const e of r.edges) {
      soft(nonempty(e.from) && nonempty(e.to) && nonempty(e.source) && ["import", "call", "config"].includes(e.kind), "依赖边缺少出处或类型无效");
      soft(e.from_contract === undefined || typeof e.from_contract === "boolean", "依赖边 from_contract 必须是布尔（缺省 false）");
      if (e.from_contract === undefined) e.from_contract = false;
    }
  }
  let moduleResults = await Promise.all(map.chunks.map(c => askGate<ModuleResult>(a2Name(c.id), feedback => actorFor(a2Name(c.id)).ask<ModuleResult>([
    `先读 ${ROLE}/a2-module-analyst.md；按 DESIGN.md 6.2 返回完整 snake_case JSON。`,
    `目标与块文件闭集：${JSON.stringify({ target, chunk: c })}；摘要：${map.tree_summary}。`,
    `本块结果写 ${board}/chunks/${c.id}.json；契约只写 ${board}/interfaces/${c.id}/。`,
    "并行阶段没有已冻结的邻块契约，不依赖其他块完成顺序；跨块不明之处记录 gaps 并退回，不猜测。",
    "coverage 必含 analyzed_files 精确路径列表和 gaps；抽样/部分读取不能计作完整深读。findings.id 以块 ID 开头；findings.where 必须为「路径:行号」或「路径:起行-止行」格式（行号取自真实读取位置）；edges 必含 source，from_contract 可选布尔（缺省 false）。",
    feedback ? `你上一次返回未通过闸门，逐条修复后重新返回完整 JSON：\n- ${feedback.split("\n").join("\n- ")}` : "",
  ].filter(Boolean).join("\n")), r => validateChunkResult(r, c))));
  // G2 汇总：模块名跨块唯一、依赖边端点可归位（可修复回流，Z03）。
  function aggregateG2Issues(results: ModuleResult[], chunks: Chunk[]): { messages: string[]; affected: Set<string> } {
    const names = results.flatMap(r => r.modules.map(m => m.name));
    const seen = new Set<string>(); const duplicated = new Set<string>();
    for (const n of names) { if (seen.has(n)) duplicated.add(n); seen.add(n); }
    const nameSet = new Set(names);
    const messages: string[] = []; const affected = new Set<string>();
    if (duplicated.size > 0) {
      messages.push(`模块名跨块重复：${[...duplicated].join("、")}`);
      results.forEach((r, i) => { if (r.modules.some(m => duplicated.has(m.name))) affected.add(chunks[i].id); });
    }
    results.forEach((r, i) => {
      const bad = r.edges.filter(e => !nameSet.has(e.from) || !nameSet.has(e.to));
      if (bad.length > 0) {
        messages.push(`块 ${chunks[i].id} 依赖边端点不能归位：${bad.map(e => `${e.from}->${e.to}`).join("、")}`);
        affected.add(chunks[i].id);
      }
    });
    return { messages, affected };
  }
  for (let round = 0; ; round++) {
    const issues = aggregateG2Issues(moduleResults, map.chunks);
    if (issues.affected.size === 0) break;
    if (round >= MAX_REPAIRS) throw new RepairableIssue(`G2 汇总不闭合：${issues.messages.join("；")}`);
    const moduleNamesNow = moduleResults.flatMap(r => r.modules.map(m => m.name));
    const prev = moduleResults;
    moduleResults = await Promise.all(map.chunks.map(async (c, i) => {
      if (!issues.affected.has(c.id)) return prev[i];
      return await askGate<ModuleResult>(a2Name(c.id), () => actorFor(a2Name(c.id)).ask<ModuleResult>([
        "G2 汇总检查发现你此前返回的块结果存在以下问题，请修复后重新返回完整 JSON：",
        `- ${issues.messages.join("\n- ")}`,
        `当前模块全集（模块命名不得与之冲突；依赖边端点必须属于该全集或本块模块）：${JSON.stringify(moduleNamesNow)}`,
        `目标与块文件闭集不变：${JSON.stringify({ target, chunk: c })}；本块结果写 ${board}/chunks/${c.id}.json；coverage 与 findings 规则同前。`,
      ].join("\n")), r => validateChunkResult(r, c));
    }));
  }
  const modules = moduleResults.flatMap(r => r.modules);
  const moduleNames = modules.map(m => m.name);
  gateChecks.push(`G2：${map.chunks.length} 块返回的制品字段与逐文件覆盖检查通过`);
  // Z11：A1/A2 声明写出的黑板制品经 helper 确定性核验存在（缺失 → A2 可修复回流；manifest 属 A1，硬阻断）。
  stage = "G2 制品落盘核验";
  await ensureArtifacts(board, helperPath, [
    { path: `${board}/manifest.json`, owner: null },
    ...map.chunks.map(c => ({ path: `${board}/chunks/${c.id}.json`, owner: a2Name(c.id) })),
    ...moduleResults.flatMap((r, i) => r.modules.map(m => ({ path: m.contract_path, owner: a2Name(map.chunks[i].id) }))),
  ], "G2 制品核验：", (owner, list) => `${owner} 声明产出的以下制品经确定性核验不存在：${list.join("、")}`);

  stage = "G3 专项闭合";
  phase("G3 专项闭合");
  const specialtySpecs: [string, string, string][] = [
    ["a3-architect.md", "架构分析员·高屋建", "architecture"],
    ["a4-dependency.md", "依赖分析员·纲举目", "dependency"],
    ["a5-build.md", "构建分析员·步就班", "build"],
  ];
  function validateSpecialty(s: Specialty, kind: string): void {
    soft(nonempty(s.summary), "专项没有摘要");
    strings(s.details, "专项要点"); strings(s.module_refs, "模块引用"); strings(s.build_files_covered, "构建覆盖"); strings(s.not_covered, "未覆盖项");
    findingsValid(s.findings, "专项发现"); claimsValid(s.claims);
    soft(s.module_refs.every(n => moduleNames.includes(n)), "专项引用不存在的模块");
    if (kind === "architecture") soft(s.claims.length > 0, "架构判定没有送验结论");
    if (kind === "build") sameSet(s.build_files_covered.map(pathKey), map.build_files.map(f => pathKey(f.path)), "构建入口覆盖");
  }
  const specialties = await Promise.all(specialtySpecs.map(([role, name, kind]) => askGate<Specialty>(name, feedback => actorFor(name).ask<Specialty>([
    `先读 ${ROLE}/${role}。黑板 ${board}；目标 ${JSON.stringify(target)}；模块全集 ${JSON.stringify(moduleNames)}；构建清单 ${JSON.stringify(map.build_files)}。`,
    `按角色契约写 ${board}/specialty/${kind}.md 及所属图表；返回 DESIGN.md 6.9 的共同摘要（snake_case）。`,
    "A3 不读原始代码；A4/A5 只定点读清单/锁文件/配置；禁止执行安装、构建和项目脚本。",
    "summary/details/findings/claims/module_refs/build_files_covered/not_covered 字段必须齐全；claims 有唯一 ID、source_role 和非空 evidence_refs。未覆盖内容如实声明。",
    "findings.where 必须为「路径:行号」或「路径:起行-止行」格式，行号取自你实际读取到的位置；没有可引用行号的观察就不作为 finding。",
    feedback ? `你上一次返回未通过闸门，逐条修复后重新返回完整 JSON：\n- ${feedback.split("\n").join("\n- ")}` : "",
  ].filter(Boolean).join("\n")), s => validateSpecialty(s, kind))));
  stage = "G3 制品落盘核验";
  await ensureArtifacts(board, helperPath, [
    { path: `${board}/specialty/architecture.md`, owner: specialtySpecs[0][1] },
    { path: `${board}/specialty/dependency.md`, owner: specialtySpecs[1][1] },
    { path: `${board}/graph/internal-deps.csv`, owner: specialtySpecs[1][1] },
    { path: `${board}/graph/external-deps.csv`, owner: specialtySpecs[1][1] },
    { path: `${board}/specialty/build.md`, owner: specialtySpecs[2][1] },
  ], "G3 制品核验：", (owner, list) => `${owner} 按角色契约声明的以下制品经确定性核验不存在：${list.join("、")}`);
  const findings = [...moduleResults.flatMap(r => r.findings), ...specialties.flatMap(s => s.findings)];
  findingsValid(findings, "全部发现");
  salvageFindings = findings.map(f => ({ ...f, status: "unverified" }));
  gateChecks.push("G3：专项引用与已声明构建入口覆盖检查通过；未运行真实构建");

  stage = "G4 独立验证";
  phase("G4 独立验证");
  const toVerify: Claim[] = [
    ...specialties.flatMap(s => s.claims),
    ...map.entry_points.map((e, i) => ({ id: `entry-${i}`, claim: `程序入口：${e.path}；${e.why}`, source_role: "A1", evidence_refs: [e.evidence] })),
    // Z28：source_role 传角色名（与 a3/a4/a5 的 claims 一致），finding id 保留在 claim id 中，不再占用 source_role。
    ...findings.filter(f => f.severity === "high").map(f => ({ id: `finding:${f.id}`, claim: `${f.what}（${f.where}）`, source_role: "A2", evidence_refs: [f.where, f.evidence] })),
  ];
  claimsValid(toVerify);
  function validateVerdicts(vs: Verdict[]): void {
    list(vs, "verdicts");
    sameSet(vs.map(v => v.claim_id), toVerify.map(c => c.id), "送验结论与 verdict");
    for (const v of vs) {
      soft(["confirmed", "refuted", "unverified"].includes(v.verdict) && nonempty(v.note), "verdict 状态或说明无效");
      soft(v.verdict === "unverified" || nonempty(v.own_evidence), "独立 verdict 缺少自己的证据");
    }
  }
  const { verdicts } = await askGate<VerdictBundle>("交叉验证员·铁证如", feedback => actorFor("交叉验证员·铁证如").ask<VerdictBundle>([
    `先读 ${ROLE}/a6-verifier.md。目标 ${JSON.stringify(target)}；逐条复查全部结论 ${JSON.stringify(toVerify)}。`,
    `写 ${board}/verification/verdicts.json，返回 {verdicts:[{claim_id,verdict,note,own_evidence}]}。`,
    "独立读取与复查，不接收原完整论证。confirmed/refuted 均须自己的非空证据；无法复查用 unverified 并说明原因，不能当作 refuted。严禁限取前 N 条。",
    feedback ? `你上一次返回未通过闸门，逐条修复后重新返回完整 verdicts：\n- ${feedback.split("\n").join("\n- ")}` : "",
  ].filter(Boolean).join("\n")), v => validateVerdicts(v.verdicts));
  const confirmed = verdicts.filter(v => v.verdict === "confirmed").length;
  const refuted = verdicts.filter(v => v.verdict === "refuted").length;
  const unverified = verdicts.filter(v => v.verdict === "unverified").length;
  gateChecks.push(`G4：${toVerify.length} 条送验结论全部有状态；${confirmed} confirmed / ${refuted} refuted / ${unverified} unverified`);
  const verdictById = new Map(verdicts.map(v => [v.claim_id, v.verdict]));
  salvageFindings = findings.map(f => ({ ...f, status: verdictById.get(`finding:${f.id}`) === "confirmed" ? "verified" : verdictById.get(`finding:${f.id}`) === "refuted" ? "refuted" : "unverified" }));
  salvageClaims = verdicts.map(v => ({ claim_id: v.claim_id, verdict: v.verdict }));
  salvageCoverage = { files: map.source_files.length, files_analyzed: moduleResults.reduce((n, r) => n + r.coverage.files_analyzed, 0), chunks: map.chunks.length, verdicts: verdicts.length, confirmed, refuted, unverified };
  stage = "G4 制品落盘核验";
  await ensureArtifacts(board, helperPath, [
    { path: `${board}/verification/verdicts.json`, owner: "交叉验证员·铁证如" },
  ], "G4 制品核验：", (owner, list) => `${owner} 声明写出的以下制品经确定性核验不存在：${list.join("、")}`);

  stage = "G5 报告制品";
  phase("G5 报告制品");
  const coverage = salvageCoverage!; // G4 完成后必已赋值；未到 G4 就失败不会进入本阶段
  const notCovered = [
    "未运行真实构建/测试；静态分析不等于软件验收或开发完成",
    "源文件枚举与落盘内容依赖宿主工具和代理报告；当前运行时仅机械检查返回结构及引用闭合",
    "低/中严重度发现未逐条独立复查",
    ...map.excluded.map(e => `排除 ${e.path}：${e.reason}`),
    ...specialties.flatMap(s => s.not_covered),
    ...verdicts.filter(v => v.verdict === "unverified").map(v => `${v.claim_id} 未验证：${v.note}`),
  ];
  // Z11：A7 只允许引用以上经 helper 核验真实存在的制品清单，未列入的文件不得作为报告素材。
  const verifiedArtifacts = {
    manifest: `${board}/manifest.json`,
    chunks: map.chunks.map(c => `${board}/chunks/${c.id}.json`),
    interfaces: modules.map(m => m.contract_path),
    specialty: specialtySpecs.map(([, , kind]) => `${board}/specialty/${kind}.md`),
    graph_csv: [`${board}/graph/internal-deps.csv`, `${board}/graph/external-deps.csv`],
    verdicts: `${board}/verification/verdicts.json`,
  };
  const reportPath = `${board}/report/analysis-report.md`;
  const verifyRun = await world.run("python3", [helperPath, "verify", "--json", JSON.stringify({ source_root: target, run_root: board, outputs: [reportPath] })]);
  requireThat(verifyRun.exitCode === 0, `发布前目录关系复核失败（exit ${verifyRun.exitCode}）：${diag(verifyRun)}`);
  const reportFile = await askGate<ReportFile>("报告撰写员·文汇章", feedback => actorFor("报告撰写员·文汇章").ask<ReportFile>([
    `先读 ${ROLE}/a7-reporter.md，只组织已有结论。黑板 ${board}；仓库 ${repoName}；中文报告写 ${reportPath}。`,
    `只允许引用以下经确定性核验真实存在的黑板制品（清单之外的文件不得作为报告素材）：${JSON.stringify(verifiedArtifacts)}。`,
    `覆盖 ${JSON.stringify(coverage)}；verdicts ${JSON.stringify(verdicts)}；未覆盖 ${JSON.stringify(notCovered)}。`,
    `报告必须保留 refuted 与 unverified，不写成全部已验证。返回 path、summary、sections（0~8）和 claim_ids（本次送验全部 ID）：${JSON.stringify(toVerify.map(c => c.id))}。`,
    feedback ? `你上一次返回未通过闸门，逐条修复后重新返回：\n- ${feedback.split("\n").join("\n- ")}` : "",
  ].filter(Boolean).join("\n")), rf => {
    soft(nonempty(rf?.summary), "报告摘要为空");
    requireThat(pathKey(rf.path) === pathKey(reportPath), "报告路径越界");
    list(rf.sections, "报告章节"); strings(rf.claim_ids, "报告结论 ID");
    sameSet(rf.sections.map(String), Array.from({ length: 9 }, (_, i) => String(i)), "报告章节声明");
    sameSet(rf.claim_ids, toVerify.map(c => c.id), "报告送验记录声明");
  });
  // 不只信代理返回的 path：helper 核实报告真实存在、普通文件、sha256 与正文，
  // 正文经 artifact.markdown 发布；文件发布仅当 run_root 在宿主 workspace 内。
  const inspectRun = await world.run("python3", [helperPath, "read-report", "--json", JSON.stringify({ path: reportFile.path, within_root: board })]);
  requireThat(inspectRun.exitCode === 0, `报告文件无法核实（exit ${inspectRun.exitCode}）：${diag(inspectRun)}`);
  const inspected = JSON.parse(inspectRun.stdout);
  requireThat(inspected?.ok === true && Number.isInteger(inspected.size) && inspected.size > 0 && /^[0-9a-f]{64}$/.test(inspected.sha256) && nonempty(inspected.body), "报告文件内容或哈希回执无效");
  await artifact.markdown("report", inspected.body, { title: `《${repoName}》代码分析报告`, description: reportFile.summary, primary: true });
  let publicationBlocked: string | null = null;
  if (nonempty(inspected.publish_relpath)) {
    await artifact.file("report-file", inspected.publish_relpath, { title: `《${repoName}》代码分析报告文件`, description: `sha256 ${inspected.sha256}` });
  } else {
    publicationBlocked = `publication_blocked：运行目录在宿主 workspace 外，报告文件保留在 ${reportPath}（sha256 ${inspected.sha256}），未复制进源码`;
    notCovered.push(publicationBlocked);
  }
  gateChecks.push(`G5：报告经 read-report 核实（size=${inspected.size}，sha256 ${String(inspected.sha256).slice(0, 12)}…），正文已 artifact.markdown 发布${publicationBlocked ? "；文件发布受阻" : ""}`);
  return {
    status: unverified > 0 || specialties.some(s => s.not_covered.length > 0) ? "partial" : "complete",
    conclusion: reportFile.summary,
    findings: salvageFindings,
    claim_verdicts: salvageClaims,
    gate_checks: gateChecks,
    not_covered: notCovered,
    coverage,
  } satisfies WorkflowReport;
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  const notCoveredFinal = [`${stage} 及后续阶段未完成；已有黑板保留，不覆盖、不将失败声明为分析完成`];
  if (salvageClaims.length > 0) {
    notCoveredFinal.push(`本次为部分完成：阻断前已取得 ${salvageFindings.filter(f => f.status === "verified").length} 条 confirmed 发现与 ${salvageClaims.filter(c => c.verdict === "confirmed").length} 条 confirmed 结论，已保留在返回值中`);
  } else if (salvageFindings.length > 0) {
    notCoveredFinal.push(`阻断前已取得 ${salvageFindings.length} 条未经独立验证的发现（状态 unverified），保留在返回值中供参考`);
  }
  // partial 保留：已有 confirmed verdict 时发布"部分完成"报告，不返回空 findings 抹掉成果。
  if (salvageClaims.length > 0) {
    const confirmedCount = salvageFindings.filter(f => f.status === "verified").length;
    const refutedCount = salvageFindings.filter(f => f.status === "refuted").length;
    const unverifiedCount = salvageFindings.filter(f => f.status === "unverified").length;
    const body = [
      `# 《${repoName}》代码分析报告（部分完成）`,
      "",
      `> 本次运行阻断于「${stage}」：${message}。以下为阻断前已取得的结论；未完成阶段见文末缺口清单。`,
      "",
      "## 结论状态统计",
      `- 已验证（A6 独立复查 confirmed）：${confirmedCount}`,
      `- 被驳回（refuted，保留标注）：${refutedCount}`,
      `- 未验证（unverified）：${unverifiedCount}`,
      `- 送验结论 ${salvageClaims.length} 条：confirmed ${salvageClaims.filter(c => c.verdict === "confirmed").length} / refuted ${salvageClaims.filter(c => c.verdict === "refuted").length} / unverified ${salvageClaims.filter(c => c.verdict === "unverified").length}`,
      "",
      "## 发现（按阻断前验证状态保留）",
      ...salvageFindings.map(f => `- [${f.severity}/${f.status}] ${f.what}（${f.where}）证据：${f.evidence}`),
      "",
      "## 未完成与缺口",
      ...notCoveredFinal.map(s => `- ${s}`),
    ].join("\n");
    try {
      await artifact.markdown("partial-report", body, { title: `《${repoName}》代码分析报告（部分完成）`, description: `阻断于 ${stage}；confirmed 结论已保留` });
    } catch (publishError) {
      notCoveredFinal.push(`部分完成报告发布失败：${publishError instanceof Error ? publishError.message : String(publishError)}`);
    }
  }
  return {
    status: "blocked", conclusion: `${stage} 未通过：${message}`,
    findings: salvageFindings,
    claim_verdicts: salvageClaims.length > 0 ? salvageClaims : undefined,
    ...(salvageCoverage ? { coverage: salvageCoverage } : {}),
    gate_checks: gateChecks,
    not_covered: notCoveredFinal,
  } satisfies WorkflowReport;
}

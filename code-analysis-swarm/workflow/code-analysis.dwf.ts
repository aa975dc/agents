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

// 预检与报告核实走 scripts/precheck.py（标准库 helper，经 world.run 固定 argv 调用，
// 回执为 stdout 单行 JSON）：realpath/lstat/目录关系/排他 mkdir/敏感路径拒绝均为
// 确定性检查，不再采信代理自述布尔（Z02/Z09）。四个根目录见 helper 文档字符串。
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
  edges: { from: string; to: string; kind: string; source: string }[];
  findings: Finding[];
  coverage: { files_claimed: number; files_analyzed: number; analyzed_files: string[]; gaps: string[] };
}
interface Specialty {
  summary: string; details: string[]; findings: Finding[]; claims: Claim[];
  module_refs: string[]; build_files_covered: string[]; not_covered: string[];
}
interface Verdict { claim_id: string; verdict: "confirmed" | "refuted" | "unverified"; note: string; own_evidence: string }
interface ReportFile { path: string; summary: string; sections: number[]; claim_ids: string[] }
interface WorkflowReport {
  status: "complete" | "partial" | "blocked";
  conclusion: string;
  findings: (Finding & { status: "verified" | "refuted" | "unverified" })[];
  verified: string[];
  notCovered: string[];
  coverage?: { files: number; files_analyzed: number; chunks: number; verdicts: number; confirmed: number; refuted: number; unverified: number };
}

function requireThat(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}
function nonempty(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}
function list(value: unknown, label: string): asserts value is unknown[] {
  requireThat(Array.isArray(value), `${label} 必须是数组`);
}
function strings(value: unknown, label: string): asserts value is string[] {
  list(value, label);
  requireThat(value.every(nonempty), `${label} 含空值或非字符串`);
}
function unique(values: string[], label: string) {
  requireThat(new Set(values).size === values.length, `${label} 重复`);
}
function sameSet(actual: string[], expected: string[], label: string) {
  unique(actual, label);
  requireThat(actual.length === expected.length && actual.every(v => expected.includes(v)), `${label} 不闭合`);
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
    requireThat(f && nonempty(f.id) && nonempty(f.where) && nonempty(f.what) && nonempty(f.evidence), `${label} 缺少 ID/位置/结论/证据`);
    requireThat(/:[1-9][0-9]*(?:-[1-9][0-9]*)?$/.test(f.where), `${label} 位置必须带真实行号引用`);
    requireThat(["low", "medium", "high"].includes(f.severity) && Number.isFinite(f.confidence) && f.confidence >= 0 && f.confidence <= 1, `${label} 严重度或置信度无效`);
  }
  unique(findings.map(f => f.id), `${label} ID`);
}
function claimsValid(claims: Claim[]) {
  list(claims, "claims");
  for (const c of claims) {
    requireThat(c && nonempty(c.id) && nonempty(c.claim) && nonempty(c.source_role), "结论缺少 ID/内容/来源角色");
    strings(c.evidence_refs, "evidence_refs");
    requireThat(c.evidence_refs.length > 0, "结论没有证据引用");
  }
  unique(claims.map(c => c.id), "结论 ID");
}

let stage = "参数检查";
const checked: string[] = [];
try {
  const target = absolute(args.target, "target");
  const teamRoot = absolute(args.team_root, "team_root");
  const helperPath = `${teamRoot}/scripts/precheck.py`;
  const ROLE = `${teamRoot}/agents`;
  const repoName = target.split("/").pop();
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
  checked.push(`路径预检：helper 排他创建 ${board}（run_id=${plan.run_id}，回执含 realpath 与创建时间）`);

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
  checked.push(`G1：${map.source_files.length} 个清单源文件分块覆盖闭合（枚举真实性依赖宿主扫描证据）`);
  for (const c of map.chunks) report({ id: c.id, title: c.id, files: c.files.length, status: "待分析" }, "chunks");

  stage = "G2 块结果合格";
  phase("G2 块结果合格");
  const moduleResults = await Promise.all(map.chunks.map(async c => {
    const r = await agent(`模块深读员·${c.id}`).ask<ModuleResult>([
      `先读 ${ROLE}/a2-module-analyst.md；按 DESIGN.md 6.2 返回完整 snake_case JSON。`,
      `目标与块文件闭集：${JSON.stringify({ target, chunk: c })}；摘要：${map.tree_summary}。`,
      `本块结果写 ${board}/chunks/${c.id}.json；契约只写 ${board}/interfaces/${c.id}/。`,
      "并行阶段没有已冻结的邻块契约，不依赖其他块完成顺序；跨块不明之处记录 gaps 并退回，不猜测。",
      "coverage 必含 analyzed_files 精确路径列表和 gaps；抽样/部分读取不能计作完整深读。findings.id 以块 ID 开头；edges 必含 source。",
    ].join("\n"));
    requireThat(r.meta?.chunk_id === c.id && nonempty(r.meta.author), "返回块 ID 错配或作者缺失");
    list(r.modules, "模块"); list(r.edges, "依赖边"); findingsValid(r.findings, "模块发现");
    requireThat(r.coverage && r.coverage.files_claimed === c.files.length && r.coverage.files_analyzed === c.files.length, `块 ${c.id} 文件计数不闭合`);
    strings(r.coverage.analyzed_files, "已分析文件"); list(r.coverage.gaps, "阅读缺口");
    sameSet(r.coverage.analyzed_files.map(pathKey), c.files.map(pathKey), `块 ${c.id} 阅读覆盖`);
    requireThat(r.coverage.gaps.length === 0, `块 ${c.id} 有阅读缺口`);
    for (const m of r.modules) {
      requireThat(nonempty(m.name) && nonempty(m.responsibility), "模块没有名称或职责");
      strings(m.entry_files, "模块入口"); strings(m.public_interfaces, "公共接口"); strings(m.depends_on, "模块依赖"); strings(m.patterns, "模式");
      requireThat(m.entry_files.every(f => c.files.map(pathKey).includes(pathKey(f))), "模块入口超出块闭集");
      requireThat(inside(m.contract_path, `${board}/interfaces/${c.id}`) && m.contract_path.endsWith(".md"), "模块契约路径缺失或越界");
    }
    for (const e of r.edges) requireThat(nonempty(e.from) && nonempty(e.to) && nonempty(e.source) && ["import", "call", "config"].includes(e.kind), "依赖边缺少出处或类型无效");
    report({ id: c.id, title: c.id, files: c.files.length, status: "已检查制品" }, "chunks");
    return r;
  }));
  const modules = moduleResults.flatMap(r => r.modules);
  const moduleNames = modules.map(m => m.name);
  unique(moduleNames, "模块名");
  for (const e of moduleResults.flatMap(r => r.edges)) requireThat(moduleNames.includes(e.from) && moduleNames.includes(e.to), "依赖边端点不能归位");
  checked.push(`G2：${map.chunks.length} 块返回的制品字段与逐文件覆盖检查通过`);

  stage = "G3 专项闭合";
  phase("G3 专项闭合");
  const specialties = await Promise.all([
    ["a3-architect.md", "架构分析员·高屋建", "architecture"],
    ["a4-dependency.md", "依赖分析员·纲举目", "dependency"],
    ["a5-build.md", "构建分析员·步就班", "build"],
  ].map(async ([role, name, kind]) => {
    const s = await agent(name).ask<Specialty>([
      `先读 ${ROLE}/${role}。黑板 ${board}；目标 ${JSON.stringify(target)}；模块全集 ${JSON.stringify(moduleNames)}；构建清单 ${JSON.stringify(map.build_files)}。`,
      `按角色契约写 ${board}/specialty/${kind}.md 及所属图表；返回 DESIGN.md 6.9 的共同摘要（snake_case）。`,
      "A3 不读原始代码；A4/A5 只定点读清单/锁文件/配置；禁止执行安装、构建和项目脚本。",
      "summary/details/findings/claims/module_refs/build_files_covered/not_covered 字段必须齐全；claims 有唯一 ID、source_role 和非空 evidence_refs。未覆盖内容如实声明。",
    ].join("\n"));
    requireThat(nonempty(s.summary), "专项没有摘要");
    strings(s.details, "专项要点"); strings(s.module_refs, "模块引用"); strings(s.build_files_covered, "构建覆盖"); strings(s.not_covered, "未覆盖项");
    findingsValid(s.findings, "专项发现"); claimsValid(s.claims);
    requireThat(s.module_refs.every(n => moduleNames.includes(n)), "专项引用不存在的模块");
    if (kind === "architecture") requireThat(s.claims.length > 0, "架构判定没有送验结论");
    if (kind === "build") sameSet(s.build_files_covered.map(pathKey), map.build_files.map(f => pathKey(f.path)), "构建入口覆盖");
    return s;
  }));
  const findings = [...moduleResults.flatMap(r => r.findings), ...specialties.flatMap(s => s.findings)];
  findingsValid(findings, "全部发现");
  checked.push("G3：专项引用与已声明构建入口覆盖检查通过；未运行真实构建");

  stage = "G4 独立验证";
  phase("G4 独立验证");
  const toVerify: Claim[] = [
    ...specialties.flatMap(s => s.claims),
    ...map.entry_points.map((e, i) => ({ id: `entry-${i}`, claim: `程序入口：${e.path}；${e.why}`, source_role: "A1", evidence_refs: [e.evidence] })),
    ...findings.filter(f => f.severity === "high").map(f => ({ id: `finding:${f.id}`, claim: `${f.what}（${f.where}）`, source_role: f.id, evidence_refs: [f.where, f.evidence] })),
  ];
  claimsValid(toVerify);
  const { verdicts } = await agent("交叉验证员·铁证如").ask<{ verdicts: Verdict[] }>([
    `先读 ${ROLE}/a6-verifier.md。目标 ${JSON.stringify(target)}；逐条复查全部结论 ${JSON.stringify(toVerify)}。`,
    `写 ${board}/verification/verdicts.json，返回 {verdicts:[{claim_id,verdict,note,own_evidence}]}。`,
    "独立读取与复查，不接收原完整论证。confirmed/refuted 均须自己的非空证据；无法复查用 unverified 并说明原因，不能当作 refuted。严禁限取前 N 条。",
  ].join("\n"));
  list(verdicts, "verdicts");
  sameSet(verdicts.map(v => v.claim_id), toVerify.map(c => c.id), "送验结论与 verdict");
  for (const v of verdicts) {
    requireThat(["confirmed", "refuted", "unverified"].includes(v.verdict) && nonempty(v.note), "verdict 状态或说明无效");
    requireThat(v.verdict === "unverified" || nonempty(v.own_evidence), "独立 verdict 缺少自己的证据");
  }
  const confirmed = verdicts.filter(v => v.verdict === "confirmed").length;
  const refuted = verdicts.filter(v => v.verdict === "refuted").length;
  const unverified = verdicts.filter(v => v.verdict === "unverified").length;
  checked.push(`G4：${toVerify.length} 条送验结论全部有状态；${confirmed} confirmed / ${refuted} refuted / ${unverified} unverified`);

  stage = "G5 报告制品";
  phase("G5 报告制品");
  const coverage = { files: map.source_files.length, files_analyzed: moduleResults.reduce((n, r) => n + r.coverage.files_analyzed, 0), chunks: map.chunks.length, verdicts: verdicts.length, confirmed, refuted, unverified };
  const notCovered = [
    "未运行真实构建/测试；静态分析不等于软件验收或开发完成",
    "源文件枚举与落盘内容依赖宿主工具和代理报告；当前运行时仅机械检查返回结构及引用闭合",
    "低/中严重度发现未逐条独立复查",
    ...map.excluded.map(e => `排除 ${e.path}：${e.reason}`),
    ...specialties.flatMap(s => s.not_covered),
    ...verdicts.filter(v => v.verdict === "unverified").map(v => `${v.claim_id} 未验证：${v.note}`),
  ];
  const reportPath = `${board}/report/analysis-report.md`;
  const verifyRun = await world.run("python3", [helperPath, "verify", "--json", JSON.stringify({ source_root: target, run_root: board, outputs: [reportPath] })]);
  requireThat(verifyRun.exitCode === 0, `发布前目录关系复核失败（exit ${verifyRun.exitCode}）：${diag(verifyRun)}`);
  const reportFile = await agent("报告撰写员·文汇章").ask<ReportFile>([
    `先读 ${ROLE}/a7-reporter.md，只组织已有结论。黑板 ${board}；仓库 ${repoName}；中文报告写 ${reportPath}。`,
    `覆盖 ${JSON.stringify(coverage)}；verdicts ${JSON.stringify(verdicts)}；未覆盖 ${JSON.stringify(notCovered)}。`,
    `报告必须保留 refuted 与 unverified，不写成全部已验证。返回 path、summary、sections（0~8）和 claim_ids（本次送验全部 ID）：${JSON.stringify(toVerify.map(c => c.id))}。`,
  ].join("\n"));
  requireThat(pathKey(reportFile.path) === pathKey(reportPath) && nonempty(reportFile.summary), "报告路径越界或摘要为空");
  list(reportFile.sections, "报告章节"); strings(reportFile.claim_ids, "报告结论 ID");
  sameSet(reportFile.sections.map(String), Array.from({ length: 9 }, (_, i) => String(i)), "报告章节声明");
  sameSet(reportFile.claim_ids, toVerify.map(c => c.id), "报告送验记录声明");
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
  checked.push(`G5：报告经 read-report 核实（size=${inspected.size}，sha256 ${String(inspected.sha256).slice(0, 12)}…），正文已 artifact.markdown 发布${publicationBlocked ? "；文件发布受阻" : ""}`);
  const verdictById = new Map(verdicts.map(v => [v.claim_id, v.verdict]));
  const result: WorkflowReport = {
    status: unverified > 0 || specialties.some(s => s.not_covered.length > 0) ? "partial" : "complete",
    conclusion: reportFile.summary,
    findings: findings.map(f => ({ ...f, status: verdictById.get(`finding:${f.id}`) === "confirmed" ? "verified" : verdictById.get(`finding:${f.id}`) === "refuted" ? "refuted" : "unverified" })),
    verified: checked, notCovered, coverage,
  };
  return result;
} catch (error) {
  return {
    status: "blocked", conclusion: `${stage} 未通过：${error instanceof Error ? error.message : String(error)}`,
    findings: [], verified: checked,
    notCovered: [`${stage} 及后续阶段未完成；已有黑板保留，不覆盖、不将失败声明为分析完成`],
  } satisfies WorkflowReport;
}

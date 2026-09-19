/* zcode-workflow
description: 代码分析智能团——对目标仓库做结构/模块/架构/依赖/构建的全面分析，产出带证据的分析报告
args:
  target:
    type: string
    required: true
    description: 目标仓库的绝对路径，例如 D:\projects\my-app
*/

// ============ 类型契约（与 .analysis-team/DESIGN.md 第 6 节对齐） ============

interface Chunk {
  /** 块编号，全局唯一，形如 chunk-01。 */
  id: string;
  /** 该块的源文件闭集（绝对路径）。 */
  files: string[];
  /** 有依赖关系的邻块编号。 */
  neighbors: string[];
}

interface CodebaseMap {
  /** 主语言 -> 文件数。 */
  languages: Record<string, number>;
  /** 估算总 LOC。 */
  locTotal: number;
  /** 程序入口文件（绝对路径）。 */
  entryPoints: string[];
  /** 构建/工程配置文件（绝对路径）。 */
  buildFiles: string[];
  /** 顶层目录一句话摘要。 */
  treeSummary: string;
  /** 分块方案。 */
  chunks: Chunk[];
}

interface Finding {
  /** 结论位置：绝对路径，适用时带行号。 */
  where: string;
  /** 一句话：发现了什么。 */
  what: string;
  /** 证据：读到的代码行或命令输出摘要。 */
  evidence: string;
  /** 严重度。 */
  severity: "low" | "medium" | "high";
}

interface ModuleInfo {
  /** 模块名，遵循仓库已有命名。 */
  name: string;
  /** 一句话职责。 */
  responsibility: string;
  /** 入口文件。 */
  entryFiles: string[];
  /** 导出签名摘要。 */
  publicInterfaces: string[];
  /** 依赖的其他模块名。 */
  dependsOn: string[];
  /** 观察到的设计模式。 */
  patterns: string[];
}

interface ModuleResult {
  chunkId: string;
  modules: ModuleInfo[];
  findings: Finding[];
}

interface Claim {
  /** 一句话关键判定，供独立验证。 */
  claim: string;
  /** 产出该判定的角色。 */
  sourceRole: string;
}

interface Specialty {
  /** 两三句话的维度总结。 */
  summary: string;
  /** 结构化要点，每条一句。 */
  details: string[];
  findings: Finding[];
  claims: Claim[];
}

interface Verdict {
  claim: string;
  /** confirmed = 独立复查成立；refuted = 证据不成立或有反例。 */
  verdict: "confirmed" | "refuted";
  /** 复查方法与所见。 */
  note: string;
}

interface ReportFile {
  /** 报告文件的工作区相对路径。 */
  path: string;
  /** 三句话摘要。 */
  summary: string;
}

interface ReportedFinding extends Finding {
  /** verified = 验证员确认；unverified = 未经独立验证。 */
  status: "verified" | "unconfirmed";
}

interface WorkflowReport {
  /** 两三句话回答用户要什么。 */
  conclusion: string;
  findings: ReportedFinding[];
  /** 本次运行检查了什么、怎么查的。 */
  verified: string[];
  /** 没看什么、为什么。 */
  notCovered: string[];
}

// ============ 运行参数与黑板 ============

const target = String(args.target ?? "");
const repoName = target.split(/[\\/]/).filter(Boolean).pop() ?? "repo";
const board = `analysis/${repoName}`;
// 绝对路径：本工作流注册为全局后，在任意窗口/任意工作区运行都必须能定位到这套角色定义
const ROLE = "C:/Users/G/.zcode/workspace/default/.analysis-team/agents";

if (!target) {
  return {
    conclusion: "未提供目标仓库路径（args.target），无法启动分析。",
    findings: [],
    verified: [],
    notCovered: ["全部——没有目标"],
  } satisfies WorkflowReport;
}

// 分块进度看板：大库分析时用户盯的就是它
artifact.board("chunks", {
  title: "分块分析进度",
  key: "id",
  status: "status",
  columns: ["待分析", "完成"],
  cardTitle: "title",
  detail: [{ field: "files", label: "文件数" }],
});

// ============ 阶段 1：勘察 ============

phase("勘察目标代码库并规划分块方案");
const map = await agent("勘察员·罗经纬").ask<CodebaseMap>(
  [
    `先读 ${ROLE}/a1-scout.md，严格按该角色的职责、决策边界与硬约束执行。`,
    `【任务参数】目标仓库：${target}。黑板：${board}/（manifest 写到 ${board}/manifest.json，目录不存在则创建）。`,
    `要求：分块 6~20 块、每块 ≤5k LOC 且 ≤150 源文件、覆盖全部源文件；返回紧凑 JSON。`,
  ].join("\n"),
);

const chunks = map.chunks.slice(0, 24);
if (map.chunks.length === 0) {
  return {
    conclusion: `勘察发现 ${target} 无可分析源文件（空仓库或不可读），已停止。`,
    findings: [],
    verified: ["勘察阶段确认仓库无可分析源文件"],
    notCovered: ["全部——目标为空"],
  } satisfies WorkflowReport;
}
for (const c of map.chunks) {
  report({ id: c.id, title: `块 ${c.id}`, files: c.files.length, status: "待分析" }, "chunks");
}
log(`勘察完成：${map.locTotal} LOC、${map.chunks.length} 块、入口 ${map.entryPoints.length} 个`);

// ============ 阶段 2：并行深读各块 ============

phase("并行深读各代码块");
const moduleResults = await Promise.all(
  chunks.map(async (c) => {
    const result = await agent(`模块深读员·${c.id}`).ask<ModuleResult>(
      [
        `先读 ${ROLE}/a2-module-analyst.md，严格按该角色执行。`,
        `【任务参数】目标仓库：${target}；块 ${c.id} 文件闭集：${c.files.join(", ")}；`,
        `邻块契约目录：${board}/interfaces/（存在则参考，不存在说明你是第一个完成的块）；`,
        `chunk 结果写 ${board}/chunks/${c.id}.json；全局摘要：${map.treeSummary}。`,
      ].join("\n"),
    );
    report({ id: c.id, title: `块 ${c.id}`, files: c.files.length, status: "完成" }, "chunks");
    return result;
  }),
);
const allModules = moduleResults.flatMap((r) => r.modules);
const allFindings = moduleResults.flatMap((r) => r.findings);
const moduleNameList = allModules.map((m) => m.name).join(", ");
log(`深读完成：${allModules.length} 个模块、${allFindings.length} 条发现`);

// ============ 阶段 3：专项分析（架构 / 依赖 / 构建 并行） ============

phase("专项分析架构、依赖与构建流程");
const [architecture, dependency, build] = await Promise.all([
  agent("架构分析员·高屋建").ask<Specialty>(
    [
      `先读 ${ROLE}/a3-architect.md，严格按该角色执行。`,
      `【任务参数】黑板：${board}/（读 manifest.json 与 chunks/*.json，不读原始代码）；`,
      `目标仓库根（仅核对文件存在性）：${target}；结果写 ${board}/specialty/architecture.md。`,
    ].join("\n"),
  ),
  agent("依赖分析员·纲举目").ask<Specialty>(
    [
      `先读 ${ROLE}/a4-dependency.md，严格按该角色执行。`,
      `【任务参数】目标仓库：${target}；黑板：${board}/（edges 在 chunks/*.json 的 edges 字段）；`,
      `模块全集：${moduleNameList}；结果写 ${board}/specialty/dependency.md 与 ${board}/graph/*.csv。`,
    ].join("\n"),
  ),
  agent("构建分析员·步就班").ask<Specialty>(
    [
      `先读 ${ROLE}/a5-build.md，严格按该角色执行（默认静态分析，不执行构建）。`,
      `【任务参数】目标仓库：${target}；构建文件清单：${map.buildFiles.join(", ") || "（勘察未标记，请自行识别并回报差异）"}；`,
      `结果写 ${board}/specialty/build.md。`,
    ].join("\n"),
  ),
]);
const specialtyFindings = [architecture, dependency, build].flatMap((s) => s.findings);
const claims = [architecture, dependency, build].flatMap((s) => s.claims);
log(`专项完成：架构 ${architecture.details.length} 要点 / 依赖 ${dependency.details.length} 要点 / 构建 ${build.details.length} 要点`);

// ============ 阶段 4：交叉验证关键结论 ============

phase("交叉验证关键结论");
const highFindings = [...allFindings, ...specialtyFindings].filter((f) => f.severity === "high");
const toVerify = [...claims, ...highFindings.map((f) => ({ claim: `${f.what}（${f.where}）`, sourceRole: "A2/专项" }))].slice(0, 20);
const verdicts = await agent("交叉验证员·铁证如").ask<{ verdicts: Verdict[] }>(
  [
    `先读 ${ROLE}/a6-verifier.md，严格按该角色执行——独立复查，不复述原论证。`,
    `【任务参数】目标仓库：${target}；待验证结论：${JSON.stringify(toVerify)}；`,
    `verdicts 写 ${board}/verification/verdicts.json。保守判定：证据不足即 refuted 并注明。`,
  ].join("\n"),
);
const confirmedClaims = new Set(verdicts.verdicts.filter((v) => v.verdict === "confirmed").map((v) => v.claim));
log(`验证完成：${verdicts.verdicts.filter((v) => v.verdict === "confirmed").length} confirmed / ${verdicts.verdicts.filter((v) => v.verdict === "refuted").length} refuted`);

// ============ 阶段 5：汇总撰写报告 ============

phase("汇总撰写分析报告");
const coverage = {
  chunks: chunks.length,
  droppedChunks: map.chunks.length - chunks.length,
  modules: allModules.length,
  loc: map.locTotal,
  findings: allFindings.length + specialtyFindings.length,
  verifiedClaims: confirmedClaims.size,
};
const reportFile = await agent("报告撰写员·文汇章").ask<ReportFile>(
  [
    `先读 ${ROLE}/a7-reporter.md，严格按该角色执行——只组织已有结论，不产生新结论。`,
    `【任务参数】仓库：${repoName}；黑板：${board}/（全部制品）；覆盖统计：${JSON.stringify(coverage)}；`,
    `验证结论：${JSON.stringify(verdicts.verdicts)}；报告语言：中文；`,
    `报告写到 ${board}/report/analysis-report.md 并返回路径与三句话摘要。`,
  ].join("\n"),
);

try {
  await artifact.file("report", reportFile.path, {
    title: `《${repoName}》全面分析报告`,
    description: reportFile.summary,
    primary: true,
  });
} catch {
  await agent("报告修复员").ask(
    `${reportFile.path} 缺失或不可发布。请按黑板制品重新撰写报告到该路径（结构遵循 ${ROLE}/a7-reporter.md）。`,
  );
  await artifact.file("report", reportFile.path, { title: `《${repoName}》全面分析报告`, primary: true });
}

const reportedFindings: ReportedFinding[] = [...allFindings, ...specialtyFindings].map((f) => ({
  ...f,
  status: confirmedClaims.has(`${f.what}（${f.where}）`) ? "verified" : "unconfirmed",
}));
const notCovered = [
  `低/中严重度发现未逐条独立验证（仅高危与关键判定送验，共验证 ${toVerify.length} 条）`,
  ...(map.chunks.length > 24 ? [`分块超过 24，仅分析前 24 块，剩余 ${map.chunks.length - 24} 块未覆盖`] : []),
];
const result: WorkflowReport = {
  conclusion: reportFile.summary,
  findings: reportedFindings,
  verified: [
    `${chunks.length} 个分块全部深读；架构/依赖/构建三个专项闭合`,
    `关键判定与高危发现经独立验证：${confirmedClaims.size} 条 confirmed`,
  ],
  notCovered,
};
return result;

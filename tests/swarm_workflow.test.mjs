import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';

// Real workflow body, fake only its existing host APIs. This does not test ZCode,
// the LLM's truthfulness, filesystem operations or artifact file contents.
const root = new URL('../code-analysis-swarm/', import.meta.url);
const source = readFileSync(new URL('workflow/code-analysis.dwf.ts', root), 'utf8');
const compiled = stripTypeScriptTypes(`async function workflow() {\n${source}\n}`);
const execute = new Function('args', 'agent', 'artifact', 'phase', 'report', 'log', 'world', `${compiled}\nreturn workflow();`);

async function run(options = {}) {
  const args = { target: '/repos/app', team_root: '/plugins/analysis', output_root: '/reports', run_id: 'run-001', ...options.args };
  const board = `${args.output_root}/${args.run_id}`;
  const files = Array.from({ length: options.chunks ?? 2 }, (_, i) => `${args.target}/file-${i}.js`);
  const map = {
    meta: { target: args.target, generated_at: '2026-09-20', tool: 'A1' }, scan_status: 'complete', scan_evidence: 'mock enumeration output',
    source_files: files, excluded: [], languages: { JavaScript: files.length }, loc_total: files.length * 10,
    entry_points: [{ path: files[0], why: 'package bin', evidence: `${args.target}/package.json:4` }],
    build_files: [{ path: `${args.target}/package.json`, kind: 'npm' }], tree_summary: 'fixture',
    chunks: files.map((file, i) => ({ id: `chunk-${i}`, files: [file], loc_est: 10, neighbors: [], rationale: 'fixture' })),
  };
  const calls = [], cards = [], publications = [], worldCalls = [];
  let claims = [];
  // world.run 桩：伪造真实宿主返回结构 {exitCode, stdout, stderr}。
  // 首参恒为字面量 "python3"，子命令与 JSON 输入在 args 数组里（无 shell 拼接）。
  const state = { planExit: 0, acquireExit: 0, acquireStdout: null, verifyExit: 0, reportExit: 0, mutateAcquire: null, mutateInspect: null, missingArtifacts: null };
  options.world?.(state);
  const worldRoots = payload => ({
    source_root: { input: payload.source_root, realpath: payload.source_root, lstat_kind: 'directory' },
    team_root: { realpath: payload.team_root },
    host_workspace_root: { realpath: '/workspace' },
    run_root: { realpath: `${payload.run_root_parent ?? '/workspace/.code-analysis-swarm-runs'}/${payload.run_id ?? 'run-001'}`, publish_relpath: null },
  });
  const world = { run: async (cmd, argv) => {
    worldCalls.push([cmd, argv]);
    if (cmd !== 'python3') throw new Error(`Unexpected command ${cmd}`);
    const payload = JSON.parse(argv[3]);
    if (argv[1] === 'plan') {
      if (state.planExit) return { exitCode: state.planExit, stdout: '', stderr: 'mock plan failure' };
      const roots = worldRoots(payload); state.mutatePlan?.(roots);
      return { exitCode: 0, stdout: JSON.stringify({ ok: true, run_id: payload.run_id ?? 'run-001', run_root_exists: false, roots, checks: {} }), stderr: '' };
    }
    if (argv[1] === 'acquire') {
      if (state.acquireExit) return { exitCode: state.acquireExit, stdout: '', stderr: 'mock acquire conflict' };
      if (state.acquireStdout !== null) return { exitCode: 0, stdout: state.acquireStdout, stderr: '' };
      const roots = worldRoots(payload); state.mutateAcquire?.(roots);
      return { exitCode: 0, stdout: JSON.stringify({ ok: true, run_id: payload.run_id, created_at: '2026-09-20T00:00:00+00:00', mode: 'mkdir_exclusive', roots }), stderr: '' };
    }
    if (argv[1] === 'verify') {
      if (state.verifyExit) return { exitCode: state.verifyExit, stdout: '', stderr: 'mock verify failure' };
      return { exitCode: 0, stdout: JSON.stringify({ ok: true }), stderr: '' };
    }
    if (argv[1] === 'check-files') {
      // Z11 制品存在性核验桩：missingArtifacts 列出的路径报 exit 3 + missing 数组，其余存在。
      const missing = (state.missingArtifacts ?? []).filter(p => payload.paths.includes(p));
      if (missing.length) return { exitCode: 3, stdout: JSON.stringify({ ok: false, kind: 'conflict', error: 'mock missing artifacts', missing }), stderr: '' };
      return { exitCode: 0, stdout: JSON.stringify({ ok: true, files: payload.paths.map(p => ({ path: p, kind: 'regular', size: 10 })) }), stderr: '' };
    }
    if (argv[1] === 'read-report') {
      if (state.reportExit) return { exitCode: state.reportExit, stdout: '', stderr: 'mock read failure' };
      const inspect = { ok: true, path: payload.path, realpath: payload.path, size: 42, sha256: 'a'.repeat(64), body: '# Fixture analysis report\n', publish_relpath: `reports/${args.run_id}/report/analysis-report.md` };
      state.mutateInspect?.(inspect);
      return { exitCode: 0, stdout: JSON.stringify(inspect), stderr: '' };
    }
    throw new Error(`Unexpected world.run subcommand ${argv[1]}`);
  } };
  const askCounts = new Map();
  const agent = name => ({ ask: async prompt => {
    calls.push({ name, prompt });
    // attempt：该代理名（同一实例）的第几次 ask，供回流用例按次注入不同失败。
    const attempt = (askCounts.get(name) ?? 0) + 1;
    askCounts.set(name, attempt);
    if (name === '勘察员·罗经纬') { options.map?.(map); return map; }
    if (name.startsWith('模块深读员·')) {
      const id = name.split('·')[1], chunk = map.chunks.find(c => c.id === id);
      const r = {
        meta: { chunk_id: id, author: 'A2' },
        modules: [{ name: id, responsibility: 'fixture module', entry_files: chunk.files,
          public_interfaces: [], depends_on: [], patterns: [], contract_path: `${board}/interfaces/${id}/module.md` }],
        edges: [], findings: [{ id: `${id}:1`, where: `${chunk.files[0]}:1`, what: 'fixture risk', evidence: 'observed fixture line', severity: 'high', confidence: 0.8 }],
        coverage: { files_claimed: chunk.files.length, files_analyzed: chunk.files.length, analyzed_files: [...chunk.files], gaps: [] },
      };
      options.chunk?.(r, id, attempt); return r;
    }
    if (['架构分析员·高屋建', '依赖分析员·纲举目', '构建分析员·步就班'].includes(name)) {
      const role = name.startsWith('架构') ? 'A3' : name.startsWith('依赖') ? 'A4' : 'A5';
      const s = { summary: `${role} summary`, details: ['fixture'], findings: [],
        claims: [{ id: `${role}:1`, claim: 'fixture conclusion', source_role: role, evidence_refs: [`${files[0]}:1`] }],
        module_refs: map.chunks.map(c => c.id), build_files_covered: role === 'A5' ? map.build_files.map(f => f.path) : [], not_covered: [] };
      options.specialty?.(s, role); return s;
    }
    if (name === '交叉验证员·铁证如') {
      // 回流修复 ask 不含送验清单，此时沿用上一次解析出的 claims。
      const marker = '逐条复查全部结论 ';
      const at = prompt.indexOf(marker);
      if (at >= 0) claims = JSON.parse(prompt.slice(at + marker.length).split('。\n')[0]);
      const r = { verdicts: claims.map(c => ({ claim_id: c.id, verdict: 'confirmed', note: 'read independently', own_evidence: `${files[0]}:2` })) };
      options.verdicts?.(r); return r;
    }
    if (name === '报告撰写员·文汇章') {
      const r = { path: `${board}/report/analysis-report.md`, summary: 'Fixture analysis report', sections: [0,1,2,3,4,5,6,7,8], claim_ids: claims.map(c => c.id) };
      options.report?.(r); return r;
    }
    throw new Error(`Unexpected agent ${name}`);
  } });
  const artifact = { board() {}, async markdown(...args) { publications.push(['markdown', ...args]); }, async file(...args) { if (options.publishFailure) throw new Error('artifact missing'); publications.push(['file', ...args]); } };
  const result = await execute(args, agent, artifact, () => {}, card => cards.push(card), () => {}, world);
  return { result, calls, cards, publications, claims, worldCalls };
}

test('full workflow uses snake_case artifacts and preserves >24 chunks / >20 claims', async () => {
  const { result, calls, claims } = await run({ chunks: 27 });
  assert.equal(result.status, 'complete');
  assert.equal(result.coverage.chunks, 27);
  assert.equal(result.coverage.files_analyzed, 27);
  assert.equal(claims.length, 31);
  assert.equal(result.coverage.confirmed, 31);
  assert.equal(calls.filter(c => c.name.startsWith('模块深读员·')).length, 27);
});

test('portable explicit team root reaches all seven role prompts', async () => {
  const { result, calls } = await run({ args: { team_root: '/Users/test/plugin directory' } });
  assert.equal(result.status, 'complete');
  for (const role of ['a1-scout', 'a2-module-analyst', 'a3-architect', 'a4-dependency', 'a5-build', 'a6-verifier', 'a7-reporter']) {
    assert(calls.some(c => c.prompt.includes(`/Users/test/plugin directory/agents/${role}.md`)));
  }
  assert(!source.includes('C:/Users/G'));
});

test('output inside target is rejected before any agent or helper run', async () => {
  const { result, calls, worldCalls } = await run({ args: { output_root: '/repos/app/reports' } });
  assert.equal(result.status, 'blocked'); assert.equal(calls.length, 0); assert.equal(worldCalls.length, 0);
});

test('duplicate separators cannot bypass lexical containment', async () => {
  const { result, calls } = await run({ args: { output_root: '/repos//app/reports' } });
  assert.equal(result.status, 'blocked'); assert.equal(calls.length, 0);
});

test('Windows paths compare case-insensitively for output containment', async () => {
  const { result, calls } = await run({ args: { target: 'C:\\Repos\\App', output_root: 'c:\\repos\\APP\\reports' } });
  assert.equal(result.status, 'blocked'); assert.equal(calls.length, 0);
});

// 预检已改为 world.run 调 scripts/precheck.py 的确定性回执；失败时无任何分析代理被调用。
for (const [name, setup] of [
  ['unreadable target', w => { w.planExit = 2; }],
  ['existing run directory', w => { w.acquireExit = 3; }],
  ['acquire receipt realpath inside target', w => { w.mutateAcquire = r => { r.run_root.realpath = '/repos/app/linked'; }; }],
  ['corrupt receipt', w => { w.acquireStdout = 'not json'; }],
  ['incomplete receipt', w => { w.acquireStdout = JSON.stringify({ ok: true, run_id: 'run-001', roots: { run_root: { realpath: '/reports/run-001' }, team_root: { realpath: '/plugins/analysis' } } }); }],
]) test(`helper preflight rejects ${name}`, async () => {
  const { result, calls } = await run({ world: setup });
  assert.equal(result.status, 'blocked'); assert.equal(calls.length, 0);
});

for (const [name, map] of [
  ['duplicate files', m => m.chunks[1].files = m.chunks[0].files],
  ['omitted chunk', m => m.chunks.pop()],
  ['duplicate chunk IDs', m => m.chunks[1].id = m.chunks[0].id],
  ['missing scan evidence', m => m.scan_evidence = ''],
  ['missing required language statistics', m => delete m.languages],
  ['unreadable source', m => m.scan_status = 'unreadable'],
  ['empty inventory', m => { m.source_files = []; m.chunks = []; }],
  ['oversize chunk', m => m.chunks[0].loc_est = 5001],
  ['unknown neighbor', m => m.chunks[0].neighbors = ['chunk-missing']],
]) test(`G1 blocks ${name} without a completed analysis claim`, async () => {
  const { result, calls } = await run({ map });
  // 预检成功会留下一条闸门检查记录；G1 失败时不得再有任何分析声明。
  assert.equal(result.status, 'blocked'); assert.equal(result.gate_checks.length, 1);
  assert(result.gate_checks[0].startsWith('路径预检'));
  assert(!calls.some(c => c.name.startsWith('模块深读员·')));
});

for (const [name, chunk] of [
  ['finding without evidence', r => r.findings[0].evidence = ''],
  ['finding without line reference', r => r.findings[0].where = '/repos/app/file.js'],
  ['wrong file despite matching count', r => r.coverage.analyzed_files = ['/repos/app/other.js']],
  ['partial file read', r => r.coverage.gaps = ['only read export signatures']],
  ['wrong chunk ID', r => r.meta.chunk_id = 'chunk-wrong'],
  ['contract path escape', r => r.modules[0].contract_path = '/repos/app/overwrite.md'],
  ['dependency without evidence', r => r.edges = [{ from: r.modules[0].name, to: r.modules[0].name, kind: 'import', source: '' }]],
]) test(`G2 blocks ${name}`, async () => {
  const { result, calls } = await run({ chunk });
  assert.equal(result.status, 'blocked'); assert(!calls.some(c => c.name === '架构分析员·高屋建'));
});

// —— 有限回流（Z03）：可修复失败携带原因重问同一实例，初次+2 次修复尝试；硬失败立即阻断 ——
test('repairable G2 failure flows back to the same A2 actor with reasons and recovers', async () => {
  const { result, calls } = await run({ chunk(r, id, attempt) { if (id === 'chunk-0' && attempt === 1) r.findings[0].evidence = ''; } });
  assert.equal(result.status, 'complete');
  const asks = calls.filter(c => c.name === '模块深读员·chunk-0');
  assert.equal(asks.length, 2);
  assert.match(asks[1].prompt, /未通过闸门[\s\S]*缺少 ID\/位置\/结论\/证据/);
});

test('G2 blocks only on the third failed attempt for distinct repairable issues', async () => {
  const { result, calls } = await run({ chunk(r, id, attempt) {
    if (id !== 'chunk-0') return;
    if (attempt === 1) r.findings[0].evidence = '';
    if (attempt === 2) r.coverage.gaps = ['stopped early'];
    if (attempt === 3) r.findings[0].where = '/repos/app/file-0.js';
  } });
  assert.equal(result.status, 'blocked');
  assert.equal(calls.filter(c => c.name === '模块深读员·chunk-0').length, 3);
  assert.match(result.conclusion, /位置必须带真实行号引用/);
});

test('identical recurring failure blocks without a third attempt', async () => {
  const { result, calls } = await run({ chunk(r, id) { if (id === 'chunk-0') r.findings[0].evidence = ''; } });
  assert.equal(result.status, 'blocked');
  assert.equal(calls.filter(c => c.name === '模块深读员·chunk-0').length, 2);
});

test('path escape in a chunk artifact blocks immediately without reflow', async () => {
  const { result, calls } = await run({ chunk(r, id) { if (id === 'chunk-0') r.modules[0].contract_path = '/repos/app/overwrite.md'; } });
  assert.equal(result.status, 'blocked');
  assert.equal(calls.filter(c => c.name === '模块深读员·chunk-0').length, 1);
  assert.match(result.conclusion, /越界/);
});

// —— from_contract 进入 G2 schema 校验（Z10）：类型错误可修复回流，缺省合法 ——
test('from_contract is schema-checked: wrong type flows back, absent field defaults to false', async () => {
  const { result, calls } = await run({ chunk(r, id, attempt) {
    if (id !== 'chunk-1') return;
    if (attempt === 1) r.edges = [{ from: 'chunk-1', to: 'chunk-0', kind: 'import', from_contract: 'yes', source: '/repos/app/file-1.js:3' }];
    else r.edges = [{ from: 'chunk-1', to: 'chunk-0', kind: 'import', source: '/repos/app/file-1.js:3' }];
  } });
  assert.equal(result.status, 'complete');
  const asks = calls.filter(c => c.name === '模块深读员·chunk-1');
  assert.equal(asks.length, 2);
  assert.match(asks[1].prompt, /from_contract 必须是布尔/);
});

test('G3 rejects missing build coverage', async () => {
  const { result } = await run({ specialty(s, role) { if (role === 'A5') s.build_files_covered = []; } });
  assert.equal(result.status, 'blocked'); assert.match(result.conclusion, /构建入口覆盖/);
});

for (const [name, verdicts] of [
  ['missing result', r => r.verdicts.pop()],
  ['duplicate result', r => r.verdicts[1] = r.verdicts[0]],
  ['confirmation without own evidence', r => r.verdicts[0].own_evidence = ''],
  ['refutation without own evidence', r => { r.verdicts[0].verdict = 'refuted'; r.verdicts[0].own_evidence = ''; }],
]) test(`G4 rejects ${name}`, async () => {
  const { result, publications } = await run({ verdicts });
  assert.equal(result.status, 'blocked'); assert.equal(publications.length, 0);
});

test('refuted is preserved and uncertainty stays unverified/partial', async () => {
  const { result } = await run({ verdicts(r) {
    const finding = r.verdicts.find(v => v.claim_id.startsWith('finding:'));
    finding.verdict = 'refuted';
    r.verdicts[0] = { ...r.verdicts[0], verdict: 'unverified', own_evidence: '', note: 'file unavailable' };
  } });
  assert.equal(result.status, 'partial');
  assert.equal(result.findings[0].status, 'refuted');
  assert.equal(result.coverage.refuted, 1); assert.equal(result.coverage.unverified, 1);
  assert(result.not_covered.some(s => s.includes('file unavailable')));
});

for (const [name, report] of [
  ['external report path', r => r.path = '/repos/app/README.md'],
  ['missing chapter', r => r.sections.pop()],
  ['missing claim record', r => r.claim_ids.pop()],
]) test(`G5 rejects ${name}`, async () => {
  const { result, publications } = await run({ report });
  assert.equal(result.status, 'blocked');
  // 未通过闸门的报告主制品不得发布；blocked 后只允许"部分完成"报告（partial-report）。
  assert(publications.every(p => p[0] !== 'file'));
  assert(publications.every(p => p[1] !== 'report' && p[1] !== 'report-file'));
});

// —— partial 保留（Z03/05 §6）：blocked 结局不抹掉已 confirmed 的成果 ——
test('blocked run preserves confirmed findings/claims and publishes a partial report', async () => {
  const { result, publications } = await run({ report(r) { r.sections.pop(); } });
  assert.equal(result.status, 'blocked');
  assert.ok(result.findings.length > 0);
  assert.ok(result.findings.every(f => f.status === 'verified'));
  assert.ok(result.claim_verdicts.length > 0);
  assert.ok(result.claim_verdicts.every(c => c.verdict === 'confirmed'));
  const partial = publications.find(p => p[0] === 'markdown' && p[1] === 'partial-report');
  assert.ok(partial, '应发布部分完成报告');
  assert.match(String(partial[2]), /部分完成/);
  assert.ok(result.not_covered.some(s => s.includes('部分完成')));
});

// —— Z11：A7 只引用核验过存在的制品；声明产出但未落盘 → 回流后仍缺失则 blocked ——
test('missing interface contract blocks before specialties and A7 are dispatched', async () => {
  const { result, calls } = await run({ world: w => { w.missingArtifacts = ['/reports/run-001/interfaces/chunk-0/module.md']; } });
  assert.equal(result.status, 'blocked');
  assert.match(result.conclusion, /未真实落盘/);
  assert(!calls.some(c => ['架构分析员·高屋建', '报告撰写员·文汇章'].includes(c.name)));
});

test('missing verdicts artifact blocks after reflow and preserves confirmed findings', async () => {
  const { result, calls, publications } = await run({ world: w => { w.missingArtifacts = ['/reports/run-001/verification/verdicts.json']; } });
  assert.equal(result.status, 'blocked');
  assert(!calls.some(c => c.name === '报告撰写员·文汇章'));
  assert.ok(result.findings.length > 0 && result.findings.every(f => f.status === 'verified'));
  assert.ok(publications.some(p => p[1] === 'partial-report'));
});

test('artifact failure cannot repair or publish an unvalidated arbitrary path', async () => {
  const { result, calls } = await run({ publishFailure: true });
  assert.equal(result.status, 'blocked');
  assert(!calls.some(c => c.name === '报告修复员'));
});

test('ZCode plugin manifest and all seven agents expose documented metadata', () => {
  const manifest = JSON.parse(readFileSync(new URL('.zcode-plugin/plugin.json', root), 'utf8'));
  assert.equal(manifest.name, 'code-analysis-swarm'); assert.equal(manifest.version, '0.2.1');
  assert.equal(manifest.commands, 'command'); assert.equal(manifest.agents, 'agents');
  assert.deepEqual(readdirSync(new URL('command/', root)), ['swarm-analyze.md']);
  assert.match(readFileSync(new URL('command/swarm-analyze.md', root), 'utf8'), /# \/swarm-analyze/);
  const agents = readdirSync(new URL('agents/', root)); assert.equal(agents.length, 7);
  for (const file of agents) {
    const body = readFileSync(new URL(`agents/${file}`, root), 'utf8');
    assert.match(body, /^---\nname: [a-z0-9-]+\ndescription: .+\n---\n/);
  }
});

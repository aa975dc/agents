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
  // SR-06：bigFiles 注入 > 阈值 200 的清单规模走索引分页；缺省 totalFiles = 块数（≤200 小库直读）。
  const totalFiles = options.bigFiles ?? (options.chunks ?? 2);
  const chunkCount = options.chunks ?? 2;
  const perChunk = Math.ceil(totalFiles / chunkCount);
  const files = Array.from({ length: totalFiles }, (_, i) => `${args.target}/file-${i}.js`);
  const map = {
    meta: { target: args.target, generated_at: '2026-09-20', tool: 'A1' }, scan_status: 'complete', scan_evidence: 'mock enumeration output',
    source_files: files, excluded: [], languages: { JavaScript: totalFiles }, loc_total: totalFiles * 10,
    entry_points: [{ path: files[0], why: 'package bin', evidence: `${args.target}/package.json:4` }],
    build_files: [{ path: `${args.target}/package.json`, kind: 'npm' }], tree_summary: 'fixture',
    chunks: Array.from({ length: chunkCount }, (_, i) => ({ id: `chunk-${i}`, files: files.slice(i * perChunk, (i + 1) * perChunk), loc_est: 10, neighbors: [], rationale: 'fixture' })),
  };
  const calls = [], cards = [], publications = [], worldCalls = [], a2Asked = [];
  // FIX05 大库 A2 结果构造器：ask 返回与黑板制品（续接回载经 read-report 读回）同构。
  const a2Result = id => {
    const chunk = map.chunks.find(c => c.id === id);
    return {
      meta: { chunk_id: id, author: 'A2' },
      modules: [{ name: id, responsibility: 'fixture module', entry_files: chunk.files,
        public_interfaces: [], depends_on: [], patterns: [], contract_path: `${board}/interfaces/${id}/module.md` }],
      edges: [], findings: [{ id: `${id}:1`, where: `${chunk.files[0]}:1`, what: 'fixture risk', evidence: 'observed fixture line', severity: 'high', confidence: 0.8 }],
      coverage: { files_claimed: chunk.files.length, files_analyzed: chunk.files.length, analyzed_files: [...chunk.files], gaps: [] },
    };
  };
  // world.run 桩：伪造真实宿主返回结构 {exitCode, stdout, stderr}。
  // 首参恒为字面量 "python3"，子命令与 JSON 输入在 args 数组里（无 shell 拼接）。
  const state = { planExit: 0, acquireExit: 0, acquireStdout: null, verifyExit: 0, reportExit: 0, mutateAcquire: null, mutateInspect: null, missingArtifacts: null, claimsWriteExit: 0, claimsPageExit: 0, claimsWritten: null,
    indexScanExit: 0, chunksWriteExit: 0, indexVerifyExit: 0, chunksPageExit: 0, g2InitExit: 0, g2MarkExit: 0, resumeRegisterExit: 0, staleAnchor: false, coverageExit: 0,
    indexScan: null, g2: null, g2Marked: [], chunksWritten: null, coverageRecorded: null, resumeRegistered: null,
    existingRun: false, chunkResults: null, chunksPageLoaded: [], a1PlanDoc: null, a1SummaryDoc: null };
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
      return { exitCode: 0, stdout: JSON.stringify({ ok: true, run_id: payload.run_id ?? 'run-001', run_root_exists: state.existingRun, roots, checks: {} }), stderr: '' };
    }
    if (argv[1] === 'acquire') {
      if (state.acquireExit) return { exitCode: state.acquireExit, stdout: '', stderr: 'mock acquire conflict' };
      if (state.acquireStdout !== null) return { exitCode: 0, stdout: state.acquireStdout, stderr: '' };
      const roots = worldRoots(payload); state.mutateAcquire?.(roots);
      if (state.existingRun) {
        if (payload.resume !== true) return { exitCode: 3, stdout: JSON.stringify({ ok: false, kind: 'conflict', error: 'mock existing run without resume' }), stderr: '' };
        return { exitCode: 0, stdout: JSON.stringify({ ok: true, run_id: payload.run_id, created_at: '2026-09-19T00:00:00+00:00', resumed_at: '2026-09-20T00:00:00+00:00', mode: 'adopt', roots }), stderr: '' };
      }
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
      // FIX05：黑板制品读取（续接恢复/分块条目/索引 manifest）有界返回正文。
      const chunkPlanEntry = payload.path.match(/\/index\/chunks\/(chunk-[A-Za-z0-9_-]+)\.json$/);
      if (chunkPlanEntry) {
        const entry = (state.chunksWritten ?? []).find(c => c.id === chunkPlanEntry[1]);
        if (!entry) return { exitCode: 3, stdout: JSON.stringify({ ok: false, kind: 'conflict', error: 'mock missing chunk entry' }), stderr: '' };
        return { exitCode: 0, stdout: JSON.stringify({ ok: true, path: payload.path, realpath: payload.path, size: 10, sha256: 'a'.repeat(64), body: JSON.stringify(entry), publish_relpath: null }), stderr: '' };
      }
      const chunkResult = payload.path.match(/\/chunks\/(chunk-[A-Za-z0-9_-]+)\.json$/);
      if (chunkResult) {
        const r = state.chunkResults?.[chunkResult[1]] ?? a2Result(chunkResult[1]);
        return { exitCode: 0, stdout: JSON.stringify({ ok: true, path: payload.path, realpath: payload.path, size: 10, sha256: 'a'.repeat(64), body: JSON.stringify(r), publish_relpath: null }), stderr: '' };
      }
      if (payload.path.endsWith('/a1-summary.json')) {
        // 上次运行的勘察摘要黑板制品；桩未显式提供时按 A1 桩同构合成（续接恢复场景）。
        const doc = state.a1SummaryDoc ?? (state.indexScan ? {
          meta: map.meta, scan_status: map.scan_status,
          scan_evidence: `index 世代 ${state.indexScan.generation}（anchor ${state.indexScan.source_anchor.slice(0, 12)}…）manifest ${state.indexScan.run_root}/index/manifest.json`,
          languages: map.languages, loc_total: map.loc_total,
          entry_points: map.entry_points, build_files: map.build_files, tree_summary: map.tree_summary,
          plan: { path: `${board}/chunk-plan.json`, chunk_count: (state.chunksWritten ?? []).length, file_count_total: (state.chunksWritten ?? []).reduce((n, c) => n + c.files.length, 0), anchor: state.indexScan.source_anchor },
        } : null);
        if (!doc) return { exitCode: 3, stdout: JSON.stringify({ ok: false, kind: 'conflict', error: 'mock summary missing' }), stderr: '' };
        return { exitCode: 0, stdout: JSON.stringify({ ok: true, path: payload.path, realpath: payload.path, size: 10, sha256: 'a'.repeat(64), body: JSON.stringify(doc), publish_relpath: null }), stderr: '' };
      }
      if (payload.path.endsWith('/index/manifest.json')) {
        if (!state.indexScan) return { exitCode: 3, stdout: JSON.stringify({ ok: false, kind: 'conflict', error: 'mock manifest missing' }), stderr: '' };
        const m = { kind: 'index_manifest', generation: state.indexScan.generation, file_count: state.indexScan.file_count, excluded_count: state.indexScan.excluded_count, source_anchor: state.indexScan.source_anchor, root_realpath: args.target };
        return { exitCode: 0, stdout: JSON.stringify({ ok: true, path: payload.path, realpath: payload.path, size: 10, sha256: 'a'.repeat(64), body: JSON.stringify(m), publish_relpath: null }), stderr: '' };
      }
      const inspect = { ok: true, path: payload.path, realpath: payload.path, size: 42, sha256: 'a'.repeat(64), body: '# Fixture analysis report\n', publish_relpath: `reports/${args.run_id}/report/analysis-report.md` };
      state.mutateInspect?.(inspect);
      return { exitCode: 0, stdout: JSON.stringify(inspect), stderr: '' };
    }
    if (argv[1] === 'claims-write') {
      // C10 送验结论落盘桩：存下 claims 供断言与分页，回执含 total 与落盘路径。
      if (state.claimsWriteExit) return { exitCode: state.claimsWriteExit, stdout: '', stderr: 'mock claims write failure' };
      state.claimsWritten = payload.claims;
      return { exitCode: 0, stdout: JSON.stringify({ ok: true, claims_file: `${payload.run_root}/verification/claims.json`, total: payload.claims.length }), stderr: '' };
    }
    if (argv[1] === 'claims-page') {
      // Z08 分片领取桩：按 page/page_size 切 claims-write 落盘的同一数组，回执结构与真实 helper 一致。
      if (state.claimsPageExit) return { exitCode: state.claimsPageExit, stdout: '', stderr: 'mock claims page failure' };
      const all = state.claimsWritten ?? [];
      const pages = Math.ceil(all.length / payload.page_size);
      return { exitCode: 0, stdout: JSON.stringify({ ok: true, claims_file: payload.claims_file, total: all.length, pages, page: payload.page, page_size: payload.page_size, claims: all.slice(payload.page * payload.page_size, (payload.page + 1) * payload.page_size) }), stderr: '' };
    }
    if (argv[1] === 'index-count') {
      // SR-06 清单预检桩：粗计数缺省 = mock 文件数；bigFiles 注入大清单规模。
      return { exitCode: 0, stdout: JSON.stringify({ ok: true, source_root: payload.source_root, file_count: options.bigFiles ?? totalFiles }), stderr: '' };
    }
    if (argv[1] === 'index-scan') {
      if (state.indexScanExit) return { exitCode: state.indexScanExit, stdout: '', stderr: 'mock index scan failure' };
      state.indexScan = { generation: 1, file_count: options.bigFiles ?? totalFiles, excluded_count: 3, source_anchor: 'b'.repeat(64), run_root: payload.run_root };
      return { exitCode: 0, stdout: JSON.stringify({ ok: true, index_db: `${payload.run_root}/index/facts.sqlite`, manifest: `${payload.run_root}/index/manifest.json`, kernel_source: 'packages', generation: 1, mode: 'git', file_count: state.indexScan.file_count, excluded_count: 3, batch_count: 1, source_anchor: state.indexScan.source_anchor }), stderr: '' };
    }
    if (argv[1] === 'chunks-write') {
      if (state.chunksWriteExit) return { exitCode: state.chunksWriteExit, stdout: '', stderr: 'mock chunks write failure' };
      // FIX05 双模式：有界导入（plan_file+anchor，argv 不携全量路径）与既有 argv 模式。
      let chunks, mode;
      if (payload.plan_file !== undefined) {
        if (payload.plan_file !== `${payload.run_root}/chunk-plan.json`) throw new Error('mock: unexpected plan_file');
        if (payload.anchor !== state.indexScan?.source_anchor) throw new Error('mock: plan anchor mismatch');
        if (/\/file-\d+\.js/.test(JSON.stringify(payload))) throw new Error('mock: plan import argv carries file paths');
        chunks = state.a1PlanDoc; mode = 'plan_import';
      } else {
        chunks = payload.chunks; mode = 'argv';
      }
      state.chunksWritten = chunks;
      return { exitCode: 0, stdout: JSON.stringify({ ok: true, mode, chunks_dir: `${payload.run_root}/index/chunks`, chunk_count: chunks.length, file_count_total: chunks.reduce((n, c) => n + c.files.length, 0) }), stderr: '' };
    }
    if (argv[1] === 'index-verify') {
      if (state.indexVerifyExit) return { exitCode: state.indexVerifyExit, stdout: JSON.stringify({ ok: false, kind: 'conflict', error: 'mock closure broken: missing 1', missing_count: 1, missing_sample: [`${args.target}/ghost.js`], extra_count: 0, extra_sample: [], duplicate_count: 0, duplicate_sample: [] }), stderr: '' };
      return { exitCode: 0, stdout: JSON.stringify({ ok: true, generation: state.indexScan.generation, source_anchor: state.indexScan.source_anchor, file_count: state.indexScan.file_count, chunk_count: state.chunksWritten.length, covered: state.indexScan.file_count, missing_count: 0, extra_count: 0, duplicate_count: 0, missing_sample: [], extra_sample: [], duplicate_sample: [] }), stderr: '' };
    }
    if (argv[1] === 'g2-progress') {
      // FIX05 per-chunk 完成标记桩：completed 列表=标记集合派生，无单体 chunk_order 持久化。
      const markersDir = `${payload.run_root}/g2/${payload.generation ?? 1}`;
      if (payload.action === 'init') {
        if (state.g2InitExit) return { exitCode: state.g2InitExit, stdout: '', stderr: 'mock g2 init failure' };
        if (state.g2 && state.g2.source_anchor !== payload.source_anchor) return { exitCode: 3, stdout: JSON.stringify({ ok: false, kind: 'conflict', error: 'mock anchor conflict' }), stderr: '' };
        if (payload.chunk_order !== undefined) throw new Error('mock: init 不再接受 chunk_order 全表');
        const total = (state.chunksWritten ?? []).length;
        state.g2 = state.g2 ?? { source_anchor: payload.source_anchor, generation: payload.generation, chunk_order: (state.chunksWritten ?? []).map(c => c.id), completed: [] };
        return { exitCode: 0, stdout: JSON.stringify({ ok: true, idempotent: false, markers_dir: markersDir, meta_path: `${markersDir}/meta.json`, completed: state.g2.completed.length, total }), stderr: '' };
      }
      if (payload.action === 'mark') {
        if (state.g2MarkExit) return { exitCode: state.g2MarkExit, stdout: '', stderr: 'mock g2 mark failure' };
        state.g2Marked.push(payload.chunk_id);
        if (payload.result_path !== `${payload.run_root}/chunks/${payload.chunk_id}.json`) throw new Error('mock: mark 应绑定块结果制品');
        if (!state.g2.completed.includes(payload.chunk_id)) state.g2.completed.push(payload.chunk_id);
        return { exitCode: 0, stdout: JSON.stringify({ ok: true, chunk_id: payload.chunk_id, idempotent: false, marker_path: `${markersDir}/${payload.chunk_id}.done`, completed: state.g2.completed.length, total: state.g2.chunk_order.length }), stderr: '' };
      }
      if (payload.action === 'read') {
        if (!state.g2) return { exitCode: 0, stdout: JSON.stringify({ ok: true, exists: false, markers_dir: markersDir, meta_path: `${markersDir}/meta.json` }), stderr: '' };
        return { exitCode: 0, stdout: JSON.stringify({ ok: true, exists: true, markers_dir: markersDir, meta_path: `${markersDir}/meta.json`, meta: { version: 1, kind: 'g2_markers', source_anchor: state.g2.source_anchor, generation: state.g2.generation, total: state.g2.chunk_order.length }, completed_count: state.g2.completed.length, total: state.g2.chunk_order.length }), stderr: '' };
      }
      if (payload.action === 'list') {
        const ids = state.g2?.completed ?? [];
        const pages = Math.ceil(ids.length / payload.page_size);
        if (ids.length && payload.page >= pages) return { exitCode: 2, stdout: JSON.stringify({ ok: false, kind: 'argument', error: 'mock list page overflow' }), stderr: '' };
        return { exitCode: 0, stdout: JSON.stringify({ ok: true, total: ids.length, pages, page: payload.page, page_size: payload.page_size, ids: ids.slice(payload.page * payload.page_size, (payload.page + 1) * payload.page_size) }), stderr: '' };
      }
      return { exitCode: 2, stdout: '', stderr: 'mock g2 action invalid' };
    }
    if (argv[1] === 'resume-register') {
      if (state.resumeRegisterExit) return { exitCode: state.resumeRegisterExit, stdout: '', stderr: 'mock resume register failure' };
      state.resumeRegistered = payload;
      return { exitCode: 0, stdout: JSON.stringify({ ok: true, activity: { kind: payload.kind, id: payload.activity_id, checkpoint_path: payload.checkpoint_path, source_anchor: payload.source_anchor, status: 'running' } }), stderr: '' };
    }
    if (argv[1] === 'resume-check') {
      if (state.staleAnchor) return { exitCode: 0, stdout: JSON.stringify({ ok: true, kind: payload.kind, id: payload.activity_id, condition: 'stale_anchor', cursor_summary: null, resume_call: null, detail: 'mock: 源锚已变化（登记=b64 当前=b65）' }), stderr: '' };
      return { exitCode: 0, stdout: JSON.stringify({ ok: true, kind: payload.kind, id: payload.activity_id, condition: 'ok', cursor_summary: 'cursor=1/2 status=running', resume_call: null, detail: null }), stderr: '' };
    }
    if (argv[1] === 'chunks-page') {
      // SR-06/FIX05 分块领取桩：跳过完成标记后的块按 page/page_size 切页；
      // loaded_entries 记录本次实际加载的分块条目数（≤页大小，可断言）。
      if (state.chunksPageExit) return { exitCode: state.chunksPageExit, stdout: '', stderr: 'mock chunks page failure' };
      const completed = new Set(state.g2?.completed ?? []);
      const pending = (state.chunksWritten ?? []).filter(c => !completed.has(c.id));
      if (!pending.length && payload.page === 0) return { exitCode: 0, stdout: JSON.stringify({ ok: true, total: state.chunksWritten.length, completed: completed.size, remaining: 0, pages: 0, page: 0, page_size: payload.page_size, loaded_entries: 0, chunks: [] }), stderr: '' };
      const pages = Math.ceil(pending.length / payload.page_size);
      if (payload.page >= pages) return { exitCode: 2, stdout: JSON.stringify({ ok: false, kind: 'argument', error: 'mock page overflow', pages, remaining: pending.length }), stderr: '' };
      const start = payload.page * payload.page_size;
      const pageChunks = pending.slice(start, start + payload.page_size);
      state.chunksPageLoaded.push(pageChunks.length);
      return { exitCode: 0, stdout: JSON.stringify({ ok: true, total: state.chunksWritten.length, completed: completed.size, remaining: pending.length, pages, page: payload.page, page_size: payload.page_size, loaded_entries: pageChunks.length, chunks: pageChunks }), stderr: '' };
    }
    if (argv[1] === 'coverage-record') {
      if (state.coverageExit) return { exitCode: state.coverageExit, stdout: '', stderr: 'mock coverage failure' };
      state.coverageRecorded = payload;
      return { exitCode: 0, stdout: JSON.stringify({ ok: true, account: `${payload.run_root}/coverage_account.json`, complete: true }), stderr: '' };
    }
    throw new Error(`Unexpected world.run subcommand ${argv[1]}`);
  } };
  const askCounts = new Map();
  const agent = name => ({ ask: async prompt => {
    calls.push({ name, prompt });
    // attempt：该代理名（同一实例）的第几次 ask，供回流用例按次注入不同失败。
    const attempt = (askCounts.get(name) ?? 0) + 1;
    askCounts.set(name, attempt);
    if (name === '勘察员·罗经纬') {
      options.map?.(map);
      // SR-06/FIX05：大库（已建索引）先把分块规划与勘察摘要"写到黑板"（桩里记录
      // 文档供 chunks-write/续接读取），返回值只带 plan 引用——无任何 chunks[].files。
      if (state.indexScan) {
        state.a1PlanDoc = map.chunks;
        const summary = { meta: map.meta, scan_status: map.scan_status, languages: map.languages, loc_total: map.loc_total, entry_points: map.entry_points, build_files: map.build_files, tree_summary: map.tree_summary,
          plan: { path: `${board}/chunk-plan.json`, chunk_count: map.chunks.length, file_count_total: map.chunks.reduce((n, c) => n + c.files.length, 0), anchor: state.indexScan.source_anchor } };
        summary.scan_evidence = `index 世代 ${state.indexScan.generation}（anchor ${state.indexScan.source_anchor.slice(0, 12)}…）manifest ${state.indexScan.run_root}/index/manifest.json`;
        state.a1SummaryDoc = summary;
        return summary;
      }
      return map;
    }
    if (name.startsWith('模块深读员·')) {
      const id = name.split('·')[1];
      a2Asked.push(id);
      const r = a2Result(id);
      options.chunk?.(r, id, attempt); return r;
    }
    if (['架构分析员·高屋建', '依赖分析员·纲举目', '构建分析员·步就班'].includes(name)) {
      const role = name.startsWith('架构') ? 'A3' : name.startsWith('依赖') ? 'A4' : 'A5';
      const s = { summary: `${role} summary`, details: ['fixture'], findings: [],
        claims: [{ id: `${role}:1`, claim: 'fixture conclusion', source_role: role, evidence_refs: [`${files[0]}:1`] }],
        module_refs: map.chunks.filter(c => a2Asked.includes(c.id)).map(c => c.id), build_files_covered: role === 'A5' ? map.build_files.map(f => f.path) : [], not_covered: [] };
      options.specialty?.(s, role); return s;
    }
    if (name === '交叉验证员·铁证如') {
      // Z08 真分片契约：每次 ask 只携带该批原文（独立 JSON 行），返回仅该批 ID 的 verdicts。
      const marker = '本批送验结论原文如下：\n';
      const at = prompt.indexOf(marker);
      assert(at >= 0, 'A6 prompt 应携带本批 claims 原文');
      const pageClaims = JSON.parse(prompt.slice(at + marker.length).split('\n')[0]);
      const r = { verdicts: pageClaims.map(c => ({ claim_id: c.id, verdict: 'confirmed', note: 'read independently', own_evidence: `${files[0]}:2` })) };
      options.verdicts?.(r, pageClaims); return r;
    }
    if (name === '报告撰写员·文汇章') {
      const r = { path: `${board}/report/analysis-report.md`, summary: 'Fixture analysis report', sections: [0,1,2,3,4,5,6,7,8], claim_ids: (state.claimsWritten ?? []).map(c => c.id) };
      options.report?.(r); return r;
    }
    throw new Error(`Unexpected agent ${name}`);
  } });
  const artifact = { board() {}, async markdown(...args) { publications.push(['markdown', ...args]); }, async file(...args) { if (options.publishFailure) throw new Error('artifact missing'); publications.push(['file', ...args]); } };
  const result = await execute(args, agent, artifact, () => {}, card => cards.push(card), () => {}, world);
  return { result, calls, cards, publications, claims: (state.claimsWritten ?? []).map(c => c.id), worldCalls, state };
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

// —— C10/CV01+Z08：A6 真分片——claims 原文经 helper 落盘，工作流逐批领取、按批 ask 同一 A6 ——
test('G4 shards claims: workflow pages through the helper and asks A6 once per page', async () => {
  const { result, calls, worldCalls, state } = await run({ chunks: 27 });
  assert.equal(result.status, 'complete');
  const write = worldCalls.find(w => w[1][1] === 'claims-write');
  assert(write, '应先经 precheck claims-write 落盘送验结论');
  const written = JSON.parse(write[1][3]);
  assert.equal(written.run_root, '/reports/run-001');
  assert.equal(written.claims.length, 31);
  assert.equal(state.claimsWritten.length, 31);
  const pageCalls = worldCalls.filter(w => w[1][1] === 'claims-page');
  assert.equal(pageCalls.length, 1, '31 条 ≤ 单页，只领取一批（向后兼容单次行为）');
  const pagePayload = JSON.parse(pageCalls[0][1][3]);
  assert.equal(pagePayload.page, 0);
  assert.equal(pagePayload.page_size, 80);
  assert.equal(pagePayload.within_root, '/reports/run-001');
  assert.equal(pagePayload.claims_file, '/reports/run-001/verification/claims.json');
  const a6 = calls.filter(c => c.name === '交叉验证员·铁证如');
  assert.equal(a6.length, 1);
  assert(a6[0].prompt.includes('共 31 条、分 1 批'));
  assert(a6[0].prompt.includes('最后一批'), '单批即最后一批，应指示写 verdicts.json');
  assert(a6[0].prompt.includes('/reports/run-001/verification/verdicts.json'));
  assert(!a6[0].prompt.includes('claims-page'), '领取职责在工作流侧，A6 不再自取分页');
});

test('claims-page failure blocks before any verdict is accepted', async () => {
  const { result, calls } = await run({ world: w => { w.claimsPageExit = 2; } });
  assert.equal(result.status, 'blocked');
  assert.match(result.conclusion, /第 1 批领取失败/);
  assert(!calls.some(c => c.name === '交叉验证员·铁证如'));
});

test('A6 true sharding: 204 claims over 3 pages, one ask per page, merged result closes', async () => {
  const { result, calls, worldCalls } = await run({ chunks: 200 });
  assert.equal(result.status, 'complete');
  assert.equal(result.coverage.verdicts, 204);
  assert.equal(result.coverage.confirmed, 204);
  assert.deepEqual(
    worldCalls.filter(w => w[1][1] === 'claims-page').map(w => JSON.parse(w[1][3]).page),
    [0, 1, 2], '工作流按 0/1/2 逐批领取');
  const a6 = calls.filter(c => c.name === '交叉验证员·铁证如');
  assert.equal(a6.length, 3, '每批一次 ask，同一实例共 3 次');
  const marker = '本批送验结论原文如下：\n';
  const pageOf = c => JSON.parse(c.prompt.slice(c.prompt.indexOf(marker) + marker.length).split('\n')[0]);
  assert.deepEqual(a6.map(pageOf).map(p => p.length), [80, 80, 44]);
  assert(!a6[0].prompt.includes('finding:chunk-100:1'), '第 1 批不得携带其他批的 claim');
  assert(a6[1].prompt.includes('finding:chunk-100:1'));
  assert(!a6[2].prompt.includes('finding:chunk-0:1'), '第 3 批不得重复第 1 批的 claim');
  assert(a6[2].prompt.includes('最后一批'));
  assert.equal(result.claim_verdicts.length, 204, '全部批合并后逐 ID 闭合');
});

test('A6 page cap pauses with honest partial instead of pretending completion', async () => {
  const { result, calls, worldCalls } = await run({ chunks: 4100 });
  // 4100 块 → 3 专项 + 1 入口 + 4100 高严重度发现 = 4104 条 → 52 批 > 上限 50。
  assert.equal(result.status, 'partial');
  assert.equal(worldCalls.filter(w => w[1][1] === 'claims-page').length, 50);
  const a6 = calls.filter(c => c.name === '交叉验证员·铁证如');
  assert.equal(a6.length, 50, '只派发前 50 批');
  assert(a6[49].prompt.includes('最后一批'), '截断处仍指示落盘已复核批次');
  assert.equal(result.coverage.verdicts, 4104);
  assert.equal(result.coverage.confirmed, 4000);
  assert.equal(result.coverage.unverified, 104);
  assert(result.not_covered.some(s => s.includes('A6 分页超上限，剩余 104 条未复核')),
    'not_covered 必须如实注明未复核条数');
  const pending = result.claim_verdicts.filter(c => c.verdict === 'unverified');
  assert.equal(pending.length, 104);
  assert(pending.every(c => c.claim_id.startsWith('finding:chunk-')), '截断只落在尾批的发现类结论');
  assert.equal(result.findings.find(f => f.id === 'chunk-4050:1').status, 'unverified');
});

test('claims-write failure blocks before A6 is dispatched', async () => {
  const { result, calls } = await run({ world: w => { w.claimsWriteExit = 2; } });
  assert.equal(result.status, 'blocked');
  assert.match(result.conclusion, /送验结论落盘失败/);
  assert(!calls.some(c => c.name === '交叉验证员·铁证如'));
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
  // SR-06 后预检闸门记录为 路径预检 + 清单预检 两条；G1 失败时不得再有任何分析声明。
  assert.equal(result.status, 'blocked');
  assert(result.gate_checks.length <= 2);
  assert(result.gate_checks.every(g => g.startsWith('路径预检') || g.startsWith('清单预检')));
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

// —— Z19 尾：多块 fixture——跨块模块名冲突、依赖边端点闭合走 G2 汇总闸门 ——
test('cross-block duplicate module names flow back through the G2 aggregate gate', async () => {
  const { result, calls } = await run({ chunks: 2, chunk(r, id, attempt) { if (attempt === 1) r.modules[0].name = 'shared-module'; } });
  assert.equal(result.status, 'complete');
  for (const id of ['chunk-0', 'chunk-1']) {
    const asks = calls.filter(c => c.name === `模块深读员·${id}`);
    assert.equal(asks.length, 2, `${id} 应回流重问`);
    assert.match(asks[1].prompt, /模块名跨块重复：shared-module/);
    assert.match(asks[1].prompt, /当前模块全集/);
  }
  assert.deepEqual(calls.filter(c => c.name.startsWith('模块深读员·')).length, 4, '两个受影响块各重问一次');
});

test('cross-block dependency edge closes against the merged module set without reflow', async () => {
  const { result, calls } = await run({ chunks: 2, chunk(r, id) {
    if (id === 'chunk-1') r.edges = [{ from: 'chunk-1', to: 'chunk-0', kind: 'import', source: '/repos/app/file-1.js:3' }];
  } });
  assert.equal(result.status, 'complete');
  assert.equal(calls.filter(c => c.name === '模块深读员·chunk-1').length, 1, '跨块边端点可归位，无需回流');
});

test('edge endpoint outside the module set flows back and recovers', async () => {
  const { result, calls } = await run({ chunks: 2, chunk(r, id, attempt) {
    if (id !== 'chunk-1') return;
    if (attempt === 1) r.edges = [{ from: 'chunk-1', to: 'ghost-module', kind: 'import', source: '/repos/app/file-1.js:3' }];
  } });
  assert.equal(result.status, 'complete');
  const asks = calls.filter(c => c.name === '模块深读员·chunk-1');
  assert.equal(asks.length, 2);
  assert.match(asks[1].prompt, /依赖边端点不能归位：chunk-1->ghost-module/);
  assert.match(asks[1].prompt, /当前模块全集/);
});

// —— Z08/A7 减负：prompt 只带汇总统计与黑板指针，明细按需读 ——
test('A7 prompt carries summary stats and blackboard pointers instead of full verdict payloads', async () => {
  const { result, calls } = await run({ chunks: 3, verdicts(r) {
    const first = r.verdicts[0];
    r.verdicts[0] = { ...first, verdict: 'unverified', own_evidence: '', note: 'cannot read file' };
  } });
  assert.equal(result.status, 'partial');
  const a7 = calls.find(c => c.name === '报告撰写员·文汇章');
  assert(a7, 'A7 应被派发');
  assert(!a7.prompt.includes('read independently'), 'verdicts 明细（note/own_evidence）不得进入 A7 prompt');
  assert(!a7.prompt.includes('cannot read file'), 'unverified 复查原因明细不得进入 A7 prompt');
  assert(a7.prompt.includes('confirmed 6 / refuted 0 / unverified 1'), '应给汇总统计');
  assert(a7.prompt.includes('逐条状态'), '应给逐 ID 状态紧凑清单');
  assert(a7.prompt.includes('/reports/run-001/verification/verdicts.json'), '应指明 verdicts 明细的黑板路径');
  assert(a7.prompt.includes('"excluded":{"count":0'), 'notCovered 应为紧凑摘要');
  assert(result.not_covered.some(s => s.includes('cannot read file')), '返回值 not_covered 仍保留完整明细');
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

// —— SR-06 主链路容量接线：小库直读保持、大库索引分页、有界派发、检查点与混代拒绝 ——
test('SR-06: small repo keeps legacy direct-read path without index machinery', async () => {
  const { result, calls, worldCalls } = await run({ chunks: 27 });
  assert.equal(result.status, 'complete');
  assert(worldCalls.some(w => w[1][1] === 'index-count'), '清单预检粗计数恒定执行');
  assert(!worldCalls.some(w => w[1][1] === 'index-scan'), '小库（≤阈值）不建索引');
  assert(!worldCalls.some(w => w[1][1] === 'chunks-page'), '小库不经分页领取');
  assert(!worldCalls.some(w => w[1][1] === 'g2-progress'), '小库不写 G2 检查点');
  const a1 = calls.find(c => c.name === '勘察员·罗经纬');
  assert(a1.prompt.includes('source_files 完整枚举'), '小库保持旧行为：A1 直读全量清单');
});

test('SR-06: big repo A1 receives summary + index refs, never the full listing', async () => {
  const { result, calls, worldCalls } = await run({ chunks: 7, bigFiles: 240 });
  assert.equal(result.status, 'complete');
  const scan = worldCalls.find(w => w[1][1] === 'index-scan');
  assert(scan, '大库应先建索引');
  assert.equal(JSON.parse(scan[1][3]).run_root, '/reports/run-001');
  const a1 = calls.find(c => c.name === '勘察员·罗经纬');
  assert(a1.prompt.includes('世代 1'), 'prompt 引用索引世代');
  assert(a1.prompt.includes('240 个源文件'), 'prompt 告知索引全集文件数（分页核对基准）');
  assert(a1.prompt.includes('bbbbbbbbbbbb'), 'prompt 携带 anchor 指纹（12 位前缀）');
  assert(a1.prompt.includes('/reports/run-001/chunk-plan.json'), 'prompt 指定分块规划黑板路径');
  assert(a1.prompt.includes('/reports/run-001/a1-summary.json'), 'prompt 指定勘察摘要黑板路径');
  assert(!a1.prompt.includes('source_files 完整枚举'), '不再要求返回全量清单（旧行为短语不出现）');
  assert(!worldCalls.some(w => w[1][1] === 'index-verify' && JSON.parse(w[1][3]).chunks), '全集比对在 precheck 侧流式做，不经协调者');
  // FIX05 有界交接：协调者只经手 plan 引用——chunks-write 的 argv 是 plan_file+anchor，
  // 不含 chunks 数组、不含任何文件全路径；门禁文本记录 plan 引用口径。
  const write = worldCalls.find(w => w[1][1] === 'chunks-write');
  assert(write, '分块规划经有界导入落盘');
  const writePayload = JSON.parse(write[1][3]);
  assert.equal(writePayload.plan_file, '/reports/run-001/chunk-plan.json');
  assert.equal(writePayload.anchor, 'b'.repeat(64));
  assert.equal(writePayload.chunks, undefined, 'argv 不再 stringify summary.chunks');
  assert(!write[1][3].includes('/repos/app/file-'), '导入 argv 不含任何文件全路径');
  assert(result.gate_checks.some(g => g.includes('index-verify 分页核对')), true, 'G1 闸门记录分页核对');
  assert(result.gate_checks.some(g => g.includes('plan 引用')), 'G1 闸门记录有界引用口径');
});

test('SR-06: adopt resume reloads 3 finished chunks and closes 3+2=5 without re-asking A1', async () => {
  // 中断前：chunk-0/1/2 已完成（标记+黑板结果）；续跑 adopt 同一 run_root，
  // 复用索引世代不重扫、不重问勘察员、不重写规划，只新派发 chunk-3/4，
  // 聚合 = 历史 3 块 + 新 2 块 = 全集 5 块（无重无漏）。
  const { result, calls, worldCalls } = await run({ chunks: 5, bigFiles: 240, world: w => {
    w.existingRun = true;
    w.indexScan = { generation: 1, file_count: 240, excluded_count: 3, source_anchor: 'b'.repeat(64), run_root: '/reports/run-001' };
    w.chunksWritten = Array.from({ length: 5 }, (_, i) => ({
      id: `chunk-${i}`,
      files: Array.from({ length: 48 }, (_, j) => `/repos/app/file-${i * 48 + j}.js`),
      loc_est: 10, neighbors: [], rationale: 'fixture',
    }));
    w.g2 = { source_anchor: 'b'.repeat(64), generation: 1,
      chunk_order: Array.from({ length: 5 }, (_, i) => `chunk-${i}`),
      completed: ['chunk-0', 'chunk-1', 'chunk-2'] };
  } });
  assert.equal(result.status, 'complete');
  const acquire = worldCalls.find(w => w[1][1] === 'acquire');
  assert.equal(JSON.parse(acquire[1][3]).resume, true, 'run_root 已存在 → 显式 resume=true');
  assert(!worldCalls.some(w => w[1][1] === 'index-scan'), '续接复用索引世代，不重扫');
  assert(!worldCalls.some(w => w[1][1] === 'chunks-write'), '续接不重写分块规划');
  assert(!calls.some(c => c.name === '勘察员·罗经纬'), '续接不重问勘察员（规划原文在黑板）');
  assert(result.gate_checks.some(g => g.includes('续接复用既有索引世代 1')));
  assert(result.gate_checks.some(g => g.includes('续接回载 3 块历史结果 + 新派发 2 块')), '门禁记录 3+2 闭合口径');
  // 派发：只有剩余 chunk-3/4 被新问；历史 3 块经黑板制品逐块回载重校验。
  const asked = calls.filter(c => c.name.startsWith('模块深读员·')).map(c => c.name.split('·')[1]);
  assert.deepEqual(asked, ['chunk-3', 'chunk-4']);
  const a2AsksBeforeDispatch = worldCalls.filter(w => w[1][1] === 'g2-progress' && /"action":"mark"/.test(w[1][3]));
  assert.deepEqual(a2AsksBeforeDispatch.map(w => JSON.parse(w[1][3]).chunk_id), ['chunk-3', 'chunk-4'], '只标记新完成块');
  const g2Mark = worldCalls.find(w => w[1][1] === 'g2-progress' && /"action":"mark"/.test(w[1][3]));
  assert(JSON.parse(g2Mark[1][3]).result_path.endsWith('/chunks/chunk-3.json'), '标记绑定块结果制品');
  // 聚合闭合：5 块发现全部在场（历史 3 块的 finding 也进汇总与送验），无重无漏。
  const findingIds = result.findings.map(f => f.id);
  assert.deepEqual(findingIds, ['chunk-0:1', 'chunk-1:1', 'chunk-2:1', 'chunk-3:1', 'chunk-4:1']);
  assert.ok(result.findings.every(f => f.status === 'verified'), '回载历史结果同样进入送验闭合');
  assert.equal(result.coverage.chunks, 5);
  assert.equal(result.coverage.files, 240);
  assert.equal(result.coverage.files_analyzed, 240, '历史+新结果的逐文件覆盖合计 = 全集');
  assert.equal(result.coverage.verdicts, 9, '送验 = 3 专项 + 1 入口 + 5 发现');
  assert.equal(result.coverage.confirmed, 9);
});

test('SR-06: big repo G2 pages chunks in bounded batches of 3 with checkpoint marks', async () => {
  const { result, calls, worldCalls, state } = await run({ chunks: 7, bigFiles: 240 });
  assert.equal(result.status, 'complete');
  const pageCalls = worldCalls.filter(w => w[1][1] === 'chunks-page');
  assert.equal(pageCalls.length, 4, '7 块 → 3+3+1 三批 + 1 次空批终态');
  for (const w of pageCalls) {
    const payload = JSON.parse(w[1][3]);
    assert.equal(payload.page, 0, '恒取 page 0（检查点即游标）');
    assert.equal(payload.page_size, 3, '批上限 = MAX_CONCURRENCY 3');
    assert.equal(payload.anchor, 'b'.repeat(64), '领取绑定索引 anchor');
  }
  const a2 = calls.filter(c => c.name.startsWith('模块深读员·'));
  assert.equal(a2.length, 7, '每块恰一次 ask');
  assert.deepEqual(
    a2.map(c => c.name.split('·')[1]),
    Array.from({ length: 7 }, (_, i) => `chunk-${i}`),
    '派发顺序 = 领取顺序（3+3+1 批推进，world.run 只带回执请求不含响应故以 ask 序断言）');
  // 有界性：单个 ask 载荷只含本块文件（块间互不可见其余清单）
  const first = a2.find(c => c.name === '模块深读员·chunk-0');
  assert(first.prompt.includes('file-0.'), '本块文件在载荷中');
  assert(!first.prompt.includes('file-100.'), '其他块文件不得进入本块载荷');
  // FIX05 真分页：每次领取实际加载的分块条目 ≤ 页大小（计数桩）
  assert.deepEqual(state.chunksPageLoaded, [3, 3, 1], '领取加载量 = 页大小有界');
  // 完成标记：逐块成功即 mark（绑定结果制品路径），order 与全集一致
  const g2Init = worldCalls.find(w => w[1][1] === 'g2-progress' && /"action":"init"/.test(w[1][3]));
  assert.equal(JSON.parse(g2Init[1][3]).chunk_order, undefined, 'init 不再持久化 chunk_order 全表');
  assert.deepEqual(state.g2Marked.slice().sort(), Array.from({ length: 7 }, (_, i) => `chunk-${i}`).sort());
  assert.equal(state.resumeRegistered.source_anchor, 'b'.repeat(64));
  assert.equal(state.resumeRegistered.kind, 'custom');
  assert.equal(result.coverage.files, 240, '覆盖分母 = 索引口径全集');
  assert.equal(result.coverage.chunks, 7);
});

test('SR-06: chunks-page skips checkpointed chunks on re-entry', async () => {
  const anchor = 'b'.repeat(64);
  const { result, calls, worldCalls } = await run({ chunks: 7, bigFiles: 240, world: w => {
    // 预置完成标记：chunk-0 已在上次执行完成（重入续跑场景）
    w.g2 = { source_anchor: anchor, generation: 1, chunk_order: Array.from({ length: 7 }, (_, i) => `chunk-${i}`), completed: ['chunk-0'] };
  } });
  assert.equal(result.status, 'complete');
  const a2 = calls.filter(c => c.name.startsWith('模块深读员·'));
  assert.equal(a2.length, 6, '已完成块被跳过，只派发剩余 6 块');
  assert(!calls.some(c => c.name === '模块深读员·chunk-0'), 'chunk-0 不再重问');
  // FIX05 续接回载：chunk-0 的历史结果从黑板制品逐块读回（read-report），重过 G2 校验。
  const reloaded = worldCalls.filter(w => w[1][1] === 'read-report' && w[1][3].includes('/chunks/chunk-0.json'));
  assert(reloaded.some(w => JSON.parse(w[1][3]).path === '/reports/run-001/chunks/chunk-0.json'), '历史块结果经 read-report 有界回载');
  assert(result.coverage.chunks, 7);
  assert(result.gate_checks.some(g => g.includes('续接回载 1 块历史结果 + 新派发 6 块')));
  // 台账登记绑定标记元数据（resume-check 的检查点是 meta.json，恒为可读 JSON）
  const register = worldCalls.find(w => w[1][1] === 'resume-register');
  assert.equal(JSON.parse(register[1][3]).checkpoint_path, '/reports/run-001/g2/1/meta.json');
});

test('SR-06: stale anchor refuses mixed-generation resume', async () => {
  const anchor = 'b'.repeat(64);
  const { result, calls } = await run({ chunks: 7, bigFiles: 240, world: w => {
    w.g2 = { source_anchor: anchor, generation: 1, chunk_order: Array.from({ length: 7 }, (_, i) => `chunk-${i}`), completed: ['chunk-0'] };
    w.staleAnchor = true;
  } });
  assert.equal(result.status, 'blocked');
  assert.match(result.conclusion, /混代续跑/);
  assert(!calls.some(c => c.name.startsWith('模块深读员·')), '混代拒绝在派发前生效');
});

test('SR-06: index-verify closure failure blocks before any module analyst', async () => {
  const { result, calls, worldCalls } = await run({ chunks: 7, bigFiles: 240, world: w => { w.indexVerifyExit = 3; } });
  assert.equal(result.status, 'blocked');
  assert.match(result.conclusion, /不闭合/);
  assert(worldCalls.some(w => w[1][1] === 'chunks-write'), '分块规划已落盘待核');
  assert(!calls.some(c => c.name.startsWith('模块深读员·')), '闭合失败不得进入 G2');
});

test('SR-06: coverage ledger records per-dimension denominators via helper', async () => {
  const small = await run({ chunks: 3 });
  assert.equal(small.result.status, 'complete');
  assert.deepEqual(small.state.coverageRecorded.dimensions.index_files, { denominator: 3, covered: 3, gaps: [] });
  assert.equal(small.state.coverageRecorded.dimensions.independent_review.denominator, 7, '分母 = 送验结论全集（3 专项 + 1 入口 + 3 发现）');
  assert.equal(small.state.coverageRecorded.generation, null, '小库无索引世代');
  assert(small.result.gate_checks.some(g => g.includes('G4 覆盖账')));

  const big = await run({ chunks: 7, bigFiles: 240 });
  assert.equal(big.state.coverageRecorded.generation, 1, '大库绑定索引世代');
  assert.equal(big.state.coverageRecorded.dimensions.index_files.denominator, 240);
});

test('SR-06: coverage-record failure is honest and does not block publication', async () => {
  const { result } = await run({ chunks: 3, world: w => { w.coverageExit = 2; } });
  assert.equal(result.status, 'complete');
  assert(result.not_covered.some(s => s.includes('覆盖账落盘失败')), '失败如实进入 not_covered');
  assert(!result.gate_checks.some(g => g.includes('G4 覆盖账')));
});

test('ZCode plugin manifest and all seven agents expose documented metadata', () => {
  const manifest = JSON.parse(readFileSync(new URL('.zcode-plugin/plugin.json', root), 'utf8'));
  assert.equal(manifest.name, 'code-analysis-swarm'); assert.equal(manifest.version, '0.3.0');
  assert.equal(manifest.commands, 'command'); assert.equal(manifest.agents, 'agents');
  assert.deepEqual(readdirSync(new URL('command/', root)), ['swarm-analyze.md']);
  assert.match(readFileSync(new URL('command/swarm-analyze.md', root), 'utf8'), /# \/swarm-analyze/);
  const agents = readdirSync(new URL('agents/', root)); assert.equal(agents.length, 7);
  for (const file of agents) {
    const body = readFileSync(new URL(`agents/${file}`, root), 'utf8');
    assert.match(body, /^---\nname: [a-z0-9-]+\ndescription: .+\n---\n/);
  }
});

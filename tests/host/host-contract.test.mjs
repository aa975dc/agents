import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

// layer A 静态守卫（host-contract）：只对 workflow 源码做文本级检查，
// 防止已知会破坏真实 ZCode 宿主 CreateWorkflow 编译的写法再次进入仓库
// （2026-09-20 实测：phase(变量) 编译被拒，诊断 "phase()'s argument must be
// a compile-time string literal"；字面量通过并运行）。
// 这不能替代真实宿主编译验收（layer C）：字符串解析刻意保持简单
// （不完整支持正则字面量与模板嵌套），通过本测试不等于宿主可编译。
const source = readFileSync(new URL('../../code-analysis-swarm/workflow/code-analysis.dwf.ts', import.meta.url), 'utf8');

// 逐字符标记每个位置的状态：0 代码、1 字符串、2 注释。
function scanStates(text) {
  const states = new Array(text.length).fill(0);
  let i = 0;
  while (i < text.length) {
    const c = text[i], d = text[i + 1];
    if (c === '/' && d === '/') { while (i < text.length && text[i] !== '\n') states[i++] = 2; continue; }
    if (c === '/' && d === '*') { states[i++] = 2; states[i++] = 2; while (i < text.length && !(text[i] === '*' && text[i + 1] === '/')) states[i++] = 2; i += 2; continue; }
    if (c === '"' || c === "'" || c === '`') {
      const quote = c; states[i++] = 1;
      while (i < text.length && text[i] !== quote) {
        if (text[i] === '\\') { states[i++] = 1; if (i < text.length) states[i++] = 1; continue; }
        states[i++] = 1;
      }
      if (i < text.length) states[i++] = 1;
      continue;
    }
    i++;
  }
  return states;
}
const states = scanStates(source);

// 提取调用首参：kind 'literal' 仅当它是引号字符串且（反引号时）不含 ${}，
// 并被顶层 , 或 ) 紧跟；其余（变量、拼接、含替换模板）一律 'expression'。
function firstArgument(start) {
  let i = start;
  while (i < source.length && /\s/.test(source[i])) i++;
  const begin = i;
  if (source[i] === '"' || source[i] === "'" || source[i] === '`') {
    const quote = source[i++];
    let literal = true;
    while (i < source.length && source[i] !== quote) {
      if (source[i] === '\\') { i += 2; continue; }
      if (quote === '`' && source[i] === '$' && source[i + 1] === '{') literal = false;
      i++;
    }
    const end = ++i;
    let j = i;
    while (j < source.length && /\s/.test(source[j])) j++;
    if (literal && (source[j] === ',' || source[j] === ')')) return { kind: 'literal', value: source.slice(begin, end) };
  }
  let depth = 0, k = begin;
  while (k < source.length) {
    const ch = source[k];
    if (states[k] === 0) {
      if (ch === '(' || ch === '[' || ch === '{') depth++;
      else if (ch === ')') { if (depth === 0) break; depth--; }
      else if (ch === ']' || ch === '}') depth--;
      else if (ch === ',' && depth === 0) break;
    }
    k++;
  }
  return { kind: 'expression', value: source.slice(begin, k).trim() };
}

function callFirstArguments(token) {
  const results = [];
  const re = new RegExp(`\\b${token.replace('.', '\\.')}\\s*\\(`, 'g');
  for (const match of source.matchAll(re)) {
    if (states[match.index] !== 0) continue; // 注释或字符串里不算调用
    results.push(firstArgument(match.index + match[0].length));
  }
  return results;
}

test('every phase() argument is a compile-time string literal', () => {
  const args = callFirstArguments('phase');
  assert.ok(args.length > 0, '守卫自检：未找到任何 phase 调用，说明解析失效，禁止空过');
  for (const arg of args) {
    assert.equal(arg.kind, 'literal', `phase(${arg.value}) 实参不是字符串字面量，真实宿主编译会拒绝`);
  }
});

test('every world.run() first argument is a string literal command name', () => {
  const args = callFirstArguments('world.run');
  for (const arg of args) {
    assert.equal(arg.kind, 'literal', `world.run(${arg.value}) 首参不是字面量命令名`);
  }
});

test('no static string literal is reused as an agent() name', () => {
  const names = callFirstArguments('agent').filter(a => a.kind === 'literal').map(a => a.value);
  const duplicated = names.filter((n, index) => names.indexOf(n) !== index);
  assert.deepEqual(duplicated, [], '相同静态字面量被多次用作 agent 名（宿主要求运行内唯一，循环里须改用计算名）');
});

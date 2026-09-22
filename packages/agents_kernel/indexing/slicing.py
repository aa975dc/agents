"""Token 预算切片：文本与结论载荷的有界切片/分批（stdlib only, Py3.9+）。

03_CAPACITY_AND_INDEXING.md §2 L2、§5、§7：
- 语义深读按 token 预算切片；超大单行硬截断并显式标记，不承诺一次模型读取，
  也不静默丢弃——截断是显式字段（truncated_line），不是降级。
- 没有精确 tokenizer 时用保守估算并标 estimation：estimate_tokens 是启发式
  （CJK≈1 token/字符，其余≈4 字符/token），是估算值而非精确 token 数。
- slice_claims 按预算分批：批数不限、全部覆盖，不抽样、不偷偷降级；
  预算驱动的消费/暂停/续跑见 execution.budget。
- 流式读取复用 kernel.digest 的 1MiB 分块口径；切片过程内存有界
  （O(read_size + 单行截断上限)），不整读大文件。
"""
import json
from typing import NamedTuple

from agents_kernel import digest
from agents_kernel.validation import CompanionError

# 启发式密度：ASCII 等按 ~4 字符 1 token 估；CJK 按 1 字符 1 token 估。
_ASCII_CHARS_PER_TOKEN = 4


def _is_cjk(ch):
    o = ord(ch)
    return (0x2E80 <= o <= 0x9FFF          # CJK 部首/标点/假名/注音/统一表意文字及扩展A
            or 0xF900 <= o <= 0xFAFF       # 兼容表意文字
            or 0xFF00 <= o <= 0xFFEF       # 全角形式
            or 0x20000 <= o <= 0x323AF)    # 统一表意文字扩展 B 及以后


def estimate_tokens(text):
    """启发式 token 估算（estimation，非精确）：CJK≈1 token/字符，其余≈4 字符 1 token。"""
    if not isinstance(text, str):
        raise CompanionError("estimate_tokens 只接受 str")
    if not text:
        return 0
    if text.isascii():
        return (len(text) + _ASCII_CHARS_PER_TOKEN - 1) // _ASCII_CHARS_PER_TOKEN
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return cjk + (other + _ASCII_CHARS_PER_TOKEN - 1) // _ASCII_CHARS_PER_TOKEN


class FileSlice(NamedTuple):
    path: str
    start_line: int        # 1 起物理行号；行号连续可追踪（03 §2 L2）
    end_line: int
    anchor: str            # "路径:起-止行" 头部行（已计入 est_tokens）
    text: str              # anchor + "\n" + 正文；含锚的估算 ≤ max_tokens
    est_tokens: int
    truncated_line: bool   # 本片含被硬截断的超大单行（物理行剩余部分已丢弃）


def slice_file(path, max_tokens, read_size=digest.READ_SIZE):
    """把单个文件按行切成连续片段，逐片产出 FileSlice（生成器，消费端内存有界）。

    - 每片含锚的总估算 ≤ max_tokens；空文件产出空序列。
    - 单行估算超过"预算-锚"时硬截断成独立片：只保留预算内最长前缀，物理行
      剩余部分丢弃并置 truncated_line=True（§5：极长单行显式处理，不静默）。
    - 行按 \n 分界（universal newline 归一），行号从 1 起连续。
    - 大文件流式读取（1MiB 分块口径），单行缓冲上限 char_cap，内存不随文件增长。
    """
    max_tokens = _require_budget(max_tokens)
    char_cap = max_tokens * _ASCII_CHARS_PER_TOKEN  # 单行缓冲上限（内存界，非精度界）
    pending = []
    start = None
    last_line_no = 0
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line_no, line, clipped in _iter_bounded_lines(handle, char_cap, read_size):
            last_line_no = line_no
            # 硬截断片锚取最长变体（带 [truncated]）预留，保证含锚估算不超预算。
            budget = max_tokens - _anchor_tokens(path, line_no, line_no, True)
            if clipped or estimate_tokens(line) > budget:
                if pending:
                    yield _emit(path, start, line_no - 1, pending)
                    pending, start = [], None
                yield _emit(path, line_no, line_no, [_hard_cut(line, budget)], True)
                continue
            if start is None:
                start = line_no
            pending.append(line)
            if _slice_tokens(path, start, line_no, pending) > max_tokens:
                pending.pop()
                yield _emit(path, start, line_no - 1, pending)
                pending, start = [line], line_no
        if pending:
            yield _emit(path, start, last_line_no, pending)


def slice_claims(claims, max_tokens):
    """把 claims 序列按预算分成连续批次，返回 [(batch_index, 批, est_tokens)]。

    - 批保持原顺序，批间无重叠无遗漏；批数不限，全部覆盖（不抽样、不降级）。
    - est_tokens 按整批 ensure_ascii=False JSON 文本估算（与投喂模型的载荷同口径），
      逐条累入时整批复核，保证每批估算 ≤ max_tokens。
    - 单条结论自身超预算时无法再切（结论原子）：显式报错，不截断不丢弃。
    - 空输入返回 []（0 批即全部覆盖）。
    """
    max_tokens = _require_budget(max_tokens)
    batches = []
    batch = []
    for claim in claims:
        try:
            text = json.dumps(claim, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise CompanionError("claim 不可 JSON 序列化：%s" % exc) from exc
        if estimate_tokens(text) > max_tokens:
            raise CompanionError(
                "单条结论估算 %d token 超过批次预算 %d；结论不可再切，无法有界分批"
                % (estimate_tokens(text), max_tokens))
        if batch and estimate_tokens(
                json.dumps(batch + [claim], ensure_ascii=False)) > max_tokens:
            batches.append(batch)
            batch = []
        batch.append(claim)
    if batch:
        batches.append(batch)
    return [(index, b, estimate_tokens(json.dumps(b, ensure_ascii=False)))
            for index, b in enumerate(batches)]


def _require_budget(max_tokens):
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        raise CompanionError("max_tokens 必须是正整数")
    return max_tokens


def _iter_bounded_lines(handle, char_cap, read_size):
    """流式产出 (行号, 行文本, 是否截断)；行缓冲 ≤ char_cap + read_size 字符。

    物理行超过 char_cap 时产出截断前缀（truncated=True）并丢弃该行剩余部分，
    保证极长单行/无换行大文件不会把内存吃满（03 §5）。
    """
    line_no = 0
    buf = ""
    while True:
        chunk = handle.read(read_size)
        if not chunk:
            break
        buf += chunk
        while True:
            cut = buf.find("\n")
            if cut < 0:
                break
            line_no += 1
            yield line_no, buf[:cut], False
            buf = buf[cut + 1:]
        if len(buf) > char_cap:
            line_no += 1
            yield line_no, buf[:char_cap], True
            buf = ""
            while True:  # 丢弃该物理行剩余部分，保留换行后的内容继续
                tail = handle.read(read_size)
                if not tail:
                    return
                cut = tail.find("\n")
                if cut >= 0:
                    buf = tail[cut + 1:]
                    break
    if buf:  # 无换行结尾的最后一行
        line_no += 1
        yield line_no, buf, False


def _anchor(path, start, end, truncated):
    return "%s:%d-%d%s" % (path, start, end, " [truncated]" if truncated else "")


def _anchor_tokens(path, start, end, truncated):
    return estimate_tokens(_anchor(path, start, end, truncated) + "\n")


def _slice_tokens(path, start, end, lines):
    return estimate_tokens(_anchor(path, start, end, False) + "\n" + "\n".join(lines))


def _emit(path, start, end, lines, truncated=False):
    anchor = _anchor(path, start, end, truncated)
    text = anchor + "\n" + "\n".join(lines)
    return FileSlice(path=path, start_line=start, end_line=end, anchor=anchor,
                     text=text, est_tokens=estimate_tokens(text), truncated_line=truncated)


def _hard_cut(line, budget):
    """保留估算 ≤ budget 的最长前缀；估算随前缀长度单调不减，二分求界。"""
    if budget < 1:
        raise CompanionError("max_tokens 过小：容纳不下行锚，无法切片")
    hi = min(len(line), budget * _ASCII_CHARS_PER_TOKEN)
    if estimate_tokens(line[:hi]) <= budget:
        return line[:hi]
    lo = 0
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(line[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return line[:lo]

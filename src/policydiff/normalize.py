"""单一职责：字段规整 + 政策文本保守归一化（normalize 阶段）。

归一化边界（每条规则都在 RULES 中登记书面理由，可审计、可回放）：

只消除“可证明的噪声”：
* 编码噪声：NFC 组合、全角 ASCII 字母/数字按 Unicode 恒等式折叠
  （全角标点与括号证据不足，保留）；
* 空白噪声：水平空白（制表符 / NBSP / 全角空格等 Zs）折叠为单个 ASCII 空格、
  行首尾修剪、CRLF 等异构换行规整为 ``\\n``、空行压缩；
* 版式噪声：BOM(ZWNBSP U+FEFF)、零宽空格(ZWSP U+200B)、软连字符(SHY U+AD)。

证据不足一律保留（v1 不写删除/折叠规则）：
* 否定词与否定语素（“不 / 非 / 未 / 无 / 不予”等，本模块不做任何词法处理）；
* 负号 U+2212、OCR 替换符 U+FFFD、零宽连接符 ZWJ/ZWNJ；
* 法律数字、货币符号、计量单位、括号（全角括号同样保留，v1 只折叠全角
  字母/数字，标点一律不动）、除硬连字符 U+2010/U+2011 外的破折号、
  引号与省略号；
* 条款边界换行（只规整不删除，空行压缩在布局阶段并留痕）。

可追溯契约：对每个文本字段，:func:`normalize_text` 返回
``text / map / dropped / folds / rule_version / source_len``：
* ``map[i]`` 给出第 i 个输出字符对应的原文区间（NFC 组合可能对应多个区间）；
* ``dropped`` 记录每个被“删除”码点的原文区间、命中规则 ID 与理由，
  其区间与 map 互补、互不重叠；
* ``folds`` 记录每个被“替换/折叠”码点（全角、硬连字符、NFC 组合、智能引号）
  的区间、规则 ID 与替换前后字符，其区间仍在 map 中，原文仍可回看；
* 原文区间被 map 与 dropped 完整且互不重叠地覆盖，:func:`recover_text`
  据此逐事件回放，原文始终可逐字符重建。

幂等：对输出 ``text`` 再次归一化得到完全相同的文本。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# 规则版本与规则登记（升级只追加，不就地改写旧规则，支持历史回放）
# ---------------------------------------------------------------------------

RULE_VERSION = "1.0.0"
NEXT_RULE_VERSION = "1.1.0"  # 演示规则升级：追加智能引号折叠，见 SMART_QUOTES


@dataclass(frozen=True)
class Rule:
    rule_id: str
    version: str
    kind: str          # drop | fold | layout
    rationale: str


# 规则 ID 是落库证据的一部分，重命名等于破坏历史回放，故保持稳定。
RULES: dict[str, Rule] = {
    "enc-nfc": Rule(
        "enc-nfc", "1.0.0", "fold",
        "Unicode NFC 规范化：仅做标准等价组合，不增删语义字符。"),
    "enc-fullwidth-ascii": Rule(
        "enc-fullwidth-ascii", "1.0.0", "fold",
        "仅全角 ASCII 字母 U+FF21..U+FF3A/U+FF41..U+FF5A 与数字 U+FF10..U+FF19 "
        "按 Unicode 恒等式 codepoint-0xFEE0 折叠为半角（可证明编码差异，数字语义不变）；"
        "全角标点与括号是政策文本的合法标点，证据不足，保留不折叠。"),
    "layout-zero-width": Rule(
        "layout-zero-width", "1.0.0", "drop",
        "BOM(U+FEFF)/零宽空格(U+200B)/软连字符(U+00AD) 为不可排版语义的版式码点；"
        "ZWJ/ZWNJ 可能改变构词，证据不足，不在此列。"),
    "punct-hard-hyphen": Rule(
        "punct-hard-hyphen", "1.0.0", "fold",
        "U+2010/U+2011 是 ASCII 连字符 U+2D 的硬连字体变体，属可证明印刷差异；"
        "en/em/figure dash 可能承载数值区间语义，证据不足，保留。"),
    "ws-linebreak": Rule(
        "ws-linebreak", "1.0.0", "layout",
        "CRLF/CR/VT/FF/NEL/LS/PS 统一规整为 U+000A，换行（条款边界）本身保留。"),
    "ws-h-horizontal": Rule(
        "ws-h-horizontal", "1.0.0", "layout",
        "TAB 及 Unicode Zs 类水平空白（含 NBSP、全角空格）折叠为单个 ASCII 空格；"
        "只改变空白宽度，不触碰任何非空白字符。"),
    "ws-line-trim": Rule(
        "ws-line-trim", "1.0.0", "layout",
        "删除行首尾水平空白：该空白不承载语义且位置可由行边界证明。"),
    "ws-collapse": Rule(
        "ws-collapse", "1.0.0", "layout",
        "行内连续水平空白压缩为一个空格：连续空白的差异不承载语义。"),
    "layout-blank-line": Rule(
        "layout-blank-line", "1.0.0", "layout",
        "压缩连续换行/空行并去除首尾换行；每个被删换行都留痕，条款边界换行保留。"),
    "punct-smart-quote": Rule(
        "punct-smart-quote", "1.1.0", "fold",
        "U+2018/2019/201C/201D 弯引号折叠为 ASCII 引号（1.1.0 才启用；"
        "1.0.0 时代落库的记录按其 pin 住的旧版本回放，保持兼容）。"),
}

# ---------------------------------------------------------------------------
# 字符分类：折叠 / 删除 / 保留的明确白名单，白名单之外一律保留
# ---------------------------------------------------------------------------

_DROP_LAYOUT = frozenset(("­", "​", "﻿"))
_HARD_HYPHENS = frozenset(("‐", "‑"))
_SMART_QUOTES = {
    "‘": "'", "’": "'",
    "“": '"', "”": '"',
}
_LINE_BREAKS = frozenset((
    "\n", "\r", "\v", "\f", "\u0085", " ", " ",
))


def _is_horizontal_ws(ch: str) -> bool:
    if ch == "\t":
        return True
    return unicodedata.category(ch) == "Zs"


def _fold_char(ch: str, rule_version: str) -> tuple[str, str | None]:
    """返回 (折叠后字符, 命中规则 ID)。不在白名单内的字符原样返回。"""
    cp = ord(ch)
    is_fullwidth_alnum = (
        0xFF10 <= cp <= 0xFF19       # ０-９
        or 0xFF21 <= cp <= 0xFF3A    # Ａ-Ｚ
        or 0xFF41 <= cp <= 0xFF5A    # ａ-ｚ
    )
    if is_fullwidth_alnum:
        return chr(cp - 0xFEE0), "enc-fullwidth-ascii"
    if ch in _HARD_HYPHENS:
        return "-", "punct-hard-hyphen"
    if rule_version != "1.0.0" and ch in _SMART_QUOTES:
        return _SMART_QUOTES[ch], "punct-smart-quote"
    return ch, None


# ---------------------------------------------------------------------------
# NFC 对齐：把组合后的每个输出字符映射回原文区间（可能 1 对多）
# ---------------------------------------------------------------------------

def _nfc_align(pending: list[tuple[str, tuple[int, int]]]):
    """pending 为 [(字符, 原文区间), ...]，产出 [(nfc字符, (区间,...)), ...]。"""
    if not pending:
        return []
    composed = unicodedata.normalize("NFC", "".join(ch for ch, _ in pending))
    if len(composed) == len(pending):  # 快速路径：无可组合序列
        return [(ch, (span,)) for ch, span in pending]

    out: list[tuple[str, tuple[tuple[int, int], ...]]] = []
    k = 0
    n = len(pending)
    while k < n:
        ch, span = pending[k]
        if composed[len(out)] == ch:
            out.append((ch, (span,)))
            k += 1
            continue
        target = composed[len(out)]
        # 组合情形（base + 组合符，偶发多字符组合），在有限窗口内找 NFC 恒等片段
        found = False
        for r in range(k + 1, min(n, k + 4)):
            substr = "".join(pending[t][0] for t in range(k, r + 1))
            if unicodedata.normalize("NFC", substr) == target:
                out.append((target, tuple(pending[t][1] for t in range(k, r + 1))))
                k = r + 1
                found = True
                break
        if not found:  # 宁可报错也不产出无法溯源的映射
            raise AssertionError(f"NFC alignment failed at {k}: {pending[k:k+4]!r}")
    if "".join(c for c, _ in out) != composed:
        raise AssertionError("NFC alignment output mismatch")
    return out


# ---------------------------------------------------------------------------
# 核心归一化
# ---------------------------------------------------------------------------

def normalize_text(raw: str, rule_version: str = RULE_VERSION) -> dict[str, Any]:
    """对单个文本做保守归一化，返回带完整溯源证据的契约对象。

    返回结构::

        {
          "text": "归一化文本",
          "map": [[out_index, src_start, src_end, [[s, e], ...]], ...],
          "dropped": [{"s", "e", "rule_id", "char", "reason"}, ...],
          "folds": [{"s", "e", "rule_id", "before", "after", "reason"}, ...],
          "rule_version": "1.0.0",
          "source_len": N,
        }
    """
    if rule_version not in ("1.0.0", "1.1.0"):
        raise ValueError(f"unknown rule_version: {rule_version!r}")

    events: list[dict[str, Any]] = []   # {"char": c, "spans": [(s,e),...]}
    dropped: list[dict[str, Any]] = []
    folds: list[dict[str, Any]] = []
    pending: list[tuple[str, tuple[int, int]]] = []

    def flush() -> None:
        if not pending:
            return
        aligned = _nfc_align(pending)
        for ch, spans in aligned:
            events.append({"char": ch, "spans": list(spans)})
            if len(spans) > 1:  # 多码点组合为一个输出字符：可证明的编码折叠
                folds.append({
                    "s": spans[0][0], "e": max(e for _, e in spans),
                    "rule_id": "enc-nfc",
                    "before": raw[spans[0][0]:max(e for _, e in spans)],
                    "after": ch, "reason": RULES["enc-nfc"].rationale,
                })
        pending.clear()

    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        is_crlf = ch == "\r" and i + 1 < n and raw[i + 1] == "\n"
        j = i + 2 if is_crlf else i + 1
        span = (i, j)
        i = j

        if is_crlf or ch in _LINE_BREAKS:
            flush()
            # 保留的换行事件是普通条款边界，不带任何规则标记：
            # 异构换行的规则证据只落在下方 dropped 的 CR/被替换码点上，
            # 这样对已规整文本再次归一化不会产生新 dropped（幂等）。
            events.append({"char": "\n", "spans": [(span[0], span[0] + 1)]})
            if is_crlf:  # CR 被删除、LF 映射为 \n，两区间互补不重叠
                dropped.append({
                    "s": span[0] + 1, "e": span[1], "rule_id": "ws-linebreak",
                    "char": raw[span[0] + 1:span[1]],
                    "reason": RULES["ws-linebreak"].rationale,
                })
            continue

        if ch in _DROP_LAYOUT:
            flush()
            dropped.append(_drop(span, "layout-zero-width", raw,
                                 RULES["layout-zero-width"].rationale))
            continue

        mapped, rule_id = _fold_char(ch, rule_version)
        if rule_id is not None and mapped != ch:
            flush()
            events.append({"char": mapped, "spans": [span]})
            folds.append({
                "s": span[0], "e": span[1], "rule_id": rule_id,
                "before": raw[span[0]:span[1]], "after": mapped,
                "reason": RULES[rule_id].rationale,
            })
            continue
        pending.append((mapped, span))
    flush()

    # ---- 布局阶段：按换行事件切块，修剪 / 折叠水平空白，压缩空行 -----------
    # 单遍扫描：把事件切成“行”，每行记录它之前连续出现的换行事件 lead_nls。
    # 第一行的 lead_nls 是前导换行；末行之后若还有换行，归入 trailing_nls。
    # 行与行之间保留一个换行作为条款边界，空行（无保留字符的行）不产生边界。
    lines: list[list[dict[str, Any]]] = []
    lead_nls: list[list[dict[str, Any]]] = []   # 与 lines 等长、同索引
    pending_nls: list[dict[str, Any]] = []
    cur_line: list[dict[str, Any]] | None = None
    for ev in events:
        if ev["char"] == "\n":
            pending_nls.append(ev)
            continue
        # 每个非换行事件开启/续接一行；新行携带它之前累积的全部换行
        if cur_line is None or pending_nls:
            cur_line = []
            lines.append(cur_line)
            lead_nls.append(pending_nls)
            pending_nls = []
        cur_line.append(ev)
    # 末行之后若仍有换行，归入尾部间隙
    trailing_nls: list[dict[str, Any]] = list(pending_nls)

    def tidy(block: list[dict[str, Any]]) -> list[tuple[str, list[tuple[int, int]]]]:
        line = list(block)
        while line and _is_horizontal_ws(line[0]["char"]):
            dropped.append(_drop_ev(line.pop(0), "ws-line-trim", raw))
        while line and _is_horizontal_ws(line[-1]["char"]):
            dropped.append(_drop_ev(line.pop(), "ws-line-trim", raw))
        out: list[tuple[str, list[tuple[int, int]]]] = []
        prev_ws = False
        for ev in line:
            if _is_horizontal_ws(ev["char"]):
                if prev_ws:
                    dropped.append(_drop_ev(ev, "ws-collapse", raw))
                else:
                    out.append((" ", ev["spans"]))
                prev_ws = True
            else:
                out.append((ev["char"], ev["spans"]))
                prev_ws = False
        return out

    kept: list[tuple[str, list[tuple[int, int]]]] = []
    seen_kept = False
    for li, block in enumerate(lines):
        line = tidy(block)  # 行内被修剪/折叠掉的空白已在 tidy 内留痕
        gap = lead_nls[li]
        if not line:
            # 空行（整行只有水平空白）：它前面的换行属于空行噪声，全部留痕
            for nl in gap:
                dropped.append(_drop_ev(nl, "layout-blank-line", raw))
            continue
        if seen_kept:
            # 与上一个保留行之间：间隙里保留第一个换行作为条款边界，
            # 其余（空行压缩）留痕；gap 为空说明此前的行已被丢弃，无边界可保留
            if gap:
                kept.append(("\n", gap[0]["spans"]))
                for nl in gap[1:]:
                    dropped.append(_drop_ev(nl, "layout-blank-line", raw))
        else:
            # 第一个保留行：它前面的换行都是前导噪声，留痕
            for nl in gap:
                dropped.append(_drop_ev(nl, "layout-blank-line", raw))
        kept.extend(line)
        seen_kept = True
    # 尾部换行（最后一行之后）不构成边界，整体留痕
    for nl in trailing_nls:
        dropped.append(_drop_ev(nl, "layout-blank-line", raw))

    dropped.sort(key=lambda d: (d["s"], d["e"]))
    folds.sort(key=lambda d: (d["s"], d["e"]))
    text = "".join(c for c, _ in kept)
    out_map = [
        [idx, spans[0][0], max(e for _, e in spans), [list(s) for s in spans]]
        for idx, (_, spans) in enumerate(kept)
    ]
    result = {
        "text": text,
        "map": out_map,
        "dropped": dropped,
        "folds": folds,
        "rule_version": rule_version,
        "source_len": n,
    }
    _assert_coverage(raw, result)
    return result


def _drop(span: tuple[int, int], rule_id: str, raw: str, reason: str) -> dict[str, Any]:
    return {
        "s": span[0], "e": span[1], "rule_id": rule_id,
        "char": raw[span[0]:span[1]], "reason": reason,
    }


def _drop_ev(ev: dict[str, Any], rule_id: str, raw: str) -> dict[str, Any]:
    s = ev["spans"][0][0]
    e = max(end for _, end in ev["spans"])
    rid = rule_id
    # 仅当一个换行作为空行被布局阶段删除时，才沿用它自身的换行来源规则
    # （如 CRLF 规整）；保留下来的换行不会经过本函数，故不影响幂等。
    if rule_id == "layout-blank-line" and ev.get("rule_id"):
        rid = ev["rule_id"]
    return {
        "s": s, "e": e, "rule_id": rid,
        "char": raw[s:e], "reason": RULES[rid].rationale,
    }


def _merge_spans(spans):
    """排序并合并重叠/相邻区间，返回连续的 [[s, e], ...]（NFC 多区间需要）。"""
    ordered = sorted(spans)
    merged: list[list[int]] = []
    for s, e in ordered:
        if e <= s:
            raise AssertionError(f"empty provenance span: {(s, e)}")
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return merged


def _assert_coverage(raw: str, result: dict[str, Any]) -> None:
    """map 与 dropped 的原文区间合并后必须无缝完整覆盖 [0, len(raw))。"""
    spans = [(m[1], m[2]) for m in result["map"]]
    spans.extend((d["s"], d["e"]) for d in result["dropped"])
    merged = _merge_spans(spans)
    cursor = 0
    for s, e in merged:
        if s != cursor:
            raise AssertionError(
                f"provenance gap/overlap near {cursor}: {(s, e)}")
        cursor = e
    if cursor != len(raw):
        raise AssertionError("provenance does not cover full source text")
    # 子区间明细本身也要落在对应主区间内，且拼接后等于主区间
    for _, s, e, sub in result["map"]:
        flat = [seg for pair in sub for seg in pair]
        if sub and (min(p[0] for p in sub) != s or max(p[1] for p in sub) != e):
            raise AssertionError("NFC sub-spans escape their main span")
        if flat and flat != sorted(flat):
            raise AssertionError("NFC sub-spans are not ordered")


def recover_text(raw: str, result: dict[str, Any]) -> str:
    """按 map + dropped 的区间事件回放，逐字符重建原文。

    map 条目的主区间已覆盖其全部子区间（NFC 组合时一个输出字符对应多个
    连续原文码点）；主区间之间可能因组合而相邻重叠，回放前先排序并合并。
    """
    spans = [(m[1], m[2]) for m in result["map"]]
    spans.extend((d["s"], d["e"]) for d in result["dropped"])
    merged = _merge_spans(spans)
    pieces: list[str] = []
    cursor = 0
    for s, e in merged:
        if s != cursor:
            raise AssertionError("recovery gap: provenance is not contiguous")
        pieces.append(raw[s:e])
        cursor = e
    if cursor != len(raw):
        raise AssertionError("recovery tail: provenance is incomplete")
    return "".join(pieces)


def source_span(result: dict[str, Any], out_start: int, out_end: int) -> tuple[int, int]:
    """把归一化文本上的输出区间 [out_start, out_end) 翻译回原文区间。"""
    starts = [m[1] for m in result["map"][out_start:out_end]]
    ends = [m[2] for m in result["map"][out_start:out_end]]
    if not starts:
        return (0, 0)
    return (min(starts), max(ends))


# ---------------------------------------------------------------------------
# 记录级处理（流水线阶段）与分批恢复
# ---------------------------------------------------------------------------

# 视为政策文本、需要保守归一化并携带溯源契约的字段
TEXT_FIELDS = ("name", "title", "text", "content", "clause_text", "body")


def process(record: dict[str, Any], rule_version: str = RULE_VERSION) -> dict[str, Any]:
    """流水线 normalize 阶段：旧标量字段行为保持不变，新增 _norm 溯源契约。

    原文优先：进入任何规整前先把文本字段快照到 ``_raw``，因此即使旧契约
    要求就地 strip ``name``，原文仍可通过 ``_raw`` + 溯源证据逐字符回看。
    """
    rec = dict(record)
    raw_snapshot = {
        field: record[field]
        for field in TEXT_FIELDS
        if isinstance(record.get(field), str)
    }
    if raw_snapshot:
        rec["_raw"] = raw_snapshot

    # 既有字段契约（旧下游兼容，行为与历史版本一致）
    rec["src"] = str(rec.get("src", "")).strip().lower()
    rec["name"] = str(rec.get("name", "")).strip()
    rec["value"] = int(rec.get("value", 0))

    norm: dict[str, Any] = {}
    for field, raw in raw_snapshot.items():
        norm[field] = normalize_text(raw, rule_version)
    if norm:
        rec["_norm"] = norm
    return rec


def backfill(records, rule_version: str = RULE_VERSION):
    """分批恢复/重放入口：对一批记录按指定规则版本重新归一化（惰性生成器）。"""
    for record in records:
        yield process(record, rule_version)


def recover_record(record: dict[str, Any]) -> dict[str, str]:
    """从落库记录的 _norm 证据重建各文本字段原文，验证原文始终可回看。

    优先使用进入流水线前冻结的 ``_raw`` 快照；对没有快照的历史记录，
    退回到当前字段值（此时要求该字段未被旧契约就地改写）。
    """
    out: dict[str, str] = {}
    norm = record.get("_norm") or {}
    snapshot = record.get("_raw") or {}
    for field, result in norm.items():
        raw = snapshot.get(field, record.get(field, ""))
        replayed = recover_text(raw, result)
        if replayed != raw:
            raise AssertionError(f"replay mismatch on field {field!r}")
        out[field] = replayed
    return out

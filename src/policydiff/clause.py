"""单一职责：clause 阶段——条款切分与稳定编号。

输入是 normalize 阶段保留下来的条款边界（归一化文本中的 ``\\n``）。
本阶段只按边界切分，不删除、不改写任何字符，因此：
* 同一条款边界在再次归一化 / 重跑后位置稳定（边界稳定）；
* 每个条款都带归一化区间和（经 doc_parse 证据翻译得到的）原文区间，
  下游可直接回看原始政策文本。
"""
from __future__ import annotations


def split_clauses(text):
    """按保留的换行边界切分，返回 [(out_start, out_end, 条款文本), ...]。"""
    out = []
    start = 0
    for idx, ch in enumerate(text):
        if ch == "\n":
            out.append((start, idx, text[start:idx]))
            start = idx + 1
    out.append((start, len(text), text[start:]))
    return out


def process(record):
    rec = dict(record)
    norm = rec.get("_norm")
    clauses = []
    if isinstance(norm, dict):
        for field, result in sorted(norm.items()):
            text = result["text"]
            for local_idx, (s, e, clause_text) in enumerate(split_clauses(text)):
                clause = {
                    "id": f"{field}#c{local_idx:03d}",
                    "field": field,
                    "seq": local_idx,
                    "text": clause_text,
                    "out_span": [s, e],
                    "rule_version": result["rule_version"],
                }
                prov = (rec.get("_provenance") or {}).get(field)
                if prov is not None:
                    clause["src_span"] = _translate(prov["map"], s, e)
                clauses.append(clause)
    rec["_clauses"] = clauses
    rec.setdefault("_stage_clause", "ok")
    return rec


def _translate(out_map, out_start, out_end):
    """复用 normalize 契约里的 map，把输出区间翻译为原文 [start, end)。"""
    selected = out_map[out_start:out_end]
    if not selected:
        return [0, 0]
    return [min(m[1] for m in selected), max(m[2] for m in selected)]

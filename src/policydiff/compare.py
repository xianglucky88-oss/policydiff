"""单一职责：compare 阶段——实质差异敏感性。

指纹基于 normalize 之后的文本（纯噪声变体归一化结果相同 → 指纹相同，
格式等价）；而否定词、负号、法律数字、金额单位、括号等被保守保留，
任何实质修改都会改变归一化文本 → 指纹变化，差异不会被清洗掩盖。

本阶段只计算指纹与逐条款差异，不做语义判断或阈值合并。
"""
from __future__ import annotations

import hashlib


def fingerprint(text):
    """归一化文本的稳定指纹（sha256 前 16 位，落库可比较）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def process(record):
    rec = dict(record)
    norm = rec.get("_norm")
    compare = {}
    if isinstance(norm, dict):
        for field, result in norm.items():
            compare[field] = {
                "rule_version": result["rule_version"],
                "text_fp": fingerprint(result["text"]),
            }
    rec["_compare"] = compare
    rec.setdefault("_stage_compare", "ok")
    return rec


def diff_records(a, b, fields=None):
    """对比两条已处理记录，按字段/条款给出指纹级差异及原文回看区间。

    返回::

        {"fields": {field: {"changed": bool, "a_fp", "b_fp",
                            "rule_versions": [...], "clauses": [...]}}}
    """
    na = a.get("_norm") or {}
    nb = b.get("_norm") or {}
    names = fields or sorted(set(na) | set(nb))
    out = {}
    clauses_a = _index_clauses(a)
    clauses_b = _index_clauses(b)
    for field in names:
        ra, rb = na.get(field), nb.get(field)
        entry = {
            "changed": _field_changed(ra, rb),
            "a_fp": fingerprint(ra["text"]) if ra else None,
            "b_fp": fingerprint(rb["text"]) if rb else None,
            "rule_versions": sorted({
                r["rule_version"] for r in (ra, rb) if r is not None
            }),
            "clauses": _diff_clauses(clauses_a.get(field, []),
                                     clauses_b.get(field, [])),
        }
        out[field] = entry
    return {"fields": out}


def _field_changed(ra, rb):
    if ra is None or rb is None:
        return ra is not rb
    return fingerprint(ra["text"]) != fingerprint(rb["text"])


def _index_clauses(record):
    out = {}
    for c in record.get("_clauses", []):
        out.setdefault(c["field"], []).append(c)
    return out


def _diff_clauses(la, lb):
    out = []
    for i in range(max(len(la), len(lb))):
        ca = la[i] if i < len(la) else None
        cb = lb[i] if i < len(lb) else None
        ta = ca["text"] if ca else None
        tb = cb["text"] if cb else None
        out.append({
            "id": (ca or cb)["id"],
            "changed": ta != tb,
            "a": ta,
            "b": tb,
            "a_src_span": ca.get("src_span") if ca else None,
            "b_src_span": cb.get("src_span") if cb else None,
        })
    return out

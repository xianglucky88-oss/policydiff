"""单一职责：doc_parse 阶段。

不修改原文、不重新归一化；只沿数据契约把 normalize 产出的溯源证据
（map / dropped / folds / rule_version）传递下去，并提供输出区间到
原文区间的翻译，供 clause、compare 定位证据。
"""
from __future__ import annotations

from . import normalize as _normalize


def process(record):
    rec = dict(record)
    rec.setdefault("_stage_doc_parse", "ok")

    norm = rec.get("_norm")
    if isinstance(norm, dict):
        provenance = {}
        for field, result in norm.items():
            provenance[field] = {
                "rule_version": result["rule_version"],
                "source_len": result["source_len"],
                "map": result["map"],
                "dropped": result["dropped"],
                "folds": result["folds"],
            }
        rec["_provenance"] = provenance
    else:
        rec.setdefault("_provenance", {})
    return rec


def source_span(record, field, out_start, out_end):
    """把归一化文本上的区间翻译回该字段原文区间（跨阶段证据回看入口）。"""
    result = (record.get("_norm") or {}).get(field)
    if result is None:
        raise KeyError(f"no normalized provenance for field {field!r}")
    return _normalize.source_span(result, out_start, out_end)

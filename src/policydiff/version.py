"""单一职责：版本完整性评估（version）。

回答一个问题：当前这份文档是否为完整版本？
该结论直接决定 compare 中“A 有而 B 没有”能否成立为“删除”：
  - B 完整 → A-only 条款可以确定为「删除」；
  - B 不完整 → 只能标为「未知/证据不足」，不能断言删除。

判定输入（任一命中即视为不完整，命中原因全部保留，可重复生成）：
  record["incomplete"] 显式标记；
  record["status"] ∈ {partial, incomplete, draft, truncated}；
  record["missing_sections"] 非空列表；
  record["text"] 含截断/缺失声明语；
  预结构化 clauses 中存在占位缺段标记。
"""
import re

_PARTIAL_STATUSES = {"partial", "incomplete", "draft", "truncated",
                     "不完整", "草稿", "节选", "截断"}
_TRUNCATION_RE = re.compile(
    r"(下略|略……|内容缺失|原文缺失|本节缺|待补充|未完待续|"
    r"truncated|incomplete|omitted|missing)",
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(r"^(缺|缺失|略|待补|\.{3,}|…+)$")


def assess(record):
    """返回 {"incomplete": bool, "reasons": [...], "missing_sections": [...]}。"""
    rec = record or {}
    reasons = []

    if rec.get("incomplete") is True:
        reasons.append("explicit_flag")
    status = str(rec.get("status", "")).strip().lower()
    if status in _PARTIAL_STATUSES:
        reasons.append("status:" + status)

    missing = rec.get("missing_sections") or []
    if isinstance(missing, list) and missing:
        reasons.append("missing_sections")

    text = rec.get("text")
    if isinstance(text, str) and _TRUNCATION_RE.search(text):
        reasons.append("truncation_marker")

    clauses = rec.get("clauses")
    if isinstance(clauses, list):
        for c in clauses:
            t = str(c.get("text", "")).strip()
            if t and _PLACEHOLDER_RE.match(t):
                reasons.append("placeholder_clause")
                break

    return {
        "incomplete": bool(reasons),
        "reasons": sorted(set(reasons)),
        "missing_sections": list(missing) if isinstance(missing, list) else [],
    }


def process(record):
    rec = dict(record)
    verdict = assess(rec)
    rec["incomplete"] = verdict["incomplete"]
    rec["incomplete_reasons"] = verdict["reasons"]
    if verdict["missing_sections"]:
        rec["missing_sections"] = verdict["missing_sections"]
    rec.setdefault("_stage_version", "ok")
    return rec

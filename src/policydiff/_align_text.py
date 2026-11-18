"""Internal helpers for policy clause normalization and similarity.

This module is deliberately free of persistence concerns: it turns already-parsed
clause dictionaries into canonical clause references and computes comparable
signals (body / heading / section / number / position).

Clause *content* is never rewritten here.  We only derive normalized forms such
as ``text_norm`` and a SHA-256 hash so that alignment can refer back to the
upstream parser output without duplicating it.
"""
from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

CLAUSE_ID_KEYS = ("clause_id", "clause_key", "id", "uid", "key")
TEXT_KEYS = ("text", "content", "body", "paragraph", "value")
HEADING_KEYS = ("heading", "title", "name", "caption", "label")
NUMBER_KEYS = ("number", "clause_no", "clause_number", "article_no", "no", "条号")
SECTION_KEYS = ("section", "section_title", "chapter", "chapter_title", "part", "part_title")
ATTACHMENT_KEYS = ("attachment", "appendix", "annex", "附件", "附录")

DOC_KEYS = ("doc_id", "document_id", "policy_id", "policy", "doc", "document")
VERSION_KEYS = ("version", "version_id", "edition", "release", "revision")
YEAR_KEYS = ("year", "effective_year", "publish_year", "pub_year", "revision_year")

ITEM_LIST_KEYS = ("clauses", "articles", "items", "paragraphs", "entries")

_PUNCT_RE = re.compile(
    "[\s　·•,，。;；:：、!！?？\"'“”‘’（）()\\[\\]【】「」『』《》<>—\-–_/\\\.…~`]+"
)
_LEADING_NUMBER_RE = re.compile(
    r"^\s*(?:第\s*[0-9零〇一二三四五六七八九十百千两]+\s*[章节条款项目篇部分编]"
    r"|[0-9]+(?:\.[0-9]+)*[、.．)）]?|[（(][0-9一二三四五六七八九十]+[)）])"
)
_ARTICLE_RE = re.compile(
    r"第\s*([0-9]+|[零〇一二三四五六七八九十百千两]+)\s*([章节条款项目篇部分编])"
)
_HEADING_NUMBER_RE = re.compile(
    r"^\s*(?:第\s*([0-9]+|[零〇一二三四五六七八九十百千两]+)\s*([章节条款项目篇部分编])"
    r"|([0-9]+(?:\.[0-9]+)*)[、.．)）]?)"
)

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}

# Synonym buckets are intentionally conservative: they only canonicalize a
# heading when an exact synonym term is present inside the heading text.
SYNONYM_GROUPS = (
    ("总则", "一般规定", "基本原则", "总纲"),
    ("适用范围", "适用对象", "适用情形", "适用条件"),
    ("附则", "其他规定", "其他事项", "补充规定"),
    ("定义", "名词解释", "术语解释", "用语含义"),
    ("监督管理", "监督", "管理", "监督检查"),
    ("法律责任", "责任", "罚则", "处罚"),
    ("施行", "生效", "实施", "施行日期"),
    ("组织机构", "机构", "职责", "组织"),
    ("申请", "申报", "报送"),
    ("审批", "审核", "核准", "批准"),
)

_ATTACHMENT_RE = re.compile(r"附件|附录|附表|appendix|annex", re.IGNORECASE)


def pick(mapping: Mapping[str, Any] | None, keys: Iterable[str], default: Any = None) -> Any:
    if not mapping:
        return default
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Mapping):
        # tolerate parser output such as {"value": "..."} / {"text": "..."}
        return _as_text(pick(value, TEXT_KEYS + HEADING_KEYS + NUMBER_KEYS, ""))
    if isinstance(value, Sequence):
        return "\n".join(_as_text(v) for v in value)
    return str(value)


def normalize_text(value: Any) -> str:
    """Normalize body text for comparison only (never persisted as content)."""
    text = unicodedata.normalize("NFKC", _as_text(value))
    text = text.lower()
    return _PUNCT_RE.sub("", text)


def normalize_heading(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _as_text(value)).lower()
    text = _LEADING_NUMBER_RE.sub("", text)
    return _PUNCT_RE.sub("", text)


def canonical_heading(value: Any) -> str:
    """Return a synonym-canonical heading key.

    Only whole synonym terms are substituted.  This lets 同义标题 (总则 vs
    一般规定, 适用范围 vs 适用对象) match while unrelated headings stay apart.
    """
    normalized = normalize_heading(value)
    if not normalized:
        return ""
    for group in SYNONYM_GROUPS:
        for term in group:
            if normalized == term:
                return "syn:" + group[0]
        # tolerate a short qualifier around an unambiguous synonym term, but do
        # not conflate e.g. "财务管理" and "数据管理" via the bare "管理" term.
        for term in group:
            if len(term) >= 3 and abs(len(normalized) - len(term)) <= 2:
                if normalized.startswith(term) or normalized.endswith(term):
                    return "syn:" + group[0]
    return normalized


def bigrams(text: str) -> frozenset[str]:
    if not text:
        return frozenset()
    if len(text) == 1:
        return frozenset({text})
    return frozenset(text[i:i + 2] for i in range(len(text) - 1))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def text_similarity(a_norm: str, b_norm: str,
                    a_grams: frozenset[str] | None = None,
                    b_grams: frozenset[str] | None = None) -> float:
    if not a_norm or not b_norm:
        return 0.0
    if a_norm == b_norm:
        return 1.0
    a_grams = a_grams if a_grams is not None else bigrams(a_norm)
    b_grams = b_grams if b_grams is not None else bigrams(b_norm)
    gram_score = jaccard(a_grams, b_grams)
    # Cheap upper bound on difflib's ratio: it can never exceed
    # ``2*min(len_a,len_b)/(len_a+len_b)``.  Skip the quadratic matcher when even
    # the best possible combination stays below the caller's useful range.
    upper_ratio = 2.0 * min(len(a_norm), len(b_norm)) / (len(a_norm) + len(b_norm))
    if 0.55 * upper_ratio + 0.45 * gram_score < 0.30:
        return 0.55 * upper_ratio + 0.45 * gram_score
    ratio = difflib.SequenceMatcher(None, a_norm, b_norm, autojunk=False).ratio()
    score = 0.55 * ratio + 0.45 * gram_score
    # containment helps when a clause is quoted/expanded inside another, but
    # should not alone imply a split; split/merge use union coverage instead.
    shorter, longer = (a_norm, b_norm) if len(a_norm) <= len(b_norm) else (b_norm, a_norm)
    if len(shorter) >= 8 and shorter in longer:
        containment = len(shorter) / len(longer)
        score = max(score, 0.55 + 0.35 * containment)
    return min(1.0, score)


def cn_to_int(token: str) -> int | None:
    if not token:
        return None
    if "." in token:
        # "3.2" style numbering: compare the parts, not their concatenation.
        parts = []
        for part in token.split("."):
            if not part.isdigit():
                return None
            parts.append(int(part))
        return int("".join("%03d" % p for p in parts)) if parts else None
    if token.isdigit():
        try:
            return int(token)
        except ValueError:
            return None
    total, section, number = 0, 0, 0
    for ch in token:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if number == 0:
                number = 1
            section += number * unit
            number = 0
        else:
            return None
    if section or number:
        total = section + number
    return total or None


def extract_number(value: Any) -> dict[str, Any] | None:
    """Extract a clause/chapter number and its kind from a raw value."""
    text = _as_text(value).strip()
    if not text:
        return None
    match = _ARTICLE_RE.search(text)
    if match:
        raw, kind = match.group(1), match.group(2)
        return {"value": cn_to_int(raw), "kind": kind, "raw": match.group(0)}
    match = _HEADING_NUMBER_RE.match(text)
    if match:
        raw = match.group(1) or match.group(3)
        kind = match.group(2) or ""
        return {"value": cn_to_int(raw), "kind": kind, "raw": match.group(0)}
    return {"value": None, "kind": "", "raw": text}


def number_similarity(a: dict[str, Any] | None, b: dict[str, Any] | None) -> float | None:
    if not a or not b:
        return None
    if a.get("value") is None or b.get("value") is None:
        return None
    if a.get("kind") and b.get("kind") and a["kind"] != b["kind"]:
        return 0.0
    return 1.0 if a["value"] == b["value"] else 0.0


def coverage(children_grams: Sequence[frozenset[str]], parent_grams: frozenset[str]) -> float:
    """How much of the parent clause's text is covered by the children union."""
    if not parent_grams:
        return 0.0
    union: set[str] = set()
    for grams in children_grams:
        union.update(grams)
    return len(union & parent_grams) / len(parent_grams)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_hash(*parts: Any, length: int = 16) -> str:
    payload = "\x1f".join(str(p) for p in parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:length]


def _detect_attachment(*values: Any) -> str:
    for value in values:
        text = _as_text(value)
        if text and _ATTACHMENT_RE.search(text):
            return text.strip()
    return ""


def looks_like_clause(obj: Mapping[str, Any]) -> bool:
    if not isinstance(obj, Mapping):
        return False
    if any(k in obj for k in TEXT_KEYS):
        return True
    return bool(pick(obj, NUMBER_KEYS) and pick(obj, HEADING_KEYS))


def _flatten_sections(sections: Any, parent_section: str = "",
                      parent_attachment: str = "", out: list[dict[str, Any]] | None = None,
                      depth: int = 0) -> list[dict[str, Any]]:
    out = out if out is not None else []
    if depth > 8 or not isinstance(sections, Sequence) or isinstance(sections, (str, bytes)):
        return out
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        heading = _as_text(pick(section, SECTION_KEYS + HEADING_KEYS, ""))
        section_name = heading or parent_section
        attachment = _detect_attachment(pick(section, ATTACHMENT_KEYS), heading) or parent_attachment
        nested = pick(section, ("sections", "subsections"))
        if nested:
            _flatten_sections(nested, section_name, attachment, out, depth + 1)
            continue
        items = pick(section, ITEM_LIST_KEYS)
        if items is not None:
            _flatten_sections(items, section_name, attachment, out, depth + 1)
            continue
        if looks_like_clause(section):
            item = dict(section)
            item.setdefault("_section_hint", section_name)
            item.setdefault("_attachment_hint", attachment)
            out.append(item)
    return out


def split_version_doc(obj: Any, default_doc_id: str | None = None,
                      default_version: str | None = None) -> dict[str, Any]:
    """Normalize many accepted input shapes into one version document.

    Accepted shapes:
      * ``{"doc_id", "version", "clauses": [...]}``
      * ``{"doc_id", "year", "sections": [{"heading", "clauses": [...]}]}``
      * a bare list of parsed clause dicts
      * a single parsed clause dict
      * an already-normalized document (returned unchanged)
    """
    if isinstance(obj, Mapping) and is_normalized(obj):
        return dict(obj)
    if isinstance(obj, Mapping):
        doc_id = _as_text(pick(obj, DOC_KEYS, default_doc_id or "")) or (default_doc_id or "")
        version = _as_text(pick(obj, VERSION_KEYS, default_version or "")) or (default_version or "")
        year = pick(obj, YEAR_KEYS)
        clauses_raw = obj.get("clauses")
        if clauses_raw is None and "sections" in obj:
            clauses_raw = _flatten_sections(obj.get("sections"))
        if clauses_raw is None and looks_like_clause(obj):
            clauses_raw = [obj]
        if clauses_raw is None:
            clauses_raw = []
    elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
        doc_id, version, year = default_doc_id or "", default_version or "", None
        clauses_raw = list(obj)
    else:
        raise TypeError("unsupported version document: %r" % (type(obj),))

    if not isinstance(clauses_raw, Sequence) or isinstance(clauses_raw, (str, bytes)):
        clauses_raw = [clauses_raw]
    return {
        "doc_id": doc_id,
        "version": version,
        "year": year,
        "clauses_raw": list(clauses_raw),
    }


def normalize_clauses(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Attach canonical identifiers, anchors and comparison forms to clauses."""
    doc_id = _as_text(doc.get("doc_id"))
    version = _as_text(doc.get("version"))
    year = doc.get("year")
    clauses: list[dict[str, Any]] = []
    seen: dict[str, int] = {}

    for ordinal, raw in enumerate(doc.get("clauses_raw") or []):
        if not isinstance(raw, Mapping):
            continue
        explicit_id = _as_text(pick(raw, CLAUSE_ID_KEYS, ""))
        number_raw = pick(raw, NUMBER_KEYS)
        heading = _as_text(pick(raw, HEADING_KEYS, ""))
        text = _as_text(pick(raw, TEXT_KEYS, ""))
        section = _as_text(pick(raw, SECTION_KEYS, "")) or _as_text(raw.get("_section_hint"))
        attachment = _detect_attachment(
            pick(raw, ATTACHMENT_KEYS), raw.get("_attachment_hint"), section
        )
        number = extract_number(number_raw)
        if number is None:
            number = extract_number(heading)
        if not heading:
            heading = _as_text(raw.get("_section_hint"))
        text_norm = normalize_text(text)
        if not text_norm:
            # Some parsers keep the actionable text in the heading.
            text_norm = normalize_heading(heading)
        doc_side = _as_text(pick(raw, DOC_KEYS, "")) or doc_id
        clause_version = _as_text(pick(raw, VERSION_KEYS, "")) or version
        anchor_raw = raw.get("anchor") or raw.get("source_anchor")
        if isinstance(anchor_raw, Mapping):
            anchor = dict(anchor_raw)
        elif anchor_raw:
            anchor = {"locator": _as_text(anchor_raw)}
        else:
            anchor = {}
        anchor.setdefault("doc_id", doc_side)
        anchor.setdefault("version", clause_version)
        anchor.setdefault("section", section)
        anchor.setdefault("number", _as_text(number_raw) if number_raw is not None else (number or {}).get("raw", ""))
        anchor.setdefault("attachment", attachment)
        anchor.setdefault("ordinal", ordinal)
        anchor_label = anchor.get("locator") or anchor.get("label") or " / ".join(
            part for part in (clause_version, section, anchor.get("number") or heading) if part
        )

        if explicit_id:
            base_id = explicit_id
        else:
            base_id = "c-" + stable_hash(
                doc_side, clause_version, section, attachment,
                (number or {}).get("raw", ""), normalize_heading(heading), text_norm,
            )
        count = seen.get(base_id, 0) + 1
        seen[base_id] = count
        clause_id = base_id if count == 1 else "%s#%d" % (base_id, count)
        key = "%s::%s" % (clause_version, clause_id)

        clause = {
            "clause_id": clause_id,
            "clause_key": key,
            "doc_id": doc_side,
            "version": clause_version,
            "year": pick(raw, YEAR_KEYS, year),
            "number": _as_text(number_raw) if number_raw is not None else (number or {}).get("raw", ""),
            "number_value": (number or {}).get("value"),
            "number_kind": (number or {}).get("kind", ""),
            "heading": heading,
            "section": section,
            "attachment": attachment,
            "ordinal": ordinal,
            "anchor": anchor,
            "anchor_label": anchor_label,
            "text_sha256": sha256_text(text),
            "text_len": len(text),
            "text_norm": text_norm,
            "text_grams": bigrams(text_norm),
            "heading_norm": canonical_heading(heading),
        }
        clauses.append(clause)

    return {
        "doc_id": doc_id,
        "version": version,
        "year": year,
        "clauses": clauses,
        "_normalized": True,
    }


def is_normalized(doc: Mapping[str, Any]) -> bool:
    return bool(doc.get("_normalized")) and isinstance(doc.get("clauses"), list)

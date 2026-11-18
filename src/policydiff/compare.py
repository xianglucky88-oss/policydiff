"""跨版本条款差异对比（compare）。

本模块把 :mod:`policydiff.align` 的结构对应关系翻译成“新增 / 删除 / 修改 / 拆分 /
合并 / 待人工”结论。**新增与删除只来自对齐结果，不来自相似度阈值**：

* 自动接受（``accepted``）或人工确认（``confirmed``）的一对一关系 → ``unchanged``
  / ``modified``（比较条款正文哈希，正文仍来自上游解析输出）。
* 一对多 → ``split``；多对一 → ``merge``。拆分子条款、合并父条款都不会被误判成
  “新增/删除”。
* 待人工（``pending``）关系 → ``pending_review``；孤立的高于门槛候选 →
  ``pending_candidate``。两者都不进入新增/删除计数，附带候选与锚点等待处理。
* 人工拒绝（``rejected``）及未匹配条款 → ``removed`` / ``added``。
* 与人工决定冲突的新候选（含被拒绝关系再次出现）→ ``conflict``，单独列出待复核。
* 被新解析取代的旧自动组（``superseded``）不参与对比，但仍保存在库中可审计。

结论按条款锚点稳定排序；同一对齐状态重复运行时产出相同 run_id 与变更列表
（幂等），并保留历史 run 供审计。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Sequence

from . import _align_store as db
from . import _align_text as tx
from . import align as _align
from . import store as _base_store

STATUSES_UNRESOLVED = frozenset({db.STATUS_PENDING})

# Changes that are "not yet decided" — they must never be counted as added/removed.
REVIEW_TYPES = frozenset({"pending_review", "pending_candidate", "conflict",
                          "ambiguous", "candidate"})


def _ensure(con: sqlite3.Connection | None) -> tuple[sqlite3.Connection, bool]:
    if con is None:
        con = _base_store.connect()
        db.ensure_schema(con)
        return con, True
    db.ensure_schema(con)
    return con, False


def _norm(obj: Any, doc_id: str | None, version: str | None) -> dict[str, Any]:
    if isinstance(obj, Mapping) and tx.is_normalized(obj):
        return dict(obj)
    doc = tx.split_version_doc(obj, default_doc_id=doc_id, default_version=version)
    if not doc.get("doc_id"):
        doc["doc_id"] = doc_id or "doc"
    if not doc.get("version"):
        doc["version"] = version or ""
    return tx.normalize_clauses(doc)


def _natural_key(value: str) -> tuple[Any, ...]:
    import re
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", str(value)))


def _ordered_pair(a, b) -> tuple[dict, dict]:
    ka = (_natural_key(a["version"]), a["doc_id"])
    kb = (_natural_key(b["version"]), b["doc_id"])
    return (a, b) if ka <= kb else (b, a)


def _ref(clause: Mapping[str, Any] | None, key: str) -> dict[str, Any]:
    if clause is None:
        return {"clause_key": key, "clause_id": key, "anchor": {}, "text_sha256": "",
                "heading": "", "number": "", "section": ""}
    return {
        "clause_key": clause["clause_key"],
        "clause_id": clause["clause_id"],
        "version": clause.get("version", ""),
        "heading": clause.get("heading", ""),
        "number": clause.get("number", ""),
        "section": clause.get("section", ""),
        "attachment": clause.get("attachment", ""),
        "anchor": clause.get("anchor", {}),
        "anchor_label": clause.get("anchor_label", ""),
        "text_sha256": clause.get("text_sha256", ""),
        "text_len": clause.get("text_len", 0),
    }


def _index(doc: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {c["clause_key"]: c for c in doc["clauses"]}


def applicable_groups(groups, low_index, high_index):
    """Keep only relations whose every member still exists in the current parse.

    A version can be re-parsed between runs; groups written for the older parse
    stay in storage for audit, but they must not drive this comparison.  Manual
    decisions are likewise not applied to clauses that no longer exist — that
    surfaces as a removal/addition instead of a misleading mapping.
    """
    kept = []
    for group in groups:
        low_ok = all(m["clause_key"] in low_index
                     for m in group["members"] if m["side"] == "low")
        high_ok = all(m["clause_key"] in high_index
                      for m in group["members"] if m["side"] == "high")
        if low_ok and high_ok and group["members"]:
            kept.append(group)
    return kept


def compare_versions(low: Any, high: Any, *, con: sqlite3.Connection | None = None,
                     doc_id: str | None = None, low_version: str | None = None,
                     high_version: str | None = None,
                     include_review: bool = True,
                     auto_align: bool = True) -> dict[str, Any]:
    """Compare two versions using persisted alignment relations.

    ``include_review=False`` omits undecided (pending/conflict) relations from
    ``changes``; they are still counted in ``review_count`` so a caller can
    assert nothing changed silently.
    """
    con, own = _ensure(con)
    try:
        doc_low = _norm(low, doc_id, low_version)
        doc_high = _norm(high, doc_id, high_version)
        doc_low, doc_high = _ordered_pair(doc_low, doc_high)
        low_index, high_index = _index(doc_low), _index(doc_high)

        groups = db.list_groups(con, doc_id=doc_low["doc_id"],
                                low_version=doc_low["version"],
                                high_version=doc_high["version"])
        # Re-align when there is no stored mapping, or when either version was
        # re-parsed with different content since the mapping was written.
        if auto_align and (not groups or _align.pair_is_stale(con, doc_low, doc_high)):
            _align.align_versions(doc_low, doc_high, con=con)
            groups = db.list_groups(con, doc_id=doc_low["doc_id"],
                                    low_version=doc_low["version"],
                                    high_version=doc_high["version"])
        candidates = db.list_candidates(con, doc_id=doc_low["doc_id"],
                                         low_version=doc_low["version"],
                                         high_version=doc_high["version"])
        groups = applicable_groups(groups, low_index, high_index)

        changes: list[dict[str, Any]] = []
        consumed_low: set[str] = set()
        consumed_high: set[str] = set()
        group_ids: set[str] = set()

        def add_change(change_type: str, low_keys: Sequence[str], high_keys: Sequence[str],
                       group: Mapping[str, Any] | None = None, **detail: Any) -> None:
            item = {
                "type": change_type,
                "from_keys": list(low_keys),
                "to_keys": list(high_keys),
                "group_id": group["group_id"] if group else None,
                "kind": group["kind"] if group else None,
                "status": group["status"] if group else None,
                "score": float(group["score"]) if group else None,
                "from": [_ref(low_index.get(k), k) for k in low_keys],
                "to": [_ref(high_index.get(k), k) for k in high_keys],
            }
            item.update(detail)
            changes.append(item)

        for group in groups:
            low_keys = [m["clause_key"] for m in group["members"] if m["side"] == "low"]
            high_keys = [m["clause_key"] for m in group["members"] if m["side"] == "high"]
            status = db.LEGACY_STATUS_MAP.get(group["status"], group["status"])
            group_ids.add(group["group_id"])

            if status in STATUSES_UNRESOLVED:
                consumed_low.update(low_keys)
                consumed_high.update(high_keys)
                if include_review:
                    add_change("pending_review", low_keys, high_keys, group,
                               needs_review=True,
                               review_reason="low_confidence" if group["kind"] == "one_to_one"
                               else "structural_hypothesis")
                continue

            if status == db.STATUS_REJECTED:
                # A manual rejection dissolves the pair: remain unmatched.
                continue

            consumed_low.update(low_keys)
            consumed_high.update(high_keys)
            if group["kind"] == "one_to_one" and len(low_keys) == 1 and len(high_keys) == 1:
                before = low_index.get(low_keys[0])
                after = high_index.get(high_keys[0])
                if before and after and before["text_sha256"] == after["text_sha256"]:
                    add_change("unchanged", low_keys, high_keys, group)
                else:
                    add_change(
                        "modified", low_keys, high_keys, group,
                        changes={
                            "text": True,
                            "heading": (before or {}).get("heading", "") !=
                                       (after or {}).get("heading", ""),
                            "number": (before or {}).get("number", "") !=
                                      (after or {}).get("number", ""),
                            "section": (before or {}).get("section", "") !=
                                       (after or {}).get("section", ""),
                        },
                    )
            elif group["kind"] == "split":
                add_change("split", low_keys, high_keys, group,
                           changes={"parts": len(high_keys)})
            elif group["kind"] == "merge":
                add_change("merge", low_keys, high_keys, group,
                           changes={"parts": len(low_keys)})
            else:
                add_change("ambiguous", low_keys, high_keys, group, needs_review=True)

        # Standalone low-confidence candidates: a plausible correspondence exists
        # but was neither auto-accepted nor confirmed.  These clauses must not be
        # asserted as added/removed; they go to review with their candidates.
        standalone = [c for c in candidates
                      if c["status"] == "pending"
                      and (c["group_id"] is None or c["group_id"] not in group_ids)]
        for candidate in standalone:
            consumed_low.update(candidate["low_keys"])
            consumed_high.update(candidate["high_keys"])
            if not include_review:
                continue
            add_change("pending_candidate", candidate["low_keys"], candidate["high_keys"],
                       None, needs_review=True, review_reason="low_confidence",
                       candidate_id=candidate["candidate_id"], score=candidate["score"],
                       kind=candidate["kind"])

        for key in low_index:
            if key not in consumed_low:
                add_change("removed", [key], [])
        for key in high_index:
            if key not in consumed_high:
                add_change("added", [], [key])

        for candidate in candidates:
            if candidate["status"] == "conflict":
                add_change("conflict", candidate["low_keys"], candidate["high_keys"],
                           None, needs_review=True,
                           conflicts_with=candidate["detail"].get("conflicts_with", []),
                           candidate_id=candidate["candidate_id"])
            elif (candidate["detail"].get("resurfaced")
                  and candidate["status"] == "rejected"
                  and candidate["group_id"] in group_ids):
                # An automatic pass re-proposed a relation a human rejected.
                # It stays rejected, but the review queue must surface it.
                add_change("conflict", candidate["low_keys"], candidate["high_keys"],
                           None, needs_review=True,
                           conflicts_with=[candidate["group_id"]],
                           candidate_id=candidate["candidate_id"],
                           review_reason="manual_rejection_resurfaced")

        changes.sort(key=lambda c: (
            min((r.get("anchor", {}).get("ordinal", 10 ** 6) for r in c["from"]), default=10 ** 6),
            min((r.get("anchor", {}).get("ordinal", 10 ** 6) for r in c["to"]), default=10 ** 6),
            c["type"], c["from_keys"], c["to_keys"],
        ))

        summary = {t: 0 for t in ("unchanged", "modified", "added", "removed", "split",
                                  "merge", "pending_review", "pending_candidate", "conflict",
                                  "candidate", "ambiguous")}
        for change in changes:
            summary[change["type"]] = summary.get(change["type"], 0) + 1
        decided = {t: v for t, v in summary.items() if t not in REVIEW_TYPES}
        review = sum(v for t, v in summary.items() if t in REVIEW_TYPES)

        align_revision = tx.stable_hash(
            *[(g["group_id"], g["status"], g["revision"]) for g in groups],
            *[(c["candidate_id"], c["status"]) for c in candidates],
            length=20,
        )
        run_id = "run-" + tx.stable_hash(
            doc_low["doc_id"], doc_low["version"], doc_high["version"],
            align_revision, length=20,
        )
        result = {
            "run_id": run_id,
            "doc_id": doc_low["doc_id"],
            "low_version": doc_low["version"],
            "high_version": doc_high["version"],
            "from_version": doc_low["version"],
            "to_version": doc_high["version"],
            "align_revision": align_revision,
            "summary": summary,
            "decided_summary": decided,
            "review_count": review,
            "changes": changes,
        }
        _persist_run(con, result)
        con.commit()
        return result
    finally:
        if own:
            con.close()


def _persist_run(con: sqlite3.Connection, result: Mapping[str, Any]) -> None:
    summary = {
        "summary": result["summary"],
        "decided_summary": result["decided_summary"],
        "review_count": result["review_count"],
    }
    now = db.utcnow()
    con.execute(
        "INSERT INTO compare_runs(run_id,doc_id,from_version,to_version,align_revision,"
        "summary_json,created_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(run_id) DO UPDATE SET summary_json=excluded.summary_json",
        (result["run_id"], result["doc_id"], result["from_version"], result["to_version"],
         result["align_revision"], json.dumps(summary, ensure_ascii=False, sort_keys=True), now),
    )
    # Replace the change list so the run row and its changes always agree.
    con.execute("DELETE FROM compare_changes WHERE run_id=?", (result["run_id"],))
    for seq, change in enumerate(result["changes"]):
        con.execute(
            "INSERT INTO compare_changes(run_id,change_seq,change_type,from_keys,to_keys,"
            "group_id,detail_json) VALUES(?,?,?,?,?,?,?)",
            (result["run_id"], seq, change["type"],
             json.dumps(change["from_keys"], ensure_ascii=False),
             json.dumps(change["to_keys"], ensure_ascii=False),
             change.get("group_id"), json.dumps(change, ensure_ascii=False, sort_keys=True)),
        )


def run_history(con: sqlite3.Connection | None = None, *,
                doc_id: str | None = None) -> list[dict[str, Any]]:
    con, own = _ensure(con)
    try:
        sql = "SELECT * FROM compare_runs"
        params: list[Any] = []
        if doc_id is not None:
            sql += " WHERE doc_id=?"
            params.append(doc_id)
        sql += " ORDER BY created_at, run_id"
        cur = con.execute(sql, params)
        columns = [d[0] for d in cur.description]
        rows = []
        for row in cur.fetchall():
            item = dict(zip(columns, row))
            item["summary"] = json.loads(item.pop("summary_json"))
            rows.append(item)
        return rows
    finally:
        if own:
            con.close()


def get_run(run_id: str, con: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    con, own = _ensure(con)
    try:
        row = con.execute("SELECT * FROM compare_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        cur = con.execute("SELECT * FROM compare_changes WHERE run_id=? ORDER BY change_seq",
                          (run_id,))
        columns = [d[0] for d in cur.description]
        changes = []
        for change_row in cur.fetchall():
            item = dict(zip(columns, change_row))
            item["detail"] = json.loads(item.pop("detail_json"))
            changes.append(item)
        return {"run_id": run_id, "summary": json.loads(row[5]), "changes": changes}
    finally:
        if own:
            con.close()


def process(record: Mapping[str, Any], con: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Pipeline stage entry: compare when a payload is present, else pass through."""
    rec = dict(record)
    payload = rec.get("compare")
    if payload is None and "low" in rec and "high" in rec:
        payload = {"low": rec["low"], "high": rec["high"]}
    if payload is None:
        rec.setdefault("_stage_compare", "ok")
        return rec
    rec["_compare"] = compare_versions(
        payload.get("low", payload.get("a")),
        payload.get("high", payload.get("b")),
        con=con, doc_id=payload.get("doc_id", rec.get("doc_id")),
        include_review=payload.get("include_review", True),
    )
    rec["_stage_compare"] = "ok"
    return rec

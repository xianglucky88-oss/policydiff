"""Persistence layer for cross-version clause alignment.

This module owns the SQLite schema and the small, auditable operations used by
:mod:`policydiff.align`:

* ``align_versions`` / ``align_clause_index`` — the parsed clause references
  (anchors and content hashes only; clause bodies stay in the upstream parsed
  output).
* ``align_groups`` / ``align_members`` — accepted, pending and manually decided
  correspondence relations.  Members carry source anchors so a mapping can be
  audited without re-reading the source document.
* ``align_candidates`` — every observed hypothesis (including low-confidence
  ones and candidates that conflict with a manual decision).  Candidates never
  overwrite groups on their own.
* ``align_decisions`` / ``align_journal`` — append-only decision history plus a
  two-phase write journal so a crash during confirmation can be recovered.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from ._align_text import stable_hash

SCHEMA_VERSION = 2

STATUS_AUTO = "accepted"
STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
# Written by an earlier automatic pass and not reproduced by the latest one.
# Kept for audit; compare/version ignore it.  Never assigned to manual rows.
STATUS_SUPERSEDED = "superseded"

MANUAL_STATUSES = frozenset({STATUS_CONFIRMED, STATUS_REJECTED})
ACTIVE_STATUSES = frozenset({STATUS_AUTO, STATUS_PENDING, STATUS_CONFIRMED, STATUS_REJECTED})
KNOWN_STATUSES = frozenset({STATUS_AUTO, STATUS_PENDING, STATUS_CONFIRMED,
                            STATUS_REJECTED, STATUS_SUPERSEDED})

LEGACY_STATUS_MAP = {
    "auto": STATUS_AUTO,
    "automatic": STATUS_AUTO,
    "review": STATUS_PENDING,
    "needs_review": STATUS_PENDING,
    "manual": STATUS_CONFIRMED,
    "reject": STATUS_REJECTED,
    "manual_reject": STATUS_REJECTED,
    "unmatched": STATUS_REJECTED,
}

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS align_versions(
        doc_id TEXT NOT NULL,
        version TEXT NOT NULL,
        year INTEGER,
        label TEXT NOT NULL,
        clause_count INTEGER NOT NULL,
        doc_hash TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        PRIMARY KEY(doc_id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS align_clause_index(
        doc_id TEXT NOT NULL,
        version TEXT NOT NULL,
        clause_key TEXT NOT NULL,
        clause_id TEXT NOT NULL,
        anchor_json TEXT NOT NULL,
        text_sha256 TEXT NOT NULL,
        heading TEXT NOT NULL,
        number TEXT NOT NULL,
        section TEXT NOT NULL,
        attachment TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        PRIMARY KEY(doc_id, version, clause_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS align_groups(
        group_id TEXT PRIMARY KEY,
        doc_id TEXT NOT NULL,
        peer_doc_id TEXT NOT NULL,
        low_version TEXT NOT NULL,
        high_version TEXT NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        score REAL NOT NULL,
        coverage REAL NOT NULL,
        manual INTEGER NOT NULL DEFAULT 0,
        decision_actor TEXT,
        decision_note TEXT,
        decision_at TEXT,
        revision INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS align_members(
        group_id TEXT NOT NULL,
        side TEXT NOT NULL,
        clause_key TEXT NOT NULL,
        clause_id TEXT NOT NULL,
        version TEXT NOT NULL,
        doc_id TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT '',
        ordinal INTEGER NOT NULL,
        anchor_json TEXT NOT NULL,
        text_sha256 TEXT NOT NULL,
        PRIMARY KEY(group_id, side, clause_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS align_candidates(
        candidate_id TEXT PRIMARY KEY,
        doc_id TEXT NOT NULL,
        peer_doc_id TEXT NOT NULL,
        low_version TEXT NOT NULL,
        high_version TEXT NOT NULL,
        kind TEXT NOT NULL,
        score REAL NOT NULL,
        coverage REAL NOT NULL,
        status TEXT NOT NULL,
        group_id TEXT,
        low_keys TEXT NOT NULL,
        high_keys TEXT NOT NULL,
        detail_json TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        runs_seen INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS align_decisions(
        decision_id TEXT PRIMARY KEY,
        group_id TEXT NOT NULL,
        action TEXT NOT NULL,
        actor TEXT NOT NULL,
        note TEXT,
        previous_status TEXT NOT NULL,
        new_status TEXT NOT NULL,
        op_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS align_journal(
        op_id TEXT PRIMARY KEY,
        group_id TEXT NOT NULL,
        action TEXT NOT NULL,
        actor TEXT NOT NULL,
        note TEXT,
        payload_json TEXT NOT NULL,
        state TEXT NOT NULL,
        result_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS align_schema_meta(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS compare_runs(
        run_id TEXT PRIMARY KEY,
        doc_id TEXT NOT NULL,
        from_version TEXT NOT NULL,
        to_version TEXT NOT NULL,
        align_revision TEXT NOT NULL,
        summary_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS compare_changes(
        run_id TEXT NOT NULL,
        change_seq INTEGER NOT NULL,
        change_type TEXT NOT NULL,
        from_keys TEXT NOT NULL,
        to_keys TEXT NOT NULL,
        group_id TEXT,
        detail_json TEXT NOT NULL,
        PRIMARY KEY(run_id, change_seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS version_runs(
        run_id TEXT PRIMARY KEY,
        doc_id TEXT NOT NULL,
        chain_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)

_EXPECTED_COLUMNS = {
    "align_versions": {
        "year": "INTEGER", "label": "TEXT NOT NULL DEFAULT ''",
        "clause_count": "INTEGER NOT NULL DEFAULT 0",
        "doc_hash": "TEXT NOT NULL DEFAULT ''",
        "first_seen": "TEXT NOT NULL DEFAULT ''",
        "last_seen": "TEXT NOT NULL DEFAULT ''",
    },
    "align_groups": {
        "peer_doc_id": "TEXT NOT NULL DEFAULT ''",
        "kind": "TEXT NOT NULL DEFAULT 'one_to_one'",
        "status": "TEXT NOT NULL DEFAULT 'pending'",
        "score": "REAL NOT NULL DEFAULT 0",
        "coverage": "REAL NOT NULL DEFAULT 0",
        "manual": "INTEGER NOT NULL DEFAULT 0",
        "decision_actor": "TEXT", "decision_note": "TEXT", "decision_at": "TEXT",
        "revision": "INTEGER NOT NULL DEFAULT 0",
        "created_at": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    },
    "align_members": {
        "doc_id": "TEXT NOT NULL DEFAULT ''",
        "role": "TEXT NOT NULL DEFAULT ''",
        "ordinal": "INTEGER NOT NULL DEFAULT 0",
        "anchor_json": "TEXT NOT NULL DEFAULT '{}'",
        "text_sha256": "TEXT NOT NULL DEFAULT ''",
    },
    "align_candidates": {
        "status": "TEXT NOT NULL DEFAULT 'pending'",
        "score": "REAL NOT NULL DEFAULT 0",
        "coverage": "REAL NOT NULL DEFAULT 0",
        "group_id": "TEXT",
        "low_keys": "TEXT NOT NULL DEFAULT '[]'",
        "high_keys": "TEXT NOT NULL DEFAULT '[]'",
        "detail_json": "TEXT NOT NULL DEFAULT '{}'",
        "first_seen": "TEXT NOT NULL DEFAULT ''",
        "last_seen": "TEXT NOT NULL DEFAULT ''",
        "runs_seen": "INTEGER NOT NULL DEFAULT 1",
    },
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create or migrate the alignment schema.

    Safe to call repeatedly.  Existing rows keep their identity; legacy status
    words are translated in place so older alignment results remain readable.
    """
    for statement in _SCHEMA:
        con.execute(statement)
    for table, columns in _EXPECTED_COLUMNS.items():
        existing = {row[1] for row in con.execute("PRAGMA table_info(%s)" % table)}
        for column, ddl in columns.items():
            if column not in existing:
                con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, ddl))
    for legacy, current in LEGACY_STATUS_MAP.items():
        con.execute("UPDATE align_groups SET status=? WHERE status=?", (current, legacy))
        con.execute("UPDATE align_candidates SET status=? WHERE status=?", (current, legacy))
    con.execute(
        "UPDATE align_groups SET manual=1 WHERE status IN (?,?)",
        (STATUS_CONFIRMED, STATUS_REJECTED),
    )
    row = con.execute("SELECT value FROM align_schema_meta WHERE key='schema_version'").fetchone()
    version = int(row[0]) if row and str(row[0]).isdigit() else 0
    if version < SCHEMA_VERSION:
        backfill_legacy_members(con)
        con.execute(
            "INSERT INTO align_schema_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
    con.commit()


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def _row_dict(row: sqlite3.Row | Sequence[Any], columns: Sequence[str]) -> dict[str, Any]:
    return {name: row[index] for index, name in enumerate(columns)}


def _select(con: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    cur = con.execute(sql, params)
    columns = [d[0] for d in cur.description] if cur.description else []
    return [_row_dict(row, columns) for row in cur.fetchall()]


def get_group(con: sqlite3.Connection, group_id: str, *,
              with_members: bool = False) -> dict[str, Any] | None:
    rows = _select(con, "SELECT * FROM align_groups WHERE group_id=?", (group_id,))
    if not rows:
        return None
    group = rows[0]
    if with_members:
        group["members"] = group_members(con, group_id)
    return group


def group_is_active(group: Mapping[str, Any]) -> bool:
    """True unless a group was superseded by a newer automatic pass."""
    return group.get("status") != STATUS_SUPERSEDED


def group_members(con: sqlite3.Connection, group_id: str) -> list[dict[str, Any]]:
    rows = _select(
        con,
        "SELECT * FROM align_members WHERE group_id=? ORDER BY side, ordinal, clause_key",
        (group_id,),
    )
    for row in rows:
        row["anchor"] = json.loads(row.pop("anchor_json") or "{}")
    return rows


def list_groups(con: sqlite3.Connection, doc_id: str | None = None,
                low_version: str | None = None, high_version: str | None = None,
                statuses: Iterable[str] | None = None,
                include_superseded: bool = False) -> list[dict[str, Any]]:
    clauses = ["SELECT * FROM align_groups WHERE 1=1"]
    params: list[Any] = []
    if not include_superseded:
        clauses.append("AND status<>?")
        params.append(STATUS_SUPERSEDED)
    if doc_id is not None:
        clauses.append("AND (doc_id=? OR peer_doc_id=?)")
        params.extend([doc_id, doc_id])
    if low_version is not None:
        clauses.append("AND low_version=?")
        params.append(low_version)
    if high_version is not None:
        clauses.append("AND high_version=?")
        params.append(high_version)
    statuses = list(statuses) if statuses is not None else None
    if statuses:
        clauses.append("AND status IN (%s)" % ",".join("?" for _ in statuses))
        params.extend(statuses)
    clauses.append("ORDER BY low_version, high_version, created_at, group_id")
    groups = _select(con, " ".join(clauses), params)
    for group in groups:
        group["members"] = group_members(con, group["group_id"])
    return groups


def list_candidates(con: sqlite3.Connection, doc_id: str | None = None,
                    low_version: str | None = None, high_version: str | None = None,
                    statuses: Iterable[str] | None = None,
                    include_superseded: bool = False) -> list[dict[str, Any]]:
    clauses = ["SELECT * FROM align_candidates WHERE 1=1"]
    params: list[Any] = []
    if not include_superseded:
        clauses.append("AND status<>?")
        params.append(STATUS_SUPERSEDED)
    if doc_id is not None:
        clauses.append("AND (doc_id=? OR peer_doc_id=?)")
        params.extend([doc_id, doc_id])
    if low_version is not None:
        clauses.append("AND low_version=?")
        params.append(low_version)
    if high_version is not None:
        clauses.append("AND high_version=?")
        params.append(high_version)
    statuses = list(statuses) if statuses is not None else None
    if statuses:
        clauses.append("AND status IN (%s)" % ",".join("?" for _ in statuses))
        params.extend(statuses)
    clauses.append("ORDER BY score DESC, candidate_id")
    rows = _select(con, " ".join(clauses), params)
    for row in rows:
        row["low_keys"] = json.loads(row["low_keys"])
        row["high_keys"] = json.loads(row["high_keys"])
        row["detail"] = json.loads(row.pop("detail_json") or "{}")
    return rows


def list_decisions(con: sqlite3.Connection, group_id: str | None = None) -> list[dict[str, Any]]:
    if group_id is None:
        return _select(con, "SELECT * FROM align_decisions ORDER BY created_at, decision_id")
    return _select(
        con, "SELECT * FROM align_decisions WHERE group_id=? ORDER BY created_at, decision_id",
        (group_id,),
    )


def list_versions(con: sqlite3.Connection, doc_id: str | None = None) -> list[dict[str, Any]]:
    if doc_id is None:
        return _select(con, "SELECT * FROM align_versions ORDER BY doc_id, version")
    return _select(con, "SELECT * FROM align_versions WHERE doc_id=? ORDER BY version", (doc_id,))


def clause_ref(con: sqlite3.Connection, doc_id: str, version: str,
               clause_key: str) -> dict[str, Any] | None:
    rows = _select(
        con,
        "SELECT * FROM align_clause_index WHERE doc_id=? AND version=? AND clause_key=?",
        (doc_id, version, clause_key),
    )
    if not rows:
        return None
    row = rows[0]
    row["anchor"] = json.loads(row.pop("anchor_json") or "{}")
    return row


# ---------------------------------------------------------------------------
# Version / clause index writes
# ---------------------------------------------------------------------------

def upsert_version(con: sqlite3.Connection, doc: Mapping[str, Any], doc_hash: str,
                   now: str | None = None) -> None:
    now = now or utcnow()
    con.execute(
        "INSERT INTO align_versions(doc_id,version,year,label,clause_count,doc_hash,"
        "first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(doc_id,version) DO UPDATE SET year=excluded.year,label=excluded.label,"
        "clause_count=excluded.clause_count,doc_hash=excluded.doc_hash,last_seen=excluded.last_seen",
        (doc["doc_id"], doc["version"], doc.get("year"), doc["version"], len(doc["clauses"]),
         doc_hash, now, now),
    )


def upsert_clause_refs(con: sqlite3.Connection, doc: Mapping[str, Any]) -> None:
    for clause in doc["clauses"]:
        con.execute(
            "INSERT INTO align_clause_index(doc_id,version,clause_key,clause_id,anchor_json,"
            "text_sha256,heading,number,section,attachment,ordinal) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(doc_id,version,clause_key) DO UPDATE SET clause_id=excluded.clause_id,"
            "anchor_json=excluded.anchor_json,text_sha256=excluded.text_sha256,"
            "heading=excluded.heading,number=excluded.number,section=excluded.section,"
            "attachment=excluded.attachment,ordinal=excluded.ordinal",
            (clause["doc_id"], clause["version"], clause["clause_key"], clause["clause_id"],
             _json(clause["anchor"]), clause["text_sha256"], clause["heading"],
             clause["number"], clause["section"], clause["attachment"], clause["ordinal"]),
        )


# ---------------------------------------------------------------------------
# Group / candidate writes
# ---------------------------------------------------------------------------

def group_id_for(doc_id: str, peer_doc_id: str, low_version: str, high_version: str,
                 low_keys: Iterable[str], high_keys: Iterable[str]) -> str:
    return "g-" + stable_hash(
        doc_id, peer_doc_id, low_version, high_version,
        ",".join(sorted(low_keys)), ",".join(sorted(high_keys)),
        length=20,
    )


def candidate_id_for(doc_id: str, peer_doc_id: str, low_version: str, high_version: str,
                     kind: str, low_keys: Iterable[str], high_keys: Iterable[str]) -> str:
    return "c-" + stable_hash(
        doc_id, peer_doc_id, low_version, high_version, kind,
        ",".join(sorted(low_keys)), ",".join(sorted(high_keys)),
        length=20,
    )


def write_group(con: sqlite3.Connection, *, group_id: str, doc_id: str, peer_doc_id: str,
                low_version: str, high_version: str, kind: str, status: str, score: float,
                coverage: float, members: Sequence[Mapping[str, Any]], now: str) -> None:
    existing = get_group(con, group_id)
    if existing and existing.get("manual"):
        # Never let an automatic run rewrite a human decision.
        con.execute(
            "UPDATE align_groups SET score=?,coverage=?,updated_at=? WHERE group_id=?",
            (score, coverage, now, group_id),
        )
        return
    con.execute(
        "INSERT INTO align_groups(group_id,doc_id,peer_doc_id,low_version,high_version,kind,"
        "status,score,coverage,manual,revision,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,0,0,?,?) "
        "ON CONFLICT(group_id) DO UPDATE SET kind=excluded.kind,status=excluded.status,"
        "score=excluded.score,coverage=excluded.coverage,updated_at=excluded.updated_at",
        (group_id, doc_id, peer_doc_id, low_version, high_version, kind, status,
         score, coverage, now, now),
    )
    con.execute("DELETE FROM align_members WHERE group_id=?", (group_id,))
    for member in members:
        con.execute(
            "INSERT OR REPLACE INTO align_members(group_id,side,clause_key,clause_id,version,"
            "doc_id,role,ordinal,anchor_json,text_sha256) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (group_id, member["side"], member["clause_key"], member["clause_id"],
             member["version"], member["doc_id"], member.get("role", ""),
             member.get("ordinal", 0), _json(member.get("anchor", {})),
             member.get("text_sha256", "")),
        )


def mark_group_superseded(con: sqlite3.Connection, group_id: str, now: str) -> None:
    """Flag an unreproduced automatic group as superseded (manual rows excluded)."""
    con.execute(
        "UPDATE align_groups SET status=?,updated_at=? "
        "WHERE group_id=? AND manual=0 AND status NOT IN (?,?)",
        (STATUS_SUPERSEDED, now, group_id, STATUS_CONFIRMED, STATUS_REJECTED),
    )
    con.execute(
        "UPDATE align_candidates SET status=? WHERE group_id=? AND status=?",
        (STATUS_SUPERSEDED, group_id, STATUS_AUTO),
    )


def write_candidate(con: sqlite3.Connection, *, candidate_id: str, doc_id: str,
                    peer_doc_id: str, low_version: str, high_version: str, kind: str,
                    score: float, coverage: float, status: str, group_id: str | None,
                    low_keys: Sequence[str], high_keys: Sequence[str],
                    detail: Mapping[str, Any], now: str) -> None:
    existing = con.execute(
        "SELECT status FROM align_candidates WHERE candidate_id=?", (candidate_id,)
    ).fetchone()
    # Preserve externally decided/conflict statuses; refresh observational data.
    preserve = bool(existing and existing[0] in ("confirmed", "rejected", "conflict"))
    con.execute(
        "INSERT INTO align_candidates(candidate_id,doc_id,peer_doc_id,low_version,high_version,"
        "kind,score,coverage,status,group_id,low_keys,high_keys,detail_json,first_seen,"
        "last_seen,runs_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1) "
        "ON CONFLICT(candidate_id) DO UPDATE SET score=excluded.score,"
        "coverage=excluded.coverage,status=CASE WHEN ? THEN align_candidates.status "
        "ELSE excluded.status END,group_id=CASE WHEN ? THEN align_candidates.group_id "
        "ELSE excluded.group_id END,detail_json=excluded.detail_json,"
        "last_seen=excluded.last_seen,runs_seen=align_candidates.runs_seen+1",
        (candidate_id, doc_id, peer_doc_id, low_version, high_version, kind, score,
         coverage, status, group_id, _json(list(low_keys)), _json(list(high_keys)),
         _json(detail), now, now, int(preserve), int(preserve)),
    )


def set_candidate_status_for_group(con: sqlite3.Connection, group_id: str,
                                   status: str, now: str) -> None:
    con.execute(
        "UPDATE align_candidates SET status=?,last_seen=? WHERE group_id=?", (status, now, group_id)
    )


def mark_candidate_rejected_again(con: sqlite3.Connection, candidate_id: str,
                                  detail: Mapping[str, Any], now: str) -> None:
    """Record that an auto run re-proposed an already rejected relation.

    The candidate stays ``rejected`` (the human decision wins); only the
    observational data and ``runs_seen`` move, plus a ``resurfaced`` flag.
    """
    con.execute(
        "INSERT INTO align_candidates(candidate_id,doc_id,peer_doc_id,low_version,high_version,"
        "kind,score,coverage,status,group_id,low_keys,high_keys,detail_json,first_seen,"
        "last_seen,runs_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0) "
        "ON CONFLICT(candidate_id) DO UPDATE SET score=excluded.score,"
        "coverage=excluded.coverage,status='rejected',detail_json=excluded.detail_json,"
        "last_seen=excluded.last_seen,runs_seen=align_candidates.runs_seen+1",
        (candidate_id, detail["doc_id"], detail.get("peer_doc_id", ""),
         detail["low_version"], detail["high_version"], detail["kind"],
         float(detail.get("score", 0.0)), float(detail.get("coverage", 0.0)),
         "rejected", detail.get("group_id"),
         _json(list(detail.get("low_keys", []))), _json(list(detail.get("high_keys", []))),
         _json(dict(detail)), now, now),
    )


def mark_candidate_conflict(con: sqlite3.Connection, candidate_id: str, detail: Mapping[str, Any],
                            now: str) -> None:
    """Flag a candidate as conflicting with an existing decision.

    A candidate that already records a *manual* outcome is never downgraded:
    the conflict is appended to its detail, but the human decision stays
    visible as such.
    """
    existing = con.execute(
        "SELECT c.status, c.group_id, g.manual FROM align_candidates c "
        "LEFT JOIN align_groups g ON g.group_id=c.group_id WHERE c.candidate_id=?",
        (candidate_id,),
    ).fetchone()
    preserve = bool(existing and existing[2] and existing[0] in (STATUS_CONFIRMED,
                                                                STATUS_REJECTED))
    con.execute(
        "INSERT INTO align_candidates(candidate_id,doc_id,peer_doc_id,low_version,high_version,"
        "kind,score,coverage,status,group_id,low_keys,high_keys,detail_json,first_seen,"
        "last_seen,runs_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0) "
        "ON CONFLICT(candidate_id) DO UPDATE SET detail_json=excluded.detail_json,"
        "status=CASE WHEN ? THEN align_candidates.status ELSE 'conflict' END,"
        "last_seen=excluded.last_seen,runs_seen=align_candidates.runs_seen+1",
        (candidate_id, detail["doc_id"], detail.get("peer_doc_id", ""),
         detail["low_version"], detail["high_version"], detail["kind"],
         float(detail.get("score", 0.0)), float(detail.get("coverage", 0.0)),
         "conflict", detail.get("group_id"),
         _json(list(detail.get("low_keys", []))), _json(list(detail.get("high_keys", []))),
         _json(dict(detail)), now, now, int(preserve)),
    )


# ---------------------------------------------------------------------------
# Two-phase manual decisions
# ---------------------------------------------------------------------------

def begin_decision(con: sqlite3.Connection, group_id: str, action: str, actor: str,
                   note: str | None = None, now: str | None = None) -> str:
    """Phase one: durably journal the intent, then return its operation id."""
    if action not in ("confirm", "reject"):
        raise ValueError("action must be 'confirm' or 'reject'")
    now = now or utcnow()
    group = get_group(con, group_id)
    if not group:
        raise KeyError("unknown alignment group: %s" % group_id)
    op_id = "op-" + stable_hash(group_id, action, actor, now, _json(note), length=20)
    payload = {"group_id": group_id, "action": action, "actor": actor, "note": note,
               "previous_status": group["status"], "requested_at": now}
    con.execute(
        "INSERT INTO align_journal(op_id,group_id,action,actor,note,payload_json,state,"
        "result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,'pending',NULL,?,?) "
        "ON CONFLICT(op_id) DO NOTHING",
        (op_id, group_id, action, actor, note, _json(payload), now, now),
    )
    con.commit()
    return op_id


def apply_decision(con: sqlite3.Connection, op_id: str, *, verify: bool = True) -> dict[str, Any]:
    """Phase two: atomically apply a journalled decision.

    Idempotent: replaying an applied operation returns the stored result.
    """
    journal = _select(con, "SELECT * FROM align_journal WHERE op_id=?", (op_id,))
    if not journal:
        raise KeyError("unknown alignment operation: %s" % op_id)
    row = journal[0]
    if row["state"] == "applied":
        return {"op_id": op_id, "state": "applied",
                "result": json.loads(row["result_json"] or "{}"), "replayed": True}
    if row["state"] == "rolled_back":
        return {"op_id": op_id, "state": "rolled_back",
                "result": json.loads(row["result_json"] or "{}"), "replayed": True}

    payload = json.loads(row["payload_json"])
    group_id = payload["group_id"]
    action = payload["action"]
    now = utcnow()
    decision_id = "d-" + stable_hash(op_id, group_id, action, length=20)

    with con:
        group = get_group(con, group_id)
        if not group:
            result = {"error": "group_missing", "group_id": group_id}
            con.execute(
                "UPDATE align_journal SET state='rolled_back',result_json=?,updated_at=? "
                "WHERE op_id=?", (_json(result), now, op_id),
            )
            return {"op_id": op_id, "state": "rolled_back", "result": result}
        if verify:
            issues = verify_group(con, group_id)
            anchor_issues = [i for i in issues if i.get("kind") == "anchor_mismatch"]
            if anchor_issues:
                result = {"error": "anchor_mismatch", "group_id": group_id,
                          "issues": anchor_issues}
                con.execute(
                    "UPDATE align_journal SET state='rolled_back',result_json=?,updated_at=? "
                    "WHERE op_id=?", (_json(result), now, op_id),
                )
                return {"op_id": op_id, "state": "rolled_back", "result": result}
        new_status = STATUS_CONFIRMED if action == "confirm" else STATUS_REJECTED
        con.execute(
            "UPDATE align_groups SET status=?,manual=1,decision_actor=?,decision_note=?,"
            "decision_at=?,revision=revision+1,updated_at=? WHERE group_id=?",
            (new_status, payload.get("actor", ""), payload.get("note"), now, now, group_id),
        )
        con.execute(
            "INSERT INTO align_decisions(decision_id,group_id,action,actor,note,"
            "previous_status,new_status,op_id,created_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(decision_id) DO NOTHING",
            (decision_id, group_id, action, payload.get("actor", ""), payload.get("note"),
             group["status"], new_status, op_id, now),
        )
        set_candidate_status_for_group(con, group_id, new_status, now)
        result = {"group_id": group_id, "previous_status": group["status"],
                  "new_status": new_status, "decision_id": decision_id,
                  "revision": int(group["revision"]) + 1}
        con.execute(
            "UPDATE align_journal SET state='applied',result_json=?,updated_at=? WHERE op_id=?",
            (_json(result), now, op_id),
        )
    return {"op_id": op_id, "state": "applied", "result": result, "replayed": False}


def recover(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Resolve every pending journal entry left behind by a crash.

    The recovery decision is based on durable state, not on in-memory progress:
    an entry whose decision already exists is simply marked applied; an entry
    whose group vanished is rolled back; anything else is replayed.
    """
    rows = _select(con, "SELECT * FROM align_journal WHERE state='pending' ORDER BY created_at, op_id")
    outcomes: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        decision_id = "d-" + stable_hash(row["op_id"], payload.get("group_id"), row["action"],
                                         length=20)
        exists = con.execute(
            "SELECT 1 FROM align_decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if exists:
            with con:
                con.execute(
                    "UPDATE align_journal SET state='applied',updated_at=? WHERE op_id=?",
                    (utcnow(), row["op_id"]),
                )
            outcomes.append({"op_id": row["op_id"], "state": "applied", "recovered": "already_decided"})
            continue
        outcomes.append(apply_decision(con, row["op_id"]))
    return outcomes


def verify_group(con: sqlite3.Connection, group_id: str) -> list[dict[str, Any]]:
    """Check that every member anchor still points at the indexed clause."""
    issues: list[dict[str, Any]] = []
    group = get_group(con, group_id)
    if not group:
        return [{"kind": "group_missing", "group_id": group_id}]
    for member in group_members(con, group_id):
        ref = clause_ref(con, member["doc_id"], member["version"], member["clause_key"])
        if ref is None:
            # Legacy rows may predate ``doc_id``; fall back to clause_key alone.
            refs = _select(
                con,
                "SELECT * FROM align_clause_index WHERE version=? AND clause_key=?",
                (member["version"], member["clause_key"]),
            )
            ref = refs[0] if refs else None
            if ref is not None:
                ref["anchor"] = json.loads(ref.pop("anchor_json") or "{}")
        if ref is None:
            issues.append({"kind": "clause_missing", "group_id": group_id,
                           "clause_key": member["clause_key"], "version": member["version"]})
            continue
        if not member["anchor"]:
            # No anchor was recorded by the legacy writer; nothing to compare.
            continue
        if ref["text_sha256"] != member["text_sha256"]:
            issues.append({"kind": "anchor_mismatch", "group_id": group_id,
                           "clause_key": member["clause_key"], "version": member["version"],
                           "expected": ref["text_sha256"], "stored": member["text_sha256"]})
        if ref["anchor"] != member["anchor"]:
            issues.append({"kind": "anchor_mismatch", "group_id": group_id,
                           "clause_key": member["clause_key"], "version": member["version"],
                           "expected_anchor": ref["anchor"], "stored_anchor": member["anchor"]})
    return issues


def backfill_legacy_members(con: sqlite3.Connection) -> int:
    """Heal member rows written before anchors/doc ids were stored.

    Older alignment results stay readable: where the clause index still has the
    clause, we fill in ``doc_id``, ordinal, anchor and hash.  Rows we cannot
    resolve are left untouched (``verify_group`` reports them).
    """
    updated = 0
    rows = _select(
        con,
        "SELECT group_id, side, clause_key, version, doc_id, anchor_json FROM align_members "
        "WHERE doc_id='' OR anchor_json='' OR anchor_json='{}'",
    )
    for row in rows:
        ref = con.execute(
            "SELECT doc_id, ordinal, anchor_json, text_sha256 FROM align_clause_index "
            "WHERE version=? AND clause_key=?", (row["version"], row["clause_key"]),
        ).fetchone()
        if not ref:
            continue
        con.execute(
            "UPDATE align_members SET doc_id=?,ordinal=?,anchor_json=?,text_sha256=? "
            "WHERE group_id=? AND side=? AND clause_key=?",
            (ref[0], ref[1], ref[2], ref[3], row["group_id"], row["side"], row["clause_key"]),
        )
        updated += 1
    if updated:
        con.commit()
    return updated


# ---------------------------------------------------------------------------
# Snapshots (old alignment results remain readable)
# ---------------------------------------------------------------------------

def export_snapshot(con: sqlite3.Connection, doc_id: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": utcnow(),
        "versions": list_versions(con, doc_id),
        "groups": list_groups(con, doc_id),
        "candidates": list_candidates(con, doc_id),
        "decisions": list_decisions(con),
    }


def import_snapshot(con: sqlite3.Connection, snapshot: Mapping[str, Any]) -> dict[str, int]:
    """Import a previously exported alignment snapshot.

    Manual groups and decisions win over existing automatic rows; this keeps old
    confirmed alignments readable after a rebuild.
    """
    ensure_schema(con)
    imported = {"groups": 0, "candidates": 0, "decisions": 0}
    now = utcnow()
    for group in snapshot.get("groups", []):
        status = LEGACY_STATUS_MAP.get(str(group.get("status")), str(group.get("status")))
        if status not in KNOWN_STATUSES:
            status = STATUS_PENDING
        write_group(
            con, group_id=group["group_id"], doc_id=group.get("doc_id", ""),
            peer_doc_id=group.get("peer_doc_id", ""),
            low_version=group.get("low_version", ""), high_version=group.get("high_version", ""),
            kind=group.get("kind", "one_to_one"), status=status,
            score=float(group.get("score", 0.0)), coverage=float(group.get("coverage", 0.0)),
            members=group.get("members", []), now=now,
        )
        if group.get("manual") or status in MANUAL_STATUSES:
            con.execute(
                "UPDATE align_groups SET manual=1,decision_actor=?,decision_note=?,"
                "decision_at=? WHERE group_id=?",
                (group.get("decision_actor"), group.get("decision_note"),
                 group.get("decision_at") or now, group["group_id"]),
            )
        imported["groups"] += 1
    for candidate in snapshot.get("candidates", []):
        status = LEGACY_STATUS_MAP.get(str(candidate.get("status")), str(candidate.get("status")))
        detail = candidate.get("detail") or {}
        write_candidate(
            con, candidate_id=candidate["candidate_id"],
            doc_id=candidate.get("doc_id", ""), peer_doc_id=candidate.get("peer_doc_id", ""),
            low_version=candidate.get("low_version", ""),
            high_version=candidate.get("high_version", ""),
            kind=candidate.get("kind", "one_to_one"),
            score=float(candidate.get("score", 0.0)),
            coverage=float(candidate.get("coverage", 0.0)), status=status,
            group_id=candidate.get("group_id"),
            low_keys=candidate.get("low_keys", []), high_keys=candidate.get("high_keys", []),
            detail=detail, now=now,
        )
        if status in ("confirmed", "rejected", "conflict"):
            # ``write_candidate`` treats an existing decided row as authoritative;
            # for a fresh import the imported status must win.
            con.execute("UPDATE align_candidates SET status=? WHERE candidate_id=?",
                        (status, candidate["candidate_id"]))
        imported["candidates"] += 1
    for decision in snapshot.get("decisions", []):
        con.execute(
            "INSERT INTO align_decisions(decision_id,group_id,action,actor,note,"
            "previous_status,new_status,op_id,created_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(decision_id) DO NOTHING",
            (decision["decision_id"], decision["group_id"], decision["action"],
             decision.get("actor", ""), decision.get("note"),
             decision.get("previous_status", ""), decision.get("new_status", ""),
             decision.get("op_id"), decision.get("created_at") or now),
        )
        imported["decisions"] += 1
    con.commit()
    return imported

"""跨年度版本关联（version）。

本模块把同一条款的各年度版本串成历史链，链上的“同一性”来自
:mod:`policydiff.align` 的映射，而不是标题或编号的字面相同。这样：

* 条款改号、章节重排后仍归入同一条历史链；
* 一对多拆分 / 多对一合并引用对齐组，天然保留分叉与汇合，不会被压成单链；
* 人工确认的映射优先于自动映射；
* 跨年度缺段（某年缺失、次年重新出现）以链路断开记录，不臆造关联；
* 对既有版本记录的重复运行是幂等的，历史 run 可查询。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Sequence

from . import _align_store as db
from . import _align_text as tx
from . import align as _align
from . import store as _base_store


def _ensure(con: sqlite3.Connection | None) -> tuple[sqlite3.Connection, bool]:
    if con is None:
        con = _base_store.connect()
        db.ensure_schema(con)
        return con, True
    db.ensure_schema(con)
    return con, False


def _natural_key(value: str) -> tuple[Any, ...]:
    import re
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", str(value)))


def _norm(obj: Any, doc_id: str | None, version: str | None) -> dict[str, Any]:
    if isinstance(obj, Mapping) and tx.is_normalized(obj):
        return dict(obj)
    doc = tx.split_version_doc(obj, default_doc_id=doc_id, default_version=version)
    if not doc.get("doc_id"):
        doc["doc_id"] = doc_id or "doc"
    if not doc.get("version"):
        doc["version"] = version or ""
    return tx.normalize_clauses(doc)


def _pair_groups(con: sqlite3.Connection, doc_id: str,
                 low_version: str, high_version: str) -> list[dict[str, Any]]:
    return db.list_groups(con, doc_id=doc_id, low_version=low_version,
                          high_version=high_version)


def build_chains(docs: Sequence[Any], *, con: sqlite3.Connection | None = None,
                 doc_id: str | None = None, auto_align: bool = True) -> dict[str, Any]:
    """Build clause history chains across the given versions.

    Each chain lists the stable clause keys (``<version>::<clause_id>``) that
    belong to one lineage.  Split / merge groups create multiple nodes in the
    same chain instead of flattening the fork into a single line, and a missing
    year is reported as a ``disappeared`` / ``new`` gap rather than invented
    continuity.  Pending relations are marked ``pending`` so history does not
    silently treat undecided mappings as established.
    """
    con, own = _ensure(con)
    try:
        prepared = [_norm(doc, doc_id, None) for doc in docs]
        by_version: dict[str, dict[str, Any]] = {}
        for doc in prepared:
            if doc["version"] in by_version:
                raise ValueError("duplicate version id: %r" % doc["version"])
            by_version[doc["version"]] = doc
        ordered = sorted(by_version.values(), key=lambda d: _natural_key(d["version"]))
        if not ordered:
            return {"doc_id": doc_id or "", "chains": [], "gaps": [], "edges": []}

        edges: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        for left, right in zip(ordered, ordered[1:]):
            groups = _pair_groups(con, left["doc_id"], left["version"], right["version"])
            if auto_align and (not groups or _align.pair_is_stale(con, left, right)):
                _align.align_versions(left, right, con=con)
                groups = _pair_groups(con, left["doc_id"], left["version"],
                                      right["version"])
            matched_left: set[str] = set()
            matched_right: set[str] = set()
            pending_left: set[str] = set()
            pending_right: set[str] = set()
            left_keys = {c["clause_key"] for c in left["clauses"]}
            right_keys = {c["clause_key"] for c in right["clauses"]}
            for group in groups:
                low_keys = [m["clause_key"] for m in group["members"] if m["side"] == "low"]
                high_keys = [m["clause_key"] for m in group["members"] if m["side"] == "high"]
                # Skip relations left over from an older parse of either version.
                if not group["members"] or \
                   not all(k in left_keys for k in low_keys) or \
                   not all(k in right_keys for k in high_keys):
                    continue
                status = db.LEGACY_STATUS_MAP.get(group["status"], group["status"])
                if status == db.STATUS_REJECTED:
                    continue
                edge = {
                    "from_version": left["version"], "to_version": right["version"],
                    "from_keys": low_keys, "to_keys": high_keys, "kind": group["kind"],
                    "status": status, "group_id": group["group_id"],
                    "score": group["score"],
                }
                edges.append(edge)
                if status in (db.STATUS_AUTO, db.STATUS_CONFIRMED):
                    matched_left.update(low_keys)
                    matched_right.update(high_keys)
                else:
                    pending_left.update(low_keys)
                    pending_right.update(high_keys)
            left_index = {c["clause_key"]: c for c in left["clauses"]}
            right_index = {c["clause_key"]: c for c in right["clauses"]}
            for key in left_index:
                if key in matched_left:
                    continue
                gap_type = "pending" if key in pending_left else "disappeared"
                gaps.append({"version": left["version"], "clause_key": key,
                             "next_version": right["version"], "type": gap_type,
                             "anchor": left_index[key]["anchor"]})
            for key in right_index:
                if key in matched_right:
                    continue
                gap_type = "pending" if key in pending_right else "new"
                gaps.append({"version": right["version"], "clause_key": key,
                             "previous_version": left["version"], "type": gap_type,
                             "anchor": right_index[key]["anchor"]})

        chains = _assemble_chains(edges)
        result = {
            "doc_id": ordered[0]["doc_id"],
            "versions": [doc["version"] for doc in ordered],
            "chains": chains,
            "edges": edges,
            "gaps": sorted(gaps, key=lambda g: (_natural_key(g["version"]), g["clause_key"])),
        }
        _persist(con, result)
        con.commit()
        return result
    finally:
        if own:
            con.close()


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _assemble_chains(edges: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    uf = _UnionFind()

    def node_of(version: str, key: str) -> str:
        # clause_key is already ``<version>::<clause_id>``; avoid double prefixes.
        if key.startswith(version + "::"):
            return key
        return "%s::%s" % (version, key)

    for edge in edges:
        keys = [node_of(edge["from_version"], k) for k in edge["from_keys"]]
        keys += [node_of(edge["to_version"], k) for k in edge["to_keys"]]
        for key in keys[1:]:
            uf.union(keys[0], key)
    components: dict[str, list[str]] = {}
    for edge in edges:
        for version, keys in ((edge["from_version"], edge["from_keys"]),
                              (edge["to_version"], edge["to_keys"])):
            for key in keys:
                node = node_of(version, key)
                components.setdefault(uf.find(node), []).append(node)
    chains = []
    for nodes in components.values():
        unique = sorted(set(nodes))
        versions: list[str] = []
        for node in unique:
            version = node.split("::", 1)[0]
            if version not in versions:
                versions.append(version)
        links = []
        for edge in edges:
            edge_nodes = {node_of(edge["from_version"], k) for k in edge["from_keys"]}
            if edge_nodes & set(unique):
                links.append({"from_version": edge["from_version"],
                              "to_version": edge["to_version"], "kind": edge["kind"],
                              "status": edge["status"], "group_id": edge["group_id"]})
        chains.append({"chain_id": "h-" + tx.stable_hash(*unique, length=20),
                       "versions": versions, "nodes": unique, "links": links})
    chains.sort(key=lambda c: c["chain_id"])
    return chains


def _persist(con: sqlite3.Connection, result: Mapping[str, Any]) -> None:
    run_id = "vrun-" + tx.stable_hash(
        result["doc_id"], json.dumps(result["chains"], sort_keys=True),
        json.dumps([(g["version"], g["clause_key"], g["type"]) for g in result["gaps"]]),
        length=20,
    )
    con.execute(
        "INSERT INTO version_runs(run_id,doc_id,chain_json,created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(run_id) DO UPDATE SET chain_json=excluded.chain_json",
        (run_id, result["doc_id"],
         json.dumps({"chains": result["chains"], "gaps": result["gaps"]},
                    ensure_ascii=False, sort_keys=True), db.utcnow()),
    )
    result["run_id"] = run_id


def history(con: sqlite3.Connection | None = None, *, doc_id: str | None = None) -> list[dict[str, Any]]:
    con, own = _ensure(con)
    try:
        sql = "SELECT * FROM version_runs"
        params: list[Any] = []
        if doc_id is not None:
            sql += " WHERE doc_id=?"
            params.append(doc_id)
        sql += " ORDER BY created_at, run_id"
        cur = con.execute(sql, params)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        if own:
            con.close()


def process(record: Mapping[str, Any], con: sqlite3.Connection | None = None) -> dict[str, Any]:
    rec = dict(record)
    versions = rec.get("versions")
    if versions and isinstance(versions, Sequence) and len(versions) >= 2:
        rec["_version"] = build_chains(list(versions), con=con, doc_id=rec.get("doc_id"))
    rec.setdefault("_stage_version", "ok")
    return rec

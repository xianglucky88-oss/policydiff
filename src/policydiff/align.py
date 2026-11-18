"""跨版本政策条款结构对齐（align）。

本模块只回答“哪个条款对应哪个条款”，并把判断、候选和人工决定持久化；它不重写
条款正文。条款内容始终来自上游 ``doc_parse`` / ``clause`` 的现有解析输出，这里只
保存条款引用、内容哈希和来源锚点（section / number / attachment / ordinal）。

契约
====
1. **方向稳定**：内部先把两份版本文档按 ``(version, doc_id, doc_hash)`` 规范化排序，
   调用方交换参数顺序得到完全相同的组、候选和 id。
2. **编号不是身份**：相似度以正文为主、标题次之；条款/章节编号只占很小权重，
   因此“章节编号换了、条文没变”仍能稳定匹配。
3. **结构只做信号**：章节重排、附件插入不参与硬约束；位置/章节只提供很弱的
   tie-break，附件插入不会把原本相邻的两条强行拆开或隔断。
4. **一对多 / 多对一单独建模**：
   - ``one_to_one``：score ≥ ``auto_threshold`` 自动接受；介于
     ``review_threshold`` 与 ``auto_threshold`` 之间进入 ``pending``。
   - ``split``（1→N）/ ``merge``（N→1）：按“子条款文本被父条款覆盖且并集覆盖
     足够”成组识别，默认进入 ``pending``（``auto_structural=True`` 时可自动接受）。
     绝不会退化成“只取最高相似度的那一对、其余标新增”。
   - N→M 交叉冲突（同一批条款既像拆分又像合并）记为 ``ambiguous``，一律 ``pending``。
   - 低于 ``review_threshold`` 的关系记入候选表（``low_confidence``），可日后提升，
     不建立映射。模板化/高度重复的文本不会被强行解释成拆分或合并。
5. **人工决定不可被相似度覆盖**：``confirmed`` / ``rejected`` 组带 ``manual`` 标记。
   重算只刷新自动结果；与其冲突的新候选以 ``conflict`` 落候选表，历史决定保持不变。
   被拒绝的关系再次出现时仍保持拒绝，并标记 ``resurfaced`` 供复核。被新一轮
   自动结果取代的旧自动组标记为 ``superseded``，保留可审计但不再参与比较。
6. **确认写入可恢复**：确认/拒绝先写 journal（pending），再在单个事务里改映射并
   追加 decision。无论在哪一步中断，``recover()`` 都能依据持久状态重放或回滚，
   不会留下半条映射或重复决定；应用前会核对成员锚点与条款索引。
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from . import _align_store as db
from . import _align_text as tx
from . import store as _base_store

# ---------------------------------------------------------------------------
# 可调阈值（同时作为契约的一部分）
# ---------------------------------------------------------------------------

AUTO_THRESHOLD = 0.80        # 一对一自动接受
REVIEW_THRESHOLD = 0.45      # 进入待人工
CANDIDATE_FLOOR = 0.30       # 低于此值不记录（噪声）
STRUCTURAL_FLOOR = 0.40      # 拆分/合并成员的最低相似度
COVERAGE_FLOOR = 0.55        # 子条款并集覆盖父条款的最低比例
COVERAGE_AUTO = 0.85         # 结构关系自动接受的最低覆盖率
STRUCTURAL_MIN_SCORE = 0.55  # 结构关系自动接受时每个成员的最低分
MARGIN = 0.06                # 一对一自动接受所需的领先优势
IDENTICAL_TEXT_SCORE = 0.88  # 正文逐字一致时的最低分（编号/标题变化视为结构噪声）

WEIGHT_TEXT = 0.68
WEIGHT_HEADING = 0.16
WEIGHT_SECTION = 0.07
WEIGHT_NUMBER = 0.05
WEIGHT_IDENTITY = 0.04

_SIDE_LOW = "low"
_SIDE_HIGH = "high"


# ---------------------------------------------------------------------------
# 连接 / 规范化
# ---------------------------------------------------------------------------

def _connect() -> tuple[sqlite3.Connection, bool]:
    con = _base_store.connect()
    db.ensure_schema(con)
    return con, True


def _ensure(con: sqlite3.Connection | None) -> tuple[sqlite3.Connection, bool]:
    if con is None:
        return _connect()
    db.ensure_schema(con)
    return con, False


def _natural_key(value: str) -> tuple[Any, ...]:
    parts = re.split(r"(\d+)", str(value))
    return tuple(int(p) if p.isdigit() else p for p in parts)


def _doc_hash(doc: Mapping[str, Any]) -> str:
    payload = [
        [c["clause_key"], c["text_sha256"], c["anchor"], c["ordinal"]]
        for c in doc["clauses"]
    ]
    return tx.stable_hash(doc.get("doc_id", ""), doc.get("version", ""),
                          json.dumps(payload, sort_keys=True, ensure_ascii=False), length=24)


def _canonical_order(a: Mapping[str, Any], b: Mapping[str, Any]) -> tuple[dict, dict]:
    """Order two normalized docs deterministically, independent of arrival order."""
    ha, hb = _doc_hash(a), _doc_hash(b)
    ka = (_natural_key(a["version"]), a["doc_id"], ha)
    kb = (_natural_key(b["version"]), b["doc_id"], hb)
    return (a, b) if ka <= kb else (b, a)


def document_fingerprint(doc: Mapping[str, Any]) -> str:
    """Content fingerprint of a normalized version document.

    ``compare`` / ``version`` use it to notice that a version was re-parsed with
    different content after alignment, so stale groups are refreshed instead of
    being applied to clauses that no longer exist.
    """
    return _doc_hash(doc)


def pair_is_stale(con: sqlite3.Connection, low: Mapping[str, Any],
                  high: Mapping[str, Any]) -> bool:
    """True when stored alignment was built from a different parse of a version."""
    for doc in (low, high):
        row = con.execute(
            "SELECT doc_hash FROM align_versions WHERE doc_id=? AND version=?",
            (doc["doc_id"], doc["version"]),
        ).fetchone()
        if row is None:
            return True
        if row[0] != _doc_hash(doc):
            return True
    return False


def _prepare(obj: Any, doc_id: str | None = None, version: str | None = None) -> dict[str, Any]:
    if isinstance(obj, Mapping) and tx.is_normalized(obj):
        return dict(obj)
    doc = tx.split_version_doc(obj, default_doc_id=doc_id, default_version=version)
    if not doc.get("doc_id"):
        doc["doc_id"] = doc_id or "doc"
    if not doc.get("version"):
        doc["version"] = version or ""
    return tx.normalize_clauses(doc)


# ---------------------------------------------------------------------------
# 相似度
# ---------------------------------------------------------------------------

class _Scorer:
    def __init__(self, low: Sequence[Mapping[str, Any]], high: Sequence[Mapping[str, Any]]):
        self.low = low
        self.high = high
        self._index: dict[str, list[int]] = {}
        for hi, clause in enumerate(high):
            for gram in clause["text_grams"]:
                self._index.setdefault(gram, []).append(hi)
        self._cache: dict[tuple[int, int], float] = {}

    def _text_sim(self, li: int, hi: int) -> float:
        a, b = self.low[li], self.high[hi]
        if a["text_sha256"] == b["text_sha256"]:
            return 1.0
        return tx.text_similarity(a["text_norm"], b["text_norm"], a["text_grams"], b["text_grams"])

    def candidates_for(self, li: int, floor: float) -> list[tuple[int, float]]:
        low = self.low[li]
        counts: dict[int, int] = {}
        for gram in low["text_grams"]:
            for hi in self._index.get(gram, ()):
                counts[hi] = counts.get(hi, 0) + 1
        out: list[tuple[int, float]] = []
        low_len = max(1, len(low["text_grams"]))
        for hi, shared in counts.items():
            high_len = max(1, len(self.high[hi]["text_grams"]))
            jac = shared / (low_len + high_len - shared)
            if jac < 0.05 and min(low_len, high_len) > 4:
                continue
            score = self.score(li, hi)
            if score >= floor:
                out.append((hi, score))
        # Short clauses have few grams; play it safe and score everything.
        if not out and (low_len <= 4 or not self._index):
            for hi in range(len(self.high)):
                score = self.score(li, hi)
                if score >= floor:
                    out.append((hi, score))
        out.sort(key=lambda item: (-item[1], self.high[item[0]]["clause_key"]))
        return out

    def structural_pairs(self) -> dict[tuple[int, int], float]:
        """Candidate ``(low, high)`` pairs for split/merge detection.

        A pair qualifies when the shorter clause's grams are mostly contained in
        the longer one.  Candidates come from the shared-gram index of the low
        clauses, so common text is the only reason a pair is examined and the
        pass stays near-linear for distinct policy wording.
        """
        pairs: dict[tuple[int, int], float] = {}
        for li, low_clause in enumerate(self.low):
            grams = low_clause["text_grams"]
            if len(low_clause["text_norm"]) < 6 or not grams:
                continue
            counts: dict[int, int] = {}
            for gram in grams:
                for hi in self._index.get(gram, ()):
                    counts[hi] = counts.get(hi, 0) + 1
            for hi, shared in counts.items():
                high_clause = self.high[hi]
                if len(low_clause["text_norm"]) <= len(high_clause["text_norm"]):
                    shorter, longer = low_clause, high_clause
                else:
                    shorter, longer = high_clause, low_clause
                if len(shorter["text_norm"]) < 6:
                    continue
                if shared < 0.55 * len(shorter["text_grams"]):
                    continue
                if _containment(shorter, longer) < 0.55:
                    continue
                pairs[(li, hi)] = self.score(li, hi)
        return pairs

    def score(self, li: int, hi: int) -> float:
        key = (li, hi)
        if key in self._cache:
            return self._cache[key]
        a, b = self.low[li], self.high[hi]
        weighted = 0.0
        total = 0.0
        text_sim = self._text_sim(li, hi)
        weighted += WEIGHT_TEXT * text_sim
        total += WEIGHT_TEXT
        if a["heading_norm"] and b["heading_norm"]:
            # ``heading_norm`` is already the canonical form from normalization.
            heading_sim = 1.0 if a["heading_norm"] == b["heading_norm"] else \
                tx.text_similarity(a["heading_norm"], b["heading_norm"])
            weighted += WEIGHT_HEADING * heading_sim
        total += WEIGHT_HEADING
        if a["section"] and b["section"]:
            section_sim = (1.0 if a["section"] == b["section"] else
                           tx.text_similarity(tx.normalize_heading(a["section"]),
                                              tx.normalize_heading(b["section"])))
            weighted += WEIGHT_SECTION * section_sim
        total += WEIGHT_SECTION
        number_sim = tx.number_similarity(
            {"value": a["number_value"], "kind": a["number_kind"]},
            {"value": b["number_value"], "kind": b["number_kind"]},
        )
        if number_sim is not None:
            # Renumbering is a weak negative, never a hard mismatch.
            weighted += WEIGHT_NUMBER * (1.0 if number_sim else 0.4)
        total += WEIGHT_NUMBER
        if a["clause_id"] == b["clause_id"]:
            weighted += WEIGHT_IDENTITY
        total += WEIGHT_IDENTITY
        score = weighted / total if total else 0.0
        if a["text_sha256"] == b["text_sha256"] and a["text_len"] > 0:
            # Byte-identical body is the strongest evidence available: chapter
            # reordering, renumbering and heading edits are structural noise.
            score = max(score, IDENTICAL_TEXT_SCORE)
        self._cache[key] = score
        return score


# ---------------------------------------------------------------------------
# 结构假设：拆分 / 合并 / 交叉
# ---------------------------------------------------------------------------

def _containment(child: Mapping[str, Any], parent: Mapping[str, Any]) -> float:
    grams = child["text_grams"]
    if not grams:
        return 0.0
    return len(grams & parent["text_grams"]) / len(grams)


def _is_part_of_key(child: Mapping[str, Any], parent: Mapping[str, Any],
                    containment: float, sim: float) -> bool:
    if child["clause_key"] == parent["clause_key"]:
        return False
    child_len = len(child["text_norm"])
    parent_len = len(parent["text_norm"])
    if child_len < 6 or parent_len < child_len:
        return False
    if child_len >= parent_len * 0.96:
        return False
    if sim < STRUCTURAL_FLOOR:
        return False
    return containment >= 0.55


def _maximal_children(children: Sequence[int], clauses: Sequence[Mapping[str, Any]]) -> list[int]:
    kept: list[int] = []
    for idx in sorted(children, key=lambda i: -len(clauses[i]["text_norm"])):
        if any(_containment(clauses[idx], clauses[other]) >= 0.90 for other in kept):
            continue
        kept.append(idx)
    return sorted(kept)


def _is_genuine_structural(children: Sequence[int], parent: Mapping[str, Any],
                           clauses: Sequence[Mapping[str, Any]]) -> bool:
    """A real split/merge has several complementary parts, not near-duplicates.

    The union of children must explain materially more of the parent than the
    single best child, and no two children may overlap almost entirely.  This
    keeps template-heavy corpora (many clauses with the same boilerplate) from
    producing spurious splits/merges.
    """
    if len(children) < 2:
        return False
    grams = [clauses[i]["text_grams"] for i in children]
    union: set[str] = set()
    for item in grams:
        union.update(item)
    parent_grams = parent["text_grams"]
    if not parent_grams:
        return False
    union_cov = len(union & parent_grams) / len(parent_grams)
    best_single = max(len(item & parent_grams) / len(parent_grams) for item in grams)
    if union_cov < COVERAGE_FLOOR:
        return False
    # Require at least ~15 percentage points of extra explanation.
    if union_cov - best_single < 0.15:
        return False
    for i in range(len(children)):
        for j in range(i + 1, len(children)):
            if _containment(clauses[children[i]], clauses[children[j]]) >= 0.90:
                return False
    return True


def _structural_hypotheses(low, high, pair_scores):
    """Detect split / merge hypotheses from index-driven candidate pairs.

    ``pair_scores`` holds ``(low_index, high_index) -> score`` for pairs whose
    text containment clears the structural floor.  A child is assigned to the
    parent that contains the largest share of its text, so one clause is never
    explained by two stories at once, and no O(N²) scan is needed.
    """
    hypotheses: list[dict[str, Any]] = []

    # Containment is directional: split asks "is the newer child inside the
    # older parent?", merge asks the reverse.
    split_containment = {key: _containment(high[key[1]], low[key[0]]) for key in pair_scores}
    merge_containment = {key: _containment(low[key[0]], high[key[1]]) for key in pair_scores}
    # best parent per newer clause (split direction)
    best_parent_for_high: dict[int, tuple[float, int, int]] = {}
    # best parent per older clause (merge direction)
    best_parent_for_low: dict[int, tuple[float, int, int]] = {}
    for (li, hi), score in pair_scores.items():
        # split direction: high child inside low parent
        high_child = high[hi]
        if len(high_child["text_norm"]) < len(low[li]["text_norm"]):
            rank = (split_containment[(li, hi)], len(low[li]["text_norm"]), -li)
            if hi not in best_parent_for_high or rank > best_parent_for_high[hi]:
                best_parent_for_high[hi] = rank
    for (li, hi), score in pair_scores.items():
        # merge direction: low child inside high parent
        low_child = low[li]
        if len(low_child["text_norm"]) < len(high[hi]["text_norm"]):
            rank = (merge_containment[(li, hi)], len(high[hi]["text_norm"]), -hi)
            if li not in best_parent_for_low or rank > best_parent_for_low[li]:
                best_parent_for_low[li] = rank

    # split: one older clause -> several newer clauses
    split_children: dict[int, list[tuple[int, float]]] = {}
    for (li, hi), score in pair_scores.items():
        child, parent = high[hi], low[li]
        if len(child["text_norm"]) >= len(parent["text_norm"]):
            continue
        if best_parent_for_high.get(hi, (0.0, 0, 0))[2] != -li:
            continue
        if not _is_part_of_key(child, parent, split_containment[(li, hi)], score):
            continue
        split_children.setdefault(li, []).append((hi, score))
    for li, children in split_children.items():
        children_idx = _maximal_children([hi for hi, _ in children], high)
        if not _is_genuine_structural(children_idx, low[li], high):
            continue
        cov = tx.coverage([high[hi]["text_grams"] for hi in children_idx],
                          low[li]["text_grams"])
        if cov < COVERAGE_FLOOR:
            continue
        hypotheses.append({
            "kind": "split", "low": [li], "high": children_idx, "coverage": cov,
            "score": sum(pair_scores[(li, hi)] for hi in children_idx) / len(children_idx),
        })

    # merge: several older clauses -> one newer clause
    merge_children: dict[int, list[tuple[int, float]]] = {}
    for (li, hi), score in pair_scores.items():
        child, parent = low[li], high[hi]
        if len(child["text_norm"]) >= len(parent["text_norm"]):
            continue
        if best_parent_for_low.get(li, (0.0, 0, 0))[2] != -hi:
            continue
        if not _is_part_of_key(child, parent, merge_containment[(li, hi)], score):
            continue
        merge_children.setdefault(hi, []).append((li, score))
    for hi, children in merge_children.items():
        children_idx = _maximal_children([li for li, _ in children], low)
        if not _is_genuine_structural(children_idx, high[hi], low):
            continue
        cov = tx.coverage([low[li]["text_grams"] for li in children_idx],
                          high[hi]["text_grams"])
        if cov < COVERAGE_FLOOR:
            continue
        hypotheses.append({
            "kind": "merge", "low": children_idx, "high": [hi], "coverage": cov,
            "score": sum(pair_scores[(li, hi)] for li in children_idx) / len(children_idx),
        })
    return hypotheses


class _UnionFind:
    def __init__(self):
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


def _merge_hypotheses(hypotheses, low, high):
    """Union overlapping hypotheses into split / merge / ambiguous groups."""
    if not hypotheses:
        return []
    uf = _UnionFind()
    for hypothesis in hypotheses:
        keys = [(_SIDE_LOW, low[i]["clause_key"]) for i in hypothesis["low"]]
        keys += [(_SIDE_HIGH, high[i]["clause_key"]) for i in hypothesis["high"]]
        anchor = "%s:%s" % keys[0]
        for side, key in keys[1:]:
            uf.union(anchor, "%s:%s" % (side, key))
        hypothesis["_anchor"] = anchor

    components: dict[str, dict[str, Any]] = {}
    for hypothesis in hypotheses:
        root = uf.find(hypothesis["_anchor"])
        comp = components.setdefault(root, {
            "kind": hypothesis["kind"], "low": set(), "high": set(),
            "coverage": 0.0, "score": 0.0, "members": [], "kinds": set(),
        })
        comp["kinds"].add(hypothesis["kind"])
        comp["low"].update(hypothesis["low"])
        comp["high"].update(hypothesis["high"])
        comp["coverage"] = max(comp["coverage"], hypothesis["coverage"])
        comp["score"] = max(comp["score"], hypothesis["score"])
        comp["members"].append(hypothesis)

    groups = []
    for comp in components.values():
        kinds = set(comp["kinds"])
        low_set, high_set = comp["low"], comp["high"]
        # A component that collapses into N→M (or a split whose parent set is
        # not exactly one) is not a clean structural story: mark it ambiguous.
        if len(low_set) != 1 and len(high_set) != 1:
            kind = "ambiguous"
        elif len(kinds) > 1:
            kind = "ambiguous"
        else:
            kind = kinds.pop()
            if kind == "split" and len(low_set) != 1:
                kind = "ambiguous"
            if kind == "merge" and len(high_set) != 1:
                kind = "ambiguous"
        groups.append({
            "kind": kind,
            "low": sorted(low_set),
            "high": sorted(high_set),
            "coverage": comp["coverage"],
            "score": comp["score"],
        })
    return groups


# ---------------------------------------------------------------------------
# 人工决定保护
# ---------------------------------------------------------------------------

def _protected_groups(con, doc_id: str, peer_doc_id: str,
                      low_version: str, high_version: str) -> list[dict[str, Any]]:
    """Manual decisions for this pair: confirmed relations lock their members.

    Rejected relations do not lock their clauses (they may legitimately match
    something else) but their exact pairing must never be re-proposed as
    accepted; the caller checks ``rejected_pairs`` separately.
    """
    existing = db.list_groups(con, low_version=low_version, high_version=high_version)
    protected = []
    for group in existing:
        if group.get("manual") or group.get("status") in db.MANUAL_STATUSES:
            if {(group["doc_id"], group["peer_doc_id"])} & {(doc_id, peer_doc_id),
                                                             (peer_doc_id, doc_id)}:
                protected.append(group)
    return protected


def _split_protected(protected: Sequence[Mapping[str, Any]]):
    """Return (confirmed groups, rejected relation pairs) for this pair."""
    confirmed: list[Mapping[str, Any]] = []
    rejected_pairs: list[tuple[frozenset[str], frozenset[str], Mapping[str, Any]]] = []
    for group in protected:
        low, high = _key_sets(group)
        status = db.LEGACY_STATUS_MAP.get(group["status"], group["status"])
        if status == db.STATUS_CONFIRMED or group.get("manual") and status != db.STATUS_REJECTED:
            confirmed.append(group)
        else:
            rejected_pairs.append((low, high, group))
    return confirmed, rejected_pairs


def _locked_keys(groups: Sequence[Mapping[str, Any]]) -> tuple[set[str], set[str]]:
    locked_low: set[str] = set()
    locked_high: set[str] = set()
    for group in groups:
        low, high = _key_sets(group)
        locked_low.update(low)
        locked_high.update(high)
    return locked_low, locked_high


def _key_sets(group: Mapping[str, Any]) -> tuple[frozenset[str], frozenset[str]]:
    low = frozenset(m["clause_key"] for m in group["members"] if m["side"] == _SIDE_LOW)
    high = frozenset(m["clause_key"] for m in group["members"] if m["side"] == _SIDE_HIGH)
    return low, high


def _member(clause: Mapping[str, Any], side: str, role: str = "") -> dict[str, Any]:
    return {
        "side": side,
        "clause_key": clause["clause_key"],
        "clause_id": clause["clause_id"],
        "version": clause["version"],
        "doc_id": clause["doc_id"],
        "role": role,
        "ordinal": clause["ordinal"],
        "anchor": clause["anchor"],
        "text_sha256": clause["text_sha256"],
    }


def _refresh_locked_groups(con, groups: Sequence[Mapping[str, Any]],
                           low: Mapping[str, Any], high: Mapping[str, Any],
                           now: str) -> list[dict[str, Any]]:
    """Re-point confirmed members at the freshly indexed clauses.

    The manual decision (status, actor, note, group id) is untouched; only the
    stored source anchors and content hashes are refreshed so ``verify_group``
    keeps working after a re-parse that changed an ordinal or section label.
    Missing clauses are reported, never silently dropped.
    """
    issues: list[dict[str, Any]] = []
    index = {c["clause_key"]: c for c in list(low["clauses"]) + list(high["clauses"])}
    for group in groups:
        low_keys, high_keys = _key_sets(group)
        for member in group["members"]:
            clause = index.get(member["clause_key"])
            if clause is None:
                issues.append({"kind": "locked_clause_missing", "group_id": group["group_id"],
                               "clause_key": member["clause_key"],
                               "version": member["version"]})
                continue
            if (clause["anchor"] != member["anchor"]
                    or clause["text_sha256"] != member["text_sha256"]):
                con.execute(
                    "UPDATE align_members SET anchor_json=?,text_sha256=?,ordinal=? "
                    "WHERE group_id=? AND side=? AND clause_key=?",
                    (json.dumps(clause["anchor"], ensure_ascii=False, sort_keys=True),
                     clause["text_sha256"], clause["ordinal"],
                     group["group_id"], member["side"], member["clause_key"]),
                )
        con.execute("UPDATE align_groups SET updated_at=? WHERE group_id=?",
                    (now, group["group_id"]))
    return issues


# ---------------------------------------------------------------------------
# 主对齐入口
# ---------------------------------------------------------------------------

def align_versions(a: Any, b: Any, *, con: sqlite3.Connection | None = None,
                   doc_id: str | None = None, version_a: str | None = None,
                   version_b: str | None = None,
                   auto_threshold: float = AUTO_THRESHOLD,
                   review_threshold: float = REVIEW_THRESHOLD,
                   candidate_floor: float = CANDIDATE_FLOOR,
                   auto_structural: bool = False) -> dict[str, Any]:
    """Align two version documents and persist groups, candidates and decisions.

    ``a`` / ``b`` may be full version documents, bare clause lists or a single
    parsed clause dict.  The result is independent of the argument order.
    """
    con, own = _ensure(con)
    try:
        doc_a = _prepare(a, doc_id=doc_id, version=version_a)
        doc_b = _prepare(b, doc_id=doc_id, version=version_b)
        if (doc_a["doc_id"], doc_a["version"], _doc_hash(doc_a)) == \
           (doc_b["doc_id"], doc_b["version"], _doc_hash(doc_b)):
            raise ValueError("cannot align a version with itself")
        low, high = _canonical_order(doc_a, doc_b)
        now = db.utcnow()
        with con:
            db.upsert_version(con, low, _doc_hash(low), now)
            db.upsert_version(con, high, _doc_hash(high), now)
            db.upsert_clause_refs(con, low)
            db.upsert_clause_refs(con, high)
        summary = _align_pair(con, low, high, now=now,
                              auto_threshold=auto_threshold,
                              review_threshold=review_threshold,
                              candidate_floor=candidate_floor,
                              auto_structural=auto_structural)
        con.commit()
        return summary
    finally:
        if own:
            con.close()


align = align_versions  # convenience alias


def _align_pair(con, low, high, *, now, auto_threshold, review_threshold,
                candidate_floor, auto_structural) -> dict[str, Any]:
    scorer = _Scorer(low["clauses"], high["clauses"])
    low_clauses, high_clauses = low["clauses"], high["clauses"]
    doc_id, peer_doc_id = low["doc_id"], high["doc_id"]
    low_version, high_version = low["version"], high["version"]

    protected = _protected_groups(con, doc_id, peer_doc_id, low_version, high_version)
    confirmed, rejected_pairs = _split_protected(protected)
    locked_low, locked_high = _locked_keys(confirmed)
    lock_issues = _refresh_locked_groups(con, confirmed, low, high, now)

    def rejected(low_keys: Iterable[str], high_keys: Iterable[str]) -> bool:
        low_set, high_set = frozenset(low_keys), frozenset(high_keys)
        return any(low_set == rl and high_set == rh for rl, rh, _ in rejected_pairs)

    def rejected_overlap(low_keys: Iterable[str], high_keys: Iterable[str]) -> list[Mapping[str, Any]]:
        """Rejected relations a new proposal would partially re-open.

        Rejecting ``A -> B`` only forbids that exact relation.  A proposal is a
        conflict when it keeps one side identical and re-expands the other
        (e.g. ``A -> {B, C}`` after ``A -> B`` was rejected), because that would
        silently override the human decision.  A genuinely different partner is
        allowed and arrives as its own candidate.
        """
        low_set, high_set = frozenset(low_keys), frozenset(high_keys)
        hits = []
        for rl, rh, group in rejected_pairs:
            same_low_reexpand = low_set == rl and high_set & rh
            same_high_reexpand = high_set == rh and low_set & rl
            if same_low_reexpand or same_high_reexpand:
                hits.append(group)
        return hits

    # --- structural hypotheses on unlocked clauses ------------------------
    hypo_low = [i for i, c in enumerate(low_clauses) if c["clause_key"] not in locked_low]
    hypo_high = [i for i, c in enumerate(high_clauses) if c["clause_key"] not in locked_high]
    sub_low = [low_clauses[i] for i in hypo_low]
    sub_high = [high_clauses[i] for i in hypo_high]
    if not locked_low and not locked_high:
        # No manual decisions to exclude: reuse the main scorer (and its cache).
        sub_scorer = scorer
    else:
        sub_scorer = _Scorer(sub_low, sub_high) if len(sub_low) and len(sub_high) else None
    structural_pairs = sub_scorer.structural_pairs() if sub_scorer else {}
    hypotheses = _structural_hypotheses(sub_low, sub_high, structural_pairs)
    merged = _merge_hypotheses(hypotheses, sub_low, sub_high)

    used_low: set[str] = set()
    used_high: set[str] = set()
    proposed: list[dict[str, Any]] = []

    for hypothesis in sorted(merged, key=lambda h: (-h["coverage"], -h["score"])):
        members_low = [sub_low[i] for i in hypothesis["low"]]
        members_high = [sub_high[i] for i in hypothesis["high"]]
        kinds = hypothesis["kind"]
        if kinds == "ambiguous":
            status = db.STATUS_PENDING
        elif (auto_structural and hypothesis["coverage"] >= COVERAGE_AUTO
              and hypothesis["score"] >= STRUCTURAL_MIN_SCORE):
            status = db.STATUS_AUTO
        else:
            status = db.STATUS_PENDING
        proposed.append({"kind": kinds, "low": members_low, "high": members_high,
                         "status": status, "score": hypothesis["score"],
                         "coverage": hypothesis["coverage"], "origin": "structural"})
        used_low.update(c["clause_key"] for c in members_low)
        used_high.update(c["clause_key"] for c in members_high)

    # --- one-to-one matching on remaining clauses -------------------------
    remaining_low = [i for i, c in enumerate(low_clauses)
                     if c["clause_key"] not in used_low and c["clause_key"] not in locked_low]
    remaining_high = [i for i, c in enumerate(high_clauses)
                      if c["clause_key"] not in used_high and c["clause_key"] not in locked_high]
    pair_scores: dict[tuple[str, str], float] = {}
    for li in remaining_low:
        for hi, score in scorer.candidates_for(li, candidate_floor):
            if high_clauses[hi]["clause_key"] in used_high or \
               high_clauses[hi]["clause_key"] in locked_high:
                continue
            lk, hk = low_clauses[li]["clause_key"], high_clauses[hi]["clause_key"]
            if rejected([lk], [hk]):
                continue
            pair_scores[(lk, hk)] = score

    # mutual-best first, then deterministic greedy over the rest
    best_high_for_low: dict[str, tuple[str, float]] = {}
    best_low_for_high: dict[str, tuple[str, float]] = {}
    for (lk, hk), score in pair_scores.items():
        if score > best_high_for_low.get(lk, ("", -1.0))[1]:
            best_high_for_low[lk] = (hk, score)
        if score > best_low_for_high.get(hk, ("", -1.0))[1]:
            best_low_for_high[hk] = (lk, score)

    ordered = sorted(pair_scores.items(), key=lambda item: (-item[1], item[0]))
    assigned_low: set[str] = set()
    assigned_high: set[str] = set()
    one_to_one: list[tuple[str, str, float]] = []

    for lk, (hk, score) in sorted(best_high_for_low.items(), key=lambda item: -item[1][1]):
        if best_low_for_high.get(hk, ("", -1.0))[0] == lk and \
           lk not in assigned_low and hk not in assigned_high and score >= review_threshold:
            one_to_one.append((lk, hk, score))
            assigned_low.add(lk)
            assigned_high.add(hk)
    for (lk, hk), score in ordered:
        if lk in assigned_low or hk in assigned_high:
            continue
        if score < review_threshold:
            continue
        # A pair is only accepted if it is not beaten by a materially better
        # rival sharing either side; this prevents one clause from being
        # assigned arbitrarily when another mapping is clearly stronger.
        rivals = [s for (ok, oh), s in pair_scores.items()
                  if (ok == lk and oh != hk) or (oh == hk and ok != lk)]
        if rivals and score < max(rivals) - MARGIN:
            continue
        one_to_one.append((lk, hk, score))
        assigned_low.add(lk)
        assigned_high.add(hk)

    low_by_key = {c["clause_key"]: c for c in low_clauses}
    high_by_key = {c["clause_key"]: c for c in high_clauses}
    for lk, hk, score in one_to_one:
        status = db.STATUS_AUTO if score >= auto_threshold else db.STATUS_PENDING
        proposed.append({"kind": "one_to_one", "low": [low_by_key[lk]],
                         "high": [high_by_key[hk]], "status": status,
                         "score": score, "coverage": 1.0, "origin": "similarity"})
        used_low.add(lk)
        used_high.add(hk)

    # --- persist groups ---------------------------------------------------
    conflicts: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    written: list[dict[str, Any]] = []
    for item in proposed:
        low_keys = sorted(c["clause_key"] for c in item["low"])
        high_keys = sorted(c["clause_key"] for c in item["high"])
        gid = db.group_id_for(doc_id, peer_doc_id, low_version, high_version,
                              low_keys, high_keys)
        if rejected(low_keys, high_keys):
            # A human said no to exactly this relation; never silently re-add it.
            suppressed.append({"group_id": gid, "kind": item["kind"],
                               "low_keys": low_keys, "high_keys": high_keys,
                               "score": item["score"], "coverage": item["coverage"],
                               "reason": "manual_rejection"})
            continue
        overlap = rejected_overlap(low_keys, high_keys)
        if overlap:
            conflicts.append({"group_id": gid, "kind": item["kind"],
                              "low_keys": low_keys, "high_keys": high_keys,
                              "score": item["score"], "coverage": item["coverage"],
                              "conflicts_with": sorted({g["group_id"] for g in overlap}),
                              "conflict_type": "rejected_overlap"})
            continue
        conflicting_locked = sorted({
            group["group_id"] for key in low_keys if key in locked_low
            for group in confirmed if key in _key_sets(group)[0]
        } | {
            group["group_id"] for key in high_keys if key in locked_high
            for group in confirmed if key in _key_sets(group)[1]
        })
        if conflicting_locked:
            conflicts.append({"group_id": gid, "kind": item["kind"],
                              "low_keys": low_keys, "high_keys": high_keys,
                              "score": item["score"], "coverage": item["coverage"],
                              "conflicts_with": conflicting_locked,
                              "conflict_type": "confirmed_overlap"})
            continue
        members = [_member(c, _SIDE_LOW,
                           "parent" if item["kind"] == "split" else
                           ("child" if item["kind"] == "merge" else ""))
                   for c in item["low"]]
        members += [_member(c, _SIDE_HIGH,
                            "child" if item["kind"] == "split" else
                            ("parent" if item["kind"] == "merge" else ""))
                    for c in item["high"]]
        db.write_group(
            con, group_id=gid, doc_id=doc_id, peer_doc_id=peer_doc_id,
            low_version=low_version, high_version=high_version,
            kind=item["kind"], status=item["status"], score=float(item["score"]),
            coverage=float(item["coverage"]), members=members, now=now,
        )
        candidate_id = db.candidate_id_for(doc_id, peer_doc_id, low_version, high_version,
                                           item["kind"], low_keys, high_keys)
        db.write_candidate(
            con, candidate_id=candidate_id, doc_id=doc_id, peer_doc_id=peer_doc_id,
            low_version=low_version, high_version=high_version, kind=item["kind"],
            score=float(item["score"]), coverage=float(item["coverage"]),
            status=item["status"], group_id=gid, low_keys=low_keys, high_keys=high_keys,
            detail={"origin": item["origin"], "low_anchors": [m["anchor"] for m in members
                                                              if m["side"] == _SIDE_LOW],
                    "high_anchors": [m["anchor"] for m in members if m["side"] == _SIDE_HIGH]},
            now=now,
        )
        written.append({"group_id": gid, "kind": item["kind"], "status": item["status"],
                        "score": item["score"], "coverage": item["coverage"],
                        "low_keys": low_keys, "high_keys": high_keys})

    # --- competing / low-confidence candidates ---------------------------
    selected = {(lk, hk) for lk, hk, _ in one_to_one}
    structural_keys = used_low | used_high
    for (lk, hk), score in ordered:
        if (lk, hk) in selected:
            continue
        low_clause, high_clause = low_by_key[lk], high_by_key[hk]
        explained = (lk in structural_keys or hk in structural_keys or
                     lk in assigned_low or hk in assigned_high or
                     lk in locked_low or hk in locked_high)
        if explained:
            reason = "already_matched"
        elif score >= review_threshold:
            reason = "competing"  # both sides free, but no confident winner
        else:
            reason = "low_confidence"
        # Only an unexplained, above-threshold pair is review work.  A losing
        # rival of an existing relation stays rejected so it cannot masquerade
        # as an undecided change.
        status = "pending" if reason == "competing" else "rejected"
        candidate_id = db.candidate_id_for(doc_id, peer_doc_id, low_version, high_version,
                                           "one_to_one", [lk], [hk])
        db.write_candidate(
            con, candidate_id=candidate_id, doc_id=doc_id, peer_doc_id=peer_doc_id,
            low_version=low_version, high_version=high_version, kind="one_to_one",
            score=score, coverage=1.0, status=status, group_id=None,
            low_keys=[lk], high_keys=[hk],
            detail={"origin": "similarity", "reason": reason,
                    "low_anchor": low_clause["anchor"], "high_anchor": high_clause["anchor"]},
            now=now,
        )

    # --- retire stale automatic groups -----------------------------------
    # Groups written by an earlier automatic pass that the current pass did
    # not reproduce are superseded.  They stay in storage for audit, but are
    # flagged ``superseded`` so compare/version only read the current mapping.
    # Manual decisions are never touched.
    current_ids = {item["group_id"] for item in written}
    superseded: list[str] = []
    for group in db.list_groups(con, doc_id=doc_id,
                                low_version=low_version, high_version=high_version):
        if group["group_id"] in current_ids:
            continue
        if group.get("manual") or group["status"] in db.MANUAL_STATUSES:
            continue
        db.mark_group_superseded(con, group["group_id"], now)
        superseded.append(group["group_id"])

    # --- resurfacing rejections -------------------------------------------
    # A rejected relation is excluded from matching above; if the automatic
    # pass would have proposed it again (score clears the review floor), record
    # that on the candidate without changing the rejected decision.
    for low_set, high_set, group in rejected_pairs:
        low_keys, high_keys = sorted(low_set), sorted(high_set)
        scores: list[float] = []
        for lk in low_set:
            for hk in high_set:
                li = next((i for i, c in enumerate(low_clauses) if c["clause_key"] == lk), None)
                hi = next((i for i, c in enumerate(high_clauses) if c["clause_key"] == hk), None)
                if li is not None and hi is not None:
                    scores.append(scorer.score(li, hi))
        if not scores or max(scores) < review_threshold:
            continue
        candidate_id = db.candidate_id_for(doc_id, peer_doc_id, low_version, high_version,
                                           group["kind"], low_keys, high_keys)
        db.mark_candidate_rejected_again(con, candidate_id, {
            "doc_id": doc_id, "peer_doc_id": peer_doc_id,
            "low_version": low_version, "high_version": high_version,
            "kind": group["kind"], "score": max(scores), "coverage": group["coverage"],
            "group_id": group["group_id"], "low_keys": low_keys, "high_keys": high_keys,
            "conflict_type": "manual_rejection", "resurfaced": True,
        }, now)

    for conflict in conflicts:
        candidate_id = db.candidate_id_for(doc_id, peer_doc_id, low_version, high_version,
                                           conflict["kind"], conflict["low_keys"],
                                           conflict["high_keys"])
        db.mark_candidate_conflict(con, candidate_id, {
            "doc_id": doc_id, "peer_doc_id": peer_doc_id,
            "low_version": low_version, "high_version": high_version,
            "kind": conflict["kind"], "score": conflict["score"],
            "coverage": conflict["coverage"], "group_id": conflict["group_id"],
            "low_keys": conflict["low_keys"], "high_keys": conflict["high_keys"],
            "conflicts_with": conflict["conflicts_with"],
            "conflict_type": conflict.get("conflict_type", "confirmed_overlap"),
        }, now)

    for item in suppressed:
        candidate_id = db.candidate_id_for(doc_id, peer_doc_id, low_version, high_version,
                                           item["kind"], item["low_keys"], item["high_keys"])
        db.mark_candidate_rejected_again(con, candidate_id, {
            "doc_id": doc_id, "peer_doc_id": peer_doc_id,
            "low_version": low_version, "high_version": high_version,
            "kind": item["kind"], "score": item["score"], "coverage": item["coverage"],
            "group_id": item["group_id"], "low_keys": item["low_keys"],
            "high_keys": item["high_keys"], "conflict_type": "manual_rejection",
            "resurfaced": True,
        }, now)

    # --- summary ----------------------------------------------------------
    matched_low = {k for item in written for k in item["low_keys"]}
    matched_high = {k for item in written for k in item["high_keys"]}
    auto = sum(1 for w in written if w["status"] == db.STATUS_AUTO)
    pending = sum(1 for w in written if w["status"] == db.STATUS_PENDING)
    return {
        "doc_id": doc_id,
        "peer_doc_id": peer_doc_id,
        "low_version": low_version,
        "high_version": high_version,
        "pair": [low_version, high_version],
        "groups": len(written),
        "accepted": auto,
        "pending": pending,
        "conflicts": len(conflicts),
        "suppressed": len(suppressed),
        "groups_detail": written,
        "unmatched_low": [c["clause_key"] for c in low_clauses
                          if c["clause_key"] not in matched_low and c["clause_key"] not in locked_low],
        "unmatched_high": [c["clause_key"] for c in high_clauses
                           if c["clause_key"] not in matched_high and c["clause_key"] not in locked_high],
        "manual_groups_preserved": len(confirmed),
        "lock_issues": lock_issues,
    }


def align_many(docs: Sequence[Any], *, con: sqlite3.Connection | None = None,
               doc_id: str | None = None, auto_structural: bool = False) -> list[dict[str, Any]]:
    """Align every version pair of a document (order independent).

    Duplicate version labels are rejected rather than silently collapsed: two
    documents claiming the same version cannot be ordered, so pairing them would
    be arbitrary.  Use distinct version ids (e.g. ``2024-r2``).
    """
    con, own = _ensure(con)
    try:
        prepared = [_prepare(doc, doc_id=doc_id) for doc in docs]
        by_version: dict[str, dict[str, Any]] = {}
        for doc in prepared:
            if doc["version"] in by_version:
                raise ValueError("duplicate version id: %r" % doc["version"])
            by_version[doc["version"]] = doc
        ordered = sorted(by_version.values(), key=lambda d: _natural_key(d["version"]))
        results = []
        for i in range(len(ordered) - 1):
            results.append(align_versions(ordered[i], ordered[i + 1], con=con,
                                          auto_structural=auto_structural))
        return results
    finally:
        if own:
            con.close()


# ---------------------------------------------------------------------------
# 人工确认 / 拒绝
# ---------------------------------------------------------------------------

def find_group(con: sqlite3.Connection, *, group_id: str | None = None,
               doc_id: str | None = None, low_version: str | None = None,
               high_version: str | None = None,
               low_keys: Iterable[str] | None = None,
               high_keys: Iterable[str] | None = None) -> dict[str, Any] | None:
    db.ensure_schema(con)
    if group_id:
        group = db.get_group(con, group_id)
        if group is not None:
            group["members"] = db.group_members(con, group_id)
        return group
    candidates = db.list_groups(con, doc_id=doc_id,
                                low_version=low_version, high_version=high_version)
    want_low = frozenset(low_keys or ())
    want_high = frozenset(high_keys or ())
    for group in candidates:
        low, high = _key_sets(group)
        if want_low <= low and want_high <= high:
            return group
    return None


def _resolve_group(con, group_id=None, **kwargs) -> dict[str, Any]:
    group = find_group(con, group_id=group_id, **kwargs)
    if not group:
        raise KeyError("alignment group not found")
    return group


def confirm(*, con: sqlite3.Connection | None = None, group_id: str | None = None,
            actor: str = "human", note: str | None = None,
            crash_after_journal: bool = False, **lookup) -> dict[str, Any]:
    """Confirm a group (or the group matching the given clause keys).

    ``crash_after_journal=True`` simulates a process dying between the two
    phases; the journal is left ``pending`` for :func:`recover`.
    """
    con, own = _ensure(con)
    try:
        group = _resolve_group(con, group_id, **lookup)
        op_id = db.begin_decision(con, group["group_id"], "confirm", actor, note)
        if crash_after_journal:
            return {"op_id": op_id, "state": "pending", "group_id": group["group_id"],
                    "simulated_crash": True}
        result = db.apply_decision(con, op_id)
        con.commit()
        result["group_id"] = group["group_id"]
        return result
    finally:
        if own:
            con.close()


def reject(*, con: sqlite3.Connection | None = None, group_id: str | None = None,
           actor: str = "human", note: str | None = None,
           crash_after_journal: bool = False, **lookup) -> dict[str, Any]:
    con, own = _ensure(con)
    try:
        group = _resolve_group(con, group_id, **lookup)
        op_id = db.begin_decision(con, group["group_id"], "reject", actor, note)
        if crash_after_journal:
            return {"op_id": op_id, "state": "pending", "group_id": group["group_id"],
                    "simulated_crash": True}
        result = db.apply_decision(con, op_id)
        con.commit()
        result["group_id"] = group["group_id"]
        return result
    finally:
        if own:
            con.close()


def recover(con: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    con, own = _ensure(con)
    try:
        outcomes = db.recover(con)
        con.commit()
        return outcomes
    finally:
        if own:
            con.close()


def promote_candidate(candidate_id: str, *, con: sqlite3.Connection | None = None,
                      actor: str = "human", note: str | None = None) -> dict[str, Any]:
    """Turn a stored candidate (pending or rejected) into a manual group."""
    con, own = _ensure(con)
    try:
        rows = [row for row in db.list_candidates(con) if row["candidate_id"] == candidate_id]
        if not rows:
            raise KeyError("candidate not found: %s" % candidate_id)
        candidate = rows[0]
        low_keys = candidate["low_keys"]
        high_keys = candidate["high_keys"]
        existing = _protected_groups(con, candidate["doc_id"], candidate["peer_doc_id"],
                                     candidate["low_version"], candidate["high_version"])
        for group in existing:
            low, high = _key_sets(group)
            if set(low_keys) & low or set(high_keys) & high:
                raise ValueError("candidate overlaps manual group %s" % group["group_id"])
        members = []
        for key in low_keys:
            ref = db.clause_ref(con, candidate["doc_id"], candidate["low_version"], key)
            if not ref:
                raise KeyError("clause reference missing: %s" % key)
            members.append(_member({**ref, "clause_key": key, "clause_id": ref["clause_id"],
                                    "version": candidate["low_version"],
                                    "doc_id": candidate["doc_id"], "ordinal": ref["ordinal"],
                                    "anchor": ref["anchor"],
                                    "text_sha256": ref["text_sha256"]}, _SIDE_LOW))
        for key in high_keys:
            ref = db.clause_ref(con, candidate["peer_doc_id"], candidate["high_version"], key)
            if not ref:
                raise KeyError("clause reference missing: %s" % key)
            members.append(_member({**ref, "clause_key": key, "clause_id": ref["clause_id"],
                                    "version": candidate["high_version"],
                                    "doc_id": candidate["peer_doc_id"],
                                    "ordinal": ref["ordinal"], "anchor": ref["anchor"],
                                    "text_sha256": ref["text_sha256"]}, _SIDE_HIGH))
        gid = db.group_id_for(candidate["doc_id"], candidate["peer_doc_id"],
                              candidate["low_version"], candidate["high_version"],
                              low_keys, high_keys)
        now = db.utcnow()
        with con:
            db.write_group(con, group_id=gid, doc_id=candidate["doc_id"],
                           peer_doc_id=candidate["peer_doc_id"],
                           low_version=candidate["low_version"],
                           high_version=candidate["high_version"],
                           kind=candidate["kind"], status=db.STATUS_CONFIRMED,
                           score=candidate["score"], coverage=candidate["coverage"],
                           members=members, now=now)
            con.execute(
                "UPDATE align_groups SET manual=1,decision_actor=?,decision_note=?,"
                "decision_at=? WHERE group_id=?", (actor, note, now, gid))
            con.execute(
                "INSERT INTO align_decisions(decision_id,group_id,action,actor,note,"
                "previous_status,new_status,op_id,created_at) VALUES(?,?,?,?,?,?,?,NULL,?) "
                "ON CONFLICT(decision_id) DO NOTHING",
                ("d-" + tx.stable_hash(gid, "promote", actor, now, length=20), gid,
                 "confirm", actor, note, candidate["status"], db.STATUS_CONFIRMED, now))
            con.execute("UPDATE align_candidates SET status='confirmed',group_id=? "
                        "WHERE candidate_id=?", (gid, candidate_id))
        con.commit()
        return {"group_id": gid, "status": db.STATUS_CONFIRMED, "candidate_id": candidate_id}
    finally:
        if own:
            con.close()


def dismiss_candidate(candidate_id: str, *, con: sqlite3.Connection | None = None,
                      actor: str = "human", note: str | None = None) -> dict[str, Any]:
    """Manually reject a standalone candidate (no group to reject)."""
    con, own = _ensure(con)
    try:
        now = db.utcnow()
        with con:
            updated = con.execute(
                "UPDATE align_candidates SET status='rejected',last_seen=? "
                "WHERE candidate_id=?", (now, candidate_id)).rowcount
        con.commit()
        if not updated:
            raise KeyError("candidate not found: %s" % candidate_id)
        return {"candidate_id": candidate_id, "status": "rejected", "actor": actor, "note": note}
    finally:
        if own:
            con.close()


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def pending(con: sqlite3.Connection | None = None, *, doc_id: str | None = None,
            low_version: str | None = None, high_version: str | None = None) -> dict[str, Any]:
    con, own = _ensure(con)
    try:
        return {
            "groups": db.list_groups(con, doc_id=doc_id, low_version=low_version,
                                     high_version=high_version, statuses=[db.STATUS_PENDING]),
            "candidates": db.list_candidates(con, doc_id=doc_id, low_version=low_version,
                                             high_version=high_version, statuses=["pending"]),
        }
    finally:
        if own:
            con.close()


def pending_work(con: sqlite3.Connection | None = None, **filters) -> dict[str, Any]:
    """Alias for :func:`pending`, kept explicit for review queues."""
    return pending(con, **filters)


def candidates(con: sqlite3.Connection | None = None, **filters) -> list[dict[str, Any]]:
    con, own = _ensure(con)
    try:
        return db.list_candidates(con, **filters)
    finally:
        if own:
            con.close()


def groups(con: sqlite3.Connection | None = None, **filters) -> list[dict[str, Any]]:
    con, own = _ensure(con)
    try:
        return db.list_groups(con, **filters)
    finally:
        if own:
            con.close()


def version_map(con: sqlite3.Connection | None = None, *, doc_id: str | None = None,
                low_version: str | None = None, high_version: str | None = None,
                include_pending: bool = True,
                include_rejected: bool = False) -> dict[str, str]:
    """Return ``{older_clause_key: newer_clause_key}`` for one-to-one mappings."""
    mapping: dict[str, str] = {}
    allowed = {db.STATUS_AUTO, db.STATUS_CONFIRMED}
    if include_pending:
        allowed.add(db.STATUS_PENDING)
    if include_rejected:
        allowed.add(db.STATUS_REJECTED)
    for group in groups(con, doc_id=doc_id, low_version=low_version,
                        high_version=high_version):
        if group["status"] not in allowed or group["kind"] != "one_to_one":
            continue
        low = [m["clause_key"] for m in group["members"] if m["side"] == _SIDE_LOW]
        high = [m["clause_key"] for m in group["members"] if m["side"] == _SIDE_HIGH]
        if len(low) == 1 and len(high) == 1:
            mapping[low[0]] = high[0]
    return mapping


# ---------------------------------------------------------------------------
# 流水线阶段入口
# ---------------------------------------------------------------------------

def process(record: Mapping[str, Any], con: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Pipeline stage entry.

    A plain record (no alignment payload) is passed through with an ``_stage_align``
    marker so the existing happy-path stays intact.  When a record carries an
    ``align`` payload (or ``low``/``high`` documents) the pair is aligned.
    """
    rec = dict(record)
    payload = rec.get("align")
    if payload is None and "low" in rec and "high" in rec:
        payload = {"low": rec["low"], "high": rec["high"]}
    if payload is None and "versions" in rec:
        versions = rec.get("versions") or []
        if len(versions) >= 2:
            payload = {"versions": versions}
    if payload is None:
        rec.setdefault("_stage_align", "ok")
        return rec

    if isinstance(payload, Mapping) and "versions" in payload:
        results = []
        versions = list(payload.get("versions") or [])
        for left, right in zip(versions, versions[1:]):
            results.append(align_versions(left, right, con=con,
                                          doc_id=payload.get("doc_id", rec.get("doc_id")),
                                          auto_structural=payload.get("auto_structural", False)))
        rec["_align"] = {"pairs": results}
    else:
        rec["_align"] = align_versions(
            payload.get("low", payload.get("a")),
            payload.get("high", payload.get("b")),
            con=con, doc_id=payload.get("doc_id", rec.get("doc_id")),
            auto_structural=payload.get("auto_structural", False),
        )
    rec["_stage_align"] = "ok"
    return rec

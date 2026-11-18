"""compare 阶段：变化分类三桶、原文证据、口径与不完整版本语义。

compare 只消费 clause 的条款与 align 的对齐关系，不自行做结构匹配。
"""
import pytest

from policydiff import align, compare
from policydiff.compare import (
    SUBSTANTIVE, FORMAT, UNCERTAIN,
    ADDED, REMOVED, REPLACED, THRESHOLD_CHANGED, MERGED, SPLIT,
    REORDERED, FORMATTING_ONLY,
    UNKNOWN_REMOVAL, UNKNOWN_ADDITION, UNCERTAIN_PAIR,
)

from _helpers import prepare


def _kinds(report, status=None):
    return [(it["status"], it["kind"]) for it in report["items"]
            if status is None or it["status"] == status]


def _by_kind(report, kind):
    return [it for it in report["items"] if it["kind"] == kind]


def _diff(a, b):
    return compare.compare_documents(a, b, align.align_pair(a, b))


# ------------------------------------------------------------ 跨年度
def test_cross_year_threshold_and_reorder_classified():
    a, b = prepare("hangzhou_year.json")
    rep = _diff(a, b)

    # 两个阈值变化：3万→5万（money），35岁→40岁（age）
    changed = _by_kind(rep, THRESHOLD_CHANGED)
    dims = {d["dim"] for it in changed for d in it["detail"]["threshold_deltas"]}
    assert {"money", "age"} <= dims
    for it in changed:
        assert it["status"] == SUBSTANTIVE
        assert it["evidence_a"]["quote"] and it["evidence_b"]["quote"]

    # 条款重排（第二条/第三条交换次序）→ 格式噪声，不进实质桶
    reordered = _by_kind(rep, REORDERED)
    assert reordered and all(it["status"] == FORMAT for it in reordered)

    # 三桶计数自洽
    counts = rep["summary"]
    assert sum(counts.values()) == len(rep["items"])
    assert counts[FORMAT] >= 1 and counts[SUBSTANTIVE] >= 2


def test_threshold_delta_is_locatable_and_base_aware():
    a, b = prepare("hangzhou_year.json")
    rep = _diff(a, b)
    money = next(it for it in _by_kind(rep, THRESHOLD_CHANGED)
                 for d in it["detail"]["threshold_deltas"]
                 if d["dim"] == "money")
    delta = next(d for d in money["detail"]["threshold_deltas"]
                 if d["dim"] == "money")
    assert delta["a_base"] == 30000.0 and delta["b_base"] == 50000.0
    assert "3万" in delta["a"] and "5万" in delta["b"]


# ------------------------------------------------------------ 纯格式噪声
def test_formatting_noise_not_substantive():
    a, b = prepare("format_noise.json")
    rep = _diff(a, b)
    assert rep["summary"][SUBSTANTIVE] == 0
    assert _by_kind(rep, FORMATTING_ONLY)
    assert all(it["status"] == FORMAT for it in rep["items"])


# ------------------------------------------------------------ 单位口径
def test_unit_caliber_incomparable_is_uncertain():
    a, b = prepare("unit_caliber.json")
    rep = _diff(a, b)
    assert rep["summary"][SUBSTANTIVE] == 0
    pair = _by_kind(rep, UNCERTAIN_PAIR)
    assert pair and pair[0]["detail"]["reason"] == "unit_caliber_incomparable"
    assert pair[0]["status"] == UNCERTAIN


def test_same_value_different_money_unit_is_not_threshold_change():
    a = prepare_clause("第一条 补贴为3万元。", "m1")
    b = prepare_clause("第一条 补贴为30000元。", "m2")
    rep = compare.compare_documents(a, b)
    assert not _by_kind(rep, THRESHOLD_CHANGED)


def prepare_clause(text, doc_id):
    from _helpers import prepare_one
    return prepare_one({"doc_id": doc_id, "text": text})


# ------------------------------------------------------------ 合并/拆分
def test_merge_and_split_with_evidence():
    docs = prepare("merge_split.json")
    merged_rep = _diff(docs[0], docs[1])
    m = _by_kind(merged_rep, MERGED)
    assert len(m) == 1 and m[0]["status"] == SUBSTANTIVE
    assert isinstance(m[0]["evidence_a"], list) and len(m[0]["evidence_a"]) == 2
    assert m[0]["evidence_b"]["quote"]

    split_rep = _diff(docs[1], docs[0])
    s = _by_kind(split_rep, SPLIT)
    assert len(s) == 1 and s[0]["status"] == SUBSTANTIVE
    assert len(s[0]["evidence_b"]) == 2


# ------------------------------------------------------------ 新增/删除
def test_added_and_removed_against_complete_versions():
    from _helpers import prepare_one
    a = prepare_one({"doc_id": "full-a", "clauses": [
        {"number": "第一条", "text": "补贴额度为3万元。"},
        {"number": "第二条", "text": "租房补贴比例为50%。"},
    ]})
    b = prepare_one({"doc_id": "full-b", "clauses": [
        {"number": "第一条", "text": "补贴额度为3万元。"},
        {"number": "第二条", "text": "创业资助一次性20万元。"},
    ]})
    rep = _diff(a, b)
    assert _by_kind(rep, REMOVED) and _by_kind(rep, ADDED)
    assert all(it["status"] == SUBSTANTIVE
               for it in _by_kind(rep, REMOVED) + _by_kind(rep, ADDED))


# ------------------------------------------------------------ 不完整版本
def test_incomplete_target_turns_removal_into_unknown():
    docs = prepare("incomplete_version.json")
    rep = _diff(docs[0], docs[1])
    # 对照版不完整 → 无法断言删除
    unknown = _by_kind(rep, UNKNOWN_REMOVAL)
    assert unknown and all(it["status"] == UNCERTAIN for it in unknown)
    assert not _by_kind(rep, REMOVED)
    # 缺段占位同样只能落证据不足
    assert _by_kind(rep, UNKNOWN_ADDITION)
    # 仍能确定的实质变化（2万→3万）照常给出
    assert _by_kind(rep, THRESHOLD_CHANGED)


def test_complete_target_allows_definite_removal():
    from _helpers import prepare_one
    a = prepare_one({"doc_id": "ca", "clauses": [
        {"number": "第一条", "text": "购房补贴50万元。"}]})
    b = prepare_one({"doc_id": "cb", "clauses": [
        {"number": "第一条", "text": "租房补贴比例50%。"}]})
    rep = _diff(a, b)
    assert _by_kind(rep, REMOVED)
    assert not _by_kind(rep, UNKNOWN_REMOVAL)


# ------------------------------------------------------------ 低置信对齐
def test_low_confidence_pair_is_uncertain_not_asserted():
    from _helpers import prepare_one
    a = prepare_one({"doc_id": "la", "clauses": [
        {"number": "第一条", "text": "完全无关的林业采伐限额规则。"}]})
    b = prepare_one({"doc_id": "lb", "clauses": [
        {"number": "第一条", "text": "海上渔业养殖补贴发放规则。"}]})
    rep = _diff(a, b)
    if _by_kind(rep, UNCERTAIN_PAIR):
        assert all(it["status"] == UNCERTAIN
                   for it in _by_kind(rep, UNCERTAIN_PAIR))


# ------------------------------------------------------------ 跨地区
def test_cross_region_pair_key_and_buckets():
    hz = prepare("hangzhou_year.json")[0]
    sx = prepare("shaoxing_region.json")[0]
    rep = _diff(hz, sx)
    assert rep["pair_key"].startswith("cmp:")
    assert set(rep["summary"]) == {SUBSTANTIVE, FORMAT, UNCERTAIN}
    # 每条结论都可回链原文
    for it in rep["items"]:
        for ev in (it["evidence_a"], it["evidence_b"]):
            if isinstance(ev, dict):
                assert ev["quote"] and ev["cid"]


# ------------------------------------------------------------ 可重复生成
def test_report_is_reproducible():
    a, b = prepare("hangzhou_year.json")
    r1 = _diff(a, b)
    r2 = _diff(a, b)
    assert r1 == r2
    # 交换输入顺序，pair_key 对称且结论集合一致（方向标签互换）
    rev = _diff(b, a)
    assert rev["pair_key"] == r1["pair_key"]
    assert rev["summary"] == r1["summary"]


def test_compare_consumes_alignment_without_re_matching():
    # 显式传入外部 alignment，compare 必须原样消费其配对关系
    a, b = prepare("hangzhou_year.json")
    forced = {"pairs": [], "merged": [], "split": [],
              "a_only": [c["cid"] for c in a["clauses"]],
              "b_only": [c["cid"] for c in b["clauses"]]}
    rep = compare.compare_documents(a, b, alignment=forced)
    # 没有任何配对 → 不会凭空产生 replaced/threshold 结论
    assert not _by_kind(rep, REPLACED)
    assert not _by_kind(rep, THRESHOLD_CHANGED)

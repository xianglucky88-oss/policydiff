"""align 阶段：对齐置信度、确定性、合并/拆分与占位排除。"""
from policydiff import align

from _helpers import prepare


def test_align_is_deterministic_and_symmetric_in_keys():
    a, b = prepare("hangzhou_year.json")
    r1 = align.align_pair(a, b)
    r2 = align.align_pair(a, b)
    assert r1 == r2                      # 重复生成完全一致
    rev = align.align_pair(b, a)
    assert {p["a_cid"] for p in rev["pairs"]} == \
        {p["b_cid"] for p in r1["pairs"]}


def test_align_confidence_levels_present():
    a, b = prepare("hangzhou_year.json")
    res = align.align_pair(a, b)
    confs = {p["confidence"] for p in res["pairs"]}
    assert confs <= {"high", "medium", "low"}
    assert res["pairs"]  # 同主题条款全部建立了配对


def test_merge_detection_many_a_to_one_b():
    docs = prepare("merge_split.json")
    res = align.align_pair(docs[0], docs[1])
    assert len(res["merged"]) == 1
    m = res["merged"][0]
    assert len(m["a_cids"]) == 2 and m["b_cid"]
    assert m["confidence"] in {"high", "medium", "low"}


def test_split_detection_one_a_to_many_b():
    docs = prepare("merge_split.json")
    # 反向：合并版在 A，拆分版在 B
    res = align.align_pair(docs[1], docs[0])
    assert len(res["split"]) == 1
    assert len(res["split"][0]["b_cids"]) == 2


def test_placeholder_clauses_excluded_from_alignment():
    docs = prepare("incomplete_version.json")
    res = align.align_pair(docs[0], docs[1])
    placeholder_cids = {c["cid"] for c in docs[1]["clauses"]
                        if c.get("placeholder")}
    involved = {p["b_cid"] for p in res["pairs"]}
    assert not (placeholder_cids & involved)

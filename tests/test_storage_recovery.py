"""结果落库、幂等重跑、部分失败恢复与旧版输入兼容。"""
import pytest

from policydiff import compare, store
from policydiff.compare import SUBSTANTIVE, UNCERTAIN

from _helpers import load_fixture, prepare


@pytest.fixture()
def con(tmp_path):
    c = store.connect(tmp_path / "t.db")
    yield c
    c.close()


def test_report_roundtrip_and_idempotent(con):
    a, b = prepare("hangzhou_year.json")
    rep = compare.compare_documents(a, b)
    store.save_diff_report(con, rep["pair_key"], rep)
    con.commit()
    assert store.diff_count(con) == 1

    # 重跑同输入 → 同一稳定键覆盖，不增行
    rep2 = compare.compare_documents(a, b)
    store.save_diff_report(con, rep2["pair_key"], rep2)
    con.commit()
    assert store.diff_count(con) == 1
    loaded = store.load_diff_report(con, rep["pair_key"])
    assert loaded["items"] == rep["items"]


def test_compare_many_partial_failure_recovery(con):
    good_a, good_b = prepare("hangzhou_year.json")
    pairs = [
        (good_a, good_b),
        (object(), object()),          # 不可解析的坏输入，触发隔离失败
    ]
    result = compare.compare_many(pairs, con)
    assert len(result["ok"]) == 1
    assert len(result["failed"]) == 1 and result["failed"][0]["error"]
    # 成功对已独立提交，坏对不污染库
    assert store.diff_count(con) == 1

    # 重跑：修复后失败对可重新计算并补齐（可重复生成）
    fixed = compare.compare_many([(good_a, good_b)], con)
    assert fixed["failed"] == [] and store.diff_count(con) == 1


def test_pair_key_is_symmetric_and_stable(con):
    a, b = prepare("hangzhou_year.json")
    k1 = compare.pair_key(a, b)
    k2 = compare.pair_key(b, a)
    assert k1 == k2 and k1.startswith("cmp:")


def test_compare_process_entry_saves_via_record_model(con):
    # process 入口挂报告，再走现有 Record/store 通道保存
    a, b = prepare("format_noise.json")
    rec = compare.process({"id": "job-1", "_compare": {"a": a, "b": b}})
    assert rec["_stage_compare"] == "ok"
    report = rec["diff_report"]
    store.upsert(con, __import__("policydiff.models", fromlist=["Record"])
                 .Record(key=rec["_pair_key"], data=rec))
    con.commit()
    assert store.count(con) == 1
    assert report["summary"][SUBSTANTIVE] == 0


def test_legacy_records_still_flow_through_compare_process():
    rec = compare.process({"id": "r-legacy", "name": "Old", "value": 1})
    assert rec["id"] == "r-legacy"
    assert rec.get("diff_report") is None


def test_legacy_inputs_produce_diff_set_with_evidence(con):
    # 旧版输入（无 doc_id/clauses 的朴素记录）也能形成差异集合
    old_a = {"id": "old-1", "src": "alpha", "name": "补贴额度3万元", "value": 3}
    old_b = {"id": "old-2", "src": "alpha", "name": "补贴额度5万元", "value": 5}
    rep = compare.compare_records(old_a, old_b)
    assert rep["items"]
    for it in rep["items"]:
        ev = it["evidence_a"] or it["evidence_b"]
        assert ev and ev["cid"]

"""clause 阶段：条款边界、稳定标识与阈值/主题抽取。"""
from policydiff import clause, doc_parse

from _helpers import prepare, prepare_one


def test_clause_boundary_splits_numbered_text():
    docs = prepare("hangzhou_year.json")
    clauses = docs[0]["clauses"]
    assert [c["number"] for c in clauses] == \
        ["第一条", "第二条", "第三条", "第四条"]
    assert [c["order"] for c in clauses] == [0, 1, 2, 3]
    # 原文完整保留，作为证据
    assert clauses[0]["text"].startswith("第一条 高层次人才安家补贴")


def test_cid_is_stable_across_runs_and_orders():
    d1 = prepare_one({"doc_id": "d", "text": "第一条 补贴为3万元。"})
    d2 = prepare_one({"doc_id": "d", "text": "第一条 补贴为3万元。"})
    assert d1["clauses"][0]["cid"] == d2["clauses"][0]["cid"]
    # 同内容不同 doc_id → 不同 cid（cid 绑定文档）
    d3 = prepare_one({"doc_id": "other", "text": "第一条 补贴为3万元。"})
    assert d3["clauses"][0]["cid"] != d1["clauses"][0]["cid"]


def test_threshold_extraction_units_and_base():
    doc = prepare_one({
        "doc_id": "th",
        "text": "第一条 补贴3万元，比例50%，30日内发放，每年1次，年龄不超过35周岁。",
    })
    th = {t["dim"]: t for t in doc["clauses"][0]["thresholds"]}
    assert th["money"]["base"] == 30000.0       # 万元 → 元
    assert th["percent"]["base"] == 50.0
    assert th["days"]["base"] == 30.0
    assert th["times"]["base"] == 1.0
    assert th["age"]["base"] == 35.0


def test_effective_date_is_not_a_threshold():
    doc = prepare_one({"doc_id": "ed",
                       "text": "第四条 本办法自2024年1月1日起施行。"})
    assert doc["clauses"][0]["thresholds"] == []


def test_cn_number_and_caliber_base():
    doc = prepare_one({"doc_id": "cn", "text": "第一条 补贴为一万元，期限三十日。"})
    th = {t["dim"]: t for t in doc["clauses"][0]["thresholds"]}
    # “一万”+“万元”→ 基准单位（元）下为 10000；中文数字三十 → 30
    assert th["money"]["base"] == 10000
    assert th["days"]["value"] == 30


def test_prestructured_clauses_and_placeholder_flag():
    doc = clause.process({
        "doc_id": "p",
        "clauses": [{"number": "第一条", "text": "补贴为3万元。"},
                    {"number": "缺失", "text": "缺"}],
    })
    assert doc["clauses"][0]["placeholder"] is False
    assert doc["clauses"][1]["placeholder"] is True


def test_legacy_record_falls_back_to_single_clause():
    rec = clause.process({"id": "r-001", "name": "Record One", "value": 10})
    assert len(rec["clauses"]) == 1
    assert "Record One" in rec["clauses"][0]["text"]

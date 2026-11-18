"""跨阶段数据契约测试：doc_parse 传递证据、clause 边界稳定、
compare 实质差异敏感、规则版本随记录落库、流水线幂等与旧字段兼容。"""
import pytest

from policydiff import normalize, doc_parse, clause, compare, version, pipeline, store


def pipe(rec, rv=normalize.RULE_VERSION):
    r = normalize.process(rec, rv)
    for mod in (doc_parse, clause, compare, version):
        r = mod.process(r)
    return r


BASE = "第一条　补贴金额不低于 ￥1，000.00 元（含），不得减扣。"
# 仅编码/空白噪声不同的对照文本
NOISY = "第一条 补贴金额不低于 ￥1，000.00 元（含），不得​减扣。"


def test_doc_parse_carries_full_provenance():
    r = pipe({"id": "a", "text": BASE})
    prov = r["_provenance"]["text"]
    norm = r["_norm"]["text"]
    assert prov["rule_version"] == norm["rule_version"]
    assert prov["map"] == norm["map"]
    assert prov["dropped"] == norm["dropped"]
    assert prov["folds"] == norm["folds"]
    # doc_parse 提供输出区间到原文区间的翻译
    s, e = doc_parse.source_span(r, "text", 0, 3)
    assert r["_raw"]["text"][s:e].startswith("第")


def test_noise_only_variants_are_equivalent():
    a = pipe({"id": "a", "text": BASE})
    b = pipe({"id": "b", "text": NOISY})
    assert a["_compare"]["text"]["text_fp"] == b["_compare"]["text"]["text_fp"]
    assert compare.diff_records(a, b)["fields"]["text"]["changed"] is False


@pytest.mark.parametrize("mutated", [
    "第一条　补贴金额不低于 ￥2，000.00 元（含），不得减扣。",   # 金额
    "第一条　补贴金额不低于 ￥1，000.00 万元（含），不得减扣。",  # 单位
    "第一条　补贴金额低于 ￥1，000.00 元（含），不得减扣。",      # 否定词
    "第一条　补贴金额不低于 ￥1，000.00 元，不得减扣。",          # 括号
    "第一条　补贴金额不低于 ￥1，000.00 元（含），减扣。",        # 否定词删除
])
def test_substantive_change_is_detected(mutated):
    a = pipe({"id": "a", "text": BASE})
    m = pipe({"id": "m", "text": mutated})
    d = compare.diff_records(a, m)
    assert d["fields"]["text"]["changed"] is True
    assert d["fields"]["text"]["a_fp"] != d["fields"]["text"]["b_fp"]


def test_minus_sign_change_is_detected():
    a = pipe({"id": "a", "text": "温度 −5°C"})
    b = pipe({"id": "b", "text": "温度 5°C"})
    assert compare.diff_records(a, b)["fields"]["text"]["changed"] is True


def test_ocr_char_is_significant():
    a = pipe({"id": "a", "text": "金额 100 � 元"})
    b = pipe({"id": "b", "text": "金额 100 元"})
    assert compare.diff_records(a, b)["fields"]["text"]["changed"] is True


def test_clause_boundaries_stable_across_noise():
    a = pipe({"id": "a", "text": "条款一\n条款二\n条款三"})
    b = pipe({"id": "b", "text": "条款一\r\n\r\n条款二\n\n条款三\n"})
    ta = [c["text"] for c in a["_clauses"]]
    tb = [c["text"] for c in b["_clauses"]]
    assert ta == tb == ["条款一", "条款二", "条款三"]
    # 条款编号稳定，且带原文回看区间
    assert [c["id"] for c in a["_clauses"]] == ["text#c000", "text#c001", "text#c002"]
    for c in b["_clauses"]:
        s, e = c["src_span"]
        assert c["text"] in b["_raw"]["text"][s:e] or b["_raw"]["text"][s:e].strip() == c["text"]


def test_diff_reports_per_clause_spans():
    a = pipe({"id": "a", "text": "第一款\n不予补贴"})
    b = pipe({"id": "b", "text": "第一款\n予以补贴"})
    d = compare.diff_records(a, b)
    clauses = d["fields"]["text"]["clauses"]
    assert clauses[0]["changed"] is False
    assert clauses[1]["changed"] is True
    assert clauses[1]["a"] == "不予补贴" and clauses[1]["b"] == "予以补贴"
    assert clauses[1]["a_src_span"] is not None


def test_rule_version_pinned_and_replayable():
    sq = chr(0x201C) + "重点" + chr(0x201D) + "项目"
    old = pipe({"id": "old", "text": sq}, "1.0.0")
    new = pipe({"id": "new", "text": sq}, "1.1.0")
    assert old["_rules_versions"] == ["1.0.0"]
    assert new["_rules_versions"] == ["1.1.0"]
    # 升级改变指纹（下游能发现），但两者原文都可回看
    assert old["_compare"]["text"]["text_fp"] != new["_compare"]["text"]["text_fp"]
    assert normalize.recover_record(old)["text"] == sq
    assert normalize.recover_record(new)["text"] == sq


def test_pipeline_runs_and_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr("policydiff.config.DB_PATH", db)
    a = pipeline.run()
    b = pipeline.run()
    assert a["fed"] == 4
    assert b["rows"] == a["rows"] == 3  # r-003 重复去重
    con = store.connect()
    try:
        rows = dict(store.all_records(con))
    finally:
        con.close()
    rec = rows["r-001"]
    # 完整契约沿流水线落库
    for key in ("_norm", "_provenance", "_clauses", "_compare",
                "_rules_versions", "_raw"):
        assert key in rec
    assert rec["_rules_versions"] == ["1.0.0"]
    # 原文可从落库记录回看
    assert normalize.recover_record(rec)["text"] == rec["_raw"]["text"]


def test_pipeline_legacy_scalar_contract_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr("policydiff.config.DB_PATH", tmp_path / "t.db")
    pipeline.run()
    con = store.connect()
    try:
        rows = dict(store.all_records(con))
    finally:
        con.close()
    r1, r2 = rows["r-001"], rows["r-002"]
    assert r1["src"] == "alpha" and r2["src"] == "beta"
    assert r1["name"] == "Record One" and r1["value"] == 10

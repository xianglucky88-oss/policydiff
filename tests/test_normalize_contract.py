"""归一化契约测试：保守清洗、逐字符溯源、幂等、规则版本与回放。

覆盖任务要求的对照文本：金额单位、括号、否定词、负号、不同编码空白
（制表符 / NBSP / 全角空格 / CRLF）、OCR 字符、全角字母数字、NFC。
"""
import pytest

from policydiff import normalize as nz


# 含全部噪声与法律要素的对照文本
RAW = (
    "第一条　补贴金额不低于 ￥1，000.00 元（含），不得​减扣。\r\n"
    "第二条　温度 −5°C；全角ＡＢＣ１２３；软连字­符；﻿条款（三）\n\n"
    "  多余  空格\t与tab \n"
)
NFD_E_ACUTE = chr(0x65) + chr(0x301)  # 真 NFD 分解，避免字面量被编辑器组合


def test_only_provable_noise_is_removed():
    r = nz.normalize_text(RAW)
    text = r["text"]
    # 噪声确实被消除
    assert "\r" not in text
    assert "​" not in text          # ZWSP
    assert "﻿" not in text          # BOM
    assert "­" not in text          # 软连字符
    assert "　" not in text          # 全角空格
    assert "\t" not in text
    assert r["rule_version"] == nz.RULE_VERSION


def test_legal_substance_is_preserved():
    text = nz.normalize_text(RAW)["text"]
    # 否定词、负号、法律数字、货币/单位、全角括号与标点全部保留
    for token in ["不低于", "不得", "−", "1，000.00", "￥", "元",
                  "（", "）", "°C", "；", "，", "（三）"]:
        assert token in text, token
    # 仅全角字母/数字折叠，标点括号不动
    assert "ABC123" in text
    assert "（含）" in text


def test_ocr_and_joining_marks_are_kept():
    # 证据不足的字符一律保留：OCR 替换符、ZWJ/ZWNJ、en/em dash、数学负号
    for ch in ["�", "‌", "‍", "–", "—", "−"]:
        assert nz.normalize_text(f"x{ch}y")["text"] == f"x{ch}y"


def test_hard_hyphen_fold_is_traceable():
    r = nz.normalize_text("A‑B‐C")
    assert r["text"] == "A-B-C"
    assert all(f["rule_id"] == "punct-hard-hyphen" for f in r["folds"])
    assert nz.recover_text("A‑B‐C", r) == "A‑B‐C"


def test_provenance_covers_source_completely():
    r = nz.normalize_text(RAW)
    # 内部覆盖不变量（构建时已断言，这里再次显式校验回放一致）
    assert nz.recover_text(RAW, r) == RAW
    # map 的输出索引连续
    assert [m[0] for m in r["map"]] == list(range(len(r["text"])))


def test_nfd_composition_maps_one_to_many():
    src = "caf" + NFD_E_ACUTE
    r = nz.normalize_text(src)
    assert r["text"] == "café"
    assert nz.recover_text(src, r) == src
    assert any(len(m[3]) == 2 for m in r["map"])       # 一个输出字符对两个原文码点
    assert any(f["rule_id"] == "enc-nfc" for f in r["folds"])


def test_idempotent_on_renormalization():
    r1 = nz.normalize_text(RAW)
    r2 = nz.normalize_text(r1["text"])
    assert r2["text"] == r1["text"]
    assert r2["dropped"] == []        # 再次归一化不再删除任何字符
    assert r2["rule_version"] == r1["rule_version"]


@pytest.mark.parametrize("src,expected", [
    ("a\r\nb\nc", "a\nb\nc"),
    ("a\n\n\nb", "a\nb"),
    ("\n\nx", "x"),
    ("x\n", "x"),
    ("p1\np2\np3", "p1\np2\np3"),
    ("  \n", ""),
    ("", ""),
    ("\n", ""),
])
def test_layout_boundaries_and_idempotency(src, expected):
    r = nz.normalize_text(src)
    assert r["text"] == expected
    assert nz.recover_text(src, r) == src
    r2 = nz.normalize_text(r["text"])
    assert r2["text"] == expected and r2["dropped"] == []


def test_dropped_events_carry_rule_and_reason():
    r = nz.normalize_text(RAW)
    rule_ids = {d["rule_id"] for d in r["dropped"]}
    # 每个删除事件都能定位到登记过的规则与书面理由
    for d in r["dropped"]:
        assert d["rule_id"] in nz.RULES
        assert d["reason"] == nz.RULES[d["rule_id"]].rationale
        assert RAW[d["s"]:d["e"]] == d["char"]
    assert "layout-zero-width" in rule_ids
    assert "ws-linebreak" in rule_ids


def test_rule_upgrade_110_folds_smart_quotes():
    sq = chr(0x201C) + "重点" + chr(0x201D) + "项目"
    old = nz.normalize_text(sq, "1.0.0")
    new = nz.normalize_text(sq, "1.1.0")
    # 旧版本保留弯引号；新版本折叠且可回放
    assert old["text"] == sq
    assert new["text"] == '"重点"项目'
    assert nz.recover_text(sq, new) == sq
    assert any(f["rule_id"] == "punct-smart-quote" for f in new["folds"])
    # 历史回放：按旧记录 pin 的版本重放，结果逐字段一致
    replay = nz.normalize_text(sq, old["rule_version"])
    assert replay["text"] == old["text"]
    assert replay["map"] == old["map"]


def test_unknown_rule_version_rejected():
    with pytest.raises(ValueError):
        nz.normalize_text("x", "9.9.9")


def test_process_keeps_legacy_fields_and_adds_contract():
    rec = nz.process({"id": "x", "src": " Alpha ", "name": " 名称　",
                      "value": "5", "text": RAW})
    # 旧标量契约不变
    assert rec["src"] == "alpha"
    assert rec["name"] == "名称"
    assert rec["value"] == 5
    # 原文快照 + 溯源契约
    assert rec["_raw"]["text"] == RAW
    assert rec["_raw"]["name"] == " 名称　"
    assert rec["_norm"]["text"]["text"] == nz.normalize_text(RAW)["text"]


def test_recover_record_replays_all_text_fields():
    rec = nz.process({"id": "x", "name": " 名称　", "text": RAW})
    recovered = nz.recover_record(rec)
    assert recovered["text"] == RAW
    assert recovered["name"] == " 名称　"


def test_backfill_is_batchable_and_version_pinned():
    sq = chr(0x201C) + "x" + chr(0x201D)
    gen = nz.backfill(iter([{"id": 1, "text": sq}, {"id": 2, "text": "平"}]), "1.1.0")
    first = next(gen)
    rest = list(gen)
    assert first["_norm"]["text"]["rule_version"] == "1.1.0"
    assert len(rest) == 1
    assert nz.recover_record(first)["text"] == sq


def test_record_without_text_fields_still_works():
    rec = nz.process({"id": "z", "src": "X", "value": 7})
    assert "_norm" not in rec and "_raw" not in rec
    assert rec["src"] == "x" and rec["value"] == 7

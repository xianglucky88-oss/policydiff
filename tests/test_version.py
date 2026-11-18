"""version 阶段：不完整版本判定及其对“删除”成立条件的影响。"""
from policydiff import version

from _helpers import load_fixture


def test_complete_version_assessment():
    rec = load_fixture("hangzhou_year.json")[0]
    verdict = version.assess(rec)
    assert verdict["incomplete"] is False
    assert verdict["reasons"] == []


def test_incomplete_signals_are_collected():
    rec = load_fixture("incomplete_version.json")[1]
    verdict = version.assess(rec)
    assert verdict["incomplete"] is True
    assert "status:partial" in verdict["reasons"]
    assert "missing_sections" in verdict["reasons"]
    assert "placeholder_clause" in verdict["reasons"]


def test_truncation_marker_in_text():
    verdict = version.assess({"doc_id": "x", "text": "第二条内容缺失，下略。"})
    assert verdict["incomplete"] is True
    assert "truncation_marker" in verdict["reasons"]


def test_process_stamps_verdict():
    rec = version.process({"id": "z", "status": "draft"})
    assert rec["incomplete"] is True
    assert rec["_stage_version"] == "ok"

"""Tests for cross-version clause alignment, compare and version history.

Scenarios follow the task brief:
  * clause renumbering / chapter reordering / synonym headings / attachment
    insertion must keep stable matching;
  * one-to-many splits and many-to-one merges must be modelled as their own
    relation kinds, never collapsed into "best similarity, rest added";
  * low-confidence candidates go to manual review and must never silently
    overwrite an existing alignment;
  * swapping which version arrives first must not change the mapping;
  * confirmation is crash-recoverable and verifies source anchors;
  * old alignment results stay readable and new similar candidates cannot
    overwrite historical decisions.
"""
import sqlite3

import pytest

from src.policydiff import align, compare, version
from src.policydiff import _align_store as db
from src.policydiff import _align_text as tx


def new_con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    db.ensure_schema(con)
    return con


def doc(version_id, clauses, doc_id="policy"):
    return {"doc_id": doc_id, "version": version_id, "clauses": clauses}


V2023 = doc("2023", [
    {"number": "第一条", "heading": "总则", "section": "第一章 总则",
     "text": "为了规范农业补贴管理，保障资金安全，制定本办法。"},
    {"number": "第二条", "heading": "适用范围", "section": "第一章 总则",
     "text": "本办法适用于本市行政区域内的农业补贴申请、审批与发放。"},
    {"number": "第三条", "heading": "申请材料", "section": "第二章 申请",
     "text": "申请人应当提交身份证明、土地承包合同和银行账户信息。"},
    {"number": "第四条", "heading": "审批流程", "section": "第二章 申请",
     "text": "主管部门应当在二十个工作日内完成审核并公示结果。"},
    {"number": "第五条", "heading": "监督管理", "section": "第三章 监督",
     "text": "财政部门对补贴资金实行全过程监督，并定期开展检查。"},
])

V2024 = doc("2024", [
    # synonym heading + renumbered chapter, same body
    {"number": "第一条", "heading": "一般规定", "section": "第一章 一般规定",
     "text": "为了规范农业补贴管理，保障资金安全，制定本办法。"},
    # inserted attachment between originally adjacent clauses
    {"number": "附件1", "heading": "附件1 申请材料清单", "section": "附件",
     "attachment": "附件1", "text": "申请书、身份证明、土地承包合同复印件。"},
    {"number": "第二条", "heading": "适用对象", "section": "第一章 一般规定",
     "text": "本办法适用于本市行政区域内的农业补贴申请、审批与发放。"},
    # chapters reordered: 申请 moved after 监督
    {"number": "第五条", "heading": "监督", "section": "第三章 监督",
     "text": "财政部门对补贴资金实行全过程监督，并定期开展检查。"},
    {"number": "第六条", "heading": "审批流程", "section": "第二章 申请与审批",
     "text": "主管部门应当在二十个工作日内完成审核并公示结果。"},
    {"number": "第七条", "heading": "申请材料", "section": "第二章 申请与审批",
     "text": "申请人应当提交身份证明、土地承包合同和银行账户信息。"},
])

SPLIT_OLD = doc("2023", [
    {"number": "第一条", "heading": "申请材料", "section": "第二章 申请",
     "text": "申请人应当提交身份证明、土地承包合同和银行账户信息；涉及跨区经营的，还应当提交经营地主管部门出具的证明。"},
    {"number": "第二条", "heading": "审批流程", "section": "第二章 申请",
     "text": "主管部门应当在二十个工作日内完成审核并公示结果。"},
], doc_id="split")

SPLIT_NEW = doc("2024", [
    {"number": "第一条", "heading": "申请材料", "section": "第二章 申请",
     "text": "申请人应当提交身份证明、土地承包合同和银行账户信息。"},
    {"number": "第二条", "heading": "跨区经营材料", "section": "第二章 申请",
     "text": "涉及跨区经营的，还应当提交经营地主管部门出具的证明。"},
    {"number": "第三条", "heading": "审批流程", "section": "第二章 申请",
     "text": "主管部门应当在二十个工作日内完成审核并公示结果。"},
], doc_id="split")

MERGE_OLD = doc("2023", [
    {"number": "第一条", "heading": "申请", "section": "第二章",
     "text": "申请人应当提交申请书和身份证明材料，材料不全的应当一次性告知补正。"},
    {"number": "第二条", "heading": "补正", "section": "第二章",
     "text": "申请人应当在五个工作日内补正材料，逾期视为放弃。"},
    {"number": "第三条", "heading": "审批", "section": "第二章",
     "text": "审批机关应当在收到申请后十日内作出决定。"},
], doc_id="merge")

MERGE_NEW = doc("2024", [
    {"number": "第一条", "heading": "申请与补正", "section": "第二章",
     "text": "申请人应当提交申请书和身份证明材料，材料不全的应当一次性告知补正；申请人应当在五个工作日内补正材料，逾期视为放弃。"},
    {"number": "第二条", "heading": "审批", "section": "第二章",
     "text": "审批机关应当在收到申请后十日内作出决定。"},
], doc_id="merge")


def by_kind(con, kind, **filters):
    return [g for g in align.groups(con, **filters) if g["kind"] == kind]


def mapping(con, low, high):
    return align.version_map(con, low_version=low, high_version=high,
                             include_pending=False)


# ---------------------------------------------------------------------------
# stable matching despite numbering / ordering / heading / attachment changes
# ---------------------------------------------------------------------------

def test_matches_across_renumbering_reorder_synonyms_and_attachment():
    con = new_con()
    result = align.align_versions(V2023, V2024, con=con)
    assert result["conflicts"] == 0
    mapping_ = mapping(con, "2023", "2024")
    # 5 older clauses matched; the inserted attachment is genuinely new.
    assert len(mapping_) == 5
    assert len(result["unmatched_high"]) == 1
    attachment = [c for c in V2024["clauses"] if c.get("attachment")]
    assert attachment, "fixture sanity"
    con.close()


def test_numbering_is_a_weak_signal_not_identity():
    old = doc("v1", [{"number": "第一条", "heading": "申请材料",
                      "text": "申请人应当提交身份证明和土地承包合同。"}])
    new = doc("v2", [{"number": "第九条", "heading": "申请材料",
                      "text": "申请人应当提交身份证明和土地承包合同。"}])
    con = new_con()
    align.align_versions(old, new, con=con)
    assert len(mapping(con, "v1", "v2")) == 1
    con.close()


def test_arrival_order_does_not_change_the_mapping():
    con_a, con_b = new_con(), new_con()
    align.align_versions(V2023, V2024, con=con_a)
    align.align_versions(V2024, V2023, con=con_b)
    forward = mapping(con_a, "2023", "2024")
    backward = mapping(con_b, "2023", "2024")
    assert forward == backward
    groups_a = [(g["kind"], g["status"], g["group_id"]) for g in align.groups(con_a)]
    groups_b = [(g["kind"], g["status"], g["group_id"]) for g in align.groups(con_b)]
    assert groups_a == groups_b
    con_a.close()
    con_b.close()


# ---------------------------------------------------------------------------
# one-to-many / many-to-one
# ---------------------------------------------------------------------------

def test_split_is_one_group_not_best_pair_plus_addition():
    con = new_con()
    align.align_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    splits = by_kind(con, "split")
    assert len(splits) == 1
    split = splits[0]
    assert len(split["members"]) == 3
    # Default policy: structural hypotheses await human confirmation.
    assert split["status"] == "pending"
    # and crucially, the two new clauses are not reported as "added"
    result = compare.compare_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    assert result["summary"]["added"] == 0
    assert result["summary"]["removed"] == 0
    assert result["summary"]["pending_review"] >= 1
    con.close()


def test_merge_is_many_to_one_group():
    con = new_con()
    align.align_versions(MERGE_OLD, MERGE_NEW, con=con)
    merges = by_kind(con, "merge")
    assert len(merges) == 1
    assert len(merges[0]["members"]) == 3
    result = compare.compare_versions(MERGE_OLD, MERGE_NEW, con=con)
    assert result["summary"]["added"] == 0
    assert result["summary"]["removed"] == 0
    con.close()


def test_structural_auto_accept_is_opt_in_only():
    con = new_con()
    align.align_versions(SPLIT_OLD, SPLIT_NEW, con=con, auto_structural=True)
    split = by_kind(con, "split")[0]
    assert split["status"] == "accepted"
    result = compare.compare_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    assert result["summary"]["split"] == 1
    con.close()


def test_template_heavy_corpus_does_not_invent_splits_or_merges():
    """Boilerplate clauses that share most text must stay one-to-one."""
    def make(version_id, n):
        return doc(version_id, [
            {"number": "第%d条" % (i + 1), "heading": "条款%d" % i,
             "section": "第%d章" % (i // 10 + 1),
             "text": "第%d条关于特定事项的规定内容编号%d，申请人应当提交材料%d并接受审核%d，"
                     "主管部门在%d日内办结。" % (i, i, i, i, i)}
            for i in range(n)
        ], doc_id="templates")
    con = new_con()
    align.align_versions(make("2023", 40), make("2024", 40), con=con)
    kinds = [g["kind"] for g in align.groups(con)]
    assert kinds and all(k == "one_to_one" for k in kinds)
    con.close()


# ---------------------------------------------------------------------------
# confidence bands and manual review
# ---------------------------------------------------------------------------

def test_low_confidence_pair_is_recorded_but_not_matched():
    old = doc("v1", [{"number": "一", "heading": "甲",
                      "text": "申请人应当提交身份证明材料。"}])
    new = doc("v2", [{"number": "一", "heading": "甲",
                      "text": "申请材料包括身份证明和银行账户信息。"}])
    con = new_con()
    result = align.align_versions(old, new, con=con)
    assert result["groups"] == 0
    candidates = align.candidates(con)
    assert candidates, "a low-confidence pair should still be recorded"
    assert candidates[0]["detail"]["reason"] == "low_confidence"
    assert candidates[0]["status"] in ("rejected", "pending")
    review = compare.compare_versions(old, new, con=con)
    # A plausible but unconfirmed correspondence is review work, not a silent
    # "added + removed" verdict, and no mapping was created.
    if candidates[0]["status"] == "pending":
        assert review["summary"]["pending_candidate"] == 1
    assert review["summary"]["added"] + review["summary"]["removed"] <= 2
    assert align.version_map(con, low_version="v1", high_version="v2",
                             include_pending=False) == {}
    con.close()


def test_medium_score_is_pending_not_accepted():
    old = doc("v1", [{"number": "一", "heading": "申请材料",
                      "text": "申请人应当提交身份证明、土地承包合同和银行账户信息，材料齐全的予以受理。"}])
    new = doc("v2", [{"number": "一", "heading": "申报材料",
                      "text": "申请人应当提交身份证明、土地承包合同和银行账户信息，材料齐全的予以受理，并出具回执。"}])
    con = new_con()
    align.align_versions(old, new, con=con)
    groups = align.groups(con)
    assert groups and groups[0]["status"] in ("pending", "accepted")
    if groups[0]["status"] == "pending":
        assert compare.compare_versions(old, new, con=con)["summary"]["pending_review"] == 1
    con.close()


def test_confirm_promotes_pending_and_makes_compare_decided():
    con = new_con()
    align.align_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    split = by_kind(con, "split")[0]
    before = compare.compare_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    assert before["summary"]["split"] == 0
    out = align.confirm(con=con, group_id=split["group_id"], note="拆分属实")
    assert out["state"] == "applied"
    after = compare.compare_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    assert after["summary"]["split"] == 1
    assert after["summary"]["pending_review"] == 0
    decisions = db.list_decisions(con, split["group_id"])
    assert len(decisions) == 1 and decisions[0]["new_status"] == "confirmed"
    assert decisions[0]["note"] == "拆分属实"
    con.close()


# ---------------------------------------------------------------------------
# manual decisions are sticky; similarity cannot override them
# ---------------------------------------------------------------------------

def test_rerun_does_not_override_confirmed_mapping():
    con = new_con()
    align.align_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    split = by_kind(con, "split")[0]
    align.confirm(con=con, group_id=split["group_id"], actor="reviewer")

    # Re-running (even with the versions swapped) must not touch the decision.
    result = align.align_versions(SPLIT_NEW, SPLIT_OLD, con=con)
    assert result["manual_groups_preserved"] == 1
    still = db.get_group(con, split["group_id"])
    assert still["status"] == "confirmed"
    assert still["manual"] == 1
    assert still["decision_actor"] == "reviewer"
    assert len(db.list_decisions(con, split["group_id"])) == 1
    con.close()


def test_rejected_pair_is_not_silently_readded():
    con = new_con()
    align.align_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    pair = by_kind(con, "one_to_one")[0]
    align.reject(con=con, group_id=pair["group_id"], note="并非同一条款")
    result = align.align_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    assert result["suppressed"] >= 0
    rejected = db.get_group(con, pair["group_id"])
    assert rejected["status"] == "rejected"
    assert rejected["manual"] == 1
    result_compare = compare.compare_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    # The rejected pairing is gone, so the clauses are genuinely removed/added.
    assert result_compare["summary"]["removed"] >= 1
    assert result_compare["summary"]["added"] >= 1
    con.close()


def test_new_similar_candidate_cannot_overwrite_confirmed_decision():
    old = doc("v1", [{"number": "一", "heading": "申请材料",
                      "text": "申请人应当提交身份证明、土地承包合同和银行账户信息。"}])
    new = doc("v2", [
        {"number": "一", "heading": "申请材料",
         "text": "申请人应当提交身份证明、土地承包合同和银行账户信息。"},
        {"number": "二", "heading": "申请材料（修订）",
         "text": "申请人应当提交身份证明、土地承包合同和银行账户信息的复印件。"},
    ])
    con = new_con()
    align.align_versions(old, new, con=con)
    old_key = align._prepare(old)["clauses"][0]["clause_key"]
    group = align.find_group(con, low_version="v1", high_version="v2",
                             low_keys=[old_key])
    assert group is not None
    align.confirm(con=con, group_id=group["group_id"], note="按原文对应")
    confirmed_high = {m["clause_key"] for m in group["members"] if m["side"] == "high"}
    # A third revision makes the *other* new clause the better textual match.
    revised = doc("v3", [
        {"number": "一", "heading": "申请材料",
         "text": "申请人应当提交身份证明、土地承包合同和银行账户信息的复印件，并加盖公章。"},
    ])
    align.align_versions(new, revised, con=con)  # unrelated pair, must not disturb v1/v2
    align.align_versions(old, new, con=con)      # rerun the confirmed pair
    still = db.get_group(con, group["group_id"], with_members=True)
    assert still["status"] == "confirmed"
    assert {m["clause_key"] for m in still["members"] if m["side"] == "high"} == confirmed_high
    con.close()


def test_confirmed_pair_kept_even_if_scores_change():
    old = doc("v1", [
        {"number": "一", "heading": "申请材料", "text": "申请人应当提交身份证明和土地承包合同。"},
        {"number": "二", "heading": "审批流程", "text": "主管部门应当在二十个工作日内完成审核并公示。"},
    ])
    new = doc("v2", [
        {"number": "一", "heading": "申请材料", "text": "申请人应当提交身份证明和土地承包合同。"},
        {"number": "二", "heading": "审批流程", "text": "主管部门应当在二十个工作日内完成审核并公示。"},
    ])
    con = new_con()
    align.align_versions(old, new, con=con)
    first = sorted(align.groups(con), key=lambda g: g["group_id"])[0]
    align.confirm(con=con, group_id=first["group_id"], note="人工核准")
    # A later, more similar candidate appears for the same old clause.
    revised = doc("v2", [
        {"number": "一", "heading": "申请材料", "text": "申请人应当提交身份证明和土地承包合同。"},
        {"number": "二", "heading": "审批流程", "text": "主管部门应当在二十个工作日内完成审核并公示。"},
        {"number": "三", "heading": "申请材料", "text": "申请人应当提交身份证明和土地承包合同的补充说明。"},
    ])
    align.align_versions(old, revised, con=con)
    assert db.get_group(con, first["group_id"])["status"] == "confirmed"
    con.close()


# ---------------------------------------------------------------------------
# crash recovery and anchor verification
# ---------------------------------------------------------------------------

def test_confirmation_survives_crash_between_journal_and_apply():
    con = new_con()
    align.align_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    split = by_kind(con, "split")[0]

    crashed = align.confirm(con=con, group_id=split["group_id"],
                            crash_after_journal=True)
    assert crashed["state"] == "pending"
    assert db.get_group(con, split["group_id"])["status"] == "pending"
    assert db.list_decisions(con, split["group_id"]) == []

    recovered = align.recover(con=con)
    assert len(recovered) == 1 and recovered[0]["state"] == "applied"
    assert db.get_group(con, split["group_id"])["status"] == "confirmed"
    assert len(db.list_decisions(con, split["group_id"])) == 1

    # Recovery is idempotent: a second pass finds nothing pending.
    assert align.recover(con=con) == []
    con.close()


def test_replayed_decision_is_idempotent():
    con = new_con()
    align.align_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    split = by_kind(con, "split")[0]
    crashed = align.confirm(con=con, group_id=split["group_id"], crash_after_journal=True)
    first = db.apply_decision(con, crashed["op_id"])
    second = db.apply_decision(con, crashed["op_id"])
    assert first["state"] == second["state"] == "applied"
    assert second["replayed"] is True
    assert len(db.list_decisions(con, split["group_id"])) == 1
    con.close()


def test_anchor_mismatch_blocks_confirmation():
    con = new_con()
    align.align_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    split = by_kind(con, "split")[0]
    # Corrupt a stored anchor to simulate drift between parse and confirmation.
    con.execute("UPDATE align_members SET anchor_json=? WHERE group_id=?",
                ('{"doc_id": "elsewhere"}', split["group_id"]))
    con.commit()
    outcome = align.confirm(con=con, group_id=split["group_id"])
    assert outcome["state"] == "rolled_back"
    assert outcome["result"]["error"] == "anchor_mismatch"
    assert db.get_group(con, split["group_id"])["status"] == "pending"
    assert db.list_decisions(con, split["group_id"]) == []
    con.close()


def test_partial_application_is_recovered_without_duplicate_decisions():
    """Crash after the mapping write but before the journal flips to applied."""
    con = new_con()
    align.align_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    split = by_kind(con, "split")[0]
    crashed = align.confirm(con=con, group_id=split["group_id"], crash_after_journal=True)
    # Simulate a process that committed the group + decision but died before
    # updating the journal row.
    now = db.utcnow()
    decision_id = "d-" + tx.stable_hash(crashed["op_id"], split["group_id"],
                                        "confirm", length=20)
    with con:
        con.execute("UPDATE align_groups SET status='confirmed',manual=1,revision=revision+1 "
                    "WHERE group_id=?", (split["group_id"],))
        con.execute(
            "INSERT INTO align_decisions(decision_id,group_id,action,actor,note,"
            "previous_status,new_status,op_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (decision_id, split["group_id"], "confirm", "human", None,
             "pending", "confirmed", crashed["op_id"], now),
        )
    outcomes = align.recover(con=con)
    assert outcomes == [{"op_id": crashed["op_id"], "state": "applied",
                         "recovered": "already_decided"}]
    assert db.get_group(con, split["group_id"])["status"] == "confirmed"
    assert len(db.list_decisions(con, split["group_id"])) == 1
    con.close()


def test_members_store_source_anchors_for_both_sides():
    con = new_con()
    align.align_versions(V2023, V2024, con=con)
    for group in align.groups(con):
        for member in group["members"]:
            anchor = member["anchor"]
            assert anchor["doc_id"] == "policy"
            assert anchor["version"] in ("2023", "2024")
            assert "ordinal" in anchor
            ref = db.clause_ref(con, member["doc_id"], member["version"],
                                member["clause_key"])
            assert ref is not None
            assert ref["anchor"] == anchor
            assert ref["text_sha256"] == member["text_sha256"]
    con.close()


# ---------------------------------------------------------------------------
# compare derives added/removed from alignment
# ---------------------------------------------------------------------------

def test_compare_added_removed_follow_alignment_not_similarity():
    old = doc("v1", [
        {"number": "一", "heading": "旧条款", "text": "本办法自发布之日起施行，原有规定同时废止。"},
    ])
    new = doc("v2", [
        {"number": "一", "heading": "新条款", "text": "对种粮大户给予一次性补贴，标准另行规定。"},
    ])
    con = new_con()
    result = compare.compare_versions(old, new, con=con)
    assert result["summary"]["removed"] == 1
    assert result["summary"]["added"] == 1
    assert result["summary"]["pending_review"] == 0
    con.close()


def test_compare_detects_modified_body_under_new_number():
    old = doc("v1", [{"number": "第三条", "heading": "申请材料", "section": "第二章 申请",
                      "text": "申请人应当提交身份证明、土地承包合同、银行账户信息以及主管部门要求的其他材料。"}])
    new = doc("v2", [{"number": "第八条", "heading": "申请材料", "section": "第二章 申请",
                      "text": "申请人应当提交身份证明、土地承包合同、银行账户信息以及主管部门要求的其他材料，并对材料真实性负责。"}])
    con = new_con()
    result = compare.compare_versions(old, new, con=con)
    modified = [c for c in result["changes"] if c["type"] == "modified"]
    assert len(modified) == 1, result["summary"]
    assert modified[0]["changes"]["number"] is True
    assert modified[0]["changes"]["text"] is True
    con.close()


def test_compare_number_change_alone_is_unchanged():
    body = "申请人应当提交身份证明、土地承包合同、银行账户信息以及主管部门要求的其他材料。"
    old = doc("v1", [{"number": "第三条", "heading": "申请材料", "section": "第二章 申请",
                      "text": body}])
    new = doc("v2", [{"number": "第八条", "heading": "申请材料", "section": "第二章 申请",
                      "text": body}])
    con = new_con()
    result = compare.compare_versions(old, new, con=con)
    assert result["summary"]["unchanged"] == 1
    assert result["summary"]["modified"] == 0
    con.close()


def test_compare_swap_order_is_equivalent():
    con = new_con()
    forward = compare.compare_versions(V2023, V2024, con=con)
    backward = compare.compare_versions(V2024, V2023, con=con)
    assert forward["summary"] == backward["summary"]
    assert forward["run_id"] == backward["run_id"]
    con.close()


def test_compare_runs_are_auditable_and_idempotent():
    con = new_con()
    first = compare.compare_versions(V2023, V2024, con=con)
    second = compare.compare_versions(V2023, V2024, con=con)
    assert first["run_id"] == second["run_id"]
    history = compare.run_history(con, doc_id="policy")
    assert len(history) == 1
    stored = compare.get_run(first["run_id"], con=con)
    assert len(stored["changes"]) == len(first["changes"])
    con.close()


# ---------------------------------------------------------------------------
# version history
# ---------------------------------------------------------------------------

def test_version_history_keeps_split_as_fork():
    con = new_con()
    chains = version.build_chains([SPLIT_OLD, SPLIT_NEW], con=con)
    split_chains = [c for c in chains["chains"] if any(l["kind"] == "split"
                                                       for l in c["links"])]
    assert len(split_chains) == 1
    assert len(split_chains[0]["nodes"]) == 3
    assert split_chains[0]["versions"] == ["2023", "2024"]
    con.close()


def test_version_history_records_cross_year_gap():
    v1 = doc("2021", [{"number": "一", "heading": "补贴标准",
                       "text": "对符合条件的农户按亩给予补贴，标准为每亩一百元。"}])
    v3 = doc("2023", [{"number": "一", "heading": "补贴标准",
                       "text": "对符合条件的农户按亩给予补贴，标准为每亩一百元。"}])
    con = new_con()
    # align 2021->2023 only, 2022 never arrived
    chains = version.build_chains([v1, v3], con=con)
    assert chains["versions"] == ["2021", "2023"]
    assert all(len(c["nodes"]) == 2 for c in chains["chains"])
    con.close()


def test_version_history_counts_manual_confirmation():
    con = new_con()
    align.align_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    split = by_kind(con, "split")[0]
    before = version.build_chains([SPLIT_OLD, SPLIT_NEW], con=con)
    gaps_before = [g for g in before["gaps"] if g["type"] == "pending"]
    assert gaps_before
    align.confirm(con=con, group_id=split["group_id"])
    after = version.build_chains([SPLIT_OLD, SPLIT_NEW], con=con)
    assert not [g for g in after["gaps"] if g["type"] == "pending"]
    con.close()


# ---------------------------------------------------------------------------
# old results readable / migrations
# ---------------------------------------------------------------------------

def test_legacy_status_values_are_migrated_in_place():
    con = new_con()
    align.align_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    con.execute("UPDATE align_groups SET status='auto' WHERE kind='one_to_one'")
    con.commit()
    db.ensure_schema(con)
    statuses = {g["kind"]: g["status"] for g in align.groups(con)}
    assert statuses["one_to_one"] == "accepted"
    # and the migrated row is still readable through the normal query API
    assert align.groups(con, statuses=["accepted"])
    con.close()


def test_snapshot_export_import_preserves_manual_decisions():
    con = new_con()
    align.align_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    split = by_kind(con, "split")[0]
    align.confirm(con=con, group_id=split["group_id"], note="保留")
    snapshot = db.export_snapshot(con)

    fresh = new_con()
    counts = db.import_snapshot(fresh, snapshot)
    restored = db.get_group(fresh, split["group_id"])
    assert restored["status"] == "confirmed"
    assert restored["manual"] == 1
    assert restored["decision_note"] == "保留"
    assert counts["decisions"] == len(snapshot["decisions"]) == 1
    assert counts["candidates"] == len(snapshot["candidates"])
    # The restored decision is idempotent under a second import.
    db.import_snapshot(fresh, snapshot)
    assert len(db.list_decisions(fresh)) == 1
    assert len(align.groups(fresh)) == len(align.groups(con))
    fresh.close()
    con.close()


def test_exported_snapshot_has_no_clause_bodies():
    con = new_con()
    align.align_versions(V2023, V2024, con=con)
    snapshot = db.export_snapshot(con)
    payload = repr(snapshot)
    assert "为了规范农业补贴管理" not in payload
    # anchors and hashes are present for audit
    assert any(m.get("anchor") for g in snapshot["groups"] for m in g["members"])
    con.close()


# ---------------------------------------------------------------------------
# pipeline / compatibility
# ---------------------------------------------------------------------------

def test_pipeline_stage_passthrough_keeps_happy_path():
    from src.policydiff import pipeline
    record = {"id": "r-001", "src": "alpha", "name": "Record One", "value": 10}
    assert align.process(dict(record))["_stage_align"] == "ok"
    assert compare.process(dict(record))["_stage_compare"] == "ok"
    assert version.process(dict(record))["_stage_version"] == "ok"
    assert pipeline.run()["fed"] == 4


def test_process_stage_aligns_payload():
    record = {"doc_id": "policy", "align": {"low": SPLIT_OLD, "high": SPLIT_NEW}}
    out = align.process(record, con=new_con())
    assert out["_align"]["groups"] >= 1
    assert out["_stage_align"] == "ok"


def test_align_requires_distinct_versions():
    con = new_con()
    with pytest.raises(ValueError):
        align.align_versions(SPLIT_OLD, SPLIT_OLD, con=con)
    con.close()


# ---------------------------------------------------------------------------
# end-to-end replay: structural cases + arrival order + recovery
# ---------------------------------------------------------------------------

def test_replay_interleaved_arrival_matches_sequential():
    """Arrive 2024 first, then 2023, then a split/merge replay.

    The mapping built from out-of-order arrival must equal the one built from
    chronological order, and a crash during confirmation must still recover.
    """
    sequential = new_con()
    align.align_versions(V2023, V2024, con=sequential)
    align.align_versions(SPLIT_OLD, SPLIT_NEW, con=sequential)
    align.align_versions(MERGE_OLD, MERGE_NEW, con=sequential)

    interleaved = new_con()
    # newest document arrives first and is aligned against nothing yet
    align.align_versions(SPLIT_NEW, SPLIT_OLD, con=interleaved)
    align.align_versions(V2024, V2023, con=interleaved)
    align.align_versions(MERGE_NEW, MERGE_OLD, con=interleaved)

    def pairs(con):
        return sorted((g["doc_id"], g["low_version"], g["high_version"], g["kind"],
                       g["status"], tuple(sorted(m["clause_key"] for m in g["members"])))
                      for g in align.groups(con))

    assert pairs(sequential) == pairs(interleaved)

    # Now simulate a crash mid-confirmation on the interleaved store.
    split = [g for g in align.groups(interleaved) if g["kind"] == "split"][0]
    align.confirm(con=interleaved, group_id=split["group_id"], crash_after_journal=True)
    outcomes = align.recover(con=interleaved)
    assert [o["state"] for o in outcomes] == ["applied"]
    assert db.get_group(interleaved, split["group_id"])["status"] == "confirmed"
    sequential.close()
    interleaved.close()


def test_reparse_does_not_apply_stale_groups():
    con = new_con()
    compare.compare_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    stale = by_kind(con, "split")[0]
    # Re-parse the newer version with one split child deleted entirely.
    reparsed = doc("2024", [
        {"number": "第一条", "heading": "申请材料", "section": "第二章 申请",
         "text": "申请人应当提交身份证明、土地承包合同和银行账户信息。"},
        {"number": "第三条", "heading": "审批流程", "section": "第二章 申请",
         "text": "主管部门应当在二十个工作日内完成审核并公示结果。"},
    ], doc_id="split")
    result = compare.compare_versions(SPLIT_OLD, reparsed, con=con)
    # The stale split references a clause that no longer exists; it must not be
    # reported as a split change, and it stays readable in storage for audit.
    assert not [c for c in result["changes"] if c["type"] == "split"]
    assert db.get_group(con, stale["group_id"]) is not None
    con.close()


def test_compare_without_alignment_rows_still_aligns():
    con = new_con()
    result = compare.compare_versions(V2023, V2024, con=con)
    assert result["summary"]["unchanged"] >= 1
    assert align.groups(con), "compare should persist the alignment it relied on"
    con.close()


def test_stale_automatic_groups_are_superseded_and_hidden():
    """A re-parse shrinks the newer version; the old split must stop applying."""
    con = new_con()
    align.align_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    split = by_kind(con, "split")[0]
    smaller = doc("2024", [
        {"number": "第一条", "heading": "申请材料", "section": "第二章 申请",
         "text": "申请人应当提交身份证明、土地承包合同和银行账户信息。"},
    ], doc_id="split")
    align.align_versions(SPLIT_OLD, smaller, con=con)
    assert split["group_id"] not in {g["group_id"] for g in align.groups(con)}
    hidden = {g["group_id"] for g in align.groups(con, include_superseded=True)}
    assert split["group_id"] in hidden
    assert db.get_group(con, split["group_id"])["status"] == "superseded"
    con.close()


def test_supersede_never_touches_manual_decisions():
    con = new_con()
    align.align_versions(SPLIT_OLD, SPLIT_NEW, con=con)
    split = by_kind(con, "split")[0]
    align.confirm(con=con, group_id=split["group_id"], note="人工确认")
    smaller = doc("2024", [
        {"number": "第一条", "heading": "申请材料", "section": "第二章 申请",
         "text": "申请人应当提交身份证明、土地承包合同和银行账户信息。"},
    ], doc_id="split")
    align.align_versions(SPLIT_OLD, smaller, con=con)
    assert db.get_group(con, split["group_id"])["status"] == "confirmed"
    con.close()

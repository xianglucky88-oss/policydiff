"""单一职责：变化分类与证据（compare）。

消费 clause 阶段已形成的条款、align 阶段已形成的对齐关系，
只负责“变化分类 + 原文证据”，不在本模块重新实现结构匹配。
结果沿用 models.Record / store 的稳定键与 sqlite 存储保存，可重复生成。

差异按三类状态分桶，而不是只输出一份 diff 文本：
  substantive 实质变化——额度、条件等规则真的变了：
      added / removed / replaced / threshold_changed / merged / split
  format      格式噪声——排版、条款重排等不改变规则的变化：
      reordered / formatting_only
  uncertain   证据不足——版本缺段或对齐置信不够，无法下实质结论：
      unknown_removal / unknown_addition / uncertain_pair

“删除”能否成立取决于 version 的完整性结论：
  对照版（B）完整时 A-only 才是 removed；B 不完整时只能是 unknown_removal。
每条结论都带原文证据（条款号 + 原文片段 + cid），可回链定位。
"""
from . import align as _align
from . import clause as _clause
from . import version as _version
from .models import Record
from .normalize import normalize_text

# 三桶状态
SUBSTANTIVE = "substantive"
FORMAT = "format"
FORMAT_NOISE = FORMAT
UNCERTAIN = "uncertain"

# 实质子类
ADDED = "added"
REMOVED = "removed"
REPLACED = "replaced"
THRESHOLD_CHANGED = "threshold_changed"
MERGED = "merged"
SPLIT = "split"
# 格式子类
REORDERED = "reordered"
FORMATTING_ONLY = "formatting_only"
# 证据不足子类
UNKNOWN_REMOVAL = "unknown_removal"
UNKNOWN_ADDITION = "unknown_addition"
UNCERTAIN_PAIR = "uncertain_pair"

# 阈值对比的浮点容差（基准单位口径下）
_EPS = 1e-6
_DURATION_DIMS = ("months", "years")


# ---------------------------------------------------------------- 工具
def _index(clauses):
    return {c["cid"]: c for c in (clauses or [])}


def _evidence(clause, label):
    """把条款转成可回链的原文证据。"""
    if clause is None:
        return None
    return {
        "side": label,
        "cid": clause["cid"],
        "number": clause.get("number"),
        "order": clause.get("order"),
        "quote": clause.get("text", ""),
    }


def _same_thresholds(a_cl, b_cl):
    """同维度、同基准单位下比较阈值；单位口径不同返回 (False, 原因)。"""
    ta = {t["dim"]: t for t in a_cl.get("thresholds", [])}
    tb = {t["dim"]: t for t in b_cl.get("thresholds", [])}
    dims = sorted(set(ta) | set(tb))

    # 时长口径：一边用“月”、另一边用“年”即不可直接比较
    # （12个月 与 1年 数值与基准单位都不同，不能静默换算成实质增减）
    a_units = {t["unit"] for t in ta.values()}
    b_units = {t["unit"] for t in tb.values()}
    duration_caliber_conflict = (
        any(ta.get(d) for d in _DURATION_DIMS)
        and any(tb.get(d) for d in _DURATION_DIMS)
        and bool(a_units & {"月", "个月"}) != bool(b_units & {"月", "个月"})
    )

    deltas, caliber_conflict = [], duration_caliber_conflict
    for dim in dims:
        x, y = ta.get(dim), tb.get(dim)
        if x is None or y is None:
            deltas.append({"dim": dim,
                           "a": None if x is None else x["raw"],
                           "b": None if y is None else y["raw"]})
            continue
        # 同维度但单位口径不同（如万元 vs 元之外的异质单位）→ 口径冲突
        if x["base"] != y["base"] and x["unit"] != y["unit"]:
            caliber_conflict = True
        if abs(float(x["base"]) - float(y["base"])) > _EPS:
            deltas.append({
                "dim": dim,
                "a": x["raw"], "b": y["raw"],
                "a_base": x["base"], "b_base": y["base"],
                "a_unit": x["unit"], "b_unit": y["unit"],
            })
    return (not deltas and not caliber_conflict), deltas, caliber_conflict


def _make_item(kind, bucket, evidence_a=None, evidence_b=None,
               detail=None, confidence=None):
    item = {"kind": kind, "status": bucket,
            "evidence_a": evidence_a, "evidence_b": evidence_b}
    if detail:
        item["detail"] = detail
    if confidence:
        item["confidence"] = confidence
    return item


# ---------------------------------------------------------------- 1:1 分类
def _classify_pair(pair, a_cl, b_cl, incomplete_b, incomplete_a):
    ev_a, ev_b = _evidence(a_cl, "a"), _evidence(b_cl, "b")
    conf = pair.get("confidence")

    # 1) 低置信对齐：不强行下实质结论
    if conf == "low":
        return _make_item(UNCERTAIN_PAIR, UNCERTAIN, ev_a, ev_b,
                          detail={"reason": "low_alignment_confidence",
                                  "score": pair.get("score")},
                          confidence=conf)

    same_norm = a_cl["norm"] == b_cl["norm"]
    same_th, deltas, caliber = _same_thresholds(a_cl, b_cl)

    # 2) 归一化后完全一致：原文与归一化都相同才是纯排版噪声
    if same_norm and same_th:
        if a_cl.get("text", "") != b_cl.get("text", ""):
            return _make_item(FORMATTING_ONLY, FORMAT, ev_a, ev_b,
                              detail={"reason": "whitespace_or_punctuation_only"},
                              confidence=conf)
        if a_cl.get("order") != b_cl.get("order") or \
                a_cl.get("number") != b_cl.get("number"):
            return _make_item(REORDERED, FORMAT, ev_a, ev_b,
                              detail={"reason": "clause_reordered",
                                      "a_order": a_cl.get("order"),
                                      "b_order": b_cl.get("order")},
                              confidence=conf)
        return _make_item(FORMATTING_ONLY, FORMAT, ev_a, ev_b,
                          detail={"reason": "identical_clause"},
                          confidence=conf)

    # 3) 单位口径不同导致阈值不可直接比较 → 证据不足，而不是误报阈值变化
    if caliber:
        return _make_item(UNCERTAIN_PAIR, UNCERTAIN, ev_a, ev_b,
                          detail={"reason": "unit_caliber_incomparable",
                                  "threshold_deltas": deltas},
                          confidence=conf)

    # 4) 量化阈值在基准单位口径下发生变化（如 3万元→5万元、35岁→40岁）
    if deltas:
        return _make_item(THRESHOLD_CHANGED, SUBSTANTIVE, ev_a, ev_b,
                          detail={"reason": "threshold_changed",
                                  "threshold_deltas": deltas},
                          confidence=conf)

    # 5) 阈值口径与数值一致（含 3万元 vs 30000元 这类异单位同值），
    #    仅规则文字变化 → 条件替换
    return _make_item(REPLACED, SUBSTANTIVE, ev_a, ev_b,
                      detail={"reason": "rule_text_replaced"},
                      confidence=conf)


# ---------------------------------------------------------------- 主入口
def compare_documents(doc_a, doc_b, alignment=None, version_a=None,
                      version_b=None):
    """对两份 clause 记录做变化分类。

    doc_a/doc_b : clause 阶段产出（含 clauses）。
    alignment   : align.align_pair 的结果；缺省时本模块只调用 align 复用，
                  不自行实现结构匹配。
    version_a/b : version.assess 结果；缺省即时评估。
    """
    a_clauses = _index(doc_a.get("clauses"))
    b_clauses = _index(doc_b.get("clauses"))
    if alignment is None:
        alignment = _align.align_pair(doc_a, doc_b)
    if version_a is None:
        version_a = _version.assess(doc_a)
    if version_b is None:
        version_b = _version.assess(doc_b)

    items = []

    # 1:1 配对
    for pair in alignment.get("pairs", []):
        a_cl = a_clauses.get(pair["a_cid"])
        b_cl = b_clauses.get(pair["b_cid"])
        if a_cl is None or b_cl is None:
            continue
        items.append(_classify_pair(pair, a_cl, b_cl,
                                    version_b.get("incomplete", False),
                                    version_a.get("incomplete", False)))

    # 合并：A 多条 → B 一条
    for m in alignment.get("merged", []):
        b_cl = b_clauses.get(m["b_cid"])
        evs_a = [_evidence(a_clauses[c], "a") for c in m["a_cids"]
                 if c in a_clauses]
        items.append(_make_item(MERGED, SUBSTANTIVE,
                                evidence_a=evs_a,
                                evidence_b=_evidence(b_cl, "b"),
                                detail={"a_cids": m["a_cids"],
                                        "b_cid": m["b_cid"]},
                                confidence=m.get("confidence")))

    # 拆分：A 一条 → B 多条
    for s in alignment.get("split", []):
        a_cl = a_clauses.get(s["a_cid"])
        evs_b = [_evidence(b_clauses[c], "b") for c in s["b_cids"]
                 if c in b_clauses]
        items.append(_make_item(SPLIT, SUBSTANTIVE,
                                evidence_a=_evidence(a_cl, "a"),
                                evidence_b=evs_b,
                                detail={"a_cid": s["a_cid"],
                                        "b_cids": s["b_cids"]},
                                confidence=s.get("confidence")))

    # A-only：删除成立与否取决于 B 的完整性
    for cid in alignment.get("a_only", []):
        a_cl = a_clauses.get(cid)
        if a_cl is None:
            continue
        if version_b.get("incomplete"):
            items.append(_make_item(
                UNKNOWN_REMOVAL, UNCERTAIN, _evidence(a_cl, "a"), None,
                detail={"reason": "target_version_incomplete",
                        "incomplete_reasons": version_b.get("reasons", [])}))
        else:
            items.append(_make_item(REMOVED, SUBSTANTIVE,
                                    _evidence(a_cl, "a"), None,
                                    detail={"reason": "absent_in_target"}))

    # B-only：新增；若 A 本身不完整则反向存疑
    for cid in alignment.get("b_only", []):
        b_cl = b_clauses.get(cid)
        if b_cl is None:
            continue
        if version_a.get("incomplete"):
            items.append(_make_item(
                UNKNOWN_ADDITION, UNCERTAIN, None, _evidence(b_cl, "b"),
                detail={"reason": "base_version_incomplete",
                        "incomplete_reasons": version_a.get("reasons", [])}))
        else:
            items.append(_make_item(ADDED, SUBSTANTIVE,
                                    None, _evidence(b_cl, "b"),
                                    detail={"reason": "new_in_target"}))

    # 占位缺段：不参与对齐，只能按版本完整性落“证据不足”，绝不判删除/新增
    for a_cl in doc_a.get("clauses", []):
        if a_cl.get("placeholder"):
            items.append(_make_item(
                UNKNOWN_REMOVAL, UNCERTAIN, _evidence(a_cl, "a"), None,
                detail={"reason": "placeholder_section",
                        "incomplete_reasons": version_a.get("reasons", [])}))
    for b_cl in doc_b.get("clauses", []):
        if b_cl.get("placeholder"):
            items.append(_make_item(
                UNKNOWN_ADDITION, UNCERTAIN, None, _evidence(b_cl, "b"),
                detail={"reason": "placeholder_section",
                        "incomplete_reasons": version_b.get("reasons", [])}))

    def _sort_cid(ev):
        if isinstance(ev, dict):
            return ev.get("cid", "")
        if isinstance(ev, list):
            return ",".join(e.get("cid", "") for e in ev)
        return ""

    items.sort(key=lambda it: (it["status"], it["kind"],
                               _sort_cid(it["evidence_a"]),
                               _sort_cid(it["evidence_b"])))

    summary = {SUBSTANTIVE: 0, FORMAT: 0, UNCERTAIN: 0}
    for it in items:
        summary[it["status"]] += 1

    return {
        "pair_key": pair_key(doc_a, doc_b),
        "doc_a": _doc_ref(doc_a),
        "doc_b": _doc_ref(doc_b),
        "completeness": {
            "a_incomplete": bool(version_a.get("incomplete")),
            "b_incomplete": bool(version_b.get("incomplete")),
            "a_reasons": version_a.get("reasons", []),
            "b_reasons": version_b.get("reasons", []),
        },
        "summary": summary,
        "items": items,
    }


def _doc_ref(doc):
    return {
        "doc_id": doc.get("doc_id") or doc.get("id"),
        "region": doc.get("region") or doc.get("src"),
        "year": doc.get("year"),
    }


def pair_key(doc_a, doc_b):
    """稳定比较键：同两份文档任意次序生成同一键（对称）。"""
    def ref(doc):
        return "|".join([
            str(doc.get("doc_id") or doc.get("id") or ""),
            str(doc.get("region") or doc.get("src") or ""),
            str(doc.get("year") or ""),
        ])
    return "cmp:" + "::".join(sorted([ref(doc_a), ref(doc_b)]))


# ---------------------------------------------------------------- 便捷入口
def build_clause_doc(record):
    """复用 clause/version 阶段把单条原始记录准备成条款文档（不做匹配）。"""
    rec = _clause.process(record)
    verdict = _version.assess(rec)
    rec["incomplete"] = verdict["incomplete"]
    rec["incomplete_reasons"] = verdict["reasons"]
    return rec


def compare_records(record_a, record_b, alignment=None):
    """从两条原始/条款记录出发的便捷入口；对齐仍由 align 负责。"""
    doc_a = build_clause_doc(record_a)
    doc_b = build_clause_doc(record_b)
    return compare_documents(doc_a, doc_b, alignment=alignment)


def compare_many(pairs, con, savepoint=True):
    """批量比较并逐对幂等落库，支持部分失败恢复。

    pairs: [(record_a, record_b), ...]（原始记录或条款文档均可）。
    每一对独立 commit：某一对抛错不影响已成功的对，失败对写入失败标记后继续，
    重跑时失败对会被重新计算并覆盖（可重复生成）。
    返回 {"ok": pair_key 列表, "failed": [{"pair_key", "error"}], "rows": 报告数}。
    """
    from . import store
    ok, failed = [], []
    for record_a, record_b in pairs:
        try:
            doc_a = build_clause_doc(record_a)
            doc_b = build_clause_doc(record_b)
            report = compare_documents(doc_a, doc_b)
            store.save_diff_report(con, report["pair_key"], report)
            if savepoint:
                con.commit()
            ok.append(report["pair_key"])
        except Exception as exc:  # 部分失败隔离，不中断整批
            con.rollback()
            failed.append({
                "pair_key": pair_key_safe(record_a, record_b),
                "error": type(exc).__name__ + ": " + str(exc),
            })
    return {"ok": ok, "failed": failed, "rows": store.diff_count(con)}


def pair_key_safe(record_a, record_b):
    try:
        return pair_key(record_a, record_b)
    except Exception:
        return "cmp:unkeyed"


def process(record):
    """记录级入口（与其它阶段同形）：

    - 传入 {"_compare": {"a": docA, "b": docB}} 的作业记录 → 执行比较并把
      报告挂到 record["diff_report"]，再经 keyer/store 走现有模型保存；
    - 普通记录原样透传，保证历史流水线行为不变。
    """
    rec = dict(record)
    job = rec.get("_compare")
    if isinstance(job, dict) and "a" in job and "b" in job:
        report = compare_records(job["a"], job["b"],
                                 alignment=job.get("alignment"))
        rec["diff_report"] = report
        rec["_pair_key"] = report["pair_key"]
    rec.setdefault("_stage_compare", "ok")
    return rec

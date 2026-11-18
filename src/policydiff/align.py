"""单一职责：跨文档条款对齐（align），输出匹配置信度。

不负责差异分类（compare 的职责），只回答“哪些条款是同一主题、置信多高”。
输入为 clause 阶段产出的两份条款记录；输出对齐结构：

    {
      "pairs":   [{"a_cid", "b_cid", "score", "confidence", "kind"}],
      "merged":  [{"a_cids": [...], "b_cid", "score", "confidence"}],
      "split":   [{"a_cid", "b_cids": [...], "score", "confidence"}],
      "a_only":  [cid, ...],
      "b_only":  [cid, ...],
    }

判定顺序（保证合并/拆分不被 1:1 贪心抢配）：
  1) 合并：B 单条对 A 多条的“包含度”达门槛（A 组主题被 B 覆盖）；
  2) 拆分：A 单条对 B 多条的包含度达门槛；
  3) 剩余条款做确定性贪心 1:1。
confidence: high(>=0.80) / medium(>=0.55) / low(<0.55)。
低置信配对仍会输出，由 compare 决定是否只能落“证据不足”状态。
所有同分情况按 cid 次序打破平局，保证结果可重复生成。
"""
from .normalize import normalize_text

HIGH = 0.80
MEDIUM = 0.55
LOW_PAIR = 0.35          # 综合分下限：低于此值视为无对齐，交由新增/删除处理
TOPIC_FLOOR = 0.30       # 主题相似度过低时，编号一致也不能强行配对
CONTAIN = 0.55           # 合并/拆分的包含关系门槛
GROUP_MIN = 2            # 成组至少 2 条才构成合并/拆分


def _jaccard(set_a, set_b):
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _containment(small, big):
    """非对称包含度：small 的主题有多少被 big 覆盖（small 为空返回 0）。"""
    if not small:
        return 0.0
    return len(small & big) / len(small)


def _number_score(a, b):
    na, nb = a.get("number"), b.get("number")
    if na and nb and na == nb:
        return 1.0
    if na and nb:
        return 0.3
    return 0.0


def _topics(clause):
    return set(clause.get("topic") or [])


def pair_score(a, b):
    """主题 0.75 + 编号 0.1 + 归一化文本 0.15，截断到 [0,1]。

    编号只是弱信号：同编号但主题不相关的条款不能被编号强行配对。
    """
    topic = _jaccard(_topics(a), _topics(b))
    number = _number_score(a, b)
    text = _jaccard(set(normalize_text(a.get("text", "")).split()),
                    set(normalize_text(b.get("text", "")).split()))
    total = round(min(1.0, 0.75 * topic + 0.1 * number + 0.15 * text), 6)
    if topic < TOPIC_FLOOR:
        return 0.0
    return total


def confidence_of(score):
    if score >= HIGH:
        return "high"
    if score >= MEDIUM:
        return "medium"
    return "low"


def _group_topics(group):
    union = set()
    for c in group:
        union |= _topics(c)
    return union


def align_pair(doc_a, doc_b):
    """对两份 clause 记录做确定性对齐。占位缺段条款不参与对齐。"""
    a_list = sorted((c for c in doc_a.get("clauses", [])
                     if not c.get("placeholder")), key=lambda c: c["cid"])
    b_list = sorted((c for c in doc_b.get("clauses", [])
                     if not c.get("placeholder")), key=lambda c: c["cid"])

    used_a, used_b = set(), set()
    merged, split = [], []

    # 1) 合并：B 一条覆盖 A 多条（A 组每条都被该 B 条充分包含）
    for b in b_list:
        bt = _topics(b)
        covered = sorted(
            (a for a in a_list if _containment(_topics(a), bt) >= CONTAIN),
            key=lambda c: c["cid"],
        )
        if len(covered) >= GROUP_MIN:
            cids = [a["cid"] for a in covered]
            score = round(_jaccard(_group_topics(covered), bt), 6)
            merged.append({"a_cids": cids, "b_cid": b["cid"],
                           "score": score,
                           "confidence": confidence_of(score)})
            used_a.update(cids)
            used_b.add(b["cid"])

    # 2) 拆分：A 一条覆盖 B 多条（跳过已用于合并的条款）
    for a in a_list:
        if a["cid"] in used_a:
            continue
        at = _topics(a)
        covered = sorted(
            (b for b in b_list
             if b["cid"] not in used_b
             and _containment(_topics(b), at) >= CONTAIN),
            key=lambda c: c["cid"],
        )
        if len(covered) >= GROUP_MIN:
            cids = [b["cid"] for b in covered]
            score = round(_jaccard(at, _group_topics(covered)), 6)
            split.append({"a_cid": a["cid"], "b_cids": cids,
                          "score": score,
                          "confidence": confidence_of(score)})
            used_a.add(a["cid"])
            used_b.update(cids)

    # 3) 剩余条款做确定性贪心 1:1
    candidates = []
    for a in a_list:
        if a["cid"] in used_a:
            continue
        for b in b_list:
            if b["cid"] in used_b:
                continue
            s = pair_score(a, b)
            if s >= LOW_PAIR:
                candidates.append((s, a["cid"], b["cid"]))
    candidates.sort(key=lambda x: (-x[0], x[1], x[2]))

    pairs = []
    for s, acid, bcid in candidates:
        if acid in used_a or bcid in used_b:
            continue
        used_a.add(acid)
        used_b.add(bcid)
        pairs.append({"a_cid": acid, "b_cid": bcid,
                      "score": s, "confidence": confidence_of(s),
                      "kind": "one_to_one"})

    return {
        "pairs": sorted(pairs, key=lambda p: (p["a_cid"], p["b_cid"])),
        "merged": sorted(merged,
                         key=lambda m: (m["b_cid"],) + tuple(m["a_cids"])),
        "split": sorted(split,
                        key=lambda s: (s["a_cid"],) + tuple(s["b_cids"])),
        "a_only": sorted(c["cid"] for c in a_list if c["cid"] not in used_a),
        "b_only": sorted(c["cid"] for c in b_list if c["cid"] not in used_b),
    }


def process(record):
    """流水线内的单记录透传标记；成对对齐请使用 align_pair。"""
    rec = dict(record)
    rec.setdefault("_stage_align", "naive")
    return rec

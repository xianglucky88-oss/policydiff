"""单一职责：条款边界与条款内容抽取（clause）。

消费 doc_parse 产出的结构块（或已预结构化的 clauses 字段，或历史记录），
产出统一的条款记录：

    {"doc_id", "doc_meta": {...}, "clauses": [Clause, ...], "incomplete": bool, ...}

Clause 字段：
    cid        稳定标识：sha1(doc_id|number|归一化文本) 前 12 位，可重复生成
    number     条款号原文（可能为 None）
    order      文档内序号（用于识别“条款重排”这类排版噪声）
    text       条款原文（证据，不做改写）
    norm       归一化文本（与 normalize.normalize_text 同口径）
    topic      主题词（归一化，用于对齐）
    thresholds 阈值抽取结果：[{"metric", "value", "unit", "base", "dim", "raw"}]

本模块不做跨文档对齐（align 的职责），也不做差异分类（compare 的职责）。
"""
import hashlib
import re

from .normalize import normalize_text

# ---------------------------------------------------------------- 中文数字
_CN_DIGITS = {"零": 0, "〇": 0, "两": 2, "一": 1, "二": 2, "三": 3,
              "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(token):
    """把简单中文数字（含“万”以内）转为 int；无法解析时返回 None。"""
    if token.isdigit():
        return int(token)
    total, section, digit = 0, 0, 0
    saw = False
    for ch in token:
        if ch in _CN_DIGITS:
            digit = _CN_DIGITS[ch]
            section = digit
            saw = True
        elif ch == "十":
            section = (digit or 1) * 10
            digit = 0
            saw = True
        elif ch == "百":
            total += (section or digit or 1) * 100
            section, digit = 0, 0
            saw = True
        elif ch == "千":
            total += (section or digit or 1) * 1000
            section, digit = 0, 0
            saw = True
        elif ch == "万":
            total = (total + section or 1) * 10000
            section, digit = 0, 0
            saw = True
        else:
            return None
    return total + section if saw else None


# ---------------------------------------------------------------- 阈值抽取
_NUM = r"(?P<num>\d+(?:\.\d+)?|[一二三四五六七八九十百零〇两]+)"
# 前置负向断言：排除日期（2026年1月1日 中的 1日、1月）
_MONEY_RE = re.compile(_NUM + r"\s*(?P<unit>亿元|万元|元|万)")
_PERCENT_RE = re.compile(_NUM + r"\s*(?P<unit>%|％|百分比|个百分点)")
_DAY_RE = re.compile(r"(?<!\d年\d月)(?<!\d年\d\d月)"
                     + _NUM + r"\s*(?P<unit>天|日)")
_MONTH_RE = re.compile(r"(?<!\d年)" + _NUM + r"\s*(?P<unit>个?月)")
# 施行/生效日期里的“2024年”“自…年…月…日”不属于规则阈值
_YEAR_RE = re.compile(r"(?<!自)(?<!\d)" + _NUM +
                      r"\s*(?P<unit>年)(?!\d{1,2}月)(?!龄)")
_AGE_RE = re.compile(_NUM + r"\s*(?:周)?岁")
_TIMES_RE = re.compile(_NUM + r"\s*(?P<unit>次|倍)")

# 单位 → (基准单位, 换算到基准单位的乘数, 维度)
_UNIT_TABLE = {
    "元":     ("元", 1.0, "money"),
    "万元":   ("元", 10000.0, "money"),
    "亿元":   ("元", 100000000.0, "money"),
    "万":     ("元", 10000.0, "money"),
    "%":      ("%", 1.0, "percent"),
    "％":     ("%", 1.0, "percent"),
    "百分比": ("%", 1.0, "percent"),
    "个百分点": ("%", 1.0, "percent"),
    "天":     ("天", 1.0, "days"),
    "日":     ("天", 1.0, "days"),
    "个月":   ("月", 1.0, "months"),
    "月":     ("月", 1.0, "months"),
    "年":     ("年", 1.0, "years"),
    "次":     ("次", 1.0, "times"),
    "倍":     ("倍", 1.0, "times"),
}


def _to_number(token):
    if token[0].isdigit():
        return float(token) if "." in token else int(token)
    return cn_to_int(token)


def extract_thresholds(text):
    """从条款原文抽取量化阈值；同一句内按出现顺序、去重后返回。"""
    found = []
    seen = set()
    patterns = (_MONEY_RE, _PERCENT_RE, _DAY_RE,
                _MONTH_RE, _YEAR_RE, _TIMES_RE)
    for rx in patterns:
        for m in rx.finditer(text or ""):
            num = _to_number(m.group("num"))
            if num is None:
                continue
            unit = m.group("unit")
            base, factor, dim = _UNIT_TABLE[unit]
            item = {
                "metric": dim,
                "value": num,
                "unit": unit,
                "base": round(float(num) * factor, 6),
                "dim": dim,
                "raw": m.group(0),
            }
            sig = (item["dim"], item["base"])
            if sig not in seen:
                seen.add(sig)
                found.append(item)
    # 年龄是独立维度，单位固定为“岁”，单独抽取
    for m in _AGE_RE.finditer(text or ""):
        num = _to_number(m.group("num"))
        if num is None:
            continue
        item = {"metric": "age", "value": num, "unit": "岁",
                "base": float(num), "dim": "age", "raw": m.group(0)}
        sig = ("age", item["base"])
        if sig not in seen:
            seen.add(sig)
            found.append(item)
    # 保持“在原文中的出现顺序”，保证可重复
    found.sort(key=lambda x: (text or "").find(x["raw"]))
    return found


# ---------------------------------------------------------------- 主题
# 拉丁词中的高频虚词，剔除后更能代表“同一主题”
_STOPWORDS = {
    "a", "an", "the", "of", "and",
    "or", "to", "in", "for", "on", "with", "is", "are", "be",
}


_CJK_RUN_RE = re.compile(r"[一-鿿0-9#]+")
_LATIN_RUN_RE = re.compile(r"[a-z][a-z0-9]+")
# 数字出现位置统一占位，避免“3万 / 30000元”“35岁 / 40岁”里的数值本身打散主题
_NUM_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?|[一二三四五六七八九十百零〇两]+")


def derive_topic(text):
    """从条款归一化文本派生主题词集合（用于 align 的主题相似度）。

    中文/数字连续段以字符二元组为主（轻微改写仍可对齐），短段整体保留；
    数值统一替换为 # 占位，使同结构、不同数值的条款仍能高相似对齐；
    拉丁词按词切分。纯数字片段不入主题，避免数值本身污染主题对齐。
    """
    norm = _NUM_TOKEN_RE.sub("#", normalize_text(text))
    grams = set()
    for run in _CJK_RUN_RE.findall(norm):
        if run.isdigit():
            continue
        if len(run) <= 2:
            grams.add(run)
        else:
            for i in range(len(run) - 1):
                grams.add(run[i:i + 2])
    for word in _LATIN_RUN_RE.findall(norm):
        if word not in _STOPWORDS:
            grams.add(word)
    return grams


def make_cid(doc_id, number, norm):
    raw = "|".join([str(doc_id), str(number or ""), norm])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


_PLACEHOLDER_RE = re.compile(r"^(缺|缺失|略|待补|\.{3,}|…+)$")


def _is_placeholder(text):
    return bool(_PLACEHOLDER_RE.match(normalize_text(text)))


def build_clause(doc_id, block, order):
    text = block["text"].strip()
    norm = normalize_text(text)
    return {
        "cid": make_cid(doc_id, block.get("number"), norm),
        "number": block.get("number"),
        "order": order,
        "text": text,
        "norm": norm,
        "topic": sorted(derive_topic(text)),
        "thresholds": extract_thresholds(text),
        "placeholder": _is_placeholder(text),
    }


# ---------------------------------------------------------------- 阶段入口
def process(record):
    """记录 → 条款记录。兼容三类输入：预结构化 clauses / doc_parse blocks / 历史记录。"""
    rec = dict(record)
    doc_id = rec.get("doc_id") or rec.get("id") or ""

    pre = rec.get("clauses")
    if isinstance(pre, list) and pre:
        clauses = []
        for i, c in enumerate(pre):
            text = str(c.get("text", ""))
            number = c.get("number")
            norm = normalize_text(text)
            clauses.append({
                "cid": c.get("cid") or make_cid(doc_id, number, norm),
                "number": number,
                "order": c.get("order", i),
                "text": text,
                "norm": norm,
                "topic": sorted(derive_topic(text)),
                "thresholds": c.get("thresholds") or extract_thresholds(text),
                "placeholder": bool(c.get("placeholder")) or _is_placeholder(text),
            })
    elif isinstance(rec.get("_blocks"), list) and rec["_blocks"]:
        clauses = [build_clause(doc_id, b, i)
                   for i, b in enumerate(rec["_blocks"])]
    else:
        # 历史兼容：非政策文档记录，退化为单条款，保证流水线不丢记录
        text = str(rec.get("name") or rec.get("text") or "")
        if rec.get("value"):
            text = (text + " " + str(rec["value"])).strip()
        clauses = [build_clause(doc_id, {"number": None, "text": text}, 0)]

    rec["doc_id"] = doc_id
    rec["clauses"] = clauses
    rec.setdefault("_stage_clause", "ok")
    return rec

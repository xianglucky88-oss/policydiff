"""单一职责：字段规整/规范化。

- 记录级规整：src/name/value/year 等字段清洗（保持历史输入兼容）。
- 文本规整：提供 normalize_text，供 clause/align/compare 复用，
  保证“纯排版差异”在各阶段得到一致的归一化口径。
"""
import re
import unicodedata

_WS_RE = re.compile(r"\s+")
# 标点统一后，去掉 ASCII 逗号/分号/冒号/句号两侧多余空白（排版噪声归一）；
# 小数点两侧不处理，保留 3.5 这类数值形态
_PAD_RE = re.compile(r"\s*([,;:.])(?!\d)\s*")
# 统一中英文引号/括号，避免标点形态被误判为实质差异
_PUNCT_MAP = str.maketrans({
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "「": '"', "」": '"', "【": "(", "】": ")",
    "（": "(", "）": ")", "，": ",", "。": ".",
    "；": ";", "：": ":", "？": "?", "！": "!",
})


def normalize_text(text):
    """把原文归一化为可比较形态：NFKC + 标点统一 + 空白折叠 + 小写。"""
    if text is None:
        return ""
    s = unicodedata.normalize("NFKC", str(text))
    s = s.translate(_PUNCT_MAP)
    s = _WS_RE.sub(" ", s)
    s = _PAD_RE.sub(r"\1", s)
    return s.strip().lower()


def process(record):
    rec = dict(record)
    rec["src"] = str(rec.get("src", "")).strip().lower()
    rec["name"] = str(rec.get("name", "")).strip()
    # 历史输入可能缺 value；保持 int 口径，缺省为 0
    if rec.get("value") in (None, ""):
        rec["value"] = 0
    else:
        rec["value"] = int(rec["value"])
    if rec.get("year") not in (None, ""):
        try:
            rec["year"] = int(rec["year"])
        except (TypeError, ValueError):
            pass
    if rec.get("text") is not None:
        rec["text"] = str(rec["text"])
    return rec

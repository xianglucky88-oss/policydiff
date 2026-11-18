"""单一职责：文档解析——把政策文档文本切成带条款号的结构块（block）。

本阶段只负责“结构块”，不负责条款边界的最终确认（clause 阶段的职责）。
输入记录两类：
  1. 政策文档：{"doc_id", "region"/"src", "year", "title", "text", ...}
  2. 历史/非文档记录：原样透传，交由 clause 阶段走兼容分支。

block 结构：{"number": str|None, "text": str, "order": int}
"""
import re

# 常见条款号：第X条 / 一、 / 1. / 1、 / （一） / (一) / 第一章 等
_NUM_ATOM = r"[0-9]+|[一二三四五六七八九十百零〇两]+"
CLAUSE_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"第\s*(?:" + _NUM_ATOM + r")\s*[条章节项]"
    r"|（\s*(?:" + _NUM_ATOM + r")\s*）"
    r"|\(\s*(?:" + _NUM_ATOM + r")\s*\)"
    r"|(?:" + _NUM_ATOM + r")\s*[、.．]"
    r")"
)


def split_blocks(text):
    """按条款号行首切分；无条款号时整段作为一个无号 block。"""
    if text is None:
        return []
    blocks = []
    current = None
    order = 0
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = CLAUSE_HEADING_RE.match(line)
        if m:
            if current is not None:
                blocks.append(current)
            number = m.group(0).strip()
            current = {"number": number,
                       "text": line,
                       "order": order}
            order += 1
        else:
            if current is None:
                current = {"number": None, "text": line, "order": order}
                order += 1
            else:
                current["text"] += "\n" + line
    if current is not None:
        blocks.append(current)
    return blocks


def process(record):
    rec = dict(record)
    text = rec.get("text")
    if isinstance(text, str) and text.strip():
        rec["_blocks"] = split_blocks(text)
    rec.setdefault("_stage_doc_parse", "ok")
    return rec

"""单一职责：从本地 fixtures 读取原始记录（离线摄取入口，不联网）。"""
import json
from .config import FIXTURES


def load():
    with open(FIXTURES, encoding="utf-8") as f:
        return json.load(f)


def load_path(path):
    """从任意本地路径读取记录列表（政策文档夹具等），保持离线纯函数。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("fixture must be a JSON list of records: %r" % path)
    return data

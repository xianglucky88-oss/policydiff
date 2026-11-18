"""测试夹具加载辅助：从 fixtures/policies 读取政策文档。"""
from pathlib import Path

from policydiff import clause, doc_parse, loader

POLICY_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "policies"


def load_fixture(name):
    return loader.load_path(POLICY_DIR / name)


def prepare(name):
    """加载并跑 doc_parse → clause，返回条款文档列表。"""
    return [clause.process(doc_parse.process(r))
            for r in load_fixture(name)]


def prepare_one(record):
    return clause.process(doc_parse.process(record))

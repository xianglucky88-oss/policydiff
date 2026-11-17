"""单一职责：对记录做单一职责变换：doc_parse。（作为流水线阶段）"""
def process(record):
    rec = dict(record)
    rec.setdefault("_stage_doc_parse", "ok")
    return rec

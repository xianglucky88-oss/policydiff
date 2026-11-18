"""单一职责：version 阶段——规则版本钉住与历史回放支持。

把归一化实际使用的规则版本随记录落库（每个文本字段 + 汇总视图），
使得旧记录可按其 pin 住的版本回放；规则升级后重跑才会产生新版本证据，
升级前后的差异可以按 rule_version 区分。
"""


def process(record):
    rec = dict(record)
    norm = rec.get("_norm") or {}
    versions = sorted({result["rule_version"] for result in norm.values()})
    if versions:
        rec["_rules_versions"] = versions
        for field, result in norm.items():
            result.setdefault("pinned_version", result["rule_version"])
    else:
        rec.setdefault("_rules_versions", [])
    rec.setdefault("_stage_version", "ok")
    return rec

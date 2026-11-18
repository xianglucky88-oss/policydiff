# policydiff — 抓不同地区政策文件，结构化抽条款，跨地区/跨年度差异对比与口径归并。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网；本地仅 Python 3.11 亦可运行，无第三方依赖）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalize` → `doc_parse` → `clause` → `align` → `compare` → `version` → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量和落库行数
make test    # 运行项目测试，包括流水线和幂等性检查
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`clause`, `compare`, `align`, `normalize`, `doc_parse`, `version`。
通用模块：`loader / normalize / keyer / store / pipeline / cli`。

## 文本归一化的边界（normalize.py）
归一化只消除**可证明的噪声**，证据不足的字符一律保留，避免“多删一个否定词/负号
就让政策差异得出相反结论”。每条规则都在 `normalize.RULES` 登记 ID、版本与书面理由。

只清洗：
- 编码噪声：NFC 组合；全角字母/数字按 Unicode 恒等式折叠（全角标点、括号保留）；
- 空白噪声：TAB / NBSP / 全角空格等水平空白折叠为单个 ASCII 空格、行首尾修剪、
  CRLF/CR/VT/FF/NEL/LS/PS 规整为 `\n`、空行压缩；
- 版式噪声：BOM(U+FEFF)、零宽空格(U+200B)、软连字符(U+00AD)；
- 印刷变体：硬连字符 U+2010/U+2011 → `-`。

证据不足而保留（v1 不写删除/折叠规则）：否定词与否定语素、负号 U+2212、
OCR 替换符 U+FFFD、ZWJ/ZWNJ、法律数字、货币符号、计量单位、括号、
en/em dash、引号与省略号、条款边界换行。

## 可追溯数据契约
每个文本字段经 `normalize_text` 产出：

| 字段 | 含义 |
| --- | --- |
| `text` | 归一化文本 |
| `map` | 每个输出字符 → 原文区间（NFC 组合时一条映射对应多个连续子区间） |
| `dropped` | 每个被**删除**码点的原文区间、规则 ID、字符与理由（与 map 互补不重叠） |
| `folds` | 每个被**替换/折叠**码点的区间、规则 ID、替换前后字符（区间仍在 map 中） |
| `rule_version` | 该字段实际使用并 pin 住的规则版本 |
| `source_len` | 原文长度 |

不变量：`map` 与 `dropped` 的区间合并后无缝完整覆盖 `[0, source_len)`；
`recover_text` 据此逐事件回放，**原文始终可逐字符重建**。记录级 `process` 还会在
处理前把原文快照到 `_raw`，即使旧契约要求就地 strip `name` 也不丢原文。

契约沿流水线传递：`normalize._norm` → `doc_parse._provenance`（含输出区间到原文
区间的翻译）→ `clause._clauses`（每条条款带 `out_span` 与 `src_span`）→
`compare._compare`（归一化文本指纹）。

## 幂等、规则版本与历史回放
- 幂等：对归一化输出再次归一化得到完全相同的文本，且不再产生任何 `dropped`。
- 规则版本：当前 `RULE_VERSION = 1.0.0`；升级只追加规则（如 1.1.0 追加智能引号
  折叠 `punct-smart-quote`），不就地改写旧规则。记录在 `version` 阶段 pin 住
  `_rules_versions`，旧记录按其版本回放结果与落库逐字段一致。
- 分批恢复：`normalize.backfill(records, rule_version)` 是惰性生成器，可按批用
  指定版本重新归一化，原文通过 `recover_record` 回看。

## 格式等价与实质差异（compare.py）
- 纯噪声变体归一化后文本相同 → 指纹相同（格式等价，不报告差异）；
- 否定词、负号、法律数字、金额单位、括号、OCR 字符等被保守保留，任何实质修改都会
  改变归一化文本与指纹，下游一定能发现；`diff_records` 按字段/条款给出差异及
  双方原文回看区间。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于 `tests/`，
覆盖归一化契约、跨阶段传递、边界稳定、实质差异敏感、规则升级回放与分批恢复，
运行方式以 `Makefile` 为准。

# policydiff — 抓不同地区政策文件，结构化抽条款，跨地区/跨年度差异对比与口径归并。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalize` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量和落库行数
make test    # 运行项目测试，包括流水线、幂等性、跨版本对齐与故障恢复
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`clause`, `compare`, `align`, `normalize`, `doc_parse`, `version`。
通用模块：`loader / normalize / keyer / store / pipeline / cli`。

## 模块说明
业务模块按独立阶段组织，通用模块负责加载、规范化、主键、存储和编排。
流水线按配置顺序调用处理阶段，输入输出均遵循项目定义的记录结构。

## 跨版本结构对齐契约（`align`）

`align` 只回答“哪个条款对应哪个条款”，不重写条款正文；正文始终来自上游
`doc_parse` / `clause` 的解析输出，对齐结果只保存条款引用、正文哈希和来源锚点
（`doc_id / version / section / number / attachment / ordinal`）。

- **稳定匹配**：编号变化、章节重排、同义标题（如“总则 / 一般规定”“适用范围 /
  适用对象”）与附件插入都不会切断正文相同的条款；编号只占很小权重，位置只做
  tie-break。
- **方向稳定**：两份文档先按 `(version, doc_id, doc_hash)` 规范化排序，交换到达
  顺序得到相同的组、候选和 `group_id`。
- **置信分档**：`score >= 0.80` 自动接受；`0.45 ~ 0.80` 进入 `pending` 待人工；
  `0.30 ~ 0.45` 只记候选不入映射；低于 `0.30` 视为噪声。
- **一对多 / 多对一**：拆分为 `split`、合并为 `merge`，按“子条款并集覆盖父条款”
  成组识别，默认进入 `pending`，不会退化成“最高相似度那一对 + 其余算新增”。
  N→M 交叉情形记为 `ambiguous`，一律待审。
- **人工决定优先**：`confirmed` / `rejected` 组带 `manual` 标记。重算不会覆盖人工
  决定；冲突的新候选以 `conflict` 落候选表；被拒绝的相似关系若再次出现，仍保持
  拒绝并在比较结果里报告为需复核冲突。
- **可恢复确认**：确认/拒绝先写 journal，再以单事务应用；`align.recover()` 依据
  持久状态重放或回滚，应用前核对成员锚点。旧 schema / 旧状态值可就地迁移，旧对齐
  结果保持可读。

常用入口：`align.align_versions`、`align.confirm` / `align.reject` / `align.recover`、
`align.pending`、`align.version_map`、`compare.compare_versions`、
`version.build_chains`。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。

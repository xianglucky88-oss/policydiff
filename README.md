# policydiff — 抓不同地区政策文件，结构化抽条款，跨地区/跨年度差异对比与口径归并。

运行栈：Python 3 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalize` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量和落库行数
make test    # 运行项目测试，包括流水线、条款抽取、对齐、差异分类与幂等/恢复
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。

```text
loader → normalize → doc_parse → clause → align → version → keyer → store(records)
                                          └──────── compare（消费 clause + align + version）
                                                          └→ store(diffs，稳定键幂等)
```

业务模块：`clause`, `compare`, `align`, `normalize`, `doc_parse`, `version`。
通用模块：`loader / keyer / store / pipeline / cli / models`。

### 各阶段职责
- `doc_parse`：按条款号把政策文本切成结构块（block），不确认最终条款边界。
- `clause`：确认条款边界，产出稳定 `cid`、原文证据、归一化文本、主题词与量化阈值
  （额度/比例/期限/年龄/次数，按基准单位换算）。不做跨文档匹配。
- `align`：只做跨文档条款对齐，输出 1:1 / 合并 / 拆分关系与 `high|medium|low` 置信度；
  同分时按 `cid` 打破平局，结果确定、可重复。占位缺段条款不参与对齐。
- `version`：评估版本是否完整（显式标记、状态、缺段清单、截断声明、占位条款）。
- `compare`：**只消费已形成的条款与对齐关系做变化分类和证据，不另行实现结构匹配**。

## 差异分类：三桶状态，而非一份 diff 文本
| 桶 | 子类 | 含义 |
| --- | --- | --- |
| `substantive` 实质变化 | `added` / `removed` / `replaced` / `threshold_changed` / `merged` / `split` | 额度、条件等规则真实变化 |
| `format` 格式噪声 | `reordered` / `formatting_only` | 条款重排、空白/全半角标点等排版变化，规则不变 |
| `uncertain` 证据不足 | `unknown_removal` / `unknown_addition` / `uncertain_pair` | 版本缺段、单位口径不可比或对齐置信不足，无法下实质结论 |

### “删除”何时成立（version 决定）
- 对照版**完整**：A 有 B 无的条款可确定为 `removed`。
- 对照版**不完整**：只能标 `unknown_removal`，绝不断言删除；占位缺段同理落 `uncertain`。
- 单位口径不可直接比较（如“12个月” vs “1年”）落 `uncertain_pair`，不静默换算成增减；
  而异单位同值（“3万元” vs “30000元”）在基准单位口径下视为数值不变。

每条结论都带原文证据（条款号、原文片段、`cid`），可回链定位；报告不含时间戳，
同输入重复生成逐字节一致，`pair_key` 对输入次序对称。

## 持久化与测试
- `records` 表保存流水线记录，`diffs` 表保存差异报告，均以稳定键幂等 upsert。
- `compare.compare_many` 逐对独立提交：单对失败被隔离并记录，已成功对不回滚，重跑可补齐。
- 政策夹具位于 `fixtures/policies/`：跨年度、跨地区、合并/拆分、单位口径、排版噪声、不完整版本。
- 测试位于 `tests/`，运行方式以 `Makefile` 为准。

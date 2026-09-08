# SQL 数值与聚合处理

四份 SQL 使用当前登记的规范 CSV。数值进入 SQL 前先经过 `retail_ops/ingestion/preview.py` 的字段类型校验。空值进入 SQL 后为 `NULL`，明确的零值保留；非法数字使本次计算失败，不替换已有结果文件。

## 字段对照

| 现有字段 | 字典定义或已登记的派生含义 | 使用位置 | 是否改名 |
| --- | --- | --- | --- |
| `store_id`、`period_start`、`period_end`、`period_month` | 规范门店标识、报表首尾日和当前自然月标签 | 读取、分组及店铺与 SKU 连接 | 否 |
| 现有次数、人数和订单计数字段 | 使用字典各字段的中文定义，保持各自单位和粒度 | 四份 SQL 的规范数值入口 | 否 |
| 现有金额和比例字段 | 使用字典各字段的中文定义，比例保留既有 `*_pct` 单位 | 四份 SQL 的规范数值入口 | 否 |
| `sku_rank`、`sku_transaction_amount` | SKU 榜单位置及所选门店、周期内该 SKU 对应的成交金额 | Demo 1、Demo 2 的 Top 3 金额汇总 | 否 |
| `top3_sku_transaction_amount` | 当前 demo 中 Top 3 SKU 的成交金额合计 | 必须同时有排名 1、2、3 及三个金额；缺一项则为空 | 否 |
| `top3_sku_transaction_amount_share_pct` | Top 3 SKU 成交金额除以店铺成交金额，再乘以 100 | 缺少合计值或分母时为空 | 否 |
| 现有 `avg_*` 字段 | 对纳入窗口的已报告指标作算术平均 | 每项指标须在全部纳入行中有值；不完整时该平均值为空 | 否 |
| 现有月份前缀和 `*_feb_to_apr_*` 字段 | 保留基础指标含义的月度值和二月至四月变动 | 重复窗口摘要 | 否 |
| `transaction_recovered_with_conversion_aov_tradeoff` | 字典已登记的最新窗口复合观察条件 | 判断所需的当前值或前期值缺失时为空 | 否 |

本次不增加业务字段。`retail_value` 是 SQL 调用的数值校验函数，使用现有规范字段名选择已登记类型。源字段的含义、单位和粒度均由字典及已登记数据集确定。

## 执行

在项目根目录运行，结果输出到终端：

```bash
.venv/bin/python3 -m retail_ops.sql_runtime --query 02_demo2_cross_store_comparability.sql
```

加 `--summary` 可只看查询名称、运行引擎、结果行数和列数。`--query` 也可选同目录中的 `01_store_a_month_over_month_diagnostic.sql`、`03_store_period_panel_coverage.sql`、`04_repeated_window_panel_summary.sql`。入口只接受已登记的四份查询，读取各自明确的数据集，并在内存中计算。

已有 Demo 1 导出脚本、重复窗口摘要生成脚本及其校验脚本也使用同一数值入口。四份查询需要注册 `retail_value`；上面的 Python 命令会完成来源校验和函数注册。DuckDB 的 Python 函数接口在当前测试环境需要 NumPy，依赖统一放在 `retail_ops/requirements.txt`。

## 处理规则

- 规范 CSV 的字段、完整自然月窗口及逻辑主键先校验。重复窗口记录须先选定版本，不通过 `MAX` 或求和来任选版本。
- SKU 汇总按门店、月份及报表首尾日分组，连接时同时比较这些字段。Top 3 不完整时，其合计和占比均为空；Demo 2 沿用已有 `insufficient_data` 状态和缺失备注。Demo 1 的榜单排序依据仍按原数据合同记录。
- 每个平均值独立检查数据是否完整。覆盖标志描述窗口覆盖，不能代表全部指标有值。
- 窗口摘要的覆盖检查明确对应 2026 年二月至四月。店铺类型或区域值跨窗口不一致时，汇总元数据为空，来源行保留原值。
- 计数必须是非负整数；金额和比例沿用现有十进制输入规则。超出 SQL 数值范围或产生非有限计算结果时停止。当前诊断仍按原查询的 REAL／DOUBLE 计算和舍入；原始精确值保存在来源中。

## 验证与后续接入

四份查询在现有完整样例上的列名和数值保持一致；两个原有导出脚本产生的 CSV 字节也保持一致。回归测试覆盖空值与零、非法数字、Top 3 缺项或重复、跨窗口连接、错误的月份组合、上下文冲突，以及校验失败时保留原导出文件。

这一步读取已登记的本地规范数据。原文入口产生的110条预览记录尚未直接交给这些查询。[发布入口](../ingestion/PUBLICATION.md) 现在可以明确选择校验通过的批次，保存来源并重算 SQL、Demo 2 fact，再让一次 RAC 分析使用同一发布版本。现有命令继续读取项目样例；分析发布数据请使用 `publication_cli analyze`。

实现参考：[SQLite 聚合函数](https://www.sqlite.org/lang_aggfunc.html)、[DuckDB Python 函数接口](https://duckdb.org/docs/current/clients/python/function)。

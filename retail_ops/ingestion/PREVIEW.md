# 导入预览

入口：`python3 -m retail_ops.ingestion.preview`。输入已核对的规范 CSV，输出 JSON 预览。上传批次在这里限定为一个已确认的门店、月份和数据集。包含多个门店或月份的文件需要先按来源区块拆分，再分别传入各自的上传上下文。

`UploadContext` 由上传端传入，表示已经确认的门店、报表窗口、数据集、粒度和排名依据。以后接 LLM 时，模型只能提供提议，不能改写这份上下文。这里的 `store_id` 必须来自已确认的门店标识；真实美团门店 ID、店名别名和账号关系还需要接入来源映射。

## 字段对照

本次使用现有字段，没有改名或修改字典定义。全部 57 个字段的允许范围在 `preview.py` 中明确列出，并与七个现有 CSV 的列名逐一核对。

| 现有字段 | 字典定义或现有用途 | 使用位置 | 是否改名 |
| --- | --- | --- | --- |
| `store_id` | 源 CSV、SQL 和指标输出中的规范门店标识 | 上传上下文、源记录、校验结果 | 否 |
| `period_start`、`period_end` | 报表窗口首日、末日 | 上传上下文、源记录 | 否 |
| `period_month` | 当前月度数据的自然月标签 | 月度规范 CSV 必填，并与窗口一致；缺失时待核对 | 否 |
| `transaction_amount` | 所选时间周期内，该账号所选择条件下门店的当天支付且当天未取消的订单用户实际支付金额 | 店铺期间记录 | 否 |
| `transaction_orders` | 所选时间周期内，该账号所选择条件下门店的当天支付且当天未取消的订单量 | 店铺期间记录 | 否 |
| `sku_transaction_amount` | 所选门店、所选周期内，该 SKU 对应的成交金额 | SKU 记录 | 否 |
| `sales_volume` | 所选门店、所选周期内，该 SKU 的销量；如后台未展示，则可以为空 | SKU 记录 | 否 |
| `search_term_exposure_times` | 对应搜索词在所选窗口的曝光次数，按字典原定义使用 | 搜索词记录 | 否 |
| `estimated_income_proxy` | 字典中的预计收入类后台观察值 | 店铺期间记录 | 否 |
| 有效订单数、无效订单数 | 当前字典没有规范列，用户已明确剔除 | 精确匹配这两个 CSV 列名后忽略，输出不留条目或数值副本 | 不新增字段 |

其他指标沿用 `../data/DATA_DICTIONARY.md` 的中文定义及现有列名。这里不计算新的业务指标。金额和比例在 Python 中使用 `Decimal`；JSON 中输出精确小数字符串。缺失值输出 `null`，明确的零值保留。`*_pct` 使用现有 CSV 的数值单位，不自动乘以 100 或去掉来源中的单位标记。

## 分类与校验

| 数据集 | `grain` | `ranking_basis` |
| --- | --- | --- |
| `store_a_monthly_metrics`、`demo2_store_period_metrics`、`store_period_panel_metrics` | `store_period` | `null` |
| `store_a_top_skus` | `store_sku_period` | `not_recorded_in_current_fixture` |
| `demo2_top_skus_by_transaction_amount` | `store_sku_period` | `transaction_amount` |
| `demo2_top_skus_by_sales_volume` | `store_sku_period` | `sales_volume` |
| `demo2_top_search_terms` | `store_search_term_period` | `null` |

`dataset_id` 在本入口选择已经登记的校验规则，不作为任意文件路径使用。预览不会向对应样例 CSV 写入记录。真实上传的目标数据集需要在接入存储时登记。

- 数据集必须同时具有数据合同和明确的字段规则。即使新的数据集使用已有 `grain`，也不会自动套用同类数据集的规则。
- 来源记录的门店和日期必须与上传上下文一致。当前只支持完整自然月窗口。
- 字段名、数据粒度和 SKU 排名依据必须匹配。两个 SKU 榜分别校验，同名商品依照原文排名保留。
- 普通指标缺失可以通过校验并保持为空。缺少主键或当前月度必填的 `period_month`、未知字段、格式错误、归属冲突或重复主键时，整个上传批次保留为 `quarantined`，`validated_records` 为空。
- 提议中的字段和数值逐项对照输入 CSV。修改已知值、补出源数据没有的值、改写去向或省略已知值都会使该批次待核对。

输出记录原文件 SHA-256、字典 SHA-256、映射版本、包含代码及合同的 schema SHA-256，以及 CSV 记录在原文件中的结束行号。这些是导入元数据，不是新增的美团业务指标。

## 使用

在项目根目录运行。`--input` 应指向包含单一店铺、单一月份的规范 CSV。确认的门店、期间及类型由操作人或上传配置提供。

```bash
python3 -m retail_ops.ingestion.preview \
  --input /实际路径/已核对的店铺数据.csv \
  --dataset-id demo2_store_period_metrics \
  --store-id B \
  --period-start 2026-03-01 --period-end 2026-03-31 \
  --grain store_period
```

输出仅到终端。全部通过时退出码为 0；需要核对时为 2。可选 `--proposals` 读取 JSON 数组，每项必须包含 `dataset_id`、`grain`、`ranking_basis` 和 `record`，顺序与 CSV 数据行一致。

输入 CSV 是核对提议的来源。如果 CSV 和提议都由同一次模型输出产生，这个比较不能验证原始数据。`text_preview.py` 已支持已登记的中文整理格式：从原文定位门店、期间、报表区块、字段和值，再调用这里的检查。来源格式和映射见 `TEXT_SOURCE.md`。

## 后续接入

其他文本格式、实际后台来源页面与单位映射、真实门店目录、跨批次去重与快照版本、SQL 写入和 RAC 读取尚未接通。当前预览通过表示本模块的检查通过，不能据此直接发布到分析库。

四份现有 SQL 已接入规范数值校验，并检查 Top 3 和平均值所需的数据是否完整，详见 [SQL 数值与聚合处理](../sql/README.md)。真实批次仍需通过事务发布明确的版本，SQL 和 RAC 从这些版本读取数据。重复上传和重叠快照的处理要在发布层验证；本入口目前只检查同一次上传中的重复主键。

新字段或新分类启用时，先核对字典定义及命名，再登记数据合同、字段类型、来源映射和目标存储，并补充正常样例与误路由样例。未知类型没有默认目标。

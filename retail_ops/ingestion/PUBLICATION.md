# 批次发布与固定版本分析

`publication_cli` 从明确列出的校验通过批次生成一个发布版本。发布目录包含所选上传原文、登记与校验结果、规范 CSV 视图、重新计算的四份 SQL 结果、Demo 2 fact，以及说明这些内容来自哪里的 manifest。

新增的 `publish --source-records` 可发布实际窗口来源记录，供 [只读日期查询](SOURCE_QUERY.md) 反复读取；该发布由数据选择触发，不由每次查询触发。原默认发布仍生成月度分析证据。

每次分析指定一个 `publication_id`。入口核对完整快照后，将验证过的字节复制到本次运行的独立目录；SQL、Demo 2 fact 和 RAC 都使用这个目录。运行期间接收或发布其他数据，不会改变本次分析的输入。

## 字段与视图对照

业务字段名称和字典中文定义保持原样。

| 现有字段 | 字典定义或既有用途 | 使用位置 | 是否改名 |
|---|---|---|---|
| `store_id` | 源 CSV、SQL 和指标输出中的规范门店标识 | 批次归属、视图主键及来源追溯 | 否 |
| `period_start`、`period_end`、`period_month` | 报表首日、末日和自然月标签 | 选择完整店月快照，核对分析窗口 | 否 |
| `transaction_amount` | 所选时间周期内，该账号所选择条件下门店的当天支付且当天未取消的订单用户实际支付金额 | 精确保存在规范视图，再交给既有 SQL 和 RAC | 否 |
| `transaction_orders` | 所选时间周期内，该账号所选择条件下门店的当天支付且当天未取消的订单量 | 缺值与显式零分别保留 | 否 |
| `sku_rank`、`sku_transaction_amount`、`sales_volume` | 原榜单排名、该 SKU 在所选店铺及周期内的成交金额、销量 | 整份榜单选择，沿用字典口径 | 否 |
| `dataset_id`、`source_path`、`grain`、`ranking_basis` | 既有数据集、来源路径、粒度及榜单依据登记 | 批次原登记及发布视图 | 否 |
| `batch_id`、`file_sha256`、`source_line_end` | 既有接入 ID、原文字节摘要及记录结束行号 | 逐行追到已保存的原文 | 否 |

当前 SQL 02 从 `demo2_store_period_metrics` 读取店月记录，重复窗口比较从 `store_period_panel_metrics` 读取。这两个接口使用同一套已登记的规范字段。`../contracts/publication.v1.json` 明确登记如下视图关系：

| 选中的输入数据集 | 发布时供分析读取的视图 | 处理 |
|---|---|---|
| `demo2_store_period_metrics` 或 `store_period_panel_metrics` | `demo2_store_period_metrics` 和 `store_period_panel_metrics` | 同一份已选择的店月记录提供给两个既有读取接口，所有规范字段逐值保留。 |
| `store_a_monthly_metrics`、`store_a_top_skus` | 各自同名视图 | 保留本数据集的记录，不转入其他分类。 |
| 两个 Demo 2 SKU 榜、`demo2_top_search_terms` | 各自同名视图 | 金额榜、销量榜与搜索词分别保留。 |

视图不是一次新的来源上传。所选批次的原 `dataset_id`、账号、门店、来源页和文件摘要继续保存在 archive 与 manifest 中。只有上述明确登记的来源能进入相应视图；程序还要求投影两端的字段集合、主键、维度、粒度、榜单依据、来源系统和重叠合同一致。不会根据名称相近或粒度相同自动建立映射。

同一 `(overlap_group, grain, ranking_basis, store_id, period_start, period_end)` 只允许选择一个完整批次。因此不能同时选中同店同月的 Demo 2 和 panel 两个来源，再将其拼接或求和；也不能选择旧版与新版、或者两份各有一部分排名的榜单来凑完整 Top 3。可以明确选择旧批次重现当时的数据，不自动追到其后继。

没有选中来源的视图按登记规则生成只有规范表头的空表，manifest 标记 `missing_input`。它表示本次没有选择该来源，不能解释为订单为零或该月没有业务。缺值仍为空，显式零仍为零；新旧版本之间没有字段补回或逐行补齐。

## 发布前的重放

1. 在同一个 SQLite 只读事务中读取所选批次及其全部归档字节。
2. 检查批次状态、内部检索字段、原文、登记、身份记录和保存结果的摘要。
3. 从归档登记中找到选中的 upload 与 binding，使用归档的身份记录独立核对；重新执行 `resolve_upload` 和 `preview_csv`，逐项对照保存的 metadata、context、提议与 preview。
4. 核对当前接入代码、字典和合同与批次处理摘要一致；规则变更时先重新核对批次。
5. 检查所选批次是否重叠，建立规范视图，然后重算四份 SQL 和 Demo 2 fact。任何失败都不公布该目录。
6. 完整目录在发布存储内原子改名为内容对应的 `publication_id`。已有版本不被覆盖；相同选择和相同规则重复发布返回同一 ID。

每批只归档了选中的身份文件。重放会检查整份原登记的结构与选中条目，再用选中的归档身份记录核对；不会假装其他门店的身份文件也已经归档。

发布继续依赖第五步的人工来源核对。文件摘要用于核对内容与版本；它不构成美团账号认证。原始后台格式和真实门店对应关系仍需明确登记。

## Manifest 与处理元数据

这些字段不进入业务指标 CSV：

| 字段 | 用途 |
|---|---|
| `publication_version`、`publication_id` | 发布结构版本及完整内容的标识 |
| `batch_ids` | 本版明确选择的批次 ID |
| `files` | 归档原文、规范视图、静态资料及生成结果的相对路径和 SHA-256 |
| `recipe_sha256` | 本版采用的静态资料及处理环境摘要 |
| `summary.views` | 视图来源登记、行数、批次列表及 `missing_input` |
| `summary.row_lineage` | 每条视图记录的规范主键、`output_line_end`、`batch_id`、`source_line_end` |
| `summary.queries`、`summary.facts` | 生成结果路径、列与记录数量 |
| `publication_runtime.json` 中的 `code_sha256`、`engines` | 分析相关代码及 Python、SQLite、DuckDB、jsonschema、NumPy 版本 |

`archive/<batch_id>/` 保存 `source.csv`、`registry.json`、`identity.json` 和 `result.json`。`evidence/` 包含本版规范视图与重算结果。SQL 和 fact 的既有相对 `source_path` 在这个目录内解析，再经 manifest 追到原文；不需要修改业务字段或添加虚构来源行。

分析返回的外层 JSON 包含 `publication_id`、`sql`、`facts`、`rac`。RAC 的 `source_path` 与判断字段继续沿用现有 schema。

## 一次分析怎样固定版本

`open_publication` 按完整 ID 打开一次，核对目录、manifest、全部文件摘要及当前处理规则，再创建独立的运行副本。`analyze_publication` 从该副本重算选中的 SQL 和 Demo 2 fact，并与本版已保存结果比较，再运行 RAC。

读取时不再访问批次库，不寻找“最新”批次，也不读取工作仓库的业务 CSV 或已保存分析输出。删除批次库不会使已经完整发布的版本失去其归档来源。发布目录中的内容被改动时，后续打开会停止；已经取得的运行副本仍使用核对过的字节。

处理代码、静态规则或记录的运行环境变化后，当前入口会拒绝按旧环境标识继续分析。需要使用对应版本的代码和环境，或在新规则下重新核对并发布；不会静默更换分析逻辑。视图与 SQL 仍采用既有数值口径：规范原值保留精确文本，SQL 派生运算沿用已登记的 REAL/DOUBLE 计算及舍入。

## 命令行

先完成第五步接入，并选择明确的批次 ID。选择文件只包含 `batch_ids` 数组；没有默认最新值，也不接受模型生成的替代来源上下文。

```json
{"batch_ids": ["这里填写接入结果中的完整批次ID"]}
```

在项目根目录执行，替换为实际已核对的选择文件：

```bash
python3 -m retail_ops.ingestion.publication_cli publish \
  --database "$HOME/agent/meituan-intake/batches.sqlite3" \
  --directory "$HOME/agent/meituan-publications" \
  --selection "$HOME/agent/meituan-intake/selected_batches.json"
```

按输出的完整 `publication_id` 检查或分析：

```bash
python3 -m retail_ops.ingestion.publication_cli inspect \
  --directory "$HOME/agent/meituan-publications" \
  --publication-id 这里填写完整发布ID

python3 -m retail_ops.ingestion.publication_cli analyze \
  --directory "$HOME/agent/meituan-publications" \
  --publication-id 这里填写完整发布ID \
  --question "Are Stores B-F directly comparable in March 2026?" \
  --output "$HOME/agent/meituan-analysis/本次新结果.json"
```

`--query` 默认选择 SQL 02，也可明确选择另外三份已登记查询。`--output` 只能写到仓库和发布存储之外的新文件；完整写入后才使结果可见，已有文件不被覆盖。不指定时输出完整 JSON 到终端。

命令成功退出为 0，操作或校验失败为 2。RAC 如因缺证据而返回 `tentative` 等状态，命令仍可正常完成；应读取 `rac.fact_check`、`rac.evidence_review.checks` 和 `rac.belief_update`，不能把进程退出码当成商业判断已得到支持。

## 当前分析范围

- 默认月度分析发布继续要求完整自然月来源。日及其他实际日期来源使用 `--source-records` 发布和 `source_query` 读取，不进入原月度 SQL/fact/RAC。
- 四份 SQL 的已登记定义保持原样。SQL 02 的 `same_period_diagnostic_ready` 仍限三月；其他月份保留各自窗口及既有范围标志。
- 本轮生成 Demo 2 fact。A 店路径重算 SQL 和 RAC，不把旧 Demo 1 fact JSON 复制到新发布。
- RAC 仍使用已登记的 A 店三月至四月、B–F 店三月及二月至四月 panel 检查范围；发布功能没有扩大问题范围。
- 现有 Docker API 与 Qdrant 尚未切换到这个入口。本轮无需重建容器。

[区间 RAC 与 API](RANGE_API.md) 已可按明确发布 ID 读取 `--source-records` 版本，核对同店及跨店的日期范围和来源口径。后续将真实来源登记、上传操作和查询界面串起来。

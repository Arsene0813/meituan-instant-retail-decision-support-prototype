# 区间证据与 API

选择数据版本、门店和日期后，可以查看来源记录，也可以让 RAC 检查这些区间值。查询和 RAC 运行都只返回本次结果；上传批次与已发布数据继续承担留存。

## 查询与检查

| 请求 | 返回内容 |
|---|---|
| `/retail/query` | 第七步的原始窗口记录、指标值、缺日、缺值及逐行来源。未选择 fields 时展示所有已登记非主键字段。 |
| `/retail/review`，一个日期区间 | 记录核对、方向假设所需证据和本次判断。默认检查成交金额、成交订单量，可明确选择其他字段。 |
| `/retail/review`，一家门店及一个前期区间 | 从同一数据版本读取两个区间，检查各指标的升、降、不变，以及已登记的同向变化。 |
| `/retail/review`，多家门店及同一日期区间 | 逐店展示数值，对来源口径已核对一致的字段做描述性对照。 |

两区间比较要求前期结束日在本次区间开始日之前，当前一次选一家门店。跨店查询使用相同起止日期。日期完全相同的自然月和手选日期请求返回相同结果。

只有店铺周期数据集进入区间 RAC；SKU 和搜索词榜继续通过 `/retail/query` 查看各来源窗口原排名。接口使用规范 `store_id`，例如 `B`，不会把 `store_B` 自动映射成 `B`。没有匹配门店记录时返回缺少记录的结果。

## RAC 如何更新

RAC 从查询结果的来源行重新核对数值、覆盖和汇总，再建立检查、假设、质疑及判断。它检查两项成交指标是否同向，以及明确选中的搜索、活动证据与成交是否同向；状态随来源变化重算。同向记录可支持进一步核对因素，但本身没有识别因素的因果作用。

数值比较需要相同数据集、字段含义、已核对的非日期筛选条件和时区。同店还要核对账号与门店身份；跨店分别保留各店身份。在已经取得区间值后的跨窗口或跨店比较中，审核人不同不单独构成业务条件不同。原每日汇总仍按第七步的完整来源声明摘要分组，未在本轮放宽。精确窗口原值与完整逐日汇总的混合比较，仅对已登记的成交金额和成交订单量开放。

两个自然月的天数可以不同。结果保留各自天数与总量差值，不自动换算日均。比例字段若比较，差值单位为百分点。缺日、缺值或口径待核对时，相应比较保持未解决；已取得的来源值仍可查看。退款和预计收入等字段按原义保留，不增加相关因果判断。

本流程使用明确登记的确定性规则。`confidence` 没有校准估计时为 null；字段的检查优先级不表示统计概率。跨店数值对照没有完成某项价格、活动或商品策略的可迁移性验证。

## 字段与输出登记

业务字段改名数为 0。中文定义以 `../data/DATA_DICTIONARY.md` 为准。

| 现有字段或检查元数据 | 定义及使用位置 | 是否改名 |
|---|---|---|
| `transaction_amount` | 所选时间周期内，该账号所选择条件下门店的当天支付且当天未取消的订单用户实际支付金额；保留来源值用于检查 | 否 |
| `transaction_orders` | 同一字典口径下的成交订单量；空缺与零分开处理 | 否 |
| `period_start`、`period_end`、`store_id` | 实际来源范围；查询参数和各证据记录分别保留 | 否 |
| 搜索、入店、转化及活动字段 | 继续使用字典中文定义；仅对登记字段执行方向检查，不生成缺失指标 | 否 |
| `publication_id`、批次与来源行 | 将所有参与检查的值连回同一数据版本及原文件 | 否 |
| `checks.difference` | 新登记的检查结果：右侧值减左侧值，保存精确十进制文本；普通字段保留原单位，比例字段为百分点 | 新增检查元数据，无业务字段改名 |
| `scope.windows`、`evidence_ids`、`check_ids` | 新区间状态 schema 中的窗口和证据关联 | 检查元数据 |
| `hypotheses`、`belief_update`、`final_report` | 按当前证据重算的候选判断、更新及回答 | 保留 RAC 流程；新区间 schema 单独登记 |

规则位于 `../../rac/contracts/range_review.v1.json`，区间状态 schema 位于 `../../rac/schemas/range_cognition_state.v1.schema.json`。

## 启动本地服务

使用接收和发布数据时相同的 `.venv`。发布入口核对代码与运行环境，因此本服务在 WSL 中运行，端口为 8001。原 Docker API 继续使用 8000。

```bash
(
set -e
cd "${MEITUAN_PROJECT_PATH:-$HOME/agent/livestream-agent-memory-layer}"
if ! .venv/bin/python -c 'import uvicorn' >/dev/null 2>&1; then
  .venv/bin/python -m pip install 'uvicorn==0.30.6'
fi
export MEITUAN_PUBLICATION_DIRECTORY="${MEITUAN_PUBLICATION_DIRECTORY:-$HOME/agent/meituan-publications}"
mkdir -p "$MEITUAN_PUBLICATION_DIRECTORY"
.venv/bin/python -m uvicorn api.retail_range_api:app --host 127.0.0.1 --port 8001 --no-access-log
)
```

`uvicorn==0.30.6` 沿用现有 `api/requirements.txt` 中的版本。这个命令只在缺少 uvicorn 时安装它，不替换已有的数据处理依赖。服务占用当前终端；用另一终端执行查询，Ctrl+C 结束服务。

`GET http://127.0.0.1:8001/health` 检查服务和配置目录可用，不证明目录中已有真实业务数据。

## 请求示例

使用实际 `publish --source-records` 得到的完整发布 ID，替换下面的占位值。这些例子本身不会创建上传数据。

```json
{
  "publication_id": "替换为实际完整发布ID",
  "dataset_id": "store_period_panel_metrics",
  "store_ids": ["B", "C"],
  "month": "2026-03",
  "fields": ["transaction_amount", "transaction_orders"]
}
```

将该 JSON 发送给 `/retail/query` 查看记录，或 `/retail/review` 检查同日期的跨店数值。手选日期时，将 `month` 替换为 `period_start` 和 `period_end`，两种写法不能同时提供。

同店前期比较请求：

```json
{
  "publication_id": "替换为实际完整发布ID",
  "dataset_id": "store_period_panel_metrics",
  "store_ids": ["B"],
  "period_start": "2026-04-01",
  "period_end": "2026-04-30",
  "baseline": {"month": "2026-03"},
  "fields": ["transaction_amount", "transaction_orders"]
}
```

`/retail/review` 返回 `publication_id`、`query`、`baseline` 和 `rac`。前期未提供时 baseline 为 null。`rac.checks` 给出证据值的核对结果，`rac.hypotheses` 给出候选判断状态，`rac.belief_update` 与 `rac.final_report` 随同一份证据重算。

服务不接受客户端提供的源数据、证据对象、文件路径、SQL 或保存参数。请求参数错误为 HTTP 422；不存在的版本为 404；版本内容或处理环境核验失败为 409；目录未配置或无法读取为 503。HTTP 200 表示请求处理成功，证据是否完整需看返回内容。

同样可以通过终端直接运行：

```bash
.venv/bin/python -m retail_ops.ingestion.range_analysis \
  --directory "$HOME/agent/meituan-publications" \
  --publication-id 实际完整发布ID \
  --dataset-id store_period_panel_metrics --store-id B \
  --month 2026-04 --baseline-month 2026-03 \
  --field transaction_amount --field transaction_orders
```

## 版本与接入进度

本轮接收规则和批次库结构保持原样。第七步已通过校验的批次可按明确 batch_ids 重新执行 `publish --source-records`，生成包含本轮处理规则的数据版本。已有发布目录不改写；旧发布继续需要其匹配的代码和环境。

目前由操作员核对来源登记，并明确选择接入批次。本地 API 已能供后续日期选择界面调用。中文原文到持久接入、上传界面与真实后台自动获取还需继续接通。

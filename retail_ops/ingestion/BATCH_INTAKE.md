# 批次留存与修订

`batch_cli` 将单店、单个实际来源窗口、单个已登记数据集的规范 CSV 保存到本地 SQLite 批次库。每次接入重新运行现有 preview，连同上传字节、登记快照、身份核对记录、哈希和校验结果一并提交事务。批次通过后可以按 `batch_id` 检查和追溯。

这里的 `validated` 表示接入校验通过。实际日期接入与只读范围查询见 [SOURCE_QUERY.md](SOURCE_QUERY.md)。现在可以通过 [批次发布与固定版本分析](PUBLICATION.md) 明确选择批次，生成快照，并让 SQL、Demo 2 fact 与 RAC 在同一次 CLI 分析中使用同一版本。现有 API 仍读取项目文件。

## 字段对照

以下均沿用字典中文定义或既有接入合同，不改业务字段名。

| 现有字段 | 字典定义或已登记用途 | 使用位置 | 是否改名 |
|---|---|---|---|
| `store_id` | 源 CSV、SQL 和指标输出中的规范门店标识 | 人工核对的门店对应关系、上传上下文、来源行 | 否 |
| `period_start`、`period_end`、`period_month` | 报表首日、末日和自然月标签 | 上传登记及来源行核对 | 否 |
| `transaction_amount` | 所选时间周期内，该账号所选择条件下门店的当天支付且当天未取消的订单用户实际支付金额 | 沿用 preview 的精确小数与空值处理 | 否 |
| `transaction_orders` | 所选时间周期内，该账号所选择条件下门店的当天支付且当天未取消的订单量 | 沿用 preview 的非负整数与空值处理 | 否 |
| `sku_transaction_amount`、`sales_volume` | 所选门店、所选周期内该 SKU 的成交金额、销量，按字典分别记录 | 完整 SKU 榜单批次，保留原排名 | 否 |
| `dataset_id`、`grain`、`ranking_basis` | 既有数据集、粒度及榜单依据合同 | 从登记解析规则，核对上传分类 | 否 |
| `batch_id`、`received_at`、`status` | 既有 `BatchMetadata` 接入元数据 | 接入程序生成批次 ID、接收时间及检查状态 | 否 |
| `source_system`、`source_name`、`source_page` | 既有来源系统、数据集来源名称和来源页面登记 | 数据合同与人工核对的上传登记 | 否 |
| `extracted_at`、`coverage_start`、`coverage_end` | 既有提取时间和报表覆盖起止日期 | 区分接收时间、提取时间、报表窗口 | 否 |
| `file_sha256`、`mapping_version`、`snapshot_semantics` | 既有文件摘要、映射版本和快照语义 | 对照原字节及当前规则，保留修订、不相加 | 否 |

接入登记在 `../contracts/intake_registry.v1.schema.json` 及兼容原字段的 `../contracts/intake_registry.v2.schema.json` 中定义，由 `intake_registry.py` 进一步核对跨条目关系、日期和证据文件。它们均为来源和处理元数据：

| 新增登记字段 | 用途 |
|---|---|
| `registry_version`、`bindings`、`uploads` | 人工核对登记的版本及两类条目 |
| `binding_id`、`source_account_id`、`source_store_id`、`dataset_ids` | 将来源账号和来源门店对应到规范 `store_id`，列明可接入的数据集 |
| `identity_evidence_path`、`identity_evidence_sha256`、`reviewed_by` | 定位并核对本地身份记录，记录核对人 |
| `upload_id` | 指向已核对的文件摘要、窗口、分类、来源页、提取时间与映射版本 |
| `supersedes_batch_id` | 明确本批次修订的先前批次；整个数据集×店铺×窗口替换 |
| `registry_sha256`、登记快照、处理代码与规则摘要 | 留存接收时使用的登记和校验规则，供重放与变更核对 |

## 保存结果的结构

`BatchMetadata` 保留原来的全部字段。外层结果补充 `upload_id`、`supersedes_batch_id`、`registration`（选中的 binding 和 upload）、`registry_sha256`、`provenance`（处理代码、数据合同及字典摘要）、`preview`、`errors`、`proposals` 和 `proposals_sha256`。`batch_id`、`received_at`、`status`、`file_sha256` 同时用于直接定位批次。接收调用的 `idempotent` 表示本次是否命中先前请求，不改变保存的检查结论。

SQLite 的 `raw_data`、`registry_bytes`、`identity_evidence` 保存字节；`registry_sha256`、`identity_sha256`、`result_sha256` 用于内容核对，`result_json` 保存完整结果。`retry_key`、`scope_key`、`upload_payload_key` 是重试、修订范围及已使用上传登记的内部检索键。库使用 application ID 和 schema version 区分自身与其他 SQLite 文件；这些字段均不进入业务指标表。接收事务会扫描既有结果及内部检索键，先检查其一致性再处理重试与修订；这项成本随批次数增长，后续扩大接入量时需一并评估。

## 身份和文件登记

登记文件由可信操作人维护，与上传文件和模型提议分开提供。上传调用只指定 `upload_id`，不能通过提议改变其门店、分类或窗口。人工核对时要确认真实来源账号、来源门店、规范门店及规范 CSV 与原文的对应关系。

身份记录是一个仅包含 `source_system`、`source_account_id`、`source_store_id` 的 JSON 对象。这三个值必须与 binding 完全相同，文件摘要必须与登记相同。证据路径限定在登记目录内，拒绝绝对路径、向上跳转和符号链接。多个账号可以对应同一规范门店；同一来源系统、账号、来源门店不能登记两条互相竞争的对应关系。

这是本地人工核对记录，尚未连接美团授权接口。哈希能检查字节与核对版本是否一致；身份和原文准确性仍依赖操作人的核对。模型与非可信上传端不能获得登记文件的写权限。这里没有实现用户登录、角色权限或后台账号认证。

新接入使用 `../contracts/intake_registry.v2.empty.json`；原 v1 模板和读取行为继续保留。空结构不包含任何真实门店映射。没有登记的 `upload_id` 会待核对。真实 ID、来源页面和 CSV 整理准确性确认后才填写登记；不要从 A–F 样例猜测真实 ID。

v1 登记继续使用 `canonical_csv_v1` 完整自然月格式；v2 登记可使用该格式或已登记的 `canonical_csv_v2` 实际来源窗口格式，后者支持日及其他明确日期范围。现有中文原文预览仍可单独运行；原始后台导出和中文多区块上传需继续接入登记与核验。收到新名称不会自动创建分类或存储去向。

## 重复上传和修订

- 原字节、选中的登记、提议、前序批次及处理规则一致时，重试返回原批次 ID，不另建一份记录。仅在登记中追加其他上传不会改变旧上传的重试结果。
- 已使用的 `upload_id` 若改了文件或对应关系，会待核对；修订使用新的 `upload_id`。
- 同一来源绑定、数据集、店铺和窗口已经有通过的批次时，新版必须显式传入 `--supersedes-batch-id`。前序批次必须校验通过、范围一致且尚无通过的后继；并发提交由 SQLite 写事务串行校验。
- 修订的提取时间不能早于前序批次。允许相同提取时间的人工纠正；两个时间相同本身不会合并数据。
- 修订保留完整上传：新版缺失的指标继续为空，明确的零保留为零，不从旧版补回。SKU 和搜索词按这次文件的全部行保存，不与旧榜单逐行拼接。
- 待核对批次保存原字节和原因，不成为可被后续有效修订替换的前序。无法解析可信登记时，相关 `BatchMetadata` 保持缺失，不填造门店或窗口。
- 不同数据集或来源绑定可能覆盖同一业务窗口，当前分别留存；发布时仍须解决 `overlap_group` 下的选择，不能把这些记录相加。

有效订单数、无效订单数继续按现有排除规则处理。若规范 CSV 表头包含这两个列名，接入在创建数据库和保存原文前停止；请提供已经按既有约定整理的规范 CSV。输入编码或 CSV 表头损坏、无法完成这项检查时，也在留存前停止。这些路径不产生留存批次，也不会把删除过列的内容标为原始字节。

## 命令行

先完成并核对登记，再在项目根目录运行。下方路径和上传 ID 必须替换为实际已核对的值；补丁安装和测试不会接入你的业务文件。

```bash
python3 -m retail_ops.ingestion.batch_cli receive \
  --database "$HOME/agent/meituan-intake/batches.sqlite3" \
  --registry "$HOME/agent/meituan-intake/reviewed_uploads.json" \
  --upload-id 已核对的上传ID \
  --input /实际路径/已核对的规范CSV.csv
```

修订在上述命令末尾加 `--supersedes-batch-id 前序批次ID`。可选 `--proposals` 使用现有 preview 的逐行提议格式；模型提议仍逐值对照独立读取的 CSV。

```bash
python3 -m retail_ops.ingestion.batch_cli show \
  --database "$HOME/agent/meituan-intake/batches.sqlite3" \
  --batch-id 接入结果中的批次ID
```

通过时退出码为 0；待核对或操作错误为 2。按 ID 读取会复核保存内容的摘要。摘要用于发现内容变化，不提供抵抗数据库管理员同时修改内容和摘要的认证保证。数据库包含上传原文，保存到项目目录之外；CLI 拒绝在仓库内创建运行库。

批次库继续只负责接入留存，不自动选择“最新版本”。发布和分析使用独立的 `publication_cli`；其两版同店同窗口数据、缺值和不完整榜单测试见 `eval/test_retail_publication_analysis.py`。

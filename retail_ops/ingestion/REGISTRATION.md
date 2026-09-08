# 核对并追加来源登记

`registration_cli` 帮助本机操作人核对一份原文件的登记，并追加到现有第 3 版 `reviewed_uploads.json`。文件与身份记录的 SHA-256 由程序计算；门店、来源、日期、数据集和提取时间由操作人独立填写。它不创建身份文件，不接收数据、不写批次库，也不发布分析版本。

安装本轮代码后先重启工作台一次，使新的重试入口生效。此后可以在另一个终端使用登记工具；每次追加成功后，在工作台“上传数据”点击刷新登记，即可选择新上传标识，无需再次重启。

## 三个命令

所有命令在项目根目录使用 `.venv/bin/python -m retail_ops.ingestion.registration_cli`。文件路径均替换为实际路径；登记须位于仓库之外。

```bash
# 只查看中文原文中实际识别到的区块，不建立可信登记。
.venv/bin/python -m retail_ops.ingestion.registration_cli inspect-text \
  --input /实际路径/原文.txt --mapping-version manual_text_v3

# 核对独立填写的登记计划和原文件；成功返回 check_sha256。
.venv/bin/python -m retail_ops.ingestion.registration_cli check \
  --registry "$HOME/agent/meituan-intake/reviewed_uploads.json" \
  --plan /实际路径/核对后的登记计划.json --input /实际路径/原文.txt

# 看过 check 中的门店、日期、分类和来源后，使用该次确切摘要追加。
.venv/bin/python -m retail_ops.ingestion.registration_cli append \
  --registry "$HOME/agent/meituan-intake/reviewed_uploads.json" \
  --plan /实际路径/核对后的登记计划.json --input /实际路径/原文.txt \
  --expected-check-sha256 此处替换为刚核对的64位摘要
```

`inspect-text` 的 `observed_context` 来自上传原文，只帮助定位问题。它不作为独立门店或日期证明，也不自动填入计划。识别失败时返回 `needs_review` 和具体错误，退出码为 2。原文件包含现有排除标签或 UTF-8 损坏时，同样停止。

`check` 不保存检查报告，不改登记。它使用现有文本完整重放或 CSV preview 逐项核对全部收据；只有实际字段与明确登记相符才返回 `checked`。这个状态表示本次登记检查通过，尚未创建上传批次；数据库修订冲突仍由实际接收步骤核对。

`append` 在同一检查范围内重新验证全部输入，再追加登记并保留旧文件原字节备份。核对后若原文件、计划、身份文件、当前登记或处理规则有变化，旧 `check_sha256` 不能继续使用。

## 准备真实身份记录

身份文件由操作人根据独立核对信息预先准备，放在登记目录内。沿用已有合同，只包含以下三个字段。这里的 `null` 是待填写项，程序会拒绝，不可直接用于接入。

```json
{
  "source_system": "meituan_merchant_backend",
  "source_account_id": null,
  "source_store_id": null
}
```

账号和来源门店必须与真实来源对应；不要从中文原文的“B店”或模型提议反推这两个标识。该文件是本机操作人的核对声明，仍需独立确认后台来源。

## 准备登记计划

计划合同为 `registration_plan.v1.schema.json`；`registration_plan.v1.empty.json` 提供空结构，未填写上传项时不能通过。计划与当前登记分别保存，不能用计划覆盖登记。

计划使用既有 `bindings` 和 `uploads` 命名。新增绑定字段与原合同相同，只省去由程序计算的 `identity_evidence_sha256`；上传收据只省去由程序计算的 `file_sha256`。以下是待填写的中文单区块结构，`null` 不会被推断或补齐：

```json
{
  "plan_version": "1",
  "bindings": [
    {
      "binding_id": null,
      "source_system": "meituan_merchant_backend",
      "source_account_id": null,
      "source_store_id": null,
      "store_id": null,
      "dataset_ids": [],
      "identity_evidence_path": null,
      "reviewed_by": null
    }
  ],
  "uploads": [
    {
      "upload_id": null,
      "binding_id": null,
      "document_id": null,
      "source_block_line": null,
      "dataset_id": null,
      "period_start": null,
      "period_end": null,
      "source_page": null,
      "extracted_at": null,
      "mapping_version": "manual_text_v3"
    }
  ]
}
```

已有绑定可直接由新收据的 `binding_id` 引用，此时 `bindings` 为空；不用重复创建或修改身份对应。首次绑定的全部身份字段必须与预先准备的身份 JSON 完全一致。`identity_evidence_path` 是相对登记目录的路径，例如已经独立核对的 `identity/store-B.json`。

一份计划只对应一个 `--input` 原文件。中文文件可以包含多个门店、窗口和榜单；每个实际数据组独立填写一条收据，整份使用一个明确 `document_id`、同一格式版本。`source_block_line` 是从 1 开始的实际门店行号。所有区块必须恰好覆盖，不能漏项、多项或依赖上一段分类。

CSV 计划只包含一条收据，不使用 `document_id` 和 `source_block_line`，明确填写 `canonical_csv_v1` 或 `canonical_csv_v2`。工具不会根据标题猜测数据集。完整自然月、实际首尾日及可选 `aggregation_scope` 沿用 [TEXT_INTAKE.md](TEXT_INTAKE.md) 与 [BATCH_INTAKE.md](BATCH_INTAKE.md) 的规则；未知的筛选条件省略，不写虚构默认条件。

中文格式继续使用已登记的 B–F 别名。新增真实门店中文别名需要单独登记来源格式；已有规范 CSV 的实际日期格式可以使用独立绑定的明确 `store_id`。本工具不扩展业务字段、来源格式或别名。

## 字段和元数据对照

业务字段改名数为 0。程序没有新增可用于判断的业务指标。

| 字段 | 字典定义或既有用途 | 本工具使用位置 | 是否改名 |
|---|---|---|---|
| `transaction_amount` / `gross_revenue` | 成交金额 / 营业额，沿用字典分别定义 | 复用原解析；未登记的营业额不转成成交金额 | 否 |
| `store_id`、`period_start`、`period_end`、`dataset_id` | 已登记规范门店、实际来源窗口、数据集 | 操作人明确收据与原文逐项比较 | 否 |
| `binding_id`、`upload_id`、`document_id` | 已有绑定、上传和文档标识 | 新增前查重，旧标识不编辑 | 否 |
| `file_sha256`、`identity_evidence_sha256` | 原文件及独立身份文件的字节摘要 | 程序计算并交给原合同核对 | 否 |
| `source_page`、`extracted_at`、`aggregation_scope` | 已有来源页面、带时区提取时间、完整非日期条件 | 只接受操作人明确提供的信息 | 否 |
| `plan_version` | 新增本地登记计划版本 `1` | 区分草拟计划与有效 intake registry | 新接入元数据 |
| `check_sha256` | 新增一次登记检查的摘要 | 覆盖当前登记、原计划、原文件、全部身份字节、候选登记及规则 | 新接入元数据 |
| `inspection_version`、`observed_context`、`observed_fields` | 新增只读检查展示字段 | 标记从来源观察到的信息，不转成独立收据 | 新接入元数据 |

`status=source_checked`、`checked`、`registered` 分别表示原文格式检查、完整登记检查、追加登记已完成。每一步仍有明确作用；`batch_created` 始终为 false。只有之后在工作台执行“核对并留存”，才产生批次。

## 并发和原子写入

现有绑定和收据在候选登记中原样保留。不能扩大旧绑定的数据集权限，不能重复使用 `upload_id`，不能给已存在的 `document_id` 追加成员。修订原文件使用新文档、新上传标识，实际接收时填写已有前序批次 ID。

追加使用 Linux/WSL 目录锁串行化本工具的写入者，核对输入后写同目录临时文件、同步字节、原子替换并同步目录。当前登记只有完整旧版或完整新版；已有工作台读取不需要停机。替换失败时旧登记保留；旧字节备份命名为 `reviewed_uploads.json.before-<旧SHA256>.json`，已有同名备份必须逐字节一致，不能覆盖冲突文件。

本机手工编辑器不遵守这个目录锁，因此检查和追加期间不要同时手工保存登记或身份文件。替换前会再次核对输入字节；它不是多用户授权服务，也没有网络写入入口。第 1/2 版登记不会自动升级。

如果原子替换已经完成，但之后目录同步失败，错误会明确说明“complete registry was replaced”。这时当前登记可能已是完整新版，应先查看登记，不要把报错解释为没有写入。旧字节备份仍保留。

读取检查仅使用临时私有副本，运行数据目录不产生检查报告或批次文件。原文件上限沿用工作台 8 MiB，计划上限为 1 MiB。有效订单数、无效订单数的已有不留存约定继续执行；程序不会修改原文以绕过检查。

## 新门店登记与原文重试

原文归档为重放完整登记而保存全部身份文件。此前新增另一家门店后，这个完整身份包变化，会让未改动的旧文档重试误报为登记变化。

工作台与 `batch_cli receive-document` 现在共同使用 `document_intake.receive_document`。它在只读事务中核对全部批次行，再同时检查当前完整文档和旧归档重放。只有全部子批次的原字节、收据、绑定、来源身份、窗口、提议、前序关系、规范记录及处理规则一致时，才返回原批次 ID 和 `idempotent=true`。无关门店的新增身份不会因此制造新批次；真正的文档变化仍进入原接收核对。

旧归档中的完整登记、身份包、处理摘要和批次内容均不改写。保存和解析的原规则未改变，旧发布仍按既有 recipe 核对。新接入通过同一个原子存储程序完成；该重试入口不会发布数据或重算旧批次。

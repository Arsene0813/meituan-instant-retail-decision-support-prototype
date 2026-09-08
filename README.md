# Meituan Instant-Retail Decision Support Prototype

Repository: `meituan-instant-retail-decision-support-prototype`

A local decision-support prototype that connects selected Meituan backend metrics, reproducible SQL diagnostics, source-linked memory facts, retrieval tests, and deterministic grounded review.

## Core Research Question

This project addresses a real Meituan instant-retail operating problem.

The Meituan merchant backend provides detailed single-store metrics. As store count increased, the analytical task became organizing store-period records across reporting windows, activity conditions, product structures, store types, and local operating contexts for consistent review.

For the selected standardized instant-retail products, operating review follows one connected path:

```text
being seen -> being entered -> being ordered -> being selected again / maintaining share
```

Promotion, subsidy, pricing, SKU arrangement, ranking position, and fulfillment stability are operating levers inside this chain. Their meaning depends on the observed store-period context, local competition, activity involvement, activity intensity, product mix, and reporting-window alignment.

The project organizes multi-store operating evidence so that each supported interpretation can be traced to defined fields, reporting periods, and source records as the business expands.

## Evidence Workflow

The repository follows one evidence path:

```text
selected merchant-backend metrics
-> canonical field dictionary
-> SQL diagnostic outputs
-> source-linked retail memory facts
-> retrieval and contract checks
-> RAC grounded review
```

Store-level source tables are manually transcribed from the merchant backend and anonymized. Metric meanings and canonical field names are fixed before transformation.

SQL organizes the selected store-period records. Generated facts retain the entity, reporting period, source fields, observed values, source paths, calculation metadata, and evidence context. Later checks verify entity, period, field, and source consistency.

`retail_ops/data/DATA_DICTIONARY.md` is the naming authority for retail fields and metric definitions.

For reviewed canonical CSV uploads, [batch intake](retail_ops/ingestion/BATCH_INTAKE.md) retains original bytes and explicit revisions. The [publication CLI](retail_ops/ingestion/PUBLICATION.md) selects batch IDs, rebuilds SQL and Demo 2 facts, and runs RAC against the same publication. [Date-range queries](retail_ops/ingestion/SOURCE_QUERY.md) read published source records without saving query results; natural-month selection resolves to the same start/end parameters. The local [range API](retail_ops/ingestion/RANGE_API.md) reads those publications and runs RAC checks over selected windows; the Docker demo endpoints retain their current project-file or Qdrant evidence paths.

## Current Implemented Work

The current retail work includes three analytical views: Demo 1 for Store A month-over-month diagnosis, Demo 2 for selected Stores B-F under one March 2026 reporting window, and the repeated-window B-F panel across 2026-02, 2026-03, and 2026-04.

The operating problem came from a 48-store business. The evidence committed to this repository covers six anonymized stores: Store A and selected Stores B-F under the reporting windows listed below. The 48-store figure describes the operating context; the committed analytical evidence covers the selected six stores.

Demo 2 provides the March 2026 B-F diagnostic under the shared field contract. Requirements for a future question-specific pairwise comparability gate are recorded in `retail_ops/COMPARABILITY_GATE_V0.md`.

| Area | Current implementation | Current role |
| --- | --- | --- |
| Livestream memory layer | Typed product facts, overwrite control, soft deactivation, active-state retrieval, fallback/refusal, scenario evaluation. | Establishes the lifecycle-aware memory foundation retained in the repository. |
| Data dictionary | Preserves selected Meituan merchant-backend metric meanings and canonical field names. | Provides the field authority used by source tables, SQL outputs, generated facts, and review results. |
| Retail Demo 1 | Store A month-over-month diagnostic across February, March, and April 2026. | Connects visibility, entry, conversion, transaction, activity, and listed-SKU evidence across three reporting periods. |
| Retail Demo 2 | Same-period B-F diagnostic for March 2026. | Organizes five stores under one reporting window and shared field contract. |
| Repeated-window panel | B-F coverage and descriptive summary across 2026-02, 2026-03, and 2026-04. | Makes three-month coverage and movement visible for the selected fields. |
| Memory facts | Converts diagnostic outputs into source-linked facts with observed values, source fields, source paths, calculation metadata, and evidence context. | Provides retrieval-facing records derived from the structured source tables. |
| Retrieval and response checks | Tests entity, period, canonical field, source, and response consistency. | Provides visible scenario-based checks over the current evidence path. |
| Factor-aware grounded review layer (RAC) | Provides deterministic factor expansion, evidence routing, critique, rule-based checks, review-state updates, and grounded report generation over local project evidence. | Connects decision factors, competing explanations, source records, and report statements. |

## Key Design Principles

This prototype emphasizes:

- preserving Meituan backend metric semantics and reporting-window grain;
- structuring store-period observations for consistent multi-store review;
- converting diagnostics into retrieval-facing evidence records with source fields and observed values;
- carrying source paths, calculation metadata, evidence-trace confidence labels, and evidence context into memory facts;
- checking entity, period, metric-definition, and source consistency in generated answers;
- connecting decision-review statements to the supporting records and definitions.

## Admissions Review Path

[`PROJECT_SUMMARY_FOR_ADMISSIONS.md`](PROJECT_SUMMARY_FOR_ADMISSIONS.md)
is the single application-facing starting point. A first-pass review can
follow five files in one narrative order:

| Step | File | Review purpose |
|---:|---|---|
| 1 | [Project summary](PROJECT_SUMMARY_FOR_ADMISSIONS.md) | Understand the business origin, evidence coverage, implemented architecture, and programme relevance. |
| 2 | [Demo 1: Store A month-over-month diagnostic](retail_ops/demo/demo_1_store_a_month_over_month_diagnostic.md) | Inspect repeated-window Store A evidence and multi-metric interpretation. |
| 3 | [Demo 2: same-period B-F diagnostic](retail_ops/demo/demo_2_cross_store_comparability_diagnostic.md) | Inspect the shared multi-store evidence structure and March 2026 diagnostic results. |
| 4 | [Experiment results](retail_ops/EXPERIMENT_RESULTS.md) | Inspect validation questions, procedures, observed outcomes, sensitivity checks, and visible failure modes. |
| 5 | [RAC grounded-review demo index](rac/DEMO_INDEX.md) | Inspect factor routing, structured-record grounding, competing explanations, and report validation. |

Supporting evidence after the first pass:

- [Repeated-window B-F coverage](retail_ops/outputs/store_period_panel_coverage_output.csv)
  and [descriptive summary](retail_ops/outputs/repeated_window_panel_summary_output.csv)
  show the implemented February-April panel.
- [Promotion-review report](rac/outputs/grounded_rac_promotion_strategy_001.md)
  shows the RAC review path across cost, subsidy, conversion, and
  supporting operating evidence.
- [Data dictionary](retail_ops/data/DATA_DICTIONARY.md) and
  [technical appendix](retail_ops/TECHNICAL_APPENDIX.md) provide the
  field contract and source-to-claim audit references.
- [Design evolution: from livestream product memory to retail decision support](case_studies/from_livestream_to_retail_decision_support.md)
  provides optional background on the repository's development from the
  earlier livestream memory-layer prototype.
- [Comparability Gate V0](retail_ops/COMPARABILITY_GATE_V0.md) specifies
  the evidence requirements for a future question-specific pairwise gate.

## Architecture

The repository has three connected layers.

| Layer | Responsibility | Main files |
|---|---|---|
| Lifecycle-aware memory layer | Stores typed product facts, controls updates and active state, retrieves current evidence, and falls back when knowledge is unsupported. | `api/`, `scripts/`, `eval/` |
| Retail evidence layer | Preserves merchant-backend metric definitions, structures store-period evidence with SQL, generates source-linked memory facts, and validates field and value lineage. | `retail_ops/` |
| Factor-aware grounded review layer (RAC) | Expands decision factors, routes local evidence, records competing explanations, applies claim and definition checks, and reports evidence coverage and review status. | `rac/` |

The reviewer-oriented evidence flow is:

```text
selected merchant-backend metrics
-> canonical field contract
-> SQL diagnostic outputs
-> source-linked memory facts
-> retrieval and RAC grounded review
-> traceable report and evidence-linked response
```

The retail evidence layer establishes what the available data supports.
RAC makes the multi-factor review path inspectable over that structured
evidence.

## Implemented API and Retrieval

The local FastAPI prototype includes general chat, memory, and retrieval endpoints. The key implemented paths are:

- `/health`
- `/chat`
- `/chat_mem`
- `/chat_livestream_kb`
- `/chat_retail_ops_kb`
- `/chat_retail_ops_demo2_kb`

The retail endpoints are local prototype endpoints over file-backed generated retail memory facts. Demo 2 endpoint checks are used to inspect evidence behavior before any live Meituan integration.

Retrieval-score inspection is a separate offline analysis. The Demo 2 endpoint uses deterministic file-backed selection.

### Retrieval Modes

| Endpoint | Current evidence mode | How to read it |
| --- | --- | --- |
| `/chat_livestream_kb` | Qdrant-backed lifecycle-aware memory retrieval. | Original memory-layer prototype for typed product facts, freshness, overwrite behavior, and fallback/refusal. |
| `/chat_retail_ops_kb` | Retail memory retrieval over implemented Store A facts. | Retail extension path for source-linked Store A diagnostic facts. |
| `/chat_retail_ops_demo2_kb` | File-backed generated Demo 2 retail memory facts. | Returns B-F same-period diagnostic facts by entity and evidence slot. |

## Retail Evidence Files

Retail demo details are kept under `retail_ops/`.

| Evidence layer | Primary file |
|---|---|
| Store A month-over-month diagnostic | `retail_ops/demo/demo_1_store_a_month_over_month_diagnostic.md` |
| B-F same-period diagnostic | `retail_ops/demo/demo_2_cross_store_comparability_diagnostic.md` |
| Retail experiment map and validation outcomes | `retail_ops/EXPERIMENT_RESULTS.md` |
| Future comparability-gate contract | `retail_ops/COMPARABILITY_GATE_V0.md` |


## Evaluation Questions

For retail field names and metric meanings,
`retail_ops/data/DATA_DICTIONARY.md` is authoritative. Detailed
procedures, outputs, and failure cases are recorded in
`retail_ops/EXPERIMENT_RESULTS.md`, `retail_ops/outputs/`, and
`eval/retail_decision_support_eval_results/`.

The evaluation layer asks four questions:

1. Can committed diagnostic values be regenerated and traced to their
   source fields and formulas?
2. Do entity, period, canonical field, formula, and source references remain
   consistent through fact generation and endpoint answers?
3. How do retrieval thresholds and wording variations affect evidence
   routing?
4. Does RAC keep a final review connected to relevant factors, source
   paths, competing explanations, and additional evidence requirements?

The current results are:

| Check | Observed result | How to read it |
|---|---|---|
| Store A value lineage | The check covers 3 source rows, 3 SQL output rows, 9 top-SKU rows, 180 source, formula, movement, ranking, and trade-off comparisons, and 5 generated facts. | The source-to-SQL-to-fact path can be inspected field by field across the multi-metric store-period diagnostic. |
| Demo 2 threshold sensitivity | Baseline notes reproduce for all 5 rows. Raising the current thresholds by 5 percentage points changes Stores C-F; lowering them by 5 percentage points changes no rows. | The sample shows how the current diagnostic notes respond to nearby threshold settings. |
| Retrieval wording stress | The applicable run covers supported, unsupported, hard-negative, entity-period-mismatch, and ambiguous queries over the current evidence corpus. | Exact counts, scores, and the exploratory threshold are recorded in the generated retrieval summaries and checked by `eval/check_retrieval_result_applicability.py`. |
| Repeated-window B-F panel | Stores B-F each retain February-April 2026 coverage across 11 selected metrics. | The panel supports descriptive review while preserving the March observation between the February and April endpoints. |
| RAC grounded review | Fixed review cases produce source-linked reports from structured records, local text evidence, and explicit boundary evidence. | Current case counts, routes, and report-contract checks are recorded in `rac/outputs/grounded_quality_summary.md`. |

These descriptive analyses, retrieval stress tests, grounded reviews, and
contract checks are reported separately with their own procedures and outputs.
Difficult retrieval cases remain visible because they show why similarity
is only one evidence-routing signal.

## Optional Local Run

The repository can be reviewed through the Markdown documents, SQL files, generated outputs, and evaluation results without running the local API.

For local reproduction, the prototype uses FastAPI, Ollama, Qdrant, and Docker Compose. The local setup is defined by `docker-compose.yml`; the current validation commands are listed below.

## Reproduce Key Checks

Run the current implemented checks from the repository root:

```bash
python3 retail_ops/scripts/validate_retail_data_contract.py
python3 retail_ops/scripts/validate_demo2_comparability_output.py
python3 retail_ops/scripts/analyze_demo2_guardrail_sensitivity.py
python3 retail_ops/scripts/validate_store_period_panel.py
python3 retail_ops/scripts/validate_repeated_window_panel_summary.py
python3 eval/eval_retail_demo2_facts.py
python3 eval/eval_retail_demo2_scope_boundary.py
python3 eval/eval_retail_demo2_answer_behavior.py
python3 eval/eval_retail_demo2_endpoint_behavior.py
python3 eval/eval_future_comparability_gate_contract.py
python3 scripts/validate_demo2_retail_endpoint_boundary.py
python3 scripts/validate_markdown_readability.py
python3 retail_ops/scripts/validate_csv_physical_rows.py
python3 rac/scripts/validate_local_evidence_resolver.py
python3 rac/scripts/validate_grounded_pipeline.py
python3 rac/scripts/validate_grounded_quality_gate.py
```

The endpoint behavior eval imports `api.main`, so run it inside the project virtual environment after dependencies are installed.

Optional offline retrieval-inspection checks:

```bash
python3 eval/analyze_retail_embedding_score_distribution.py
python3 eval/analyze_retail_query_robustness.py
python3 eval/check_retrieval_result_applicability.py
```

These retrieval checks inspect score distribution and wording-variation behavior over the current file-backed retail evidence corpus.

## Consistency Rules

Retail field names and metric meanings must follow `retail_ops/data/DATA_DICTIONARY.md`.

Retail experiment wording and validation claims should stay aligned with `retail_ops/EXPERIMENT_RESULTS.md`.

Future question-specific comparison work follows `retail_ops/COMPARABILITY_GATE_V0.md`.

## Factor-Aware Grounded Review (RAC)

The `rac/` module operates over the structured retail evidence and records
factor selection, evidence routing, competing explanations, claim checks,
review-state updates, and evidence requirements for the report.

Its implemented workflow covers:

- question analysis and factor expansion;
- interpretable factor weighting;
- source-aware local evidence routing;
- factor-level evidence requirements;
- competing explanations;
- claim-to-source and definition checks;
- review-state updates that record evidence coverage and open requirements;
- grounded report generation;
- a deterministic report-contract quality gate.

Start with `rac/DEMO_INDEX.md` for the reviewer-facing cases, generated
reports, execution commands, and quality-gate results.

The current implementation is deterministic and file-grounded. It
connects the field dictionary, SQL diagnostics, generated facts, retrieval
checks, and grounded reports in one inspectable evidence path.

# Project Summary for Admissions Review

## Project Title

Meituan Instant-Retail Decision Support Prototype

Repository: `meituan-instant-retail-decision-support-prototype`

## Project Summary

This local prototype addresses a real operating problem in a 48-store Meituan instant-retail business. The merchant backend provides detailed single-store metrics, while multi-store review requires consistent organization of store-period records across reporting windows, activity conditions, product structures, store types, and local operating contexts.

The repository turns selected backend observations into a traceable decision-support prototype. Metric definitions are fixed in the data dictionary, diagnostic calculations are reproduced in SQL, derived findings retain their source lineage, and retrieval and review steps remain connected to the supporting evidence.

## Reviewer Reading Path

This file is the single application-facing entry point. Continue through
the project in this five-step order:

| Step | File | What to inspect |
|---:|---|---|
| 1 | [Project summary](PROJECT_SUMMARY_FOR_ADMISSIONS.md) | Business origin, evidence coverage, architecture, implemented work, and programme relevance. |
| 2 | [Demo 1: Store A month-over-month diagnostic](retail_ops/demo/demo_1_store_a_month_over_month_diagnostic.md) | Store A repeated-window evidence and multi-metric interpretation. |
| 3 | [Demo 2: same-period B-F diagnostic](retail_ops/demo/demo_2_cross_store_comparability_diagnostic.md) | Shared multi-store evidence structure and March 2026 diagnostic results. |
| 4 | [Experiment results](retail_ops/EXPERIMENT_RESULTS.md) | Validation questions, procedures, observed outcomes, sensitivity checks, and visible failure modes. |
| 5 | [RAC grounded-review demo index](rac/DEMO_INDEX.md) | Factor-aware evidence routing, record grounding, competing explanations, and report validation. |

After this first pass, use:

- [Repeated-window B-F coverage](retail_ops/outputs/store_period_panel_coverage_output.csv)
  and [summary](retail_ops/outputs/repeated_window_panel_summary_output.csv)
  for the February-April supporting panel;
- the [data dictionary](retail_ops/data/DATA_DICTIONARY.md) and
  [technical appendix](retail_ops/TECHNICAL_APPENDIX.md) for field and
  source-to-claim review;
- [Design evolution](case_studies/from_livestream_to_retail_decision_support.md)
  as optional background on how the repository developed from the earlier
  livestream memory-layer prototype;
- [Comparability Gate V0](retail_ops/COMPARABILITY_GATE_V0.md) for the
  evidence requirements of a future question-specific pairwise gate.

## Business Decision Problem

For standardized products such as contact lenses, care solutions, and related eye-care items, operating review follows one connected path:

```text
being seen
-> being entered
-> being ordered
-> being selected again / maintaining share
```

Promotion, subsidy, pricing, SKU arrangement, ranking work, fulfillment support, and local competition provide operating context for different parts of this path. Their interpretation depends on the store, reporting period, activity conditions, product structure, and evidence available for the question.

The central decision problem is to produce operating interpretations that can be reproduced from documented fields, matched to the correct entity and reporting period, and traced through the available evidence.

## Evidence Coverage

| Scope | Evidence represented in the repository |
| --- | --- |
| Business setting | The operating problem emerged in a 48-store Meituan instant-retail business. |
| Repository evidence | Manually transcribed and anonymized observations for six selected stores: Store A and Stores B-F. |
| Demo 1 | Store A across February, March, and April 2026. |
| Demo 2 | Stores B-F under one March 2026 reporting window. |
| Repeated-window panel | Stores B-F across February, March, and April 2026 using the documented supporting-table coverage. |
| Analytical work | Descriptive diagnostics, value lineage, evidence routing, endpoint checks, and RAC grounded review. |


## Prototype Design

### Evidence path

```text
selected Meituan backend metrics
-> DATA_DICTIONARY.md field contract
-> reproducible SQL diagnostics
-> generated retail memory facts
-> contract and lineage checks
-> retrieval stress tests
-> RAC grounded review over local evidence
```

`retail_ops/data/DATA_DICTIONARY.md` is the authoritative source for field names, Chinese metric definitions, formulas, grains, and reporting conventions.

SQL provides the calculation and structuring layer. It converts selected source records into inspectable diagnostic outputs while preserving the documented meanings of the backend metrics.

Generated memory facts retain the entity, reporting period, evidence slot, source fields, observed values, calculation metadata, source paths, confidence label, and evidence context. The confidence label records evidence traceability and definition coverage.

### Layer responsibilities

| Implemented layer | Role in the prototype |
| --- | --- |
| Metric dictionary | Preserves canonical project field names and the Chinese definitions of the Meituan backend metrics. |
| SQL diagnostics | Reproduces selected diagnostic values and keeps transformations inspectable. |
| Generated memory facts | Retains structured findings together with source fields, source paths, observed values, calculation metadata, evidence-trace confidence labels, and evidence context. |
| Contract and lineage checks | Detects selected naming, formula, header, metadata, path, and source-to-output inconsistencies. |
| Retrieval stress tests | Examines evidence routing under wording variations, entity-period changes, and comparison requests. |
| Retrieval and response checks | Checks entity, period, canonical field, formula, source, and response consistency. |
| RAC grounded review | Decomposes multi-factor questions, routes local evidence, develops competing explanations, applies claim and definition checks, updates review state, and produces an inspectable report. |

## Evaluation Logic

The evaluation is organized around reproducibility, boundary
preservation, retrieval failure modes, and grounded review.

The current results are:

| Check | Observed result | How to read it |
|---|---|---|
| Store A value lineage | The check covers 3 source rows, 3 SQL output rows, 9 top-SKU rows, 180 source, formula, movement, ranking, and trade-off comparisons, and 5 generated facts. | The source-to-SQL-to-fact path can be inspected field by field across the multi-metric store-period diagnostic. |
| Demo 2 threshold sensitivity | Baseline notes reproduce for all 5 rows. Raising the current thresholds by 5 percentage points changes Stores C-F; lowering them by 5 percentage points changes no rows. | The sample shows how the current diagnostic notes respond to nearby threshold settings. |
| Retrieval wording stress | The applicable run covers supported, unsupported, hard-negative, entity-period-mismatch, and ambiguous queries over the current evidence corpus. | Exact counts, scores, and the exploratory threshold are recorded in the generated retrieval summaries and checked by `eval/check_retrieval_result_applicability.py`. |
| Repeated-window B-F panel | Stores B-F each retain February-April 2026 coverage across 11 selected metrics. | The panel supports descriptive review while preserving the March observation between the February and April endpoints. |
| RAC grounded review | Fixed review cases produce source-linked reports from structured records, local text evidence, and explicit boundary evidence. | Current case counts, routes, and report-contract checks are recorded in `rac/outputs/grounded_quality_summary.md`. |

These analyses are reported separately with their procedures and
outputs. The retrieval results are used to inspect routing behavior under
wording, entity, and period changes rather than to claim calibrated retrieval
performance.

## Implemented Retail Path

### Demo 1: Store A Month-over-Month Diagnostic

Demo 1 analyzes Store A across February, March, and April 2026 through visibility, entry, conversion, transaction, activity, and listed-SKU evidence.

Main file:

- `retail_ops/demo/demo_1_store_a_month_over_month_diagnostic.md`

### Demo 2: Same-Period B-F Diagnostic

Demo 2 extends the analysis to selected Stores B-F under the same March 2026 reporting window. Current SKU evidence uses the selected top-SKU ranking views documented in the source tables.

Main file:

- `retail_ops/demo/demo_2_cross_store_comparability_diagnostic.md`

A threshold-sensitivity check records how the Demo 2 diagnostic notes respond to nearby settings. In the current B-F sample, four of five rows change when the SQL thresholds are raised by 5 percentage points, while lowering them by 5 percentage points produces no note changes.

A query-robustness inspection maps how wording variations and entity-period changes affect evidence routing. Query-level results retain the corresponding similarity scores and selected evidence.

### Repeated-Window B-F Panel

The repeated-window panel adds selected Stores B-F across 2026-02, 2026-03, and 2026-04, making repeated store-period coverage visible across the same selected fields.
The panel places February, March, and April values side by side and preserves March between the February-to-April endpoint summaries. It supports descriptive movement review only; question-specific comparison would require the additional evidence defined separately in `retail_ops/COMPARABILITY_GATE_V0.md`.

Main outputs:

- `retail_ops/outputs/store_period_panel_coverage_output.csv`
- `retail_ops/outputs/repeated_window_panel_summary_output.csv`

## Factor-Aware Grounded Review Layer

RAC is a deterministic source-aware review layer over local project evidence.

The layer decomposes an operating question into relevant factors, routes each factor to local evidence, develops competing explanations, applies claim and definition checks, and recomputes observations and review status from selected evidence, and produces a grounded report with explicit uncertainty, evidence context, source paths, structured-record locators for CSV evidence, source-line pointers for text evidence, and canonical evidence fields.

This layer makes the review path visible across decision factors, source records, competing explanations, and additional evidence requirements.

Reviewer entry points:

| File | Purpose |
|---|---|
| [RAC demo index](rac/DEMO_INDEX.md) | Reviewer-facing index of deterministic RAC cases. |
| [Store A report](rac/outputs/grounded_rac_store_a_attribution_001.md) | Shows deterministic CSV record grounding and multi-factor review. |
| [Cross-store report](rac/outputs/grounded_rac_cross_store_comparability_001.md) | Shows multi-store quantitative evidence routing. |
| [Promotion-review report](rac/outputs/grounded_rac_promotion_strategy_001.md) | Shows review across activity, cost, subsidy, conversion, and supporting operating evidence. |
| [Grounded pipeline](rac/src/grounded_pipeline.py) | Deterministic review and report-generation implementation. |

## Future Comparability-Gate Specification

`retail_ops/COMPARABILITY_GATE_V0.md` specifies the evidence requirements for future question-specific pairwise comparison. The current results use the repeated-window panel for descriptive analysis.

## Field Consistency

`retail_ops/data/DATA_DICTIONARY.md` governs the retail field names, Chinese definitions, formulas, grains, and reporting conventions used by the current retail evidence path.

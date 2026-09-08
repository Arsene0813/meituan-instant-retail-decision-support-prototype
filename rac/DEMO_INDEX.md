# Factor-Aware Grounded Review Demo Index

This page is the reviewer-facing entry point for the `rac/`
implementation. RAC is an important factor-aware grounded review layer
over the structured retail evidence.

It makes factor selection, source routing, unavailable evidence,
competing explanations, critique, rule-based claim and definition checks, and evidence-routing coverage
inspectable before a report is accepted.

The implemented workflow is:

```text
question
-> question analysis
-> factor expansion
-> factor weighting
-> source-aware local evidence grounding
-> boundary evidence for unavailable requirements
-> exact-value and coverage checks
-> hypothesis update
-> critique
-> rule-based claim and definition check
-> review-state update
-> grounded final report
-> report-contract quality gate
```

## 30-Second Summary

RAC turns a retail decision question into an inspectable review path.
Each case records which factors were considered, where the supporting
evidence came from, which requirements remain unavailable, and how the
final judgment was bounded.

The implemented review process:

1. Identify relevant factors.
2. Assign interpretable factor weights.
3. Route each factor to local evidence.
4. Distinguish quantitative evidence from boundary evidence.
5. Record source paths, structured-record locators for CSV evidence, source-line pointers for text evidence, and canonical evidence fields.
6. Recompute observations and competing hypotheses from the selected evidence.
7. Critique weak claims.
8. Check unsupported claims and definition conflicts.
9. Produce a grounded report with a routing coverage score,
   evidence-dependent statuses, unknown confidence where unestimated, and explicit limitations.
10. Validate the report through a report-contract quality gate.

The current implementation is deterministic and source-aware. It uses
local project files, explicit boundary evidence, a shared review-state
contract, and fixed evaluation cases.

## Current Implementation Status

| Component | Status | Current role |
|---|---|---|
| Structured review contracts | Implemented | `rac/prompts/`, `rac/schemas/`, and `rac/eval/` define the review inputs, shared state, and fixed cases. |
| Deterministic review baseline | Implemented | `rac/src/mock_pipeline.py` establishes the typed review-state and report contracts. |
| Local evidence resolver | Implemented | `rac/src/local_evidence_resolver.py` routes factors to structured CSV records, local text evidence, or explicit boundary evidence. |
| Grounded RAC pipeline | Implemented | `rac/src/grounded_pipeline.py` generates source-aware JSON and Markdown reports. |
| Report-contract quality gate | Implemented | `rac/scripts/validate_grounded_quality_gate.py` checks report structure, evidence references, factor evidence status, limitations, and selected claim boundaries. |
| Pairwise comparability gate | Deferred evidence contract | Requires stronger repeated-window, operating-condition, and market-context evidence before a question-specific pairwise decision can be supported. |

Files beginning with `grounded_rac_` are the reviewer-facing evidence
reports. Files beginning with `rac_` are deterministic mock fixtures used
to verify the shared review-state and report contracts.

## How To Run

Run all grounded RAC demo cases:

  python3 rac/scripts/run_grounded_pipeline.py --all-eval

Validate the grounded reports:

  python3 rac/scripts/validate_grounded_quality_gate.py

Expected quality-gate result:

  [OK] RAC report-contract quality gate passed

Exact counts and per-case routes are generated in
[`grounded_quality_summary.md`](outputs/grounded_quality_summary.md).

## Demo Cases

| Case | Question | What It Demonstrates | Grounded Report |
|---|---|---|---|
| `rac_store_a_attribution_001` | Can Store A's March-to-April increases in transaction amount and transaction orders be attributed to search exposure alone? | Checks transaction amount and order-count directions against March-April Store A records, then reviews search, conversion and activity explanations. | [Store A attribution report](outputs/grounded_rac_store_a_attribution_001.md) |
| `rac_cross_store_comparability_001` | Are Stores B-F directly comparable in March 2026? | Selects B-F record values for the declared factors and retains competition context as boundary evidence. | [Cross-store boundary report](outputs/grounded_rac_cross_store_comparability_001.md) |
| `rac_promotion_strategy_001` | What should be checked before changing promotions for a store? | Routes available transaction, cost, and conversion evidence while retaining required SKU-level margin and competitor context as unresolved requirements. | [Promotion-strategy report](outputs/grounded_rac_promotion_strategy_001.md) |
| `rac_system_design_001` | How should RAC connect to the existing memory layer? | Shows how typed memory records feed a factor-aware grounded review path. | [System-design report](outputs/grounded_rac_system_design_001.md) |

## Cross-Store Record Grounding

For `rac_cross_store_comparability_001`, the current evidence routes are:

- The declared record factors select B-F structured records and expose their
  row keys, fields, and values.
- March records cover the reporting period, store type, transaction orders,
  transaction amount, activity evidence, `region_type`, and top-3 SKU
  transaction-amount evidence.
- The repeated-window route exposes the observed February-April transaction
  amounts and order counts for each store.
- Competition context remains `boundary_evidence`.

The exact routes and selected fields are generated in
[`grounded_quality_summary.md`](outputs/grounded_quality_summary.md), and the
selected values are shown in the
[cross-store report](outputs/grounded_rac_cross_store_comparability_001.md).

This case checks whether each declared factor resolves to the expected local evidence. The B-F repeated-window panel covers 2026-02 to 2026-04. A future pairwise comparability gate would need evidence across additional stores, months, activity conditions, and market contexts.

## Recommended Review Order

For a quick review, read these files in order:

1. [RAC demo index](DEMO_INDEX.md)
2. [Store A attribution-boundary report](outputs/grounded_rac_store_a_attribution_001.md)
3. [Cross-store comparability-boundary report](outputs/grounded_rac_cross_store_comparability_001.md)
4. [Promotion-review report](outputs/grounded_rac_promotion_strategy_001.md)
5. [Report-contract quality summary](outputs/grounded_quality_summary.md)
6. [RAC implementation guide](README.md)

For code review, inspect:

1. [`mock_pipeline.py`](src/mock_pipeline.py)
2. [`local_evidence_resolver.py`](src/local_evidence_resolver.py)
3. [`demo2_csv_grounding.py`](src/demo2_csv_grounding.py)
4. [`store_a_csv_grounding.py`](src/store_a_csv_grounding.py)
5. [`evidence_review.py`](src/evidence_review.py)
6. [`grounded_pipeline.py`](src/grounded_pipeline.py)
7. [`validate_grounded_quality_gate.py`](scripts/validate_grounded_quality_gate.py)

## What The Grounded Reports Show

Each grounded report includes:

- a direct answer;
- the detected question type;
- factor weights and their interpretation limits;
- local evidence source paths and grounding roles;
- structured-record locators for CSV evidence and source-line audit pointers for text evidence;
- canonical evidence fields;
- competing hypotheses with recomputed statuses and explicit unknown confidence;
- critic findings;
- claim and definition-check status;
- a bounded final judgment;
- a routing coverage score and its calculation inputs;
- explicit limitations;
- a review-state update.

This structure keeps the evidence path, unresolved requirements, and
interpretation boundary visible in the final report.

## Review Contract

A reviewer can assess each grounded output at three linked levels:

1. **Factor coverage:** whether the question was decomposed into the factors needed for the current decision.
2. **Evidence routing:** whether each factor was linked to an appropriate local source or recorded as explicit boundary evidence.
3. **Judgment boundary:** whether the final judgment remains within the evidence, definitions, entity scope, and reporting period available to the case.

Each evidence record retains its source path, grounding role, and canonical evidence fields. Structured CSV evidence records the selected row keys and values; text evidence records a local line pointer. Missing requirements remain visible in the review trace rather than being replaced by an unsupported conclusion.

## Current Evidence and Integration Boundary

The current implemented scope includes:

- deterministic question classification and factor expansion;
- fixed and interpretable factor-weight buckets;
- factor-specific local evidence routing;
- explicit boundary evidence for unavailable requirements;
- exact-value checks and evidence-dependent hypotheses;
- critique and rule-based claim and definition-check records;
- JSON Schema and cross-record state validation;
- fixed evaluation cases and generated quality summaries.

The local evidence resolver uses deterministic matching over project
files. Factor weights order review attention. Grounded numerical confidence
remains unknown; statuses and check results change with the selected evidence.

For pairwise cross-store decisions, the next evidence contract requires
defined thresholds, repeated reporting windows, and additional operating
and market-context evidence. Pairwise comparability remains future work.

## Current Extension Boundary

The reviewer-facing scope is the deterministic, file-grounded review
path and the fixed evaluation cases documented above. Additional model,
retrieval, orchestration, or live-backend work should be treated as a
separate experiment only when it answers a defined evaluation question
without weakening source traceability, entity and period constraints,
or judgment boundaries.
## Score Explainability Note

Grounded reports use Routing coverage score. It summarizes record- or keyword-matched local routes, explicit boundary routes, missing-source status, and fallback status.

Mock reports use Scenario-Template Confidence. This is a deterministic template value, not a calculated routing coverage score.

The grounded score weights are fixed prototype heuristics:

- `0.45` for record- or keyword-matched local routes;
- `0.25` for supported-or-boundary coverage;
- `0.15` for source-file traceability;
- `0.15` for fallback avoidance.

They are not learned, calibrated, or presented as optimized decision weights. Sensitivity testing remains a planned robustness check.

# Factor-Aware Grounded Review (RAC)

RAC is the factor-aware grounded review layer of the current
decision-support prototype.

It takes a decision question and structured local evidence, makes the
review path explicit, and records where the available evidence supports
a statement, where only a boundary can be established, and where a
stronger conclusion should remain unresolved.

The runnable implementation is deterministic and file-grounded. It uses
local project files and explicit review rules so that the reasoning
contract can be inspected and evaluated independently of model behavior.

For the reviewer-facing cases and generated reports, start with
[`DEMO_INDEX.md`](DEMO_INDEX.md).

## Quick Start

Generate the reviewer-facing grounded reports:

```bash
python3 rac/scripts/run_grounded_pipeline.py --all-eval
```

Validate the generated reports and their evidence boundaries:

```bash
python3 rac/scripts/validate_grounded_pipeline.py
python3 rac/scripts/validate_grounded_quality_gate.py
```

## Implemented Review Path

```text
question
-> question analysis
-> factor expansion
-> factor weighting
-> source-aware local evidence routing
-> boundary evidence for unavailable requirements
-> exact-value and coverage checks
-> competing hypotheses updated from those checks
-> critique
-> rule-based claim and definition check
-> review-state update
-> grounded final report
-> report-contract quality gate
```

The pipeline separates factor selection, evidence retrieval, hypothesis
formation, critique, and report generation. A final answer therefore
retains an inspectable path back to the evidence packets and limitations
used during review.

## Why This Layer Exists

A plausible answer is not necessarily a supported decision.

RAC adds explicit checks for:

- which factors are relevant to the question;
- which source files were used;
- which assumptions remain unsupported;
- which competing explanations remain possible;
- which requirements are absent from the current evidence;
- which conclusion should be qualified or withheld;
- whether the final report preserves the evidence boundary.

This layer complements the retail field dictionary, SQL diagnostics,
generated memory facts, lineage validation, and answer-boundary
evaluation.

## Current Implementation

The current RAC path includes:

- a shared review-state contract;
- deterministic question analysis;
- question-specific factor expansion;
- explicit heuristic factor weights;
- local file-based evidence routing;
- source-path existence checks;
- structured CSV record selection and source-specific text-anchor matching;
- local evidence snippets and line ranges;
- boundary evidence for unavailable requirements;
- evidence-dependent hypothesis statements and statuses;
- critique and rule-based claim and definition-check stages;
- routing coverage scoring;
- explicit limitation updates;
- grounded Markdown and JSON reports;
- a deterministic report-contract quality gate.

The current runnable path uses local files and deterministic rules.
Graph orchestration, model calls, vector retrieval, and live
merchant-backend access are future integration options rather than
requirements for the present evidence contract.

Reviewer-facing terminology is deliberately narrow: the claim and definition check applies deterministic rules to selected unsupported claims and definition conflicts, while the report-contract quality gate validates report structure and selected evidence boundaries. Neither mechanism establishes that an operating conclusion is correct.

## Evidence Routes

RAC distinguishes four evidence-routing outcomes.

| Route | Meaning | Review treatment |
|---|---|---|
| Record evidence | Structured local records are selected with row keys, canonical fields, and values. | The report may use those observations within the documented entity, period, and field scope. |
| Definition or context route | A local document supplies a relevant definition, rule, or contextual description. | The route establishes definitions or review context, but is not treated as observed numeric support. |
| Boundary evidence | A local source records that a required field, gate, or condition is unavailable or not implemented. | The report records the missing requirement instead of inventing support. |
| Fallback route | No sufficiently specific record, definition, context, or boundary route was resolved. | Routing coverage is reduced and the unresolved requirement remains visible. |

For the cross-store comparability case, the February-April 2026 B-F
panel provides repeated-window record evidence for descriptive review.
Competition context and the requirements for a future pairwise gate remain
boundary evidence in `retail_ops/COMPARABILITY_GATE_V0.md`.

In factor-weight rows, `partially_supported` means that at least one
selected observation or registered document/boundary route is available.
Individual checks show which values are present and which requirements
remain unresolved.

## Factor Weight Generation

Factor weights are generated in `rac/src/mock_pipeline.py` using explicit
factor-ID buckets.

```text
high-priority review factors   -> 0.85
medium-priority review factors -> 0.72
default relevant factors       -> 0.60
```

### High-priority factors

```text
promotion_intensity
activity_intensity
order_conversion
sku_margin_structure
evidence_packets
belief_records
retrieval_trace
```

### Medium-priority factors

```text
search_exposure
entry_conversion
same_reporting_period
store_type
order_volume
transaction_amount
transaction_orders
payment_conversion
typed_memory
hypotheses
confidence
limitations
active_state_filtering
```

Any relevant factor not listed in the high- or medium-priority buckets
receives the default value.

These values are review-priority weights. They are fixed prototype
heuristics, not learned parameters, metric-derived estimates,
probabilities, business thresholds, or causal-effect estimates.

The grounded reports expose the bucket, factor membership, assigned
weight, and interpretation limit so the weighting rule can be reviewed
directly.

## Routing Coverage Score

Grounded RAC reports use a formula-based routing coverage score:

```text
routing_coverage_score
= 0.45 * record_or_keyword_route_rate
+ 0.25 * resolved_or_boundary_route_rate
+ 0.15 * no_missing_source_file_score
+ 0.15 * no_fallback_score
```

| Component | Weight | Rationale |
|---|---:|---|
| `record_or_keyword_route_rate` | 0.45 | Record- or keyword-matched local routes receive the highest weight. |
| `resolved_or_boundary_route_rate` | 0.25 | Explicit boundary evidence is preferable to an unsupported inference. |
| `no_missing_source_file_score` | 0.15 | Existing source paths are required for traceability. |
| `no_fallback_score` | 0.15 | Unresolved fallback packets reduce routing coverage. |

The score summarizes whether the current deterministic resolver found local
evidence or recorded an explicit boundary for requested evidence routes. It
does not measure evidence strength, conclusion correctness, business impact,
decision quality, or model confidence. It is not used to select or rank the
final judgment.

Grounded hypothesis and belief `confidence` values are `null`: no calibrated
numerical estimate is available. Hypothesis `status`, evidence checks and
belief validity conditions carry the current assessment. Fixed mock-fixture
numbers remain confined to the mock path. The evaluation cases expect
`unknown` confidence for grounded runs.

## Competing Hypotheses, Critique, and Fact Checks

RAC does not route evidence directly into a single preferred narrative.

For each review case, the pipeline can retain multiple plausible
explanations, then check:

- whether each hypothesis has relevant evidence;
- whether the hypothesis exceeds the evidence scope;
- whether another explanation remains plausible;
- whether a causal or strategy-transfer statement is unsupported;
- whether source paths and snippets are present;
- whether the final report carries unresolved limitations forward.

This structure is especially important when observational store metrics
move in different directions or when same-period records lack the
question-specific context required for comparison.

## Runnable Layers

| Layer | Run command | Validation command | Main output |
|---|---|---|---|
| Deterministic review path | `python3 rac/scripts/run_mock_pipeline.py --all-eval` | `python3 rac/scripts/validate_mock_pipeline.py` | Generated review-state and report outputs in `rac/outputs/` |
| Local evidence resolver | `python3 rac/scripts/run_local_evidence_resolver.py --all-eval` | `python3 rac/scripts/validate_local_evidence_resolver.py` | Source-grounded evidence packets |
| Grounded deterministic pipeline | `python3 rac/scripts/run_grounded_pipeline.py --all-eval` | `python3 rac/scripts/validate_grounded_pipeline.py` | `rac/outputs/grounded_*.json` and `rac/outputs/grounded_*.md` |
| Report-contract quality gate | Generated with the grounded pipeline | `python3 rac/scripts/validate_grounded_quality_gate.py` | `grounded_quality_summary.json` and `grounded_quality_summary.md` |

The deterministic review pipeline establishes the review-state contract. The local
resolver connects factors to repository evidence. The grounded pipeline
builds its factor plan first, resolves evidence, then computes observations,
hypotheses, critique, fact checks and belief state. The quality gate checks
whether those reports preserve the required evidence structure.

## Grounded Report Contract

The report-contract quality gate checks that every generated report contains:

- the reviewed question;
- expanded factors and factor weights;
- competing hypotheses;
- critic findings;
- rule-based claim and definition-check output;
- local source paths;
- structured record keys, canonical fields, and selected values for CSV evidence;
- source line ranges and local snippets for text evidence;
- explicit limitations;
- evidence-routing status;
- no missing source files;
- no forbidden positive overclaims.

This contract prevents the RAC layer from becoming a loose prompt
wrapper. A report must show what evidence it used, where that evidence
came from, and what remains unresolved.

## Reviewer Cases

The reviewer-facing cases are indexed in
[`DEMO_INDEX.md`](DEMO_INDEX.md).

They cover questions such as:

### Store A attribution review

Can Store A's April change be attributed to search exposure?

The review should consider search exposure, entry conversion, order
conversion, activity involvement, and competing explanations rather than
assigning the movement to one metric.

### Cross-store comparability review

Are Stores B-F directly comparable in March 2026?

The review should preserve the common reporting period and metric
definitions while identifying the additional question-specific evidence
required for pairwise comparability.

### Promotion-strategy review

What should be checked before changing a store's promotion strategy?

The review should identify relevant factors, retrieve available evidence,
record missing requirements, and distinguish diagnostic checks from a
strategy recommendation.

## Interpretation Boundary

The current RAC implementation supports an inspectable, deterministic
review over structured local evidence. It can expose relevant factors,
source use, competing explanations, evidence gaps, and report
limitations.

Causal inference, learned factor weights, live merchant-backend access,
automated pairwise comparability decisions, and autonomous operating
actions require additional evidence or implementation beyond the
current review contract.

## Extension Boundary

The implemented reference condition is the deterministic, file-grounded
pipeline documented above. An additional retrieval, model-assisted,
orchestration, weighting, or backend-integration experiment belongs in
this repository only when it has a defined comparison question and can
be evaluated against the same reviewer cases without changing the
canonical retail field contract.

Such an experiment must preserve:

- typed review state;
- source references for evidence claims;
- entity and reporting-period constraints;
- explicit unresolved requirements and limitations;
- competing hypotheses and critique;
- routing coverage as route resolution rather than evidence strength;
- withholding of conclusions that exceed the available evidence.


## Evidence Recalculation Contract

The current registered data scopes are Store A, March-April 2026, and
Stores B-F, March 2026, with the registered February-April panel used for
repeated-window context. Other requested stores or windows require a
registered route. General promotion and memory-design questions use document
routes and do not select store records.

`rac/src/evidence_review.py` builds only the plan before retrieval. After
retrieval, it compares exact source numbers, checks record coverage and
rebuilds the decision-bearing fields. Store A's six factors include both
`transaction_amount` and `transaction_orders`. Missing values remain missing;
zero is a provided value. A falling or mixed transaction pattern changes the
observation, hypothesis statuses and belief text. The question's own growth
premise is checked against those values.

Registered CSV routes retain their errors. A document anchor cannot replace
missing or invalid CSV records. The CSV readers validate complete reporting
dates, canonical columns and logical keys before selection. The repeated-window
summary is reconciled against the current registered panel before its selected
cells are used. The [publication CLI](../retail_ops/ingestion/PUBLICATION.md)
rebuilds SQL diagnostics and Demo 2 facts from selected batches, then gives
RAC the same verified publication for the full run. Existing RAC scripts still
read project fixtures; the separate range API reads selected source publications.

The review records exact source operands and comparison results in
`evidence_review`. State validation recomputes the decision-bearing fields
against that selected snapshot. The report gate additionally checks source
values and evidence links. These are deterministic checks for the registered
operations; they do not validate arbitrary natural-language claims.

| Existing field or added RAC metadata | Definition and use | Naming decision |
|---|---|---|
| `transaction_amount` | Existing canonical backend metric and factor ID; now also selected for the Store A attribution review. Its business definition is unchanged. | Reuse existing name. |
| `transaction_orders` | Existing canonical count; retained separately from amount. | No rename. |
| `evidence_review` | Added RAC review metadata, registered in `cognition_state.schema.json`. | New internal metadata; no source metric added. |
| `method`, `confidence_method` | Registered check-rule version and `not_estimated` confidence method. | Internal review metadata. |
| `scope.store_ids`, `scope.period_start`, `scope.period_end` | Independently validated request scope; document-only reviews use no store/date selection. | Internal scope metadata; source `store_id` stays unchanged. |
| `checks` | Per-field comparison, recorded-value or document-route checks. | Internal review metadata. |
| `check_id`, `factor_id`, `evidence_id`, `source_path`, `field` | Links each check to its registered factor, selected packet and canonical field. | Existing identifiers reused in the new check records. |
| `operation`, `status`, `result`, `claim` | Check operation and its computed result; descriptive claims are rendered from those results. | Internal check metadata. |
| `operands.row_key`, `operands.value` | Exact source strings associated with selected keys; blank operands become JSON null for checking, while the evidence packet keeps its source text. | Internal check metadata; no backfill. |
| Hypothesis/belief `confidence` | Required existing field; grounded runs store null when no calibrated estimate exists. | No rename; explicit unknown value registered. |
| `expected_confidence` | Existing evaluation field; grounded cases now expect `unknown`. | No rename. |
| `belief_id` value `store_a_march_april_increase_not_search_only` | The active review now uses `store_a_march_april_attribution`, so its identifier does not assert a direction before checking data. | One generated RAC record ID updated; field name unchanged. |

The business-field rename count is zero. The dictionary, metric definitions
and source CSV values are unchanged by this implementation step.

## Selected date ranges

The [range API and CLI](../retail_ops/ingestion/RANGE_API.md) read actual-date
source publications. One range returns recorded values and evidence gaps;
one store with an earlier baseline adds temporal comparisons; several stores
under one range add descriptive value comparisons. `range_analysis` opens the
publication once for all windows and every RAC check.

`range_evidence_review.py` reconciles query summaries with their source rows,
checks reporting conditions, evaluates alternative directions and selected
co-movement statements, and recomputes the belief and final answer. The default
review selects `transaction_amount` and `transaction_orders`; other registered
fields can be selected explicitly. Complete daily sums retain the Step 7 rules.
A recorded exact-window value remains visible when comparison context is missing.

The contract is registered in `rac/contracts/range_review.v1.json` and
`rac/schemas/range_cognition_state.v1.schema.json`. Factor priority orders
attention; confidence remains null where no estimate has been calibrated.
This path returns a transient state and does not save queries or replace the
existing monthly review state schema. Its descriptive comparisons do not
implement the future operating-strategy comparability gate.

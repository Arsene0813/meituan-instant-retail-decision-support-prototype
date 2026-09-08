# Grounded RAC Report

## 1. Direct Answer

Stores B-F have verified March 2026 records for same-period diagnostic review. transaction_amount: B=11665.5; C=7064.09; D=18078.7; E=5784.87; F=9301.8. transaction_orders: B=299; C=175; D=404; E=158; F=266. observed_month_count: B=3; C=3; D=3; E=3; F=3. Direct comparability remains unresolved.

Deterministic local-file review; routing scores summarize route resolution under the current rules.

## 2. Question Type

- Question type: comparability_judgment
- Domain: retail_operations

## 3. Factor Weights

### 3a. How Factor Weights Are Generated

Factor weights are fixed review-priority buckets assigned by explicit rules in `rac/src/mock_pipeline.py`. They order review attention within the current evidence scope.

| Bucket | Weight | Rule | Factors in This Report |
|---|---:|---|---|
| high | 0.85 | Central to avoiding overconfident or misleading conclusions. | activity_intensity |
| medium | 0.72 | Important context but not sufficient on its own. | same_reporting_period, store_type, order_volume, transaction_amount |
| default | 0.60 | Potentially relevant but requires stronger evidence. | region_context, competition, sku_structure, repeated_reporting_windows |

Weighting boundary:

- Use these values only to order review attention within the current evidence scope.

### 3b. Factor Weights Used in This Report

| Decision Factor ID | Weight | Bucket | Evidence Status | Why It Matters |
|---|---:|---|---|---|
| same_reporting_period | 0.72 | medium | partially_supported | Important context but not sufficient on its own. |
| store_type | 0.72 | medium | partially_supported | Important context but not sufficient on its own. |
| order_volume | 0.72 | medium | partially_supported | Important context but not sufficient on its own. |
| transaction_amount | 0.72 | medium | partially_supported | Important context but not sufficient on its own. |
| activity_intensity | 0.85 | high | partially_supported | Central to avoiding overconfident or misleading conclusions. |
| region_context | 0.60 | default | partially_supported | Potentially relevant but requires stronger evidence. |
| competition | 0.60 | default | partially_supported | Potentially relevant but requires stronger evidence. |
| sku_structure | 0.60 | default | partially_supported | Potentially relevant but requires stronger evidence. |
| repeated_reporting_windows | 0.60 | default | partially_supported | Potentially relevant but requires stronger evidence. |

`partially_supported` means at least one selected observation or registered document/boundary route is available for the factor. Read the evidence checks to distinguish values from context.

## 4. Local Evidence Grounding

- Total evidence packets: 9
- Record matched packets: 8
- Keyword matched packets: 0
- Boundary matched packets: 1
- Fallback packets: 0
- Missing source files: 0

For CSV evidence, `Source Locator` shows the selected record scope and `Selected Values` shows values read from those records. For Markdown evidence, the locator remains a local line-range pointer.
`Decision Factor ID` is an internal RAC review identifier. The field column shows canonical project fields where available and labels unresolved requirements explicitly.

| Decision Factor ID | Source | Evidence Type | Status | Source Locator | Canonical Evidence Fields / Requirement | Selected Values |
|---|---|---|---|---|---|---|
| same_reporting_period | retail_ops/outputs/demo2_cross_store_comparability_output.csv | context_evidence | record_matched | records: store_id=B, C, D, E, F; period_month=2026-03; rows=5 | period_start, period_end, period_month | store_id=B, period_month=2026-03: period_start=2026-03-01, period_end=2026-03-31, period_month=2026-03<br>store_id=C, period_month=2026-03: period_start=2026-03-01, period_end=2026-03-31, period_month=2026-03<br>store_id=D, period_month=2026-03: period_start=2026-03-01, period_end=2026-03-31, period_month=2026-03<br>store_id=E, period_month=2026-03: period_start=2026-03-01, period_end=2026-03-31, period_month=2026-03<br>store_id=F, period_month=2026-03: period_start=2026-03-01, period_end=2026-03-31, period_month=2026-03 |
| store_type | retail_ops/outputs/demo2_cross_store_comparability_output.csv | context_evidence | record_matched | records: store_id=B, C, D, E, F; period_month=2026-03; rows=5 | store_type | store_id=B, period_month=2026-03: store_type=self-operated<br>store_id=C, period_month=2026-03: store_type=self-operated<br>store_id=D, period_month=2026-03: store_type=self-operated<br>store_id=E, period_month=2026-03: store_type=partner<br>store_id=F, period_month=2026-03: store_type=partner |
| order_volume | retail_ops/outputs/demo2_cross_store_comparability_output.csv | quantitative_evidence | record_matched | records: store_id=B, C, D, E, F; period_month=2026-03; rows=5 | transaction_orders | store_id=B, period_month=2026-03: transaction_orders=299<br>store_id=C, period_month=2026-03: transaction_orders=175<br>store_id=D, period_month=2026-03: transaction_orders=404<br>store_id=E, period_month=2026-03: transaction_orders=158<br>store_id=F, period_month=2026-03: transaction_orders=266 |
| transaction_amount | retail_ops/outputs/demo2_cross_store_comparability_output.csv | quantitative_evidence | record_matched | records: store_id=B, C, D, E, F; period_month=2026-03; rows=5 | transaction_amount | store_id=B, period_month=2026-03: transaction_amount=11665.5<br>store_id=C, period_month=2026-03: transaction_amount=7064.09<br>store_id=D, period_month=2026-03: transaction_amount=18078.7<br>store_id=E, period_month=2026-03: transaction_amount=5784.87<br>store_id=F, period_month=2026-03: transaction_amount=9301.8 |
| activity_intensity | retail_ops/outputs/demo2_cross_store_comparability_output.csv | quantitative_evidence | record_matched | records: store_id=B, C, D, E, F; period_month=2026-03; rows=5 | activity_orders, activity_order_share_pct, activity_cost, activity_cost_ratio_pct | store_id=B, period_month=2026-03: activity_orders=265, activity_order_share_pct=88.63, activity_cost=3361.3, activity_cost_ratio_pct=24.12<br>store_id=C, period_month=2026-03: activity_orders=124, activity_order_share_pct=70.86, activity_cost=490.21, activity_cost_ratio_pct=9.45<br>store_id=D, period_month=2026-03: activity_orders=337, activity_order_share_pct=83.42, activity_cost=2776.4, activity_cost_ratio_pct=14.92<br>store_id=E, period_month=2026-03: activity_orders=109, activity_order_share_pct=68.99, activity_cost=1576.26, activity_cost_ratio_pct=29.38<br>store_id=F, period_month=2026-03: activity_orders=217, activity_order_share_pct=81.58, activity_cost=1008.3, activity_cost_ratio_pct=12.16 |
| region_context | retail_ops/outputs/demo2_cross_store_comparability_output.csv | context_evidence | record_matched | records: store_id=B, C, D, E, F; period_month=2026-03; rows=5 | region_type | store_id=B, period_month=2026-03: region_type=Qingdao<br>store_id=C, period_month=2026-03: region_type=Qingdao<br>store_id=D, period_month=2026-03: region_type=Yantai<br>store_id=E, period_month=2026-03: region_type=Yantai<br>store_id=F, period_month=2026-03: region_type=Yantai |
| competition | retail_ops/COMPARABILITY_GATE_V0.md | boundary_evidence | boundary_matched | lines 101-103 | competition-context requirement in comparability contract | n/a |
| sku_structure | retail_ops/outputs/demo2_cross_store_comparability_output.csv | product_mix_evidence | record_matched | records: store_id=B, C, D, E, F; period_month=2026-03; rows=5 | top3_sku_transaction_amount, top3_sku_transaction_amount_share_pct | store_id=B, period_month=2026-03: top3_sku_transaction_amount=1300.9, top3_sku_transaction_amount_share_pct=11.15<br>store_id=C, period_month=2026-03: top3_sku_transaction_amount=2004.84, top3_sku_transaction_amount_share_pct=28.38<br>store_id=D, period_month=2026-03: top3_sku_transaction_amount=3055.78, top3_sku_transaction_amount_share_pct=16.9<br>store_id=E, period_month=2026-03: top3_sku_transaction_amount=726.25, top3_sku_transaction_amount_share_pct=12.55<br>store_id=F, period_month=2026-03: top3_sku_transaction_amount=1798.4, top3_sku_transaction_amount_share_pct=19.33 |
| repeated_reporting_windows | retail_ops/outputs/repeated_window_panel_summary_output.csv | quantitative_evidence | record_matched | records: store_id=B, C, D, E, F; rows=5 | observed_month_count, feb_transaction_amount, mar_transaction_amount, apr_transaction_amount, feb_transaction_orders, mar_transaction_orders, apr_transaction_orders | store_id=B: observed_month_count=3, feb_transaction_amount=10468.0, mar_transaction_amount=11665.5, apr_transaction_amount=11496.8, feb_transaction_orders=259.0, mar_transaction_orders=299.0, apr_transaction_orders=293.0<br>store_id=C: observed_month_count=3, feb_transaction_amount=9503.7, mar_transaction_amount=7064.09, apr_transaction_amount=6756.8, feb_transaction_orders=253.0, mar_transaction_orders=175.0, apr_transaction_orders=178.0<br>store_id=D: observed_month_count=3, feb_transaction_amount=20332.2, mar_transaction_amount=18078.7, apr_transaction_amount=14087.2, feb_transaction_orders=466.0, mar_transaction_orders=404.0, apr_transaction_orders=308.0<br>store_id=E: observed_month_count=3, feb_transaction_amount=6794.9, mar_transaction_amount=5784.87, apr_transaction_amount=11264.72, feb_transaction_orders=148.0, mar_transaction_orders=158.0, apr_transaction_orders=377.0<br>store_id=F: observed_month_count=3, feb_transaction_amount=12549.1, mar_transaction_amount=9301.8, apr_transaction_amount=14090.7, feb_transaction_orders=307.0, mar_transaction_orders=266.0, apr_transaction_orders=424.0 |


## 5. Competing Hypotheses

Hypothesis statements and statuses are recomputed from the selected evidence. Confidence is unknown where no calibrated estimate is available.

| Hypothesis | Confidence | Status | Weakness |
|---|---:|---|---|
| Stores B-F have verified March 2026 records for same-period diagnostic review. transaction_amount: B=11665.5; C=7064.09; D=18078.7; E=5784.87; F=9301.8. transaction_orders: B=299; C=175; D=404; E=158; F=266. observed_month_count: B=3; C=3; D=3; E=3; F=3. | unknown | strong | Only the stated fields and reporting windows were checked. |
| The current evidence establishes direct comparability for an operating decision. | unknown | unsupported | A question-specific pairwise gate is not implemented; Region type remains weak context. |

## 6. Critic Findings

- [medium] Shared reporting dates do not establish direct comparability. Recommendation: Use observations and tests appropriate to the proposed decision.

## 7. Claim and Definition Check

- Status: pass

### Evidence Checks

| Check | Evidence | Status | Result |
|---|---|---|---|
| same_reporting_period/period_start | evidence_same_reporting_period | supported | period_start: B=2026-03-01; C=2026-03-01; D=2026-03-01; E=2026-03-01; F=2026-03-01. |
| same_reporting_period/period_end | evidence_same_reporting_period | supported | period_end: B=2026-03-31; C=2026-03-31; D=2026-03-31; E=2026-03-31; F=2026-03-31. |
| same_reporting_period/period_month | evidence_same_reporting_period | supported | period_month: B=2026-03; C=2026-03; D=2026-03; E=2026-03; F=2026-03. |
| store_type/store_type | evidence_store_type | supported | store_type: B=self-operated; C=self-operated; D=self-operated; E=partner; F=partner. |
| order_volume/transaction_orders | evidence_order_volume | supported | transaction_orders: B=299; C=175; D=404; E=158; F=266. |
| transaction_amount/transaction_amount | evidence_transaction_amount | supported | transaction_amount: B=11665.5; C=7064.09; D=18078.7; E=5784.87; F=9301.8. |
| activity_intensity/activity_orders | evidence_activity_intensity | supported | activity_orders: B=265; C=124; D=337; E=109; F=217. |
| activity_intensity/activity_order_share_pct | evidence_activity_intensity | supported | activity_order_share_pct: B=88.63; C=70.86; D=83.42; E=68.99; F=81.58. |
| activity_intensity/activity_cost | evidence_activity_intensity | supported | activity_cost: B=3361.3; C=490.21; D=2776.4; E=1576.26; F=1008.3. |
| activity_intensity/activity_cost_ratio_pct | evidence_activity_intensity | supported | activity_cost_ratio_pct: B=24.12; C=9.45; D=14.92; E=29.38; F=12.16. |
| region_context/region_type | evidence_region_context | supported | region_type: B=Qingdao; C=Qingdao; D=Yantai; E=Yantai; F=Yantai. |
| competition/document | evidence_competition | supported | competition: registered boundary_evidence anchor matched. |
| sku_structure/top3_sku_transaction_amount | evidence_sku_structure | supported | top3_sku_transaction_amount: B=1300.9; C=2004.84; D=3055.78; E=726.25; F=1798.4. |
| sku_structure/top3_sku_transaction_amount_share_pct | evidence_sku_structure | supported | top3_sku_transaction_amount_share_pct: B=11.15; C=28.38; D=16.9; E=12.55; F=19.33. |
| repeated_reporting_windows/observed_month_count | evidence_repeated_reporting_windows | supported | observed_month_count: B=3; C=3; D=3; E=3; F=3. |
| repeated_reporting_windows/feb_transaction_amount | evidence_repeated_reporting_windows | supported | feb_transaction_amount: B=10468.0; C=9503.7; D=20332.2; E=6794.9; F=12549.1. |
| repeated_reporting_windows/mar_transaction_amount | evidence_repeated_reporting_windows | supported | mar_transaction_amount: B=11665.5; C=7064.09; D=18078.7; E=5784.87; F=9301.8. |
| repeated_reporting_windows/apr_transaction_amount | evidence_repeated_reporting_windows | supported | apr_transaction_amount: B=11496.8; C=6756.8; D=14087.2; E=11264.72; F=14090.7. |
| repeated_reporting_windows/feb_transaction_orders | evidence_repeated_reporting_windows | supported | feb_transaction_orders: B=259.0; C=253.0; D=466.0; E=148.0; F=307.0. |
| repeated_reporting_windows/mar_transaction_orders | evidence_repeated_reporting_windows | supported | mar_transaction_orders: B=299.0; C=175.0; D=404.0; E=158.0; F=266.0. |
| repeated_reporting_windows/apr_transaction_orders | evidence_repeated_reporting_windows | supported | apr_transaction_orders: B=293.0; C=178.0; D=308.0; E=377.0; F=424.0. |

- Unsupported claims detected by current rules: none
- Definition conflicts detected by current rules: none

## 8. Final Judgment

Stores B-F have verified March 2026 records for same-period diagnostic review. transaction_amount: B=11665.5; C=7064.09; D=18078.7; E=5784.87; F=9301.8. transaction_orders: B=299; C=175; D=404; E=158; F=266. observed_month_count: B=3; C=3; D=3; E=3; F=3. Direct comparability remains unresolved.

The judgment is bounded by the cited local evidence and the unresolved requirements recorded above.

## 9. Evidence-Routing Coverage

Packet composition:

- Total packets: 9
- Record matched packets: 8
- Keyword matched packets: 0
- Boundary matched packets: 1
- Fallback packets: 0
- Missing source files: 0

- Routing coverage score: 0.95
- Read this value as route resolution under the current rules, not as evidence strength or decision quality.

How this score is calculated:

```text
routing_coverage_score =
  0.45 * record_or_keyword_route_rate
+ 0.25 * resolved_or_boundary_route_rate
+ 0.15 * no_missing_source_file_score
+ 0.15 * no_fallback_score
```

Weight rationale:

| Component | Weight | Why |
|---|---:|---|
| `record_or_keyword_route_rate` | 0.45 | Highest priority because record- or keyword-matched local routes should matter more than boundary-only evidence. |
| `resolved_or_boundary_route_rate` | 0.25 | Boundary evidence is valuable because it explicitly records missing requirements instead of hiding them. |
| `no_missing_source_file_score` | 0.15 | Source files must exist, but this is a basic traceability check rather than evidence strength. |
| `no_fallback_score` | 0.15 | Fallback packets indicate unresolved routing and reduce the current routing score. |

Score contract:

- Component weights are fixed prototype heuristics.
- The score summarizes route resolution under the current rules.
- Alternative weights are a formula sensitivity check; the report judgment is produced separately.

Score inputs (contract fields):

- total_packets = 9
- record_matched_packets = 8
- keyword_matched_packets = 0
- boundary_matched_packets = 1
- fallback_packets = 0
- missing_source_files = 0

Derived rates and checks:

- record_or_keyword_route_rate = (record_matched_packets + keyword_matched_packets) / total_packets = 0.89
- resolved_or_boundary_route_rate = (record_matched_packets + keyword_matched_packets + boundary_matched_packets) / total_packets = 1.00
- no_missing_source_file_score = 1.00
- no_fallback_score = 1.00

Reading the score:

- A higher value means more requested evidence routes were resolved or explicitly bounded.
- Boundary evidence contributes when it documents a missing requirement.
- Read the score as coverage rather than evidence strength, causal validity, decision quality, or business impact.

## 10. What Cannot Be Concluded

- Same-period diagnostic review is not a pairwise comparability gate.
- Region type remains weak context.

## 11. Review-State Update

- review_state_id: stores_b_f_same_period_not_directly_comparable
- status: active
- validity_conditions:
  - B-F, March 2026; repeated evidence uses the registered February-April panel.
  - Recompute after source or saved-summary changes.

# Grounded RAC Report

## 1. Direct Answer

transaction_amount increased from 6454.84 to 9083.72 (2026-03 to 2026-04). transaction_orders increased from 207 to 337 (2026-03 to 2026-04). The selected observations do not isolate search exposure as the sole cause.

Deterministic local-file review; routing scores summarize route resolution under the current rules.

## 2. Question Type

- Question type: causal_diagnostic
- Domain: retail_operations

## 3. Factor Weights

### 3a. How Factor Weights Are Generated

Factor weights are fixed review-priority buckets assigned by explicit rules in `rac/src/mock_pipeline.py`. They order review attention within the current evidence scope.

| Bucket | Weight | Rule | Factors in This Report |
|---|---:|---|---|
| high | 0.85 | Central to avoiding overconfident or misleading conclusions. | order_conversion, promotion_intensity |
| medium | 0.72 | Important context but not sufficient on its own. | search_exposure, entry_conversion, transaction_orders, transaction_amount |
| default | 0.60 | Potentially relevant but requires stronger evidence. | none |

Weighting boundary:

- Use these values only to order review attention within the current evidence scope.

### 3b. Factor Weights Used in This Report

| Decision Factor ID | Weight | Bucket | Evidence Status | Why It Matters |
|---|---:|---|---|---|
| search_exposure | 0.72 | medium | partially_supported | Important context but not sufficient on its own. |
| entry_conversion | 0.72 | medium | partially_supported | Important context but not sufficient on its own. |
| order_conversion | 0.85 | high | partially_supported | Central to avoiding overconfident or misleading conclusions. |
| promotion_intensity | 0.85 | high | partially_supported | Central to avoiding overconfident or misleading conclusions. |
| transaction_orders | 0.72 | medium | partially_supported | Important context but not sufficient on its own. |
| transaction_amount | 0.72 | medium | partially_supported | Important context but not sufficient on its own. |

`partially_supported` means at least one selected observation or registered document/boundary route is available for the factor. Read the evidence checks to distinguish values from context.

## 4. Local Evidence Grounding

- Total evidence packets: 6
- Record matched packets: 6
- Keyword matched packets: 0
- Boundary matched packets: 0
- Fallback packets: 0
- Missing source files: 0

For CSV evidence, `Source Locator` shows the selected record scope and `Selected Values` shows values read from those records. For Markdown evidence, the locator remains a local line-range pointer.
`Decision Factor ID` is an internal RAC review identifier. The field column shows canonical project fields where available and labels unresolved requirements explicitly.

| Decision Factor ID | Source | Evidence Type | Status | Source Locator | Canonical Evidence Fields / Requirement | Selected Values |
|---|---|---|---|---|---|---|
| search_exposure | retail_ops/data/store_a_monthly_metrics.csv | quantitative_evidence | record_matched | records: store_id=A; period_month=2026-03, 2026-04; rows=2 | search_exposure_users, search_average_rank, search_entry_users | store_id=A, period_month=2026-03: search_exposure_users=4172, search_average_rank=20, search_entry_users=445<br>store_id=A, period_month=2026-04: search_exposure_users=7736, search_average_rank=18, search_entry_users=839 |
| entry_conversion | retail_ops/data/store_a_monthly_metrics.csv | quantitative_evidence | record_matched | records: store_id=A; period_month=2026-03, 2026-04; rows=2 | exposure_users, entry_users, entry_conversion_rate_pct | store_id=A, period_month=2026-03: exposure_users=4663, entry_users=522, entry_conversion_rate_pct=11.19<br>store_id=A, period_month=2026-04: exposure_users=8366, entry_users=906, entry_conversion_rate_pct=10.83 |
| order_conversion | retail_ops/data/store_a_monthly_metrics.csv | quantitative_evidence | record_matched | records: store_id=A; period_month=2026-03, 2026-04; rows=2 | entry_users, order_users, order_conversion_rate_pct | store_id=A, period_month=2026-03: entry_users=522, order_users=221, order_conversion_rate_pct=42.34<br>store_id=A, period_month=2026-04: entry_users=906, order_users=339, order_conversion_rate_pct=37.42 |
| promotion_intensity | retail_ops/data/store_a_monthly_metrics.csv | quantitative_evidence | record_matched | records: store_id=A; period_month=2026-03, 2026-04; rows=2 | activity_original_transaction_amount, activity_orders, activity_cost, merchant_subsidy_amount, platform_subsidy_amount, activity_cost_ratio_pct | store_id=A, period_month=2026-03: activity_original_transaction_amount=10035.00, activity_orders=201, activity_cost=3868.16, merchant_subsidy_amount=3727.16, platform_subsidy_amount=141.00, activity_cost_ratio_pct=38.55<br>store_id=A, period_month=2026-04: activity_original_transaction_amount=15006.90, activity_orders=329, activity_cost=6105.86, merchant_subsidy_amount=5947.36, platform_subsidy_amount=158.50, activity_cost_ratio_pct=40.69 |
| transaction_orders | retail_ops/data/store_a_monthly_metrics.csv | quantitative_evidence | record_matched | records: store_id=A; period_month=2026-03, 2026-04; rows=2 | transaction_orders | store_id=A, period_month=2026-03: transaction_orders=207<br>store_id=A, period_month=2026-04: transaction_orders=337 |
| transaction_amount | retail_ops/data/store_a_monthly_metrics.csv | quantitative_evidence | record_matched | records: store_id=A; period_month=2026-03, 2026-04; rows=2 | transaction_amount | store_id=A, period_month=2026-03: transaction_amount=6454.84<br>store_id=A, period_month=2026-04: transaction_amount=9083.72 |


## 5. Competing Hypotheses

Hypothesis statements and statuses are recomputed from the selected evidence. Confidence is unknown where no calibrated estimate is available.

| Hypothesis | Confidence | Status | Weakness |
|---|---:|---|---|
| transaction_amount increased from 6454.84 to 9083.72 (2026-03 to 2026-04). transaction_orders increased from 207 to 337 (2026-03 to 2026-04). | unknown | strong | These are within-store observations in the selected windows. |
| Search exposure and both transaction measures move in the same direction. | unknown | plausible | search_exposure_users increased from 4172 to 7736 (2026-03 to 2026-04); Co-movement alone does not identify a search-only cause. |
| Activity participation remains an alternative explanation to investigate. | unknown | plausible | activity_original_transaction_amount increased from 10035.00 to 15006.90 (2026-03 to 2026-04). activity_orders increased from 201 to 329 (2026-03 to 2026-04). activity_cost increased from 3868.16 to 6105.86 (2026-03 to 2026-04). merchant_subsidy_amount increased from 3727.16 to 5947.36 (2026-03 to 2026-04). platform_subsidy_amount increased from 141.00 to 158.50 (2026-03 to 2026-04). activity_cost_ratio_pct increased from 38.55 to 40.69 (2026-03 to 2026-04); The selected activity metrics do not isolate campaign effects. |

## 6. Critic Findings

- [high] Alternative explanations have not been isolated by the selected observations. Recommendation: Use aligned intervention and comparison evidence to test causal explanations.

## 7. Claim and Definition Check

- Status: pass

### Evidence Checks

| Check | Evidence | Status | Result |
|---|---|---|---|
| search_exposure/search_exposure_users | evidence_search_exposure | supported | search_exposure_users increased from 4172 to 7736 (2026-03 to 2026-04). |
| search_exposure/search_average_rank | evidence_search_exposure | supported | search_average_rank decreased from 20 to 18 (2026-03 to 2026-04). |
| search_exposure/search_entry_users | evidence_search_exposure | supported | search_entry_users increased from 445 to 839 (2026-03 to 2026-04). |
| entry_conversion/exposure_users | evidence_entry_conversion | supported | exposure_users increased from 4663 to 8366 (2026-03 to 2026-04). |
| entry_conversion/entry_users | evidence_entry_conversion | supported | entry_users increased from 522 to 906 (2026-03 to 2026-04). |
| entry_conversion/entry_conversion_rate_pct | evidence_entry_conversion | supported | entry_conversion_rate_pct decreased from 11.19 to 10.83 (2026-03 to 2026-04). |
| order_conversion/entry_users | evidence_order_conversion | supported | entry_users increased from 522 to 906 (2026-03 to 2026-04). |
| order_conversion/order_users | evidence_order_conversion | supported | order_users increased from 221 to 339 (2026-03 to 2026-04). |
| order_conversion/order_conversion_rate_pct | evidence_order_conversion | supported | order_conversion_rate_pct decreased from 42.34 to 37.42 (2026-03 to 2026-04). |
| promotion_intensity/activity_original_transaction_amount | evidence_promotion_intensity | supported | activity_original_transaction_amount increased from 10035.00 to 15006.90 (2026-03 to 2026-04). |
| promotion_intensity/activity_orders | evidence_promotion_intensity | supported | activity_orders increased from 201 to 329 (2026-03 to 2026-04). |
| promotion_intensity/activity_cost | evidence_promotion_intensity | supported | activity_cost increased from 3868.16 to 6105.86 (2026-03 to 2026-04). |
| promotion_intensity/merchant_subsidy_amount | evidence_promotion_intensity | supported | merchant_subsidy_amount increased from 3727.16 to 5947.36 (2026-03 to 2026-04). |
| promotion_intensity/platform_subsidy_amount | evidence_promotion_intensity | supported | platform_subsidy_amount increased from 141.00 to 158.50 (2026-03 to 2026-04). |
| promotion_intensity/activity_cost_ratio_pct | evidence_promotion_intensity | supported | activity_cost_ratio_pct increased from 38.55 to 40.69 (2026-03 to 2026-04). |
| transaction_orders/transaction_orders | evidence_transaction_orders | supported | transaction_orders increased from 207 to 337 (2026-03 to 2026-04). |
| transaction_amount/transaction_amount | evidence_transaction_amount | supported | transaction_amount increased from 6454.84 to 9083.72 (2026-03 to 2026-04). |

- Unsupported claims detected by current rules: none
- Definition conflicts detected by current rules: none

## 8. Final Judgment

transaction_amount increased from 6454.84 to 9083.72 (2026-03 to 2026-04). transaction_orders increased from 207 to 337 (2026-03 to 2026-04). The selected observations do not isolate search exposure as the sole cause.

The judgment is bounded by the cited local evidence and the unresolved requirements recorded above.

## 9. Evidence-Routing Coverage

Packet composition:

- Total packets: 6
- Record matched packets: 6
- Keyword matched packets: 0
- Boundary matched packets: 0
- Fallback packets: 0
- Missing source files: 0

- Routing coverage score: 1.00
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

- total_packets = 6
- record_matched_packets = 6
- keyword_matched_packets = 0
- boundary_matched_packets = 0
- fallback_packets = 0
- missing_source_files = 0

Derived rates and checks:

- record_or_keyword_route_rate = (record_matched_packets + keyword_matched_packets) / total_packets = 1.00
- resolved_or_boundary_route_rate = (record_matched_packets + keyword_matched_packets + boundary_matched_packets) / total_packets = 1.00
- no_missing_source_file_score = 1.00
- no_fallback_score = 1.00

Reading the score:

- A higher value means more requested evidence routes were resolved or explicitly bounded.
- Boundary evidence contributes when it documents a missing requirement.
- Read the score as coverage rather than evidence strength, causal validity, decision quality, or business impact.

## 10. What Cannot Be Concluded

- No comparison group or intervention evidence is supplied by these records.

## 11. Review-State Update

- review_state_id: store_a_march_april_attribution
- status: active
- validity_conditions:
  - Store A, 2026-03-01 through 2026-04-30.
  - Recompute these checks after any source record changes.

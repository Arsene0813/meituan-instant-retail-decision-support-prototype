# Grounded RAC Report

## 1. Direct Answer

The registered local documents cover the promotion-review checklist: activity_orders, activity_cost, merchant_subsidy, platform_subsidy, order_conversion, payment_conversion, sku_margin_structure, competitor_context.

Deterministic local-file review; routing scores summarize route resolution under the current rules.

## 2. Question Type

- Question type: strategic_recommendation
- Domain: retail_operations

## 3. Factor Weights

### 3a. How Factor Weights Are Generated

Factor weights are fixed review-priority buckets assigned by explicit rules in `rac/src/mock_pipeline.py`. They order review attention within the current evidence scope.

| Bucket | Weight | Rule | Factors in This Report |
|---|---:|---|---|
| high | 0.85 | Central to avoiding overconfident or misleading conclusions. | order_conversion, sku_margin_structure |
| medium | 0.72 | Important context but not sufficient on its own. | payment_conversion |
| default | 0.60 | Potentially relevant but requires stronger evidence. | activity_orders, activity_cost, merchant_subsidy, platform_subsidy, competitor_context |

Weighting boundary:

- Use these values only to order review attention within the current evidence scope.

### 3b. Factor Weights Used in This Report

| Decision Factor ID | Weight | Bucket | Evidence Status | Why It Matters |
|---|---:|---|---|---|
| activity_orders | 0.60 | default | partially_supported | Potentially relevant but requires stronger evidence. |
| activity_cost | 0.60 | default | partially_supported | Potentially relevant but requires stronger evidence. |
| merchant_subsidy | 0.60 | default | partially_supported | Potentially relevant but requires stronger evidence. |
| platform_subsidy | 0.60 | default | partially_supported | Potentially relevant but requires stronger evidence. |
| order_conversion | 0.85 | high | partially_supported | Central to avoiding overconfident or misleading conclusions. |
| payment_conversion | 0.72 | medium | partially_supported | Important context but not sufficient on its own. |
| sku_margin_structure | 0.85 | high | partially_supported | Central to avoiding overconfident or misleading conclusions. |
| competitor_context | 0.60 | default | partially_supported | Potentially relevant but requires stronger evidence. |

`partially_supported` means at least one selected observation or registered document/boundary route is available for the factor. Read the evidence checks to distinguish values from context.

## 4. Local Evidence Grounding

- Total evidence packets: 8
- Record matched packets: 0
- Keyword matched packets: 6
- Boundary matched packets: 2
- Fallback packets: 0
- Missing source files: 0

For CSV evidence, `Source Locator` shows the selected record scope and `Selected Values` shows values read from those records. For Markdown evidence, the locator remains a local line-range pointer.
`Decision Factor ID` is an internal RAC review identifier. The field column shows canonical project fields where available and labels unresolved requirements explicitly.

| Decision Factor ID | Source | Evidence Type | Status | Source Locator | Canonical Evidence Fields / Requirement | Selected Values |
|---|---|---|---|---|---|---|
| activity_orders | retail_ops/data/DATA_DICTIONARY.md | definition_evidence | keyword_matched | lines 659-661 | activity_orders | n/a |
| activity_cost | retail_ops/data/DATA_DICTIONARY.md | definition_evidence | keyword_matched | lines 665-667 | activity_cost | n/a |
| merchant_subsidy | retail_ops/data/DATA_DICTIONARY.md | definition_evidence | keyword_matched | lines 671-673 | merchant_subsidy_amount | n/a |
| platform_subsidy | retail_ops/data/DATA_DICTIONARY.md | definition_evidence | keyword_matched | lines 677-679 | platform_subsidy_amount | n/a |
| order_conversion | retail_ops/data/DATA_DICTIONARY.md | definition_evidence | keyword_matched | lines 523-525 | order_conversion_rate_pct, order_users, entry_users | n/a |
| payment_conversion | retail_ops/data/DATA_DICTIONARY.md | definition_evidence | keyword_matched | lines 575-577 | payment_conversion_rate_pct, payment_users, order_users | n/a |
| sku_margin_structure | retail_ops/COMPARABILITY_GATE_V0.md | boundary_evidence | boundary_matched | lines 137-139 | required SKU margin context; unavailable in current evidence | n/a |
| competitor_context | retail_ops/COMPARABILITY_GATE_V0.md | boundary_evidence | boundary_matched | lines 71-73 | required competitor context; unavailable in current evidence | n/a |


## 5. Competing Hypotheses

Hypothesis statements and statuses are recomputed from the selected evidence. Confidence is unknown where no calibrated estimate is available.

| Hypothesis | Confidence | Status | Weakness |
|---|---:|---|---|
| The registered local documents cover the promotion-review checklist: activity_orders, activity_cost, merchant_subsidy, platform_subsidy, order_conversion, payment_conversion, sku_margin_structure, competitor_context. | unknown | strong | No store/window-specific promotion observations were selected. |
| The current document routes alone establish a promotion action. | unknown | unsupported | No store/window-specific promotion observations were selected. |

## 6. Critic Findings

- [medium] The selected document anchors describe review requirements, not measured operating outcomes. Recommendation: Use observations and tests appropriate to the proposed decision.

## 7. Claim and Definition Check

- Status: pass

### Evidence Checks

| Check | Evidence | Status | Result |
|---|---|---|---|
| activity_orders/document | evidence_activity_orders | supported | activity_orders: registered definition_evidence anchor matched. |
| activity_cost/document | evidence_activity_cost | supported | activity_cost: registered definition_evidence anchor matched. |
| merchant_subsidy/document | evidence_merchant_subsidy | supported | merchant_subsidy: registered definition_evidence anchor matched. |
| platform_subsidy/document | evidence_platform_subsidy | supported | platform_subsidy: registered definition_evidence anchor matched. |
| order_conversion/document | evidence_order_conversion | supported | order_conversion: registered definition_evidence anchor matched. |
| payment_conversion/document | evidence_payment_conversion | supported | payment_conversion: registered definition_evidence anchor matched. |
| sku_margin_structure/document | evidence_sku_margin_structure | supported | sku_margin_structure: registered boundary_evidence anchor matched. |
| competitor_context/document | evidence_competitor_context | supported | competitor_context: registered boundary_evidence anchor matched. |

- Unsupported claims detected by current rules: none
- Definition conflicts detected by current rules: none

## 8. Final Judgment

The registered local documents cover the promotion-review checklist: activity_orders, activity_cost, merchant_subsidy, platform_subsidy, order_conversion, payment_conversion, sku_margin_structure, competitor_context.

The judgment is bounded by the cited local evidence and the unresolved requirements recorded above.

## 9. Evidence-Routing Coverage

Packet composition:

- Total packets: 8
- Record matched packets: 0
- Keyword matched packets: 6
- Boundary matched packets: 2
- Fallback packets: 0
- Missing source files: 0

- Routing coverage score: 0.89
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

- total_packets = 8
- record_matched_packets = 0
- keyword_matched_packets = 6
- boundary_matched_packets = 2
- fallback_packets = 0
- missing_source_files = 0

Derived rates and checks:

- record_or_keyword_route_rate = (record_matched_packets + keyword_matched_packets) / total_packets = 0.75
- resolved_or_boundary_route_rate = (record_matched_packets + keyword_matched_packets + boundary_matched_packets) / total_packets = 1.00
- no_missing_source_file_score = 1.00
- no_fallback_score = 1.00

Reading the score:

- A higher value means more requested evidence routes were resolved or explicitly bounded.
- Boundary evidence contributes when it documents a missing requirement.
- Read the score as coverage rather than evidence strength, causal validity, decision quality, or business impact.

## 10. What Cannot Be Concluded

- No store/window-specific promotion observations were selected.

## 11. Review-State Update

- review_state_id: promotion_changes_require_multi_factor_check
- status: active
- validity_conditions:
  - Only the registered definition/design routes are used.

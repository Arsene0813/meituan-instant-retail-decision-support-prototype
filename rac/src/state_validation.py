from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class CognitionStateValidationError(ValueError):
    """Raised when a RAC cognition state violates its declared contract."""


def _path_text(parts: object) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def _require_unique(values: list[str], label: str) -> None:
    duplicates = sorted(
        value
        for value in set(values)
        if values.count(value) > 1
    )
    if duplicates:
        raise CognitionStateValidationError(
            f"Duplicate {label}: {duplicates}"
        )


def validate_cognition_state(
    state: dict[str, Any],
    *,
    root: Path,
) -> None:
    schema_path = root / "rac/schemas/cognition_state.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    schema_errors = sorted(
        validator.iter_errors(state),
        key=lambda error: tuple(
            str(part) for part in error.absolute_path
        ),
    )

    if schema_errors:
        details = [
            f"{_path_text(error.absolute_path)}: {error.message}"
            for error in schema_errors
        ]
        raise CognitionStateValidationError(
            "Cognition-state schema validation failed:\n- "
            + "\n- ".join(details)
        )

    factors = state["factors"]
    factor_ids = [str(row["factor_id"]).strip() for row in factors]

    if any(not factor_id for factor_id in factor_ids):
        raise CognitionStateValidationError(
            "factor_id values must not be empty"
        )

    _require_unique(factor_ids, "factor_id values")
    factor_id_set = set(factor_ids)

    weight_ids = [
        str(row["factor_id"]).strip()
        for row in state["factor_weights"]
    ]
    _require_unique(weight_ids, "factor-weight factor_id values")

    if set(weight_ids) != factor_id_set:
        raise CognitionStateValidationError(
            "factor_weights must contain exactly one record "
            "for every factor_id"
        )

    evidence_ids = [
        str(row["evidence_id"]).strip()
        for row in state["evidence_packets"]
    ]

    if any(not evidence_id for evidence_id in evidence_ids):
        raise CognitionStateValidationError(
            "evidence_id values must not be empty"
        )

    _require_unique(evidence_ids, "evidence_id values")

    expected_evidence_ids = {
        f"evidence_{factor_id}"
        for factor_id in factor_ids
    }

    if set(evidence_ids) != expected_evidence_ids:
        raise CognitionStateValidationError(
            "evidence_packets must contain exactly one packet "
            "for every factor_id"
        )

    hypothesis_ids: list[str] = []

    for hypothesis in state["hypotheses"]:
        hypothesis_id = str(
            hypothesis["hypothesis_id"]
        ).strip()
        claim = str(hypothesis["claim"]).strip()

        if not hypothesis_id:
            raise CognitionStateValidationError(
                "hypothesis_id must not be empty"
            )

        if not claim:
            raise CognitionStateValidationError(
                f"{hypothesis_id} claim must not be empty"
            )

        supporting_factors = [
            str(value).strip()
            for value in hypothesis["supporting_factors"]
        ]

        if not supporting_factors or any(
            not value for value in supporting_factors
        ):
            raise CognitionStateValidationError(
                f"{hypothesis_id} supporting_factors "
                "must not be empty"
            )

        unknown = sorted(
            set(supporting_factors) - factor_id_set
        )

        if unknown:
            raise CognitionStateValidationError(
                f"{hypothesis_id} references unknown factors: "
                f"{unknown}"
            )

        hypothesis_ids.append(hypothesis_id)

    _require_unique(hypothesis_ids, "hypothesis_id values")

    if "evidence_review" in state:
        _validate_evidence_review(state)


def _validate_evidence_review(state: dict[str, Any]) -> None:
    """Check the scope, packet links and decision state of a grounded review.

    Source-value reconciliation is performed by the registered readers and
    positive-fixture quality gate. This check detects altered or stale review
    fields relative to the selected evidence snapshot.
    """
    from copy import deepcopy
    from rac.src.evidence_review import build_review_plan, recompute_review
    from rac.src.local_evidence_resolver import candidate_sources_for_packet

    try:
        plan = build_review_plan(state["question"])
        for key in ("question_type", "domain", "factors"):
            if state[key] != plan[key]:
                raise ValueError(f"{key} differs from the registered question plan")
        for key in ("method", "scope", "confidence_method"):
            if state["evidence_review"][key] != plan["evidence_review"][key]:
                raise ValueError(f"evidence_review.{key} differs from the declared scope")
        packets = state["grounded_evidence"]["resolved_packets"]
        ids = [packet["evidence_id"] for packet in packets]
        if len(ids) != len(set(ids)) or set(ids) != {packet["evidence_id"] for packet in plan["evidence_packets"]}:
            raise ValueError("grounded evidence must resolve exactly one packet per planned factor")
        for packet in packets:
            if packet["evidence_id"] != "evidence_" + packet["factor_id"]:
                raise ValueError("grounded evidence factor and evidence ID disagree")
            candidates = candidate_sources_for_packet(packet, question_type=state["question_type"])
            if packet["source_path"] != candidates[0]["source_path"]:
                raise ValueError("grounded evidence does not use its authoritative registered source")
        expected = deepcopy(state)
        recompute_review(expected)
        for key in ("evidence_review", "evidence_packets", "factor_weights", "hypotheses",
                    "critic_findings", "fact_check", "belief_update"):
            if state[key] != expected[key]:
                raise ValueError(f"{key} does not match the current evidence checks")
        from rac.src.grounded_pipeline import build_grounded_evidence_rows, write_grounded_final_report
        if state["grounded_evidence_rows"] != build_grounded_evidence_rows(state["grounded_evidence"]):
            raise ValueError("rendered evidence rows do not match the selected packets")
        if state["final_report"] != write_grounded_final_report(state):
            raise ValueError("final report does not match the current review state")
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise CognitionStateValidationError(f"Grounded review consistency failed: {exc}") from exc

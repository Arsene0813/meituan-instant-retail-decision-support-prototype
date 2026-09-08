from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rac.src.local_evidence_resolver import resolve_state_evidence
from rac.src.evidence_review import build_review_plan


def load_eval_cases() -> list[dict]:
    path = ROOT / "rac" / "eval" / "rac_eval_cases.json"
    return json.loads(path.read_text(encoding="utf-8"))


def run_case(case: dict) -> dict:
    state = build_review_plan(case["question"])
    resolved = resolve_state_evidence(state, root=ROOT)

    return {
        "case_id": case["case_id"],
        "question": case["question"],
        "question_type": case["question_type"],
        "resolver_result": resolved
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local evidence resolver for RAC outputs.")
    parser.add_argument("--case-id", type=str, default=None)
    parser.add_argument("--all-eval", action="store_true")
    args = parser.parse_args()

    output_dir = ROOT / "rac" / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    cases = load_eval_cases()

    if args.case_id:
        selected = [case for case in cases if case["case_id"] == args.case_id]
        if not selected:
            raise SystemExit(f"Unknown case id: {args.case_id}")

        result = run_case(selected[0])
        output_path = output_dir / f"{args.case_id}_local_evidence.json"
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] Wrote {output_path}")
        return

    if args.all_eval:
        results = [run_case(case) for case in cases]

        all_path = output_dir / "local_evidence_resolver_all_cases.json"
        sample_path = output_dir / "local_evidence_resolver_sample.json"

        all_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        sample_path.write_text(json.dumps(results[0], ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"[OK] Wrote {all_path}")
        print(f"[OK] Wrote {sample_path}")

        for result in results:
            summary = result["resolver_result"]["summary"]
            print(
                "[OK] "
                f"{result['case_id']} "
                f"packets={summary['total_packets']} "
                f"record_matched="
                f"{summary['record_matched_count']} "
                f"keyword_matched="
                f"{summary['keyword_matched_count']} "
                f"boundary_matched="
                f"{summary['boundary_matched_count']} "
                f"fallback={summary['fallback_count']} "
                f"missing={summary['source_missing_count']}"
            )

        return

    raise SystemExit("Provide --case-id or --all-eval")


if __name__ == "__main__":
    main()

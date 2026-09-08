"""Inspect original text, check explicit registration additions, or append them locally."""
from __future__ import annotations

import argparse
from pathlib import Path

from . import registration_workflow as workflow
from .preview import preview_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect-text", help="show source observations without creating trusted registration")
    inspect.add_argument("--input", required=True, type=Path)
    inspect.add_argument("--mapping-version", required=True, choices=sorted(workflow.intake_registry.TEXT_MAPPINGS))
    for name in ("check", "append"):
        command = commands.add_parser(name)
        command.add_argument("--registry", required=True, type=Path)
        command.add_argument("--plan", required=True, type=Path)
        command.add_argument("--input", required=True, type=Path)
        if name == "append":
            command.add_argument("--expected-check-sha256", required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    try:
        if args.command == "inspect-text":
            result = workflow.inspect_text(root, args.input, args.mapping_version)
        elif args.command == "check":
            result = workflow.check(root, args.registry, args.plan, args.input)
        else:
            result = workflow.append(root, args.registry, args.plan, args.input, args.expected_check_sha256)
    except (ValueError, OSError, TypeError, KeyError) as exc:
        parser.exit(2, "Cannot prepare registration: " + str(exc) + "\n")
    print(preview_json(result))
    return 2 if result["status"] == "needs_review" else 0


if __name__ == "__main__":
    raise SystemExit(main())

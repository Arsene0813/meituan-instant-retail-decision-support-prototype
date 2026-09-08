"""Store and inspect reviewed CSV uploads or complete source documents."""
from __future__ import annotations

import argparse
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from .batch_store import read_batch, receive_batch
from .document_intake import receive_document
from .intake_registry import _unique
from .preview import preview_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    receive = commands.add_parser("receive", help="archive bytes and recompute ingestion checks")
    receive.add_argument("--database", required=True, type=Path)
    receive.add_argument("--registry", required=True, type=Path)
    receive.add_argument("--upload-id", required=True)
    receive.add_argument("--input", required=True, type=Path)
    receive.add_argument("--supersedes-batch-id")
    receive.add_argument("--proposals", type=Path)
    document = commands.add_parser("receive-document", help="atomically archive every reviewed text source group")
    document.add_argument("--database", required=True, type=Path)
    document.add_argument("--registry", required=True, type=Path)
    document.add_argument("--document-id", required=True)
    document.add_argument("--input", required=True, type=Path)
    document.add_argument("--supersedes", type=Path, help="JSON map of upload IDs to predecessor batch IDs")
    document.add_argument("--proposals", type=Path)
    show = commands.add_parser("show", help="verify and inspect one stored batch by ID")
    show.add_argument("--database", required=True, type=Path)
    show.add_argument("--batch-id", required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    try:
        database = args.database.expanduser().absolute()
        if root == database.resolve() or root in database.resolve().parents:
            raise ValueError("keep the runtime database outside the repository")
        if args.command == "receive":
            proposals = (json.loads(args.proposals.expanduser().read_bytes(), parse_float=Decimal,
                                    object_pairs_hook=_unique) if args.proposals else None)
            result = receive_batch(root, database, args.registry.expanduser(), args.upload_id,
                                   args.input.expanduser().read_bytes(),
                                   supersedes_batch_id=args.supersedes_batch_id, proposals=proposals)
        elif args.command == "receive-document":
            proposals = (json.loads(args.proposals.expanduser().read_bytes(), parse_float=Decimal,
                                    object_pairs_hook=_unique) if args.proposals else None)
            supersedes = (json.loads(args.supersedes.expanduser().read_bytes(), object_pairs_hook=_unique)
                          if args.supersedes else None)
            result = receive_document(root, database, args.registry.expanduser(), args.document_id,
                                      args.input.expanduser().read_bytes(), supersedes=supersedes, proposals=proposals)
        else:
            result = read_batch(database, args.batch_id)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"Cannot process batch: {exc}\n")
    print(preview_json(result))
    return 0 if result["status"] == "validated" else 2


if __name__ == "__main__":
    raise SystemExit(main())

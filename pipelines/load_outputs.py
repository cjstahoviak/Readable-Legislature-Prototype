"""Load scored-bill JSON files from out/ into PostgreSQL.

    python -m pipelines.load_outputs [--dir out]

Backfills the database from the prototype's file outputs (the ten
golden bills). Those files carry scores and target groups but no bill
text and no summaries, so loaded bills land as llm_status='partial':
once ingestion fetches their text, the scoring job fills the summary
without re-scoring the dimensions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from . import db
from .congress import BILL_TYPE_SLUG
from .prompts import PROMPT_VERSION


def parse_bill_id(bill_id: str) -> tuple[int, str, int]:
    """Split '119-hr-2138' into (119, 'hr', 2138)."""
    try:
        congress, bill_type, number = bill_id.split("-")
        bill_type = bill_type.lower()
        if bill_type not in BILL_TYPE_SLUG:
            raise ValueError(f"unknown bill type '{bill_type}'")
        return int(congress), bill_type, int(number)
    except ValueError as exc:
        raise ValueError(f"malformed bill_id '{bill_id}': {exc}") from exc


def load_payload(conn, payload: dict[str, Any]) -> str:
    """Insert one output file's contents; returns the bill label."""
    congress, bill_type, number = parse_bill_id(payload["bill_id"])
    bill_meta = payload.get("bill") or {}
    summary = payload.get("summary") or {}

    with conn.transaction():
        fields: dict[str, Any] = {
            "title": bill_meta.get("title"),
            "source_url": bill_meta.get("url"),
            "text_version_type": bill_meta.get("text_version_type"),
            "text_source_url": bill_meta.get("text_source_url"),
            "llm_model": payload.get("model"),
            "llm_prompt_version": payload.get("prompt_version", PROMPT_VERSION),
            "llm_samples": payload.get("samples"),
            "llm_processed_at": payload.get("fetched_at"),
            # Text and (usually) summaries are absent from file outputs;
            # 'partial' hands the gap to the scoring job once ingestion
            # has fetched the bill text.
            "llm_status": "partial",
        }
        if summary.get("tldr") and summary.get("overview"):
            fields["summary_tldr"] = summary["tldr"]
            fields["summary_overview"] = summary["overview"]

        bill_id = db.upsert_bill(conn, congress, bill_type, number, **fields)
        db.replace_scores(conn, bill_id, payload.get("scores") or {})
        db.replace_target_groups(conn, bill_id, payload.get("target_groups") or [])
    return f"{congress}-{bill_type}-{number}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Load out/*.json scoring outputs into PostgreSQL."
    )
    parser.add_argument("--dir", default="out")
    args = parser.parse_args(argv)
    load_dotenv()

    paths = sorted(Path(args.dir).glob("*.json"))
    if not paths:
        print(f"No JSON files in {args.dir}/.", file=sys.stderr)
        return 1

    failed = 0
    with db.connect() as conn:
        for path in paths:
            try:
                with path.open(encoding="utf-8") as fh:
                    payload = json.load(fh)
                label = load_payload(conn, payload)
                print(f"  loaded {label} from {path.name}")
            except Exception as exc:
                failed += 1
                print(f"  FAILED {path.name}: {exc}", file=sys.stderr)
    print(f"\n{len(paths) - failed}/{len(paths)} files loaded.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Score bills from the database that need LLM work.

    python -m pipelines.score_pending --max-bills 25 [--samples 3]

Selects bills whose text is present and that are pending, partially
scored, stale (text hash changed since scoring), or scored under an
older prompt version — active bills first, capped by --max-bills so a
prompt bump never triggers an unbounded re-score. For each bill it runs
only the missing work: unscored dimensions, absent target groups, and
absent summaries. Model changes alone do NOT requeue bills — the tiered
backfill deliberately scores different bills with different models;
force a migration by bumping PROMPT_VERSION in pipelines/prompts.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

from . import db
from .prompts import PROMPT_VERSION, build_rubric_text, build_system_blocks
from .scoring import DEFAULT_MODEL, resolve_api_key, score_all
from .taxonomy import load_taxonomy, select_values

_CANDIDATES_SQL = """
SELECT id, congress, bill_type, bill_number, title, text_hash,
       scored_text_hash, llm_status, llm_prompt_version,
       summary_tldr, summary_overview
FROM bills
WHERE text_hash IS NOT NULL
  AND (llm_status IN ('pending', 'partial')
       OR scored_text_hash IS DISTINCT FROM text_hash
       OR llm_prompt_version IS DISTINCT FROM %(prompt_version)s)
ORDER BY latest_action_date DESC NULLS LAST, id
LIMIT %(max_bills)s
"""


def missing_dimensions(
    taxonomy: dict[str, Any],
    coverage: dict[str, set[str]],
    include_complement: bool = False,
) -> list[str]:
    """Dimension ids whose stored value rows are incomplete."""
    missing = []
    for dim in taxonomy["dimensions"]:
        expected = {v["id"] for v in select_values(dim, include_complement)}
        if expected - coverage.get(dim["id"], set()):
            missing.append(dim["id"])
    return missing


def needs_full_run(bill: dict[str, Any]) -> bool:
    """Whether prior scores are absent or invalidated entirely."""
    return (
        bill["llm_status"] == "pending"
        or bill["scored_text_hash"] != bill["text_hash"]
        or bill["llm_prompt_version"] != PROMPT_VERSION
    )


def resolve_status(
    dims_missing_after: list[str],
    total_dims: int,
    groups_ok: bool,
    summary_ok: bool,
) -> str:
    """Final llm_status for a bill after a scoring pass.

    complete: every piece present; failed: nothing present at all;
    partial: anything in between (retried by the next run).
    """
    if not dims_missing_after and groups_ok and summary_ok:
        return "complete"
    nothing = (
        len(dims_missing_after) == total_dims
        and not groups_ok
        and not summary_ok
    )
    return "failed" if nothing else "partial"


def score_one_bill(
    conn,
    client: Anthropic,
    taxonomy: dict[str, Any],
    rubric_text: str,
    bill: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, int]:
    """Run the missing LLM work for one bill and persist it."""
    bill_id = bill["id"]
    label = f"{bill['congress']}-{bill['bill_type']}-{bill['bill_number']}"

    full = needs_full_run(bill)
    if full:
        dims_to_run: list[str] | None = None
        need_groups = True
        need_summary = True
    else:
        coverage = db.score_coverage(conn, bill_id)
        dims_to_run = missing_dimensions(taxonomy, coverage)
        need_groups = db.target_group_count(conn, bill_id) == 0
        need_summary = not (bill["summary_tldr"] and bill["summary_overview"])

    if dims_to_run == [] and not need_groups and not need_summary:
        # Nothing actually missing (e.g. only the prompt-version marker
        # was stale on an otherwise-complete bill).
        with conn.transaction():
            db.upsert_bill(
                conn,
                bill["congress"],
                bill["bill_type"],
                bill["bill_number"],
                llm_status="complete",
                llm_prompt_version=PROMPT_VERSION,
            )
        return {"input": 0, "output": 0, "cache_read": 0}

    text = db.get_bill_text(conn, bill_id, bill["text_hash"])
    if text is None:
        print(f"  {label}: text row missing; skipping", file=sys.stderr)
        return {"input": 0, "output": 0, "cache_read": 0}

    n_dims = "all" if dims_to_run is None else len(dims_to_run)
    print(
        f"  {label}: scoring (dims={n_dims}, groups={need_groups}, "
        f"summary={need_summary}, samples={args.samples})"
    )
    system_blocks = build_system_blocks(rubric_text, text)
    results, target_groups, summary, validation, totals = score_all(
        client,
        args.model,
        system_blocks,
        taxonomy,
        include_complement=False,
        use_thinking=not args.no_thinking,
        samples=args.samples,
        concurrency=args.concurrency,
        extract_groups=need_groups,
        dimensions=dims_to_run,
        generate_summary=need_summary,
    )

    problems = {
        section: checks
        for section, checks in validation.items()
        if any(checks.values())
    }
    if problems:
        print(f"  {label}: validation problems: {problems}", file=sys.stderr)

    with conn.transaction():
        ran_dims = list(results) if dims_to_run is None else dims_to_run
        db.replace_scores(conn, bill_id, results, dimensions=ran_dims)
        groups_failed = need_groups and target_groups is None
        if need_groups and target_groups is not None:
            db.replace_target_groups(conn, bill_id, target_groups)

        summary_fields: dict[str, Any] = {}
        if need_summary and summary is not None:
            summary_fields = {
                "summary_tldr": summary["tldr"],
                "summary_overview": summary["overview"],
            }

        coverage_after = db.score_coverage(conn, bill_id)
        dims_missing_after = missing_dimensions(taxonomy, coverage_after)
        groups_ok = not groups_failed
        summary_ok = bool(summary_fields) or not need_summary
        status = resolve_status(
            dims_missing_after,
            len(taxonomy["dimensions"]),
            groups_ok,
            summary_ok,
        )

        db.upsert_bill(
            conn,
            bill["congress"],
            bill["bill_type"],
            bill["bill_number"],
            llm_status=status,
            llm_model=args.model,
            llm_prompt_version=PROMPT_VERSION,
            llm_samples=args.samples,
            llm_processed_at=dt.datetime.now(dt.timezone.utc),
            scored_text_hash=bill["text_hash"],
            **summary_fields,
        )
    print(f"  {label}: -> {status}")
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score DB bills that are pending, partial, or stale."
    )
    parser.add_argument(
        "--max-bills",
        type=int,
        default=10,
        help="Budget cap: bills to process this run (default 10).",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--no-thinking", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the bills that would be scored, then exit.",
    )
    args = parser.parse_args(argv)
    load_dotenv()

    anthropic_key = resolve_api_key()
    if not anthropic_key and not args.dry_run:
        print(
            "Set PIPELINE_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY).",
            file=sys.stderr,
        )
        return 1

    taxonomy = load_taxonomy()
    rubric_text = build_rubric_text(taxonomy["scoring"])

    with db.connect() as conn:
        candidates = conn.execute(
            _CANDIDATES_SQL,
            {"prompt_version": PROMPT_VERSION, "max_bills": args.max_bills},
        ).fetchall()
        if not candidates:
            print("Nothing to score.")
            return 0
        print(f"{len(candidates)} bill(s) need work:")
        if args.dry_run:
            for b in candidates:
                print(
                    f"  {b['congress']}-{b['bill_type']}-{b['bill_number']} "
                    f"[{b['llm_status']}] {b['title'] or ''}"
                )
            return 0

        client = Anthropic(api_key=anthropic_key, max_retries=6)
        grand = {"input": 0, "output": 0, "cache_read": 0}
        failed = 0
        for bill in candidates:
            try:
                totals = score_one_bill(
                    conn, client, taxonomy, rubric_text, bill, args
                )
                for key in grand:
                    grand[key] += totals[key]
            except Exception as exc:  # keep the batch alive
                failed += 1
                print(
                    f"  ERROR {bill['congress']}-{bill['bill_type']}-"
                    f"{bill['bill_number']}: {exc}",
                    file=sys.stderr,
                )
        print(
            f"\nToken totals: input={grand['input']} "
            f"(cache_read={grand['cache_read']}) output={grand['output']}"
        )
        return 1 if failed == len(candidates) else 0


if __name__ == "__main__":
    raise SystemExit(main())

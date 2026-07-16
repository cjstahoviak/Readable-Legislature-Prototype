"""Score congressional bills against the demographic taxonomy (CLI).

Fetches each bill and its text from the Congress.gov API, runs the
per-dimension scoring calls plus target-group extraction (see
``pipelines.scoring``), validates everything against ``taxonomy.yaml``,
and writes one JSON file per bill to ``out/`` for manual review.

Run as ``python -m pipelines.score_bill``. ``--bills hr-2138,s-129``
scores several bills in one run; ``--samples N`` enables resampling.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

from .congress import (
    BILL_TYPE_SLUG,
    DEFAULT_MAX_CHARS,
    bill_web_url,
    fetch_bill,
    fetch_bill_text,
    ordinal_suffix,
)
from .prompts import PROMPT_VERSION, build_rubric_text, build_system_blocks
from .scoring import DEFAULT_MODEL, resolve_api_key, score_all
from .taxonomy import load_taxonomy

OUT_DIR = Path(__file__).resolve().parent.parent / "out"


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def write_results(out_path: Path, payload: dict[str, Any]) -> None:
    """Write the results JSON, indented for human review."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def print_summary(
    results: dict[str, dict[str, Any]], taxonomy: dict[str, Any]
) -> None:
    """Print a compact dimension/value score table (high scores first)."""
    label_by_dim = {d["id"]: d["label"] for d in taxonomy["dimensions"]}
    print("\nScore summary (0=low, 1=moderate, 2=high):")
    for dim_id, scores in results.items():
        print(f"\n  {label_by_dim[dim_id]} [{dim_id}]")
        if not scores:
            print("    <no scores>")
            continue
        ordered = sorted(
            scores.items(), key=lambda kv: -_score_of(kv[1])
        )
        for value_id, entry in ordered:
            line = f"    {_score_of(entry)}  {value_id}"
            if isinstance(entry, dict) and "agreement" in entry:
                line += f"  ({int(round(entry['agreement'] * 100))}%)"
            print(line)


def _score_of(entry: Any) -> int:
    """Best-effort score for sorting/printing (-1 if malformed)."""
    if isinstance(entry, dict) and isinstance(entry.get("score"), int):
        return entry["score"]
    return -1


def print_target_groups(groups: list[dict[str, Any]] | None) -> None:
    """Print a one-line-per-group summary of the bill's targets."""
    print("\nTarget groups (who the bill explicitly targets):")
    if not groups:
        print("  <none>")
        return
    for group in groups:
        conds = " + ".join(
            f"{c.get('dimension')}={c.get('value')}"
            for c in group.get("conditions", [])
        )
        extra = "; ".join(group.get("other_criteria") or [])
        if not conds:
            conds = "<no taxonomy match>" if extra else "<everyone>"
        line = f"  {conds}"
        if extra:
            line += f"  ({extra})"
        if "agreement" in group:
            line += f"  [{int(round(group['agreement'] * 100))}% of runs]"
        print(line)


def print_validation(validation: dict[str, dict[str, list[str]]]) -> None:
    """Surface any sections whose output failed validation."""
    problems = {
        section: {name: check for name, check in checks.items() if check}
        for section, checks in validation.items()
        if any(checks.values())
    }
    if not problems:
        print("\nValidation: all sections OK.")
        return
    print("\nValidation PROBLEMS:")
    for section, checks in problems.items():
        print(f"  {section}: {checks}")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """Score one or more bills and write one JSON file per bill."""
    args = _parse_args(argv)
    load_dotenv()

    congress_key = os.environ.get("CONGRESS_API_KEY")
    anthropic_key = resolve_api_key()
    missing = [
        name
        for name, val in (
            ("CONGRESS_API_KEY", congress_key),
            ("PIPELINE_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY)", anthropic_key),
        )
        if not val
    ]
    if missing:
        print(
            f"Missing env var(s): {', '.join(missing)}. Copy .env.example "
            "to .env and fill them in.",
            file=sys.stderr,
        )
        return 1

    taxonomy = load_taxonomy()
    rubric_text = build_rubric_text(taxonomy["scoring"])
    client = Anthropic(api_key=anthropic_key, max_retries=6)

    bills = _parse_bill_list(args)
    statuses: list[str] = []
    grand = {"input": 0, "output": 0, "cache_read": 0}
    failed = 0
    for bill_type, number in bills:
        bill_id = f"{args.congress}-{bill_type}-{number}"
        try:
            totals = run_one_bill(
                client,
                taxonomy,
                rubric_text,
                congress_key,
                bill_type,
                number,
                args,
            )
            for key in grand:
                grand[key] += totals[key]
            statuses.append(f"  ok      {bill_id}")
        except Exception as exc:  # one bad bill must not sink the batch
            failed += 1
            print(f"ERROR: {bill_id}: {exc}", file=sys.stderr)
            statuses.append(f"  FAILED  {bill_id}: {exc}")

    if len(bills) > 1:
        print("\n" + "=" * 60)
        print("Batch summary:")
        for line in statuses:
            print(line)
        print(
            f"Grand totals: input={grand['input']} "
            f"(cache_read={grand['cache_read']}) output={grand['output']}"
        )
    return 1 if failed == len(bills) else 0


def run_one_bill(
    client: Anthropic,
    taxonomy: dict[str, Any],
    rubric_text: str,
    congress_key: str,
    bill_type: str,
    number: int,
    args: argparse.Namespace,
) -> dict[str, int]:
    """Fetch, score, and write one bill; returns its token totals."""
    ordinal = f"{args.congress}{ordinal_suffix(args.congress)}"
    print(f"\nFetching {bill_type.upper()} {number} ({ordinal} Congress)...")
    bill = fetch_bill(args.congress, bill_type, number, congress_key)
    bill_text, source = fetch_bill_text(
        args.congress,
        bill_type,
        number,
        congress_key,
        max_chars=args.max_chars,
    )
    print(
        f"  Title: {bill.get('title', '<no title>')}\n"
        f"  Text version: {source['type']} ({len(bill_text)} chars)"
    )

    system_blocks = build_system_blocks(rubric_text, bill_text)
    print(
        f"  Scoring {len(taxonomy['dimensions'])} dimensions "
        f"x {args.samples} sample(s)..."
    )
    results, target_groups, summary, validation, totals = score_all(
        client,
        args.model,
        system_blocks,
        taxonomy,
        include_complement=args.include_complement,
        use_thinking=not args.no_thinking,
        samples=args.samples,
        concurrency=args.concurrency,
        extract_groups=not args.no_target_groups,
        generate_summary=not args.no_summary,
    )

    bill_id = f"{args.congress}-{bill_type}-{number}"
    payload = {
        "bill_id": bill_id,
        "bill": {
            "congress": args.congress,
            "type": bill.get("type", bill_type.upper()),
            "number": number,
            "title": bill.get("title"),
            "url": bill_web_url(args.congress, bill_type, number),
            "text_version_type": source["type"],
            "text_source_url": source["url"],
        },
        "fetched_at": _utc_now_iso(),
        "model": args.model,
        "prompt_version": PROMPT_VERSION,
        "samples": args.samples,
        "summary": summary,
        "scores": results,
        "target_groups": target_groups or [],
        "validation": validation,
    }

    out_path = OUT_DIR / f"{bill_id}.json"
    write_results(out_path, payload)

    if summary:
        print(f"\nTLDR: {summary['tldr']}")
    print_summary(results, taxonomy)
    if not args.no_target_groups:
        print_target_groups(target_groups)
    print_validation(validation)
    print(
        f"\nToken totals: input={totals['input']} "
        f"(cache_read={totals['cache_read']}) output={totals['output']}"
    )
    print(f"Wrote {out_path}")
    return totals


def _parse_bill_list(args: argparse.Namespace) -> list[tuple[str, int]]:
    """Bills to score: ``--bills hr-2138,s-129`` or the single-bill args."""
    if not args.bills:
        return [(args.bill_type, args.number)]
    bills: list[tuple[str, int]] = []
    for item in args.bills.split(","):
        bill_type, _, number = item.strip().lower().rpartition("-")
        if bill_type not in BILL_TYPE_SLUG or not number.isdigit():
            raise SystemExit(
                f"--bills entries must look like 'hr-2138'; got '{item}'"
            )
        bills.append((bill_type, int(number)))
    return bills


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Score one congressional bill against the demographic taxonomy."
        )
    )
    parser.add_argument("--congress", type=int, default=119)
    parser.add_argument("--bill-type", default="hr")
    parser.add_argument("--number", type=int, default=2138)
    parser.add_argument(
        "--bills",
        default=None,
        help=(
            "Comma-separated bills like 'hr-2138,s-129'; overrides "
            "--bill-type/--number."
        ),
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--samples",
        type=int,
        default=1,
        help=(
            "Runs per call; N>1 aggregates by majority vote and stores "
            "an agreement ratio. (Opus 4.8 removed the temperature "
            "parameter, so variance is the model's natural run-to-run "
            "spread.)"
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Parallel API calls per bill.",
    )
    parser.add_argument(
        "--include-complement",
        action="store_true",
        help="Also score score_complement:false (negative-space) values.",
    )
    parser.add_argument(
        "--no-thinking",
        action="store_true",
        help="Disable adaptive extended thinking.",
    )
    parser.add_argument(
        "--no-target-groups",
        action="store_true",
        help="Skip the extra target-group extraction call.",
    )
    parser.add_argument(
        "--no-summary",
        action="store_true",
        help="Skip the plain-language summary call.",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help="Truncate bill text beyond this many characters.",
    )
    args = parser.parse_args(argv)
    args.bill_type = args.bill_type.lower()
    return args


def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string (``...Z``)."""
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())

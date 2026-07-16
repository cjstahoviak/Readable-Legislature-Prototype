"""Evaluate a candidate model/config against the golden outputs.

    python -m pipelines.eval_models --model claude-haiku-4-5 --samples 1

Re-scores the bills in out/ with the candidate configuration — fetching
the exact text version each golden was scored on — and reports how the
candidate's scores diverge from the goldens. This is what decides
whether a cheaper tier is good enough for backfill (and, on prompt
changes, whether behavior drifted).

Metrics per bill and aggregated:
  * exact / off-by-1 / off-by-2 score agreement over all values
    (off-by-2 means a 0<->2 flip — the bad kind)
  * nonzero precision/recall: does the candidate flag the same values
    as relevant that the golden does?
  * target-group condition-set overlap (Jaccard)

Writes a JSON report to out/eval/ and prints a summary table.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests
from anthropic import Anthropic
from dotenv import load_dotenv

from .congress import REQUEST_TIMEOUT, _html_to_text, _xml_to_text
from .prompts import PROMPT_VERSION, build_rubric_text, build_system_blocks
from .scoring import score_all
from .taxonomy import load_taxonomy

OUT_DIR = Path(__file__).resolve().parent.parent / "out"


# --------------------------------------------------------------------------
# Comparison (pure)
# --------------------------------------------------------------------------
def compare_scores(
    golden: dict[str, dict[str, Any]], candidate: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Compare two score maps over the values present in the golden."""
    exact = off1 = off2 = missing = 0
    tp = fp = fn = 0
    disagreements: list[dict[str, Any]] = []
    for dim_id, gvalues in golden.items():
        cvalues = candidate.get(dim_id) or {}
        for value_id, gentry in gvalues.items():
            g = gentry.get("score")
            centry = cvalues.get(value_id)
            c = centry.get("score") if isinstance(centry, dict) else None
            if c is None:
                missing += 1
                continue
            diff = abs(g - c)
            if diff == 0:
                exact += 1
            elif diff == 1:
                off1 += 1
            else:
                off2 += 1
            if diff:
                disagreements.append(
                    {"dimension": dim_id, "value": value_id, "golden": g, "candidate": c}
                )
            if g > 0 and c > 0:
                tp += 1
            elif g == 0 and c > 0:
                fp += 1
            elif g > 0 and c == 0:
                fn += 1
    compared = exact + off1 + off2
    return {
        "compared": compared,
        "missing": missing,
        "exact": exact,
        "off_by_1": off1,
        "off_by_2": off2,
        "exact_rate": round(exact / compared, 3) if compared else None,
        "nonzero_precision": round(tp / (tp + fp), 3) if (tp + fp) else None,
        "nonzero_recall": round(tp / (tp + fn), 3) if (tp + fn) else None,
        "disagreements": disagreements,
    }


def _condition_sets(groups: list[dict[str, Any]]) -> set[frozenset]:
    return {
        frozenset(
            (c.get("dimension"), c.get("value"))
            for c in g.get("conditions", [])
        )
        for g in groups
    }


def compare_groups(
    golden: list[dict[str, Any]], candidate: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Jaccard overlap of target groups keyed by their condition sets."""
    gsets = _condition_sets(golden)
    csets = _condition_sets(candidate or [])
    union = gsets | csets
    jaccard = round(len(gsets & csets) / len(union), 3) if union else 1.0
    return {
        "golden_groups": len(gsets),
        "candidate_groups": len(csets),
        "jaccard": jaccard,
    }


def aggregate(per_bill: list[dict[str, Any]]) -> dict[str, Any]:
    """Corpus-level rollup of the per-bill comparisons."""
    totals = {"compared": 0, "missing": 0, "exact": 0, "off_by_1": 0, "off_by_2": 0}
    jaccards = []
    for b in per_bill:
        for key in totals:
            totals[key] += b["scores"][key]
        jaccards.append(b["groups"]["jaccard"])
    precisions = [
        b["scores"]["nonzero_precision"]
        for b in per_bill
        if b["scores"]["nonzero_precision"] is not None
    ]
    recalls = [
        b["scores"]["nonzero_recall"]
        for b in per_bill
        if b["scores"]["nonzero_recall"] is not None
    ]
    compared = totals["exact"] + totals["off_by_1"] + totals["off_by_2"]
    return {
        **totals,
        "exact_rate": round(totals["exact"] / compared, 3) if compared else None,
        "off_by_2_rate": round(totals["off_by_2"] / compared, 3) if compared else None,
        "mean_nonzero_precision": round(sum(precisions) / len(precisions), 3)
        if precisions
        else None,
        "mean_nonzero_recall": round(sum(recalls) / len(recalls), 3)
        if recalls
        else None,
        "mean_group_jaccard": round(sum(jaccards) / len(jaccards), 3)
        if jaccards
        else None,
    }


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
def fetch_text_from_url(url: str) -> str:
    """Fetch the exact text version a golden was scored on."""
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    if url.lower().endswith(".xml"):
        return _xml_to_text(resp.text).strip()
    return _html_to_text(resp.text).strip()


def evaluate_bill(
    client: Anthropic,
    taxonomy: dict[str, Any],
    rubric_text: str,
    golden: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    text = fetch_text_from_url(golden["bill"]["text_source_url"])
    system_blocks = build_system_blocks(rubric_text, text)
    results, target_groups, _, validation, totals = score_all(
        client,
        args.model,
        system_blocks,
        taxonomy,
        include_complement=False,
        use_thinking=not args.no_thinking,
        samples=args.samples,
        concurrency=args.concurrency,
        extract_groups=True,
        generate_summary=False,  # goldens carry no summaries to compare
    )
    # Compare only dimensions the golden actually has data for (a golden
    # with a validation hole, e.g. a refused dimension, is skipped there).
    golden_scores = {
        dim: values for dim, values in golden["scores"].items() if values
    }
    return {
        "bill_id": golden["bill_id"],
        "title": golden["bill"].get("title"),
        "scores": compare_scores(golden_scores, results),
        "groups": compare_groups(golden.get("target_groups") or [], target_groups),
        "candidate_validation_problems": {
            k: v for k, v in validation.items() if any(v.values())
        },
        "tokens": totals,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate a candidate model against the golden outputs."
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--no-thinking", action="store_true")
    parser.add_argument(
        "--goldens-dir", default=str(OUT_DIR), help="Directory of golden JSONs."
    )
    parser.add_argument(
        "--bills",
        default=None,
        help="Comma-separated bill ids (e.g. 119-hr-2138) to limit the eval.",
    )
    args = parser.parse_args(argv)
    load_dotenv()

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 1

    wanted = set(args.bills.split(",")) if args.bills else None
    goldens = []
    for path in sorted(Path(args.goldens_dir).glob("*.json")):
        with path.open(encoding="utf-8") as fh:
            payload = json.load(fh)
        if "bill_id" in payload and (not wanted or payload["bill_id"] in wanted):
            goldens.append(payload)
    if not goldens:
        print("No golden files found.", file=sys.stderr)
        return 1

    taxonomy = load_taxonomy()
    rubric_text = build_rubric_text(taxonomy["scoring"])
    client = Anthropic(api_key=anthropic_key, max_retries=6)

    per_bill = []
    for golden in goldens:
        print(f"Evaluating {golden['bill_id']} with {args.model}...")
        try:
            per_bill.append(
                evaluate_bill(client, taxonomy, rubric_text, golden, args)
            )
        except Exception as exc:
            print(f"  FAILED {golden['bill_id']}: {exc}", file=sys.stderr)

    if not per_bill:
        return 1

    summary = aggregate(per_bill)
    report = {
        "model": args.model,
        "samples": args.samples,
        "thinking": not args.no_thinking,
        "prompt_version": PROMPT_VERSION,
        "run_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "golden_bills": [b["bill_id"] for b in per_bill],
        "aggregate": summary,
        "per_bill": per_bill,
    }
    eval_dir = Path(args.goldens_dir) / "eval"
    eval_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    out_path = eval_dir / f"{args.model}_s{args.samples}_{stamp}.json"
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(f"\n=== {args.model} vs goldens ({len(per_bill)} bills) ===")
    print(f"  values compared: {summary['compared']} (missing {summary['missing']})")
    print(f"  exact:           {summary['exact']} ({summary['exact_rate']})")
    print(f"  off by 1:        {summary['off_by_1']}")
    print(f"  off by 2:        {summary['off_by_2']} ({summary['off_by_2_rate']})")
    print(f"  nonzero P/R:     {summary['mean_nonzero_precision']} / {summary['mean_nonzero_recall']}")
    print(f"  group jaccard:   {summary['mean_group_jaccard']}")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

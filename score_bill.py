"""Score a single congressional bill against the demographic taxonomy.

Throwaway prototype (see PROTOTYPE_HANDOFF.md). Fetches a bill and its
text from the Congress.gov API, makes one Claude call per taxonomy
dimension (all of that dimension's values scored together), validates
the result against ``taxonomy.yaml``, and writes one JSON file to
``out/`` for manual review.

Stages are split into small, independently testable functions:
fetch -> build prompt -> call -> validate -> write.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import requests
import yaml
from anthropic import Anthropic
from dotenv import load_dotenv

CONGRESS_API_BASE = "https://api.congress.gov/v3"
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_MAX_TOKENS = 16000
DEFAULT_MAX_CHARS = 600_000  # safety cap for pathologically long bills
REQUEST_TIMEOUT = 30  # seconds

TAXONOMY_PATH = Path(__file__).resolve().parent / "taxonomy.yaml"
OUT_DIR = Path(__file__).resolve().parent / "out"

# congress.gov URL slugs for each bill type.
BILL_TYPE_SLUG = {
    "hr": "house-bill",
    "s": "senate-bill",
    "hjres": "house-joint-resolution",
    "sjres": "senate-joint-resolution",
    "hconres": "house-concurrent-resolution",
    "sconres": "senate-concurrent-resolution",
    "hres": "house-resolution",
    "sres": "senate-resolution",
}


# --------------------------------------------------------------------------
# Taxonomy
# --------------------------------------------------------------------------
def load_taxonomy(path: Path = TAXONOMY_PATH) -> dict[str, Any]:
    """Load the taxonomy YAML (scoring rubric + dimensions)."""
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def select_values(
    dimension: dict[str, Any], include_complement: bool
) -> list[dict[str, Any]]:
    """Return the values to score, honoring ``score_complement``.

    Negative-space values (tagged ``score_complement: false``, e.g.
    ``non_veteran``) are skipped by default to save tokens.
    """
    values = dimension["values"]
    if include_complement:
        return values
    return [v for v in values if v.get("score_complement", True) is not False]


# --------------------------------------------------------------------------
# Congress.gov API
# --------------------------------------------------------------------------
def fetch_bill(
    congress: int, bill_type: str, number: int, api_key: str
) -> dict[str, Any]:
    """Fetch a single bill's metadata from the Congress.gov API."""
    url = f"{CONGRESS_API_BASE}/bill/{congress}/{bill_type}/{number}"
    resp = requests.get(
        url,
        params={"api_key": api_key, "format": "json"},
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_congress_status(resp)
    return resp.json()["bill"]


def fetch_bill_text(
    congress: int,
    bill_type: str,
    number: int,
    api_key: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[str, dict[str, str]]:
    """Fetch the latest bill text, stripped to plain text.

    Returns ``(text, source)`` where ``source`` describes the version
    used (its ``type`` and the URL the text came from).
    """
    url = f"{CONGRESS_API_BASE}/bill/{congress}/{bill_type}/{number}/text"
    resp = requests.get(
        url,
        params={"api_key": api_key, "format": "json"},
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_congress_status(resp)

    versions = resp.json().get("textVersions", [])
    if not versions:
        raise RuntimeError("No text versions available for this bill.")

    version = max(versions, key=lambda v: v.get("date") or "")
    fmt = _pick_format(version.get("formats", []))
    if fmt is None:
        raise RuntimeError(
            "No parseable (Formatted Text / XML) format for the bill."
        )

    doc = requests.get(fmt["url"], timeout=REQUEST_TIMEOUT)
    doc.raise_for_status()
    if fmt["type"] == "Formatted XML":
        text = _xml_to_text(doc.text)
    else:
        text = _html_to_text(doc.text)

    text = text.strip()
    if max_chars and len(text) > max_chars:
        print(
            f"  WARNING: bill text {len(text)} chars exceeds cap "
            f"{max_chars}; truncating.",
            file=sys.stderr,
        )
        text = text[:max_chars]

    return text, {"type": version.get("type", "unknown"), "url": fmt["url"]}


def _raise_for_congress_status(resp: requests.Response) -> None:
    """Raise an informative error if a Congress.gov call failed."""
    if resp.status_code != 200:
        raise RuntimeError(
            f"Congress.gov API error {resp.status_code}: {resp.text[:300]}"
        )


def _pick_format(formats: list[dict[str, str]]) -> dict[str, str] | None:
    """Prefer Formatted Text, then Formatted XML; ignore PDF."""
    by_type = {f.get("type"): f for f in formats}
    for preferred in ("Formatted Text", "Formatted XML"):
        if preferred in by_type:
            return by_type[preferred]
    return None


def _html_to_text(raw: str) -> str:
    """Strip a congress.gov HTML bill page down to plain text."""
    match = re.search(r"<pre[^>]*>(.*?)</pre>", raw, re.IGNORECASE | re.DOTALL)
    body = match.group(1) if match else raw
    body = re.sub(r"(?is)<(script|style).*?</\1>", "", body)
    body = re.sub(r"<[^>]+>", "", body)
    return _normalize(html.unescape(body))


def _xml_to_text(raw: str) -> str:
    """Extract readable text from a bill XML document."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return _html_to_text(raw)
    return _normalize(" ".join(t for t in root.itertext()))


def _normalize(text: str) -> str:
    """Trim trailing spaces and collapse runs of blank lines."""
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)


# --------------------------------------------------------------------------
# Prompt + schema construction
# --------------------------------------------------------------------------
def build_rubric_text(scoring: dict[str, Any]) -> str:
    """Render the 0/1/2 scoring contract for the system prompt."""
    lines = [
        "You assess how RELEVANT a U.S. congressional bill is to "
        "specific demographic groups.",
        "",
        "Score each group on this scale:",
    ]
    for level in scoring["scale"]:
        definition = " ".join(level["definition"].split())
        lines.append(f"  {level['value']} = {level['label']}: {definition}")
    lines += ["", "Rules:"]
    lines += [f"  - {' '.join(rule.split())}" for rule in scoring["rules"]]
    lines += [
        "",
        "NEUTRALITY: never say whether an effect is good or bad for a "
        "group. Report relevance only; direction is the reader's call.",
    ]
    return "\n".join(lines)


def build_system_blocks(
    rubric_text: str, bill_text: str
) -> list[dict[str, Any]]:
    """Stable cached prefix: the rubric, then the bill text (cached).

    The bill text is identical across all dimension calls, so a cache
    breakpoint on it lets calls 2..N reuse it instead of re-sending.
    """
    return [
        {"type": "text", "text": rubric_text},
        {
            "type": "text",
            "text": f"BILL TEXT:\n\n{bill_text}",
            "cache_control": {"type": "ephemeral"},
        },
    ]


def build_user_prompt(
    dimension: dict[str, Any], values: list[dict[str, Any]]
) -> str:
    """Per-dimension instruction listing exactly the values to score."""
    lines = [f"Dimension: {dimension['label']} (id: {dimension['id']})"]
    guidance = dimension.get("guidance")
    if guidance:
        lines.append(f"Guidance: {' '.join(guidance.split())}")
    lines += ["", "Score each of these values for the bill above:"]
    for v in values:
        line = f"  - {v['id']} ({v['label']})"
        desc = v.get("description")
        if desc:
            line += f": {' '.join(desc.split())}"
        lines.append(line)
    lines += [
        "",
        'Return a JSON object mapping each value id to {"score": 0|1|2, '
        '"reason": "1-3 sentences"}. Score every value id listed above '
        "and no others.",
    ]
    return "\n".join(lines)


def build_dimension_schema(values: list[dict[str, Any]]) -> dict[str, Any]:
    """JSON schema forcing exactly the value ids and 0/1/2 scores."""
    value_schema = {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "enum": [0, 1, 2]},
            "reason": {"type": "string"},
        },
        "required": ["score", "reason"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {v["id"]: value_schema for v in values},
        "required": [v["id"] for v in values],
        "additionalProperties": False,
    }


# --------------------------------------------------------------------------
# Claude scoring call
# --------------------------------------------------------------------------
def score_dimension(
    client: Anthropic,
    model: str,
    system_blocks: list[dict[str, Any]],
    dimension: dict[str, Any],
    values: list[dict[str, Any]],
    use_thinking: bool,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """Make one Claude call scoring all values in a dimension.

    Returns ``(scores, usage)``; ``scores`` is ``None`` on a refusal or
    unparseable response so the caller can continue the other dimensions.
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": [
            {"role": "user", "content": build_user_prompt(dimension, values)}
        ],
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": build_dimension_schema(values),
            }
        },
    }
    if use_thinking:
        kwargs["thinking"] = {"type": "adaptive"}

    resp = client.messages.create(**kwargs)
    usage = _usage_dict(resp.usage)

    if resp.stop_reason == "refusal":
        print(f"    note: model refused {dimension['id']}", file=sys.stderr)
        return None, usage
    if resp.stop_reason == "max_tokens":
        print(
            f"    WARNING: hit max_tokens on {dimension['id']}; output "
            "may be truncated.",
            file=sys.stderr,
        )

    text = _extract_text(resp)
    try:
        return json.loads(_strip_fences(text)), usage
    except json.JSONDecodeError as exc:
        print(
            f"    WARNING: unparseable JSON for {dimension['id']}: {exc}",
            file=sys.stderr,
        )
        return None, usage


def _usage_dict(usage: Any) -> dict[str, int]:
    """Pull the token counts we care about off a response usage object."""
    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(
            usage, "cache_read_input_tokens", 0
        )
        or 0,
        "cache_creation_input_tokens": getattr(
            usage, "cache_creation_input_tokens", 0
        )
        or 0,
    }


def _extract_text(resp: Any) -> str:
    """Concatenate the text blocks of a Claude response."""
    parts = [
        block.text
        for block in resp.content
        if getattr(block, "type", None) == "text"
    ]
    return "".join(parts).strip()


def _strip_fences(text: str) -> str:
    """Drop ```json fences if the model wrapped its output in them."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def validate_scores(
    scores: dict[str, Any] | None, values: list[dict[str, Any]]
) -> dict[str, list[str]]:
    """Check returned keys/scores against the dimension's value ids."""
    expected = {v["id"] for v in values}
    if scores is None:
        return {
            "missing": sorted(expected),
            "extra": [],
            "bad_scores": ["<no scores returned>"],
        }
    got = set(scores)
    bad = [
        key
        for key, entry in scores.items()
        if not (isinstance(entry, dict) and entry.get("score") in (0, 1, 2))
    ]
    return {
        "missing": sorted(expected - got),
        "extra": sorted(got - expected),
        "bad_scores": sorted(bad),
    }


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
            print(f"    {_score_of(entry)}  {value_id}")


def _score_of(entry: Any) -> int:
    """Best-effort score for sorting/printing (-1 if malformed)."""
    if isinstance(entry, dict) and isinstance(entry.get("score"), int):
        return entry["score"]
    return -1


def print_validation(validation: dict[str, dict[str, list[str]]]) -> None:
    """Surface any dimensions whose output failed validation."""
    problems = {
        dim: v
        for dim, v in validation.items()
        if v["missing"] or v["extra"] or v.get("bad_scores")
    }
    if not problems:
        print("\nValidation: all dimensions OK.")
        return
    print("\nValidation PROBLEMS:")
    for dim, v in problems.items():
        print(f"  {dim}: {v}")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """Fetch one bill, score every dimension, and write the JSON."""
    args = _parse_args(argv)
    load_dotenv()

    congress_key = os.environ.get("CONGRESS_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    missing = [
        name
        for name, val in (
            ("CONGRESS_API_KEY", congress_key),
            ("ANTHROPIC_API_KEY", anthropic_key),
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

    ordinal = f"{args.congress}{_ordinal_suffix(args.congress)}"
    print(
        f"Fetching {args.bill_type.upper()} {args.number} "
        f"({ordinal} Congress)..."
    )
    bill = fetch_bill(args.congress, args.bill_type, args.number, congress_key)
    bill_text, source = fetch_bill_text(
        args.congress,
        args.bill_type,
        args.number,
        congress_key,
        max_chars=args.max_chars,
    )
    print(
        f"  Title: {bill.get('title', '<no title>')}\n"
        f"  Text version: {source['type']} ({len(bill_text)} chars)"
    )

    client = Anthropic(api_key=anthropic_key)
    system_blocks = build_system_blocks(rubric_text, bill_text)

    results: dict[str, Any] = {}
    validation: dict[str, Any] = {}
    totals = {"input": 0, "output": 0, "cache_read": 0}

    for dim in taxonomy["dimensions"]:
        values = select_values(dim, args.include_complement)
        print(f"Scoring {dim['id']} ({len(values)} values)...")
        scores, usage = score_dimension(
            client,
            args.model,
            system_blocks,
            dim,
            values,
            use_thinking=not args.no_thinking,
        )
        results[dim["id"]] = scores or {}
        validation[dim["id"]] = validate_scores(scores, values)
        totals["input"] += usage["input_tokens"]
        totals["output"] += usage["output_tokens"]
        totals["cache_read"] += usage["cache_read_input_tokens"]
        print(
            f"    usage: in={usage['input_tokens']} "
            f"cache_read={usage['cache_read_input_tokens']} "
            f"out={usage['output_tokens']}"
        )

    bill_id = f"{args.congress}-{args.bill_type}-{args.number}"
    payload = {
        "bill_id": bill_id,
        "bill": {
            "congress": args.congress,
            "type": bill.get("type", args.bill_type.upper()),
            "number": args.number,
            "title": bill.get("title"),
            "url": _bill_web_url(args.congress, args.bill_type, args.number),
            "text_version_type": source["type"],
            "text_source_url": source["url"],
        },
        "fetched_at": _utc_now_iso(),
        "model": args.model,
        "scores": results,
        "validation": validation,
    }

    out_path = OUT_DIR / f"{bill_id}.json"
    write_results(out_path, payload)

    print_summary(results, taxonomy)
    print_validation(validation)
    print(
        f"\nToken totals: input={totals['input']} "
        f"(cache_read={totals['cache_read']}) output={totals['output']}"
    )
    print(f"Wrote {out_path}")
    return 0


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
    parser.add_argument("--model", default=DEFAULT_MODEL)
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
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help="Truncate bill text beyond this many characters.",
    )
    args = parser.parse_args(argv)
    args.bill_type = args.bill_type.lower()
    return args


def _bill_web_url(congress: int, bill_type: str, number: int) -> str:
    """Human-facing congress.gov URL for the bill."""
    slug = BILL_TYPE_SLUG.get(bill_type.lower(), bill_type.lower())
    ordinal = f"{congress}{_ordinal_suffix(congress)}"
    return f"https://www.congress.gov/bill/{ordinal}-congress/{slug}/{number}"


def _ordinal_suffix(n: int) -> str:
    """Return the ordinal suffix (st/nd/rd/th) for ``n``."""
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string (``...Z``)."""
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())

"""Score a single congressional bill against the demographic taxonomy.

Throwaway prototype (see PROTOTYPE_HANDOFF.md). Fetches a bill and its
text from the Congress.gov API, makes one Claude call per taxonomy
dimension (all of that dimension's values scored together) plus one
call extracting the bill's explicitly targeted groups as conjunctions
of taxonomy values, validates everything against ``taxonomy.yaml``,
and writes one JSON file per bill to ``out/`` for manual review.

Resampling (handoff "Phase 2"): with ``--samples N`` every call runs
N times and the results are aggregated - majority vote per score with
an agreement ratio as pseudo-confidence, and target groups deduped on
their condition sets. The handoff suggested "nonzero temperature",
but Opus 4.8 removed the temperature parameter; resampling relies on
the model's natural run-to-run variance instead (empirically ~+/-1 on
borderline scores). ``--bills`` scores several bills in one run.

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
import statistics
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def build_target_group_prompt(taxonomy: dict[str, Any]) -> str:
    """Instruction for the target-group extraction call."""
    lines = ["Taxonomy dimensions and their value ids:"]
    for dim in taxonomy["dimensions"]:
        ids = ", ".join(v["id"] for v in dim["values"])
        lines.append(f"  - {dim['id']}: {ids}")
    lines += [
        "",
        "List every group the bill above EXPLICITLY targets. A group",
        "is a conjunction: the conditions one person must ALL meet to",
        "be a direct subject of the bill. Example: a bill raising a",
        "benefit for disabled veterans yields one group with",
        "veteran_status=veteran AND disability_status=has_disability.",
        "",
        "Rules:",
        "  - One group per distinct targeted population; bills often",
        "    target several.",
        "  - `conditions` may only use dimension and value ids from",
        "    the list above.",
        "  - Put constraints the taxonomy cannot express (e.g.",
        '    "receives VA disability compensation") in',
        "    `other_criteria` as short free-text strings.",
        "  - If a targeted group cannot be expressed with any",
        "    taxonomy value, leave `conditions` empty and describe",
        "    it fully in `other_criteria`.",
        "  - If the bill applies to essentially everyone, return one",
        "    group with empty `conditions` and empty",
        "    `other_criteria`.",
        "  - Keep `reason` to 1-2 neutral sentences; never say",
        "    whether the effect is good or bad for the group.",
    ]
    return "\n".join(lines)


# JSON schema for the target-group call. `dimension`/`value` stay
# plain strings (per-dimension enums can't be expressed conditionally
# in one schema); ids are checked against the taxonomy in
# ``validate_target_groups`` instead.
TARGET_GROUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "conditions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "dimension": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["dimension", "value"],
                            "additionalProperties": False,
                        },
                    },
                    "other_criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "reason": {"type": "string"},
                },
                "required": ["conditions", "other_criteria", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["target_groups"],
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
    return _call_claude_json(
        client,
        model,
        system_blocks,
        build_user_prompt(dimension, values),
        build_dimension_schema(values),
        use_thinking,
        label=dimension["id"],
        max_tokens=max_tokens,
    )


def extract_target_groups(
    client: Anthropic,
    model: str,
    system_blocks: list[dict[str, Any]],
    taxonomy: dict[str, Any],
    use_thinking: bool,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[list[dict[str, Any]] | None, dict[str, int]]:
    """Make one Claude call listing the bill's targeted groups.

    Returns ``(groups, usage)``; ``groups`` is ``None`` on a refusal
    or unparseable response.
    """
    parsed, usage = _call_claude_json(
        client,
        model,
        system_blocks,
        build_target_group_prompt(taxonomy),
        TARGET_GROUP_SCHEMA,
        use_thinking,
        label="target_groups",
        max_tokens=max_tokens,
    )
    if parsed is None:
        return None, usage
    return parsed.get("target_groups"), usage


def _call_claude_json(
    client: Anthropic,
    model: str,
    system_blocks: list[dict[str, Any]],
    user_prompt: str,
    schema: dict[str, Any],
    use_thinking: bool,
    label: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """One schema-constrained Claude call against the cached bill text.

    Returns ``(parsed, usage)``; ``parsed`` is ``None`` on a refusal or
    unparseable response so the caller can continue.
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": [{"role": "user", "content": user_prompt}],
        "output_config": {
            "format": {"type": "json_schema", "schema": schema}
        },
    }
    if use_thinking:
        kwargs["thinking"] = {"type": "adaptive"}

    resp = client.messages.create(**kwargs)
    usage = _usage_dict(resp.usage)

    if resp.stop_reason == "refusal":
        print(f"    note: model refused {label}", file=sys.stderr)
        return None, usage
    if resp.stop_reason == "max_tokens":
        print(
            f"    WARNING: hit max_tokens on {label}; output "
            "may be truncated.",
            file=sys.stderr,
        )

    text = _extract_text(resp)
    try:
        return json.loads(_strip_fences(text)), usage
    except json.JSONDecodeError as exc:
        print(
            f"    WARNING: unparseable JSON for {label}: {exc}",
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
# Resampling aggregation (handoff "Phase 2")
# --------------------------------------------------------------------------
def aggregate_scores(
    samples: list[dict[str, Any]], values: list[dict[str, Any]]
) -> dict[str, Any]:
    """Combine N sampled score maps for one dimension into one map.

    Per value: the median vote (equal to the majority winner whenever
    one exists; breaks rare 0-vs-2 splits toward the middle), the
    share of samples agreeing with it (``agreement``), and the vote
    distribution (``votes``). The reason comes from the first sample
    that voted with the winner.
    """
    combined: dict[str, Any] = {}
    for value in values:
        vid = value["id"]
        entries = [
            s[vid]
            for s in samples
            if isinstance(s.get(vid), dict)
            and s[vid].get("score") in (0, 1, 2)
        ]
        if not entries:
            continue  # validate_scores flags the value as missing
        votes = [e["score"] for e in entries]
        score = statistics.median_low(votes)
        reason = next(
            e.get("reason", "") for e in entries if e["score"] == score
        )
        combined[vid] = {
            "score": score,
            "reason": reason,
            "agreement": round(votes.count(score) / len(votes), 2),
            "votes": {str(v): votes.count(v) for v in sorted(set(votes))},
        }
    return combined


def aggregate_target_groups(
    samples: list[list[dict[str, Any]]], n_samples: int
) -> list[dict[str, Any]]:
    """Combine N sampled group lists, deduped on their condition sets.

    Groups are keyed by their set of (dimension, value) conditions;
    display fields (criteria, reason) come from the first occurrence.
    ``agreement`` is the share of samples that produced the group, so
    low-agreement groups stay visible rather than silently kept.
    """
    first_seen: dict[frozenset, dict[str, Any]] = {}
    counts: dict[frozenset, int] = {}
    for groups in samples:
        keys_this_sample = set()
        for group in groups:
            key = frozenset(
                (c.get("dimension"), c.get("value"))
                for c in group.get("conditions", [])
            )
            if key not in first_seen:
                first_seen[key] = group
            keys_this_sample.add(key)
        for key in keys_this_sample:
            counts[key] = counts.get(key, 0) + 1
    combined = []
    for key, group in first_seen.items():
        entry = dict(group)
        entry["agreement"] = round(counts[key] / n_samples, 2)
        combined.append(entry)
    combined.sort(key=lambda g: -g["agreement"])
    return combined


# --------------------------------------------------------------------------
# Orchestration of all calls for one bill
# --------------------------------------------------------------------------
def score_all(
    client: Anthropic,
    model: str,
    system_blocks: list[dict[str, Any]],
    taxonomy: dict[str, Any],
    include_complement: bool,
    use_thinking: bool,
    samples: int,
    concurrency: int,
    extract_groups: bool,
) -> tuple[
    dict[str, Any], list[dict[str, Any]] | None, dict[str, Any], dict[str, int]
]:
    """Run every dimension call (x samples) plus the target-group call.

    Calls run on a small thread pool; a failed call (even after SDK
    retries) costs one sample, not the bill. Returns
    ``(results, target_groups, validation, totals)``.
    """
    dims = taxonomy["dimensions"]
    dim_by_id = {d["id"]: d for d in dims}
    values_by_id = {
        d["id"]: select_values(d, include_complement) for d in dims
    }

    tasks: list[tuple[str, int]] = [
        (d["id"], i) for d in dims for i in range(samples)
    ]
    if extract_groups:
        tasks += [("__groups__", i) for i in range(samples)]

    def run(task: tuple[str, int]) -> tuple[Any, dict[str, int]]:
        kind, i = task
        try:
            if kind == "__groups__":
                return extract_target_groups(
                    client, model, system_blocks, taxonomy, use_thinking
                )
            return score_dimension(
                client,
                model,
                system_blocks,
                dim_by_id[kind],
                values_by_id[kind],
                use_thinking,
            )
        except Exception as exc:  # keep the batch alive
            print(
                f"    WARNING: call failed for {kind} "
                f"(sample {i + 1}): {exc}",
                file=sys.stderr,
            )
            return None, {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            }

    # Run the first call alone so it writes the prompt cache (long
    # bills only); concurrent identical prefixes would all miss it.
    outputs: dict[tuple[str, int], tuple[Any, dict[str, int]]] = {}
    outputs[tasks[0]] = run(tasks[0])
    done = 1
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(run, t): t for t in tasks[1:]}
        for fut in as_completed(futures):
            task = futures[fut]
            outputs[task] = fut.result()
            done += 1
            print(f"    [{done}/{len(tasks)}] {task[0]}")

    totals = {"input": 0, "output": 0, "cache_read": 0}

    def collect(kind: str) -> list[Any]:
        good = []
        for i in range(samples):
            parsed, usage = outputs[(kind, i)]
            totals["input"] += usage["input_tokens"]
            totals["output"] += usage["output_tokens"]
            totals["cache_read"] += usage["cache_read_input_tokens"]
            if parsed is not None:
                good.append(parsed)
        return good

    results: dict[str, Any] = {}
    validation: dict[str, Any] = {}
    for dim in dims:
        maps = collect(dim["id"])
        if not maps:
            scores = None
        elif samples == 1:
            scores = maps[0]
        else:
            scores = aggregate_scores(maps, values_by_id[dim["id"]])
        results[dim["id"]] = scores or {}
        validation[dim["id"]] = validate_scores(
            scores, values_by_id[dim["id"]]
        )

    target_groups: list[dict[str, Any]] | None = None
    if extract_groups:
        group_lists = collect("__groups__")
        if group_lists:
            if samples == 1:
                target_groups = group_lists[0]
            else:
                target_groups = aggregate_target_groups(
                    group_lists, samples
                )
        validation["target_groups"] = validate_target_groups(
            target_groups, taxonomy
        )

    return results, target_groups, validation, totals


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


def validate_target_groups(
    groups: list[dict[str, Any]] | None, taxonomy: dict[str, Any]
) -> dict[str, list[str]]:
    """Check group conditions against the taxonomy's ids."""
    if groups is None:
        return {"unknown_ids": ["<no target groups returned>"]}
    values_by_dim = {
        d["id"]: {v["id"] for v in d["values"]}
        for d in taxonomy["dimensions"]
    }
    unknown: list[str] = []
    for i, group in enumerate(groups):
        for cond in group.get("conditions", []):
            dim = cond.get("dimension")
            val = cond.get("value")
            if dim not in values_by_dim:
                unknown.append(f"group {i}: unknown dimension '{dim}'")
            elif val not in values_by_dim[dim]:
                unknown.append(f"group {i}: unknown value '{dim}.{val}'")
    return {"unknown_ids": unknown}


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
    ordinal = f"{args.congress}{_ordinal_suffix(args.congress)}"
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
    results, target_groups, validation, totals = score_all(
        client,
        args.model,
        system_blocks,
        taxonomy,
        include_complement=args.include_complement,
        use_thinking=not args.no_thinking,
        samples=args.samples,
        concurrency=args.concurrency,
        extract_groups=not args.no_target_groups,
    )

    bill_id = f"{args.congress}-{bill_type}-{number}"
    payload = {
        "bill_id": bill_id,
        "bill": {
            "congress": args.congress,
            "type": bill.get("type", bill_type.upper()),
            "number": number,
            "title": bill.get("title"),
            "url": _bill_web_url(args.congress, bill_type, number),
            "text_version_type": source["type"],
            "text_source_url": source["url"],
        },
        "fetched_at": _utc_now_iso(),
        "model": args.model,
        "samples": args.samples,
        "scores": results,
        "target_groups": target_groups or [],
        "validation": validation,
    }

    out_path = OUT_DIR / f"{bill_id}.json"
    write_results(out_path, payload)

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

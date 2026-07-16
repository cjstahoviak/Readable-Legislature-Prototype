"""Claude scoring calls, resampling aggregation, and orchestration.

One call per taxonomy dimension (all of that dimension's values scored
together so they calibrate against each other) plus one call extracting
the bill's explicitly targeted groups. With ``samples > 1`` every call
runs N times and results are aggregated: majority vote per score with
an agreement ratio as pseudo-confidence, and target groups deduped on
their condition sets. (Opus 4.8 removed the temperature parameter, so
resampling relies on the model's natural run-to-run variance.)
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from anthropic import Anthropic

from .prompts import (
    SUMMARY_SCHEMA,
    TARGET_GROUP_SCHEMA,
    build_dimension_schema,
    build_summary_prompt,
    build_target_group_prompt,
    build_user_prompt,
)
from .taxonomy import select_values
from .validation import validate_scores, validate_target_groups

DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_MAX_TOKENS = 16000


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


def summarize_bill(
    client: Anthropic,
    model: str,
    system_blocks: list[dict[str, Any]],
    use_thinking: bool,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[dict[str, str] | None, dict[str, int]]:
    """Make one Claude call producing the plain-language summaries.

    Returns ``({"tldr", "overview"}, usage)``; the dict is ``None`` on
    a refusal or unparseable response. Runs once per bill regardless of
    resampling — majority-voting prose is meaningless.
    """
    parsed, usage = _call_claude_json(
        client,
        model,
        system_blocks,
        build_summary_prompt(),
        SUMMARY_SCHEMA,
        use_thinking,
        label="summary",
        max_tokens=max_tokens,
    )
    if not isinstance(parsed, dict):
        return None, usage
    tldr = (parsed.get("tldr") or "").strip()
    overview = (parsed.get("overview") or "").strip()
    if not tldr or not overview:
        return None, usage
    return {"tldr": tldr, "overview": overview}, usage


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
# Resampling aggregation
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
    dimensions: list[str] | None = None,
    generate_summary: bool = False,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]] | None,
    dict[str, str] | None,
    dict[str, Any],
    dict[str, int],
]:
    """Run the dimension calls (x samples) plus group/summary calls.

    ``dimensions`` restricts scoring to a subset of dimension ids (the
    scoring job uses this to retry only what's missing); ``None`` means
    all. Calls run on a small thread pool; a failed call (even after
    SDK retries) costs one sample, not the bill. Returns
    ``(results, target_groups, summary, validation, totals)``.
    """
    dims = [
        d
        for d in taxonomy["dimensions"]
        if dimensions is None or d["id"] in dimensions
    ]
    dim_by_id = {d["id"]: d for d in dims}
    values_by_id = {
        d["id"]: select_values(d, include_complement) for d in dims
    }

    tasks: list[tuple[str, int]] = [
        (d["id"], i) for d in dims for i in range(samples)
    ]
    if extract_groups:
        tasks += [("__groups__", i) for i in range(samples)]
    if generate_summary:
        tasks += [("__summary__", 0)]  # prose: one call, never resampled
    if not tasks:
        return {}, None, None, {}, {"input": 0, "output": 0, "cache_read": 0}

    def run(task: tuple[str, int]) -> tuple[Any, dict[str, int]]:
        kind, i = task
        try:
            if kind == "__groups__":
                return extract_target_groups(
                    client, model, system_blocks, taxonomy, use_thinking
                )
            if kind == "__summary__":
                return summarize_bill(
                    client, model, system_blocks, use_thinking
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

    summary: dict[str, str] | None = None
    if generate_summary:
        summary, usage = outputs[("__summary__", 0)]
        totals["input"] += usage["input_tokens"]
        totals["output"] += usage["output_tokens"]
        totals["cache_read"] += usage["cache_read_input_tokens"]
        validation["summary"] = {
            "missing": [] if summary else ["<no summary returned>"]
        }

    return results, target_groups, summary, validation, totals

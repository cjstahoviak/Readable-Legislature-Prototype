"""Validate structured LLM output against the taxonomy's ids."""

from __future__ import annotations

from typing import Any


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

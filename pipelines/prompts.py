"""Prompt and JSON-schema construction for the LLM scoring calls.

The system prompt is a stable cached prefix (rubric + bill text) shared
across all per-dimension calls for a bill; each call's user prompt names
the dimension and exactly the value ids to score.
"""

from __future__ import annotations

from typing import Any


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
# ``validation.validate_target_groups`` instead.
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

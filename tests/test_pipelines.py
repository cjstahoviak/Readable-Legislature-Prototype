"""Offline tests for the pipelines package.

Everything here runs without network access: prompt construction and
validation are exercised against the real taxonomy.yaml, and the
committed scoring outputs in out/ serve as golden fixtures. These
tests guard the Phase 0 restructure (behavior parity with the original
score_bill.py) and will back the Phase 1 eval harness.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipelines.export_taxonomy import build_export
from pipelines.prompts import (
    TARGET_GROUP_SCHEMA,
    build_dimension_schema,
    build_rubric_text,
    build_user_prompt,
)
from pipelines.score_bill import _parse_bill_list
from pipelines.scoring import aggregate_scores, aggregate_target_groups
from pipelines.taxonomy import load_taxonomy, select_values
from pipelines.validation import validate_scores, validate_target_groups

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_BILL = REPO_ROOT / "out" / "119-hr-2138.json"


@pytest.fixture(scope="module")
def taxonomy():
    return load_taxonomy()


@pytest.fixture(scope="module")
def golden():
    with GOLDEN_BILL.open(encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# Taxonomy
# --------------------------------------------------------------------------
def test_taxonomy_shape(taxonomy):
    # 16, not the 13 the original planning docs mention — the taxonomy
    # grew after those were written. Update deliberately when it changes.
    assert len(taxonomy["dimensions"]) == 16
    assert {level["value"] for level in taxonomy["scoring"]["scale"]} == {0, 1, 2}
    ids = [d["id"] for d in taxonomy["dimensions"]]
    assert len(ids) == len(set(ids)), "dimension ids must be unique"


def test_select_values_skips_complements(taxonomy):
    veteran = next(
        d for d in taxonomy["dimensions"] if d["id"] == "veteran_status"
    )
    default = [v["id"] for v in select_values(veteran, include_complement=False)]
    assert default == ["veteran"]
    everything = [v["id"] for v in select_values(veteran, include_complement=True)]
    assert everything == ["veteran", "non_veteran"]


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------
def test_rubric_text_renders_scale_and_neutrality(taxonomy):
    rubric = build_rubric_text(taxonomy["scoring"])
    assert "0 = low" in rubric
    assert "2 = high" in rubric
    assert "NEUTRALITY" in rubric


def test_user_prompt_lists_exactly_the_values(taxonomy):
    housing = next(
        d for d in taxonomy["dimensions"] if d["id"] == "housing_status"
    )
    values = select_values(housing, include_complement=False)
    prompt = build_user_prompt(housing, values)
    assert "Dimension: Housing status (id: housing_status)" in prompt
    for v in values:
        assert f"- {v['id']} (" in prompt


def test_dimension_schema_requires_exactly_the_value_ids(taxonomy):
    income = next(d for d in taxonomy["dimensions"] if d["id"] == "income")
    values = select_values(income, include_complement=False)
    schema = build_dimension_schema(values)
    expected = [v["id"] for v in values]
    assert schema["required"] == expected
    assert sorted(schema["properties"]) == sorted(expected)
    assert schema["additionalProperties"] is False
    score_schema = schema["properties"][expected[0]]["properties"]["score"]
    assert score_schema["enum"] == [0, 1, 2]


def test_target_group_schema_shape():
    items = TARGET_GROUP_SCHEMA["properties"]["target_groups"]["items"]
    assert set(items["required"]) == {"conditions", "other_criteria", "reason"}


# --------------------------------------------------------------------------
# Resampling aggregation
# --------------------------------------------------------------------------
def test_aggregate_scores_majority_vote():
    values = [{"id": "renter"}]
    samples = [
        {"renter": {"score": 2, "reason": "first"}},
        {"renter": {"score": 2, "reason": "second"}},
        {"renter": {"score": 1, "reason": "third"}},
    ]
    combined = aggregate_scores(samples, values)
    entry = combined["renter"]
    assert entry["score"] == 2
    assert entry["reason"] == "first"  # first sample voting with the winner
    assert entry["agreement"] == 0.67
    assert entry["votes"] == {"1": 1, "2": 2}


def test_aggregate_scores_breaks_split_toward_middle():
    values = [{"id": "renter"}]
    samples = [
        {"renter": {"score": 0, "reason": "low"}},
        {"renter": {"score": 2, "reason": "high"}},
    ]
    combined = aggregate_scores(samples, values)
    assert combined["renter"]["score"] == 0  # median_low of a 0/2 split


def test_aggregate_scores_skips_missing_values():
    values = [{"id": "renter"}, {"id": "homeowner"}]
    samples = [{"renter": {"score": 1, "reason": "r"}}]
    combined = aggregate_scores(samples, values)
    assert "homeowner" not in combined  # validate_scores flags it instead


def test_aggregate_target_groups_dedupes_on_conditions():
    veteran_group = {
        "conditions": [{"dimension": "veteran_status", "value": "veteran"}],
        "other_criteria": [],
        "reason": "first occurrence wins",
    }
    everyone_group = {"conditions": [], "other_criteria": [], "reason": "all"}
    samples = [
        [veteran_group, everyone_group],
        [dict(veteran_group, reason="later duplicate")],
        [veteran_group],
    ]
    combined = aggregate_target_groups(samples, n_samples=3)
    assert len(combined) == 2
    by_reason = {g["reason"]: g for g in combined}
    assert by_reason["first occurrence wins"]["agreement"] == 1.0
    assert by_reason["all"]["agreement"] == 0.33
    assert combined[0]["agreement"] >= combined[-1]["agreement"]


# --------------------------------------------------------------------------
# Validation, against the golden output
# --------------------------------------------------------------------------
def test_golden_bill_scores_validate_clean(taxonomy, golden):
    for dim in taxonomy["dimensions"]:
        values = select_values(dim, include_complement=False)
        checks = validate_scores(golden["scores"][dim["id"]], values)
        assert not any(checks.values()), f"{dim['id']}: {checks}"


def test_golden_bill_target_groups_validate_clean(taxonomy, golden):
    checks = validate_target_groups(golden["target_groups"], taxonomy)
    assert checks == {"unknown_ids": []}


def test_validate_scores_flags_problems():
    values = [{"id": "renter"}, {"id": "homeowner"}]
    checks = validate_scores(
        {"renter": {"score": 5, "reason": "bad"}, "stranger": {"score": 1}},
        values,
    )
    assert checks["missing"] == ["homeowner"]
    assert checks["extra"] == ["stranger"]
    assert checks["bad_scores"] == ["renter"]


def test_validate_target_groups_flags_unknown_ids(taxonomy):
    groups = [
        {"conditions": [{"dimension": "age", "value": "age_18_25"}]},
        {"conditions": [{"dimension": "nope", "value": "x"}]},
        {"conditions": [{"dimension": "age", "value": "not_a_bracket"}]},
    ]
    checks = validate_target_groups(groups, taxonomy)
    assert checks["unknown_ids"] == [
        "group 1: unknown dimension 'nope'",
        "group 2: unknown value 'age.not_a_bracket'",
    ]


# --------------------------------------------------------------------------
# CLI helpers
# --------------------------------------------------------------------------
def test_parse_bill_list_single_and_multi():
    single = SimpleNamespace(bills=None, bill_type="hr", number=2138)
    assert _parse_bill_list(single) == [("hr", 2138)]
    multi = SimpleNamespace(bills="hr-2138, s-129,hjres-7", bill_type="hr", number=1)
    assert _parse_bill_list(multi) == [("hr", 2138), ("s", 129), ("hjres", 7)]


def test_parse_bill_list_rejects_garbage():
    bad = SimpleNamespace(bills="notabill", bill_type="hr", number=1)
    with pytest.raises(SystemExit):
        _parse_bill_list(bad)


# --------------------------------------------------------------------------
# Taxonomy export for the web app
# --------------------------------------------------------------------------
def test_export_matches_taxonomy(taxonomy):
    export = build_export(taxonomy)
    assert [d["id"] for d in export["dimensions"]] == [
        d["id"] for d in taxonomy["dimensions"]
    ]
    assert {level["value"] for level in export["scale"]} == {0, 1, 2}
    veteran = next(
        d for d in export["dimensions"] if d["id"] == "veteran_status"
    )
    scored = {v["id"]: v["scored"] for v in veteran["values"]}
    assert scored == {"veteran": True, "non_veteran": False}
    assert json.dumps(export)  # round-trips to JSON

"""Tests for the Phase 1 jobs: ingestion, scoring-job planning, eval.

Pure-logic tests run everywhere; the database round-trip tests run only
when TEST_DATABASE_URL is set (pointing at a database with the schema
applied) and are skipped otherwise.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pipelines.eval_models import aggregate, compare_groups, compare_scores
from pipelines.ingest import classify_action, derive_stage
from pipelines.load_outputs import parse_bill_id
from pipelines.prompts import SUMMARY_SCHEMA, build_summary_prompt
from pipelines.score_pending import (
    missing_dimensions,
    needs_full_run,
    resolve_status,
)
from pipelines.taxonomy import load_taxonomy, select_values

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def taxonomy():
    return load_taxonomy()


# --------------------------------------------------------------------------
# Stage derivation
# --------------------------------------------------------------------------
def _action(text, atype=None, date="2026-01-01"):
    return {"text": text, "type": atype, "actionDate": date}


def test_derive_stage_progression():
    actions = [
        _action("Introduced in House", "IntroReferral", "2026-01-01"),
        _action(
            "Referred to the Committee on Veterans' Affairs.",
            "IntroReferral",
            "2026-01-02",
        ),
        _action(
            "Passed/agreed to in House: On motion to suspend the rules.",
            "Floor",
            "2026-02-01",
        ),
        _action(
            "Passed/agreed to in Senate: Passed Senate without amendment.",
            "Floor",
            "2026-03-01",
        ),
        _action("Presented to President.", "President", "2026-03-05"),
        _action("Became Public Law No: 119-72.", "BecameLaw", "2026-03-10"),
    ]
    assert derive_stage(actions) == "enacted"
    assert derive_stage(actions[:5]) == "to_president"
    assert derive_stage(actions[:4]) == "passed_senate"
    assert derive_stage(actions[:3]) == "passed_house"
    assert derive_stage(actions[:2]) == "committee"
    assert derive_stage(actions[:1]) == "introduced"
    assert derive_stage([]) == "introduced"


def test_stage_never_moves_backward():
    actions = [
        _action("Passed/agreed to in House.", "Floor", "2026-02-01"),
        # a later referral (e.g. to a Senate committee) must not demote
        _action(
            "Referred to the Committee on Finance.", "IntroReferral", "2026-02-02"
        ),
    ]
    assert derive_stage(actions) == "passed_house"


def test_classify_action_unrecognized_is_none():
    assert classify_action(_action("Sponsor introductory remarks on measure.")) is None


def test_adopts_prior_scores():
    from pipelines.ingest import adopts_prior_scores

    seeded = {
        "text_hash": None,
        "scored_text_hash": None,
        "llm_status": "partial",
        "text_source_url": "https://congress.gov/text/v1.htm",
    }
    assert adopts_prior_scores(seeded, "https://congress.gov/text/v1.htm")
    # A newer text version was published: don't adopt, re-score.
    assert not adopts_prior_scores(seeded, "https://congress.gov/text/v2.htm")
    # Never-seeded bills and already-ingested bills don't adopt.
    assert not adopts_prior_scores(None, "https://congress.gov/text/v1.htm")
    assert not adopts_prior_scores(
        {**seeded, "text_hash": "abc"}, "https://congress.gov/text/v1.htm"
    )
    assert not adopts_prior_scores(
        {**seeded, "llm_status": "pending"}, "https://congress.gov/text/v1.htm"
    )


# --------------------------------------------------------------------------
# Scoring-job planning
# --------------------------------------------------------------------------
def test_missing_dimensions(taxonomy):
    full_coverage = {
        d["id"]: {v["id"] for v in select_values(d, False)}
        for d in taxonomy["dimensions"]
    }
    assert missing_dimensions(taxonomy, full_coverage) == []

    partial = dict(full_coverage)
    partial["religion"] = set()  # the GENIUS Act failure mode
    partial["age"] = set(list(full_coverage["age"])[:2])  # incomplete dim
    assert missing_dimensions(taxonomy, partial) == ["age", "religion"]

    assert len(missing_dimensions(taxonomy, {})) == 16


def test_needs_full_run():
    base = {
        "llm_status": "partial",
        "scored_text_hash": "abc",
        "text_hash": "abc",
        "llm_prompt_version": "1",
    }
    assert not needs_full_run(base)
    assert needs_full_run({**base, "llm_status": "pending"})
    assert needs_full_run({**base, "text_hash": "def"})  # stale text
    assert needs_full_run({**base, "llm_prompt_version": "0"})


def test_resolve_status():
    assert resolve_status([], 16, True, True) == "complete"
    assert resolve_status(["religion"], 16, True, True) == "partial"
    assert resolve_status([], 16, False, True) == "partial"
    assert resolve_status([], 16, True, False) == "partial"
    all_dims = [f"d{i}" for i in range(16)]
    assert resolve_status(all_dims, 16, False, False) == "failed"


# --------------------------------------------------------------------------
# Summary prompt
# --------------------------------------------------------------------------
def test_summary_prompt_and_schema():
    prompt = build_summary_prompt()
    assert "tldr" in prompt and "overview" in prompt
    assert "NEUTRALITY" in prompt
    assert set(SUMMARY_SCHEMA["required"]) == {"tldr", "overview"}


# --------------------------------------------------------------------------
# Eval comparison
# --------------------------------------------------------------------------
def test_compare_scores_counts_and_disagreements():
    golden = {
        "housing_status": {
            "homeowner": {"score": 2},
            "renter": {"score": 1},
            "other": {"score": 0},
        }
    }
    candidate = {
        "housing_status": {
            "homeowner": {"score": 2},  # exact
            "renter": {"score": 0},     # off by 1, false negative
            # "other" missing entirely
        }
    }
    result = compare_scores(golden, candidate)
    assert result["compared"] == 2
    assert result["missing"] == 1
    assert result["exact"] == 1
    assert result["off_by_1"] == 1
    assert result["off_by_2"] == 0
    assert result["nonzero_precision"] == 1.0  # 1 tp, 0 fp
    assert result["nonzero_recall"] == 0.5  # 1 tp, 1 fn
    assert result["disagreements"] == [
        {"dimension": "housing_status", "value": "renter", "golden": 1, "candidate": 0}
    ]


def test_compare_scores_flags_zero_two_flips():
    golden = {"d": {"v": {"score": 0}}}
    candidate = {"d": {"v": {"score": 2}}}
    assert compare_scores(golden, candidate)["off_by_2"] == 1


def test_compare_groups_jaccard():
    golden = [
        {"conditions": [{"dimension": "veteran_status", "value": "veteran"}]},
        {"conditions": []},
    ]
    same = compare_groups(golden, golden)
    assert same["jaccard"] == 1.0
    half = compare_groups(
        golden,
        [{"conditions": [{"dimension": "veteran_status", "value": "veteran"}]}],
    )
    assert half["jaccard"] == 0.5
    assert compare_groups([], []) == {
        "golden_groups": 0,
        "candidate_groups": 0,
        "jaccard": 1.0,
    }


def test_aggregate_rolls_up():
    per_bill = [
        {
            "scores": {
                "compared": 10, "missing": 0, "exact": 8, "off_by_1": 2,
                "off_by_2": 0, "nonzero_precision": 1.0, "nonzero_recall": 0.8,
            },
            "groups": {"jaccard": 1.0},
        },
        {
            "scores": {
                "compared": 10, "missing": 1, "exact": 9, "off_by_1": 0,
                "off_by_2": 1, "nonzero_precision": None, "nonzero_recall": None,
            },
            "groups": {"jaccard": 0.5},
        },
    ]
    agg = aggregate(per_bill)
    assert agg["exact"] == 17
    assert agg["exact_rate"] == 0.85
    assert agg["off_by_2_rate"] == 0.05
    assert agg["mean_nonzero_precision"] == 1.0
    assert agg["mean_group_jaccard"] == 0.75


# --------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------
def test_parse_bill_id():
    assert parse_bill_id("119-hr-2138") == (119, "hr", 2138)
    assert parse_bill_id("119-SRES-5") == (119, "sres", 5)
    with pytest.raises(ValueError):
        parse_bill_id("hr-2138")
    with pytest.raises(ValueError):
        parse_bill_id("119-xx-1")


# --------------------------------------------------------------------------
# Database round-trip (requires TEST_DATABASE_URL with schema applied)
# --------------------------------------------------------------------------
needs_db = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set",
)


@needs_db
def test_loader_roundtrip(taxonomy):
    from pipelines import db
    from pipelines.load_outputs import load_payload

    golden_path = REPO_ROOT / "out" / "119-hr-2138.json"
    with golden_path.open(encoding="utf-8") as fh:
        payload = json.load(fh)

    with db.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        # Everything inside rolls back on exit — the database (which may
        # be a shared dev database) is left exactly as it was found.
        with conn.transaction(force_rollback=True):
            label = load_payload(conn, payload)
            assert label == "119-hr-2138"

            bill = db.get_bill(conn, 119, "hr", 2138)
            assert bill is not None
            assert bill["llm_samples"] == payload["samples"]

            coverage = db.score_coverage(conn, bill["id"])
            expected_values = sum(
                len(select_values(d, False)) for d in taxonomy["dimensions"]
            )
            assert sum(len(v) for v in coverage.values()) == expected_values

            assert db.target_group_count(conn, bill["id"]) == len(
                payload["target_groups"]
            )

            # Loading again must be idempotent, not duplicate.
            load_payload(conn, payload)
            coverage2 = db.score_coverage(conn, bill["id"])
            assert coverage2 == coverage

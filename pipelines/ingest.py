"""Ingest bills from the Congress.gov API into PostgreSQL.

Two modes:

    # Specific bills (full refresh of each):
    python -m pipelines.ingest --congress 119 --bills hr-2138,s-5

    # Incremental: every bill updated since a timestamp, oldest first:
    python -m pipelines.ingest --congress 119 --since 2026-07-01T00:00:00Z

A refresh pulls metadata, sponsors + cosponsors, the action history,
committees, and the latest bill text. Everything for one bill commits
in a single transaction. When the bill's text hash changes, its
``llm_status`` drops back to ``pending`` so the scoring job picks it
up; metadata-only updates never trigger re-scoring.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import sys
from typing import Any

import psycopg
from dotenv import load_dotenv

from . import db
from .congress import api_get, bill_web_url, fetch_bill_text, paginate

# Stage precedence: a later action can only move the stage forward
# (equal rank resolves to the later action, so a bill that passed the
# House and then the Senate lands on passed_senate).
_STAGE_RANK = {
    "introduced": 1,
    "committee": 2,
    "floor": 3,
    "passed_house": 4,
    "passed_senate": 4,
    "to_president": 5,
    "failed": 6,
    "vetoed": 6,
    "enacted": 7,
}


def classify_action(action: dict[str, Any]) -> str | None:
    """Map one Congress.gov action to a lifecycle stage, if any.

    Matches the standard phrasings Congress.gov uses for milestones;
    unrecognized actions return None and never move the stage.
    """
    text = (action.get("text") or "").lower()
    atype = action.get("type") or ""
    if atype == "BecameLaw" or "became public law" in text:
        return "enacted"
    if atype == "Veto" or "vetoed by president" in text:
        return "vetoed"
    if "failed of passage" in text or "failed passage" in text:
        return "failed"
    if atype == "President" or "presented to president" in text:
        return "to_president"
    if text.startswith("passed/agreed to in house") or "passed house" in text:
        return "passed_house"
    if text.startswith("passed/agreed to in senate") or "passed senate" in text:
        return "passed_senate"
    if atype in ("Floor", "Calendars") or "placed on" in text and "calendar" in text:
        return "floor"
    if atype == "Committee" or (
        "referred to" in text and ("committee" in text or "subcommittee" in text)
    ):
        return "committee"
    if atype == "IntroReferral":
        return "introduced"
    return None


def derive_stage(actions: list[dict[str, Any]]) -> str:
    """Derive the bill's lifecycle stage from its action history."""
    stage = "introduced"
    ordered = sorted(actions, key=lambda a: a.get("actionDate") or "")
    for action in ordered:
        candidate = classify_action(action)
        if candidate and _STAGE_RANK[candidate] >= _STAGE_RANK[stage]:
            stage = candidate
    return stage


def _action_chamber(action: dict[str, Any]) -> str | None:
    source = ((action.get("sourceSystem") or {}).get("name") or "").lower()
    if "house" in source:
        return "house"
    if "senate" in source:
        return "senate"
    return None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_api_datetime(value: str | None) -> dt.datetime | None:
    """Parse Congress.gov timestamps ('...Z' or date-only) as aware UTC."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def adopts_prior_scores(
    existing: dict[str, Any] | None, source_url: str
) -> bool:
    """Whether newly fetched text is the version prior scores came from.

    Bills seeded by pipelines.load_outputs carry scores plus the text
    *URL* they were scored on, but no text and no scored_text_hash.
    When ingestion later fetches that exact text version, the stored
    scores are valid for it — record the provenance instead of
    invalidating 59 perfectly good scores. Applies only to rows that
    have never been ingested (text_hash is NULL); after first ingest
    the normal hash-comparison rules take over.
    """
    return bool(
        existing
        and existing["text_hash"] is None
        and existing["scored_text_hash"] is None
        and existing["llm_status"] in ("partial", "complete")
        and existing["text_source_url"] == source_url
    )


# --------------------------------------------------------------------------
# One-bill refresh
# --------------------------------------------------------------------------
def refresh_bill(
    conn: psycopg.Connection,
    api_key: str,
    congress: int,
    bill_type: str,
    number: int,
) -> str:
    """Fetch one bill end-to-end and upsert it; returns a status word."""
    detail = api_get(f"bill/{congress}/{bill_type}/{number}", api_key)["bill"]

    actions_raw = list(
        paginate(f"bill/{congress}/{bill_type}/{number}/actions", api_key, "actions")
    )
    committees_raw = list(
        paginate(
            f"bill/{congress}/{bill_type}/{number}/committees", api_key, "committees"
        )
    )
    cosponsors_raw = list(
        paginate(
            f"bill/{congress}/{bill_type}/{number}/cosponsors", api_key, "cosponsors"
        )
    )

    text = source = None
    try:
        text, source = fetch_bill_text(congress, bill_type, number, api_key)
    except RuntimeError as exc:  # e.g. no text versions published yet
        print(f"  note: no text for {bill_type}-{number}: {exc}", file=sys.stderr)

    latest_action = detail.get("latestAction") or {}
    fields: dict[str, Any] = {
        "title": detail.get("title"),
        "stage": derive_stage(actions_raw),
        "policy_area": (detail.get("policyArea") or {}).get("name"),
        "introduced_date": detail.get("introducedDate"),
        "source_url": bill_web_url(congress, bill_type, number),
        "latest_action_text": latest_action.get("text"),
        "latest_action_date": latest_action.get("actionDate"),
        "congress_update_date": detail.get("updateDate"),
        "last_fetched_at": dt.datetime.now(dt.timezone.utc),
    }

    with conn.transaction():
        existing = db.get_bill(conn, congress, bill_type, number)
        text_changed = False
        if text is not None:
            text_hash = sha256_text(text)
            text_changed = existing is None or existing["text_hash"] != text_hash
            fields.update(
                text_hash=text_hash,
                text_version_type=source["type"],
                text_source_url=source["url"],
            )
            if adopts_prior_scores(existing, source["url"]):
                # Loader-seeded bill: the stored scores came from this
                # exact text version, so record that instead of
                # invalidating them.
                fields["scored_text_hash"] = text_hash
            elif text_changed and (
                existing is None or existing["llm_status"] == "complete"
            ):
                # New or changed text invalidates existing scores.
                fields["llm_status"] = "pending"

        bill_id = db.upsert_bill(conn, congress, bill_type, number, **fields)

        if text is not None and text_changed:
            db.store_bill_text(
                conn,
                bill_id,
                fields["text_hash"],
                text,
                text_version_type=source["type"],
                source_url=source["url"],
            )

        sponsors = [
            {
                "bioguide_id": s["bioguideId"],
                "full_name": s.get("fullName") or s.get("firstName", ""),
                "party": s.get("party"),
                "state": s.get("state"),
                "role": "sponsor",
            }
            for s in detail.get("sponsors") or []
            if s.get("bioguideId")
        ] + [
            {
                "bioguide_id": c["bioguideId"],
                "full_name": c.get("fullName") or c.get("firstName", ""),
                "party": c.get("party"),
                "state": c.get("state"),
                "role": "cosponsor",
                "sponsored_date": c.get("sponsorshipDate"),
            }
            for c in cosponsors_raw
            if c.get("bioguideId")
        ]
        db.replace_sponsors(conn, bill_id, sponsors)

        db.replace_actions(
            conn,
            bill_id,
            [
                {
                    "action_date": a["actionDate"],
                    "action_text": a.get("text") or "",
                    "action_type": a.get("type"),
                    "chamber": _action_chamber(a),
                }
                for a in actions_raw
                if a.get("actionDate")
            ],
        )
        db.replace_committees(
            conn,
            bill_id,
            [
                {
                    "committee_name": c["name"],
                    "chamber": (c.get("chamber") or "").lower() or None,
                }
                for c in committees_raw
                if c.get("name")
            ],
        )

    if existing is None:
        return "created"
    return "updated (new text)" if text_changed else "updated"


# --------------------------------------------------------------------------
# Incremental sync
# --------------------------------------------------------------------------
def sync_updated_bills(
    conn: psycopg.Connection,
    api_key: str,
    congress: int,
    since: str,
    limit: int | None = None,
) -> dict[str, int]:
    """Refresh every bill updated on Congress.gov since ``since``.

    Walks the list endpoint oldest-update-first and skips bills whose
    stored congress_update_date already matches, so an interrupted run
    resumes cheaply.
    """
    counts = {"seen": 0, "refreshed": 0, "skipped": 0, "failed": 0}
    listing = paginate(
        f"bill/{congress}",
        api_key,
        "bills",
        fromDateTime=since,
        sort="updateDate+asc",
    )
    for item in listing:
        bill_type = (item.get("type") or "").lower()
        number = int(item.get("number") or 0)
        if bill_type not in _VALID_TYPES or not number:
            continue
        counts["seen"] += 1
        existing = db.get_bill(conn, congress, bill_type, number)
        remote_updated = _parse_api_datetime(item.get("updateDate"))
        if (
            existing
            and remote_updated
            and existing["congress_update_date"] is not None
            and existing["congress_update_date"] >= remote_updated
        ):
            counts["skipped"] += 1
            continue
        try:
            status = refresh_bill(conn, api_key, congress, bill_type, number)
            counts["refreshed"] += 1
            print(f"  {status}: {congress}-{bill_type}-{number}")
        except Exception as exc:  # one bad bill must not sink the sync
            counts["failed"] += 1
            print(
                f"  FAILED {congress}-{bill_type}-{number}: {exc}",
                file=sys.stderr,
            )
        if limit and counts["refreshed"] >= limit:
            break
    return counts


_VALID_TYPES = {"hr", "s", "hjres", "sjres", "hconres", "sconres", "hres", "sres"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest bills from Congress.gov into PostgreSQL."
    )
    parser.add_argument("--congress", type=int, default=119)
    parser.add_argument(
        "--bills",
        default=None,
        help="Comma-separated bills like 'hr-2138,s-129' for a full refresh.",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="ISO timestamp (e.g. 2026-07-01T00:00:00Z): refresh every "
        "bill Congress.gov updated since then.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop an incremental sync after refreshing this many bills.",
    )
    args = parser.parse_args(argv)
    load_dotenv()

    api_key = os.environ.get("CONGRESS_API_KEY")
    if not api_key:
        print("CONGRESS_API_KEY is not set.", file=sys.stderr)
        return 1
    if not args.bills and not args.since:
        print("Provide --bills or --since.", file=sys.stderr)
        return 1

    with db.connect() as conn:
        if args.bills:
            failed = 0
            for item in args.bills.split(","):
                bill_type, _, number = item.strip().lower().rpartition("-")
                if bill_type not in _VALID_TYPES or not number.isdigit():
                    raise SystemExit(
                        f"--bills entries must look like 'hr-2138'; got '{item}'"
                    )
                try:
                    status = refresh_bill(
                        conn, api_key, args.congress, bill_type, int(number)
                    )
                    print(f"  {status}: {args.congress}-{bill_type}-{number}")
                except Exception as exc:
                    failed += 1
                    print(f"  FAILED {item.strip()}: {exc}", file=sys.stderr)
            return 1 if failed else 0

        counts = sync_updated_bills(
            conn, api_key, args.congress, args.since, args.limit
        )
        print(
            f"\nSync done: {counts['refreshed']} refreshed, "
            f"{counts['skipped']} skipped, {counts['failed']} failed "
            f"({counts['seen']} listed)."
        )
        return 1 if counts["failed"] and not counts["refreshed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

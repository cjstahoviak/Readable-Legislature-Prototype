"""PostgreSQL persistence for the pipeline jobs (psycopg 3).

All functions take an open connection and run inside the caller's
transaction; jobs wrap each bill in ``with conn.transaction():`` so a
failure rolls back that bill only. Column names for the dynamic bill
upsert are checked against an allowlist — never pass user input as a
field name.
"""

from __future__ import annotations

import os
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# Updatable columns on bills (everything except id/created_at and the
# natural key, which is the conflict target).
BILL_COLUMNS = frozenset(
    {
        "title",
        "stage",
        "policy_area",
        "introduced_date",
        "source_url",
        "latest_action_text",
        "latest_action_date",
        "congress_update_date",
        "last_fetched_at",
        "text_hash",
        "text_version_type",
        "text_source_url",
        "llm_status",
        "llm_model",
        "llm_prompt_version",
        "llm_samples",
        "llm_processed_at",
        "scored_text_hash",
        "summary_tldr",
        "summary_overview",
    }
)


def connect(url: str | None = None) -> psycopg.Connection:
    """Open a connection from ``DATABASE_URL`` with dict rows."""
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and "
            "point it at your database."
        )
    return psycopg.connect(url, row_factory=dict_row)


def upsert_bill(
    conn: psycopg.Connection,
    congress: int,
    bill_type: str,
    bill_number: int,
    **fields: Any,
) -> int:
    """Insert or update a bill by its natural key; returns bills.id.

    Only the provided ``fields`` are updated on conflict, so partial
    writers (the loader, the scoring job) never clobber columns owned
    by other jobs.
    """
    unknown = set(fields) - BILL_COLUMNS
    if unknown:
        raise ValueError(f"unknown bill column(s): {sorted(unknown)}")

    cols = ["congress", "bill_type", "bill_number", *fields]
    values = {
        "congress": congress,
        "bill_type": bill_type,
        "bill_number": bill_number,
        **fields,
    }
    updates = [
        sql.SQL("{col} = EXCLUDED.{col}").format(col=sql.Identifier(c))
        for c in fields
    ]
    updates.append(sql.SQL("updated_at = now()"))
    stmt = sql.SQL(
        "INSERT INTO bills ({cols}) VALUES ({vals}) "
        "ON CONFLICT (congress, bill_type, bill_number) "
        "DO UPDATE SET {updates} RETURNING id"
    ).format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        vals=sql.SQL(", ").join(sql.Placeholder(c) for c in cols),
        updates=sql.SQL(", ").join(updates),
    )
    row = conn.execute(stmt, values).fetchone()
    return row["id"]


def get_bill(
    conn: psycopg.Connection, congress: int, bill_type: str, bill_number: int
) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT * FROM bills WHERE congress = %s AND bill_type = %s "
        "AND bill_number = %s",
        (congress, bill_type, bill_number),
    ).fetchone()


def store_bill_text(
    conn: psycopg.Connection,
    bill_id: int,
    text_hash: str,
    content: str,
    text_version_type: str | None = None,
    source_url: str | None = None,
) -> None:
    """Keep every text version we have seen, keyed by content hash."""
    conn.execute(
        "INSERT INTO bill_texts "
        "(bill_id, text_hash, text_version_type, source_url, content) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (bill_id, text_hash) DO NOTHING",
        (bill_id, text_hash, text_version_type, source_url, content),
    )


def get_bill_text(
    conn: psycopg.Connection, bill_id: int, text_hash: str
) -> str | None:
    row = conn.execute(
        "SELECT content FROM bill_texts WHERE bill_id = %s AND text_hash = %s",
        (bill_id, text_hash),
    ).fetchone()
    return row["content"] if row else None


def upsert_legislator(
    conn: psycopg.Connection,
    bioguide_id: str,
    full_name: str,
    party: str | None = None,
    state: str | None = None,
    chamber: str | None = None,
) -> int:
    """Insert or refresh a legislator by bioguide id; returns its id."""
    row = conn.execute(
        "INSERT INTO legislators (bioguide_id, full_name, party, state, chamber) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (bioguide_id) DO UPDATE SET "
        "  full_name = EXCLUDED.full_name, "
        "  party = COALESCE(EXCLUDED.party, legislators.party), "
        "  state = COALESCE(EXCLUDED.state, legislators.state), "
        "  chamber = COALESCE(EXCLUDED.chamber, legislators.chamber) "
        "RETURNING id",
        (bioguide_id, full_name, party, state, chamber),
    ).fetchone()
    return row["id"]


def replace_sponsors(
    conn: psycopg.Connection, bill_id: int, sponsors: list[dict[str, Any]]
) -> None:
    """Replace a bill's sponsor rows (legislators are upserted).

    Each entry: bioguide_id, full_name, role ('sponsor'/'cosponsor'),
    and optional party/state/chamber/sponsored_date.
    """
    conn.execute("DELETE FROM bill_sponsors WHERE bill_id = %s", (bill_id,))
    seen: dict[str, int] = {}
    for s in sponsors:
        bioguide = s["bioguide_id"]
        if bioguide not in seen:
            seen[bioguide] = upsert_legislator(
                conn,
                bioguide,
                s["full_name"],
                s.get("party"),
                s.get("state"),
                s.get("chamber"),
            )
        conn.execute(
            "INSERT INTO bill_sponsors (bill_id, legislator_id, role, sponsored_date) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (bill_id, seen[bioguide], s["role"], s.get("sponsored_date")),
        )


def replace_actions(
    conn: psycopg.Connection, bill_id: int, actions: list[dict[str, Any]]
) -> None:
    """Replace a bill's action history (the API has no stable ids)."""
    conn.execute(
        "DELETE FROM bill_status_history WHERE bill_id = %s", (bill_id,)
    )
    for a in actions:
        conn.execute(
            "INSERT INTO bill_status_history "
            "(bill_id, action_date, action_text, action_type, chamber) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                bill_id,
                a["action_date"],
                a["action_text"],
                a.get("action_type"),
                a.get("chamber"),
            ),
        )


def replace_committees(
    conn: psycopg.Connection, bill_id: int, committees: list[dict[str, Any]]
) -> None:
    conn.execute("DELETE FROM bill_committees WHERE bill_id = %s", (bill_id,))
    for c in committees:
        conn.execute(
            "INSERT INTO bill_committees (bill_id, committee_name, chamber) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (bill_id, c["committee_name"], c.get("chamber")),
        )


def replace_scores(
    conn: psycopg.Connection,
    bill_id: int,
    scores: dict[str, dict[str, Any]],
    dimensions: list[str] | None = None,
) -> None:
    """Replace score rows for the given dimensions (all if None).

    ``scores`` is the pipeline shape: dimension id -> value id ->
    {score, reason, agreement?, votes?}.
    """
    dims = list(scores) if dimensions is None else dimensions
    conn.execute(
        "DELETE FROM bill_impact_scores WHERE bill_id = %s "
        "AND dimension_key = ANY(%s)",
        (bill_id, dims),
    )
    for dim_id in dims:
        for value_id, entry in (scores.get(dim_id) or {}).items():
            if not isinstance(entry, dict) or entry.get("score") not in (0, 1, 2):
                continue  # validation reports it; don't store junk
            votes = entry.get("votes")
            conn.execute(
                "INSERT INTO bill_impact_scores "
                "(bill_id, dimension_key, value_key, score, reason, agreement, votes) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    bill_id,
                    dim_id,
                    value_id,
                    entry["score"],
                    entry.get("reason", ""),
                    entry.get("agreement"),
                    Jsonb(votes) if votes is not None else None,
                ),
            )


def replace_target_groups(
    conn: psycopg.Connection, bill_id: int, groups: list[dict[str, Any]]
) -> None:
    """Replace a bill's target groups, conditions, and criteria."""
    conn.execute(
        "DELETE FROM bill_target_groups WHERE bill_id = %s", (bill_id,)
    )
    for g in groups:
        row = conn.execute(
            "INSERT INTO bill_target_groups (bill_id, reason, agreement) "
            "VALUES (%s, %s, %s) RETURNING id",
            (bill_id, g.get("reason", ""), g.get("agreement")),
        ).fetchone()
        for cond in g.get("conditions", []):
            conn.execute(
                "INSERT INTO bill_target_group_conditions "
                "(group_id, dimension_key, value_key) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (row["id"], cond["dimension"], cond["value"]),
            )
        for crit in g.get("other_criteria", []) or []:
            conn.execute(
                "INSERT INTO bill_target_group_criteria "
                "(group_id, criterion_text) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (row["id"], crit),
            )


def score_coverage(
    conn: psycopg.Connection, bill_id: int
) -> dict[str, set[str]]:
    """Value ids currently scored per dimension, for gap detection."""
    coverage: dict[str, set[str]] = {}
    rows = conn.execute(
        "SELECT dimension_key, value_key FROM bill_impact_scores "
        "WHERE bill_id = %s",
        (bill_id,),
    ).fetchall()
    for r in rows:
        coverage.setdefault(r["dimension_key"], set()).add(r["value_key"])
    return coverage


def target_group_count(conn: psycopg.Connection, bill_id: int) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM bill_target_groups WHERE bill_id = %s",
        (bill_id,),
    ).fetchone()
    return row["n"]

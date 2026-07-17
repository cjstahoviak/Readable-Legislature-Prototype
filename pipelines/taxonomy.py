"""Load the demographic taxonomy (taxonomy.yaml at the repo root).

The taxonomy is the single source of truth for the scoring rubric and
every dimension/value id. Stored scores reference these ids, so ids
are contracts — see the header comments in taxonomy.yaml.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

TAXONOMY_PATH = Path(__file__).resolve().parent.parent / "taxonomy.yaml"


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

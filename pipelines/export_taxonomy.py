"""Export taxonomy.yaml as JSON for the web app.

The web UI must render dimensions and values from the same source of
truth the pipeline scores against, so this emits a machine-readable
subset of taxonomy.yaml: the scoring scale (for the methodology page)
and every dimension/value with its display fields. Model-facing fields
(scoring rules, per-dimension guidance) are deliberately excluded.

Run as ``python -m pipelines.export_taxonomy [--out PATH]``; writes to
stdout by default. The web build (Phase 2) runs this and imports the
generated file — never hand-copy taxonomy values into the frontend.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .taxonomy import load_taxonomy


def build_export(taxonomy: dict[str, Any]) -> dict[str, Any]:
    """Shape the taxonomy for frontend consumption (order preserved)."""
    scale = [
        {
            "value": level["value"],
            "label": level["label"],
            "definition": " ".join(level["definition"].split()),
        }
        for level in taxonomy["scoring"]["scale"]
    ]
    dimensions = []
    for dim in taxonomy["dimensions"]:
        values = []
        for v in dim["values"]:
            entry: dict[str, Any] = {"id": v["id"], "label": v["label"]}
            desc = v.get("description")
            if desc:
                entry["description"] = " ".join(desc.split())
            # score_complement: false marks negative-space values the
            # pipeline never scores; the UI can offer them as explicit
            # "not X" selections that simply contribute no matches.
            entry["scored"] = v.get("score_complement", True) is not False
            values.append(entry)
        dimensions.append(
            {
                "id": dim["id"],
                "label": dim["label"],
                "type": dim["type"],
                "values": values,
            }
        )
    return {"scale": scale, "dimensions": dimensions}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export taxonomy.yaml as JSON for the web app."
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output file path (default: stdout).",
    )
    args = parser.parse_args(argv)

    export = build_export(load_taxonomy())
    rendered = json.dumps(export, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

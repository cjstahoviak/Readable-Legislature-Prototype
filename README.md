# Readable Legislature — Scoring Prototype

Throwaway prototype that scores a single congressional bill against the
demographic taxonomy in [`taxonomy.yaml`](taxonomy.yaml) to check that the
0/1/2 impact rubric produces a **sensible, differentiated** spread — not
everything hedged to `1`.

It fetches a bill and its text from the Congress.gov API, makes **one Claude
call per taxonomy dimension** (scoring all of that dimension's values together
so they calibrate against each other), validates the output against the
taxonomy, and writes one human-readable JSON file to `out/`.

No database — JSON only. See `PROTOTYPE_HANDOFF.md` for the full brief.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then edit .env and fill in both keys
```

- `CONGRESS_API_KEY` — free from <https://api.data.gov>
- `ANTHROPIC_API_KEY` — from <https://console.anthropic.com>

## Run

```bash
# Defaults to H.R. 2138 (119th Congress), model claude-opus-4-8
python score_bill.py

# Any bill (bill-type: hr, s, hjres, sjres, hconres, sconres, hres, sres):
python score_bill.py --congress 119 --bill-type s --number 5
```

Flags:

| Flag | Effect |
|---|---|
| `--model <id>` | Override the Claude model. |
| `--no-thinking` | Disable adaptive extended thinking. |
| `--include-complement` | Also score `score_complement: false` values. |
| `--max-chars <n>` | Safety cap; truncate very long bill text. |

Output: `out/<congress>-<bill_type>-<number>.json`, plus a score summary and
token/cache usage printed to the console.

## What a good run looks like

- A real spread in the console summary (some `2`s, some `1`s, many `0`s) —
  **not** every value scored `1`.
- `Validation: all dimensions OK.` — structured output matched the taxonomy's
  value ids and only used scores in `{0,1,2}`.
- `cache_read` > 0 on dimensions after the first — the bill text is cached
  across all 13 dimension calls.

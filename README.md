# Readable Legislature

Civic tech project (working name) that parses congressional bills from
the Congress.gov API into plain-language summaries and per-demographic
impact scores, so a visitor can filter by their own demographics and see
which bills matter to them. **Core principle: relevance, not verdicts** —
scores say which bills touch a group; readers judge good or bad themselves.

## Repository layout

```
├── pipelines/      # Python jobs: Congress.gov fetch + LLM scoring
├── db/migrations/  # ordered SQL migrations (dbmate format)
├── tests/          # offline tests (prompts, aggregation, validation)
├── out/            # scored-bill JSON files; doubles as golden fixtures
├── taxonomy.yaml   # single source of truth: dimensions + scoring rubric
├── docker-compose.yml  # local PostgreSQL
└── web/            # Next.js app (coming in a later phase)
```

`taxonomy.yaml` defines 16 demographic dimensions and the 0/1/2 scoring
contract. Dimension/value ids are storage contracts — see the header
comments in that file before editing.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in the keys

# Local database (optional until the DB-backed jobs land):
docker compose up -d
dbmate up                 # applies db/migrations; install from
                          # https://github.com/amacneil/dbmate
```

- `CONGRESS_API_KEY` — free from <https://api.data.gov>
- `ANTHROPIC_API_KEY` — from <https://console.anthropic.com>
- `DATABASE_URL` — defaults to the docker-compose database

## Pipeline jobs

```bash
# Ingest bills from Congress.gov into the database:
python -m pipelines.ingest --congress 119 --bills hr-2138,s-5   # specific
python -m pipelines.ingest --congress 119 --since 2026-07-01T00:00:00Z  # incremental

# Score DB bills that are pending / partial / stale (budget-capped):
python -m pipelines.score_pending --max-bills 25 [--samples 3] [--dry-run]

# Backfill the database from the prototype's file outputs:
python -m pipelines.load_outputs

# Compare a cheaper model against the golden outputs in out/:
python -m pipelines.eval_models --model claude-haiku-4-5 --samples 1
```

Ingestion pulls metadata, sponsors, actions, committees, and bill text
(hashed; a text change drops the bill back to `llm_status = 'pending'`).
The scoring job runs only the missing LLM work per bill — unscored
dimensions, absent target groups, absent summaries — so a partially
failed bill is retried piecemeal, and a prompt-version bump re-scores
active bills first under `--max-bills`.

## Score bills to files (no database)

```bash
# Defaults to H.R. 2138 (119th Congress), model claude-opus-4-8
python -m pipelines.score_bill

# Any bills (types: hr, s, hjres, sjres, hconres, sconres, hres, sres):
python -m pipelines.score_bill --bills hr-2138,s-129 --samples 3
```

Makes one Claude call per taxonomy dimension (all of a dimension's
values scored together so they calibrate against each other), one call
extracting the bill's explicitly targeted groups, and one producing the
plain-language summaries, validates the structured output against the
taxonomy, and writes one JSON file per bill to `out/`.

| Flag | Effect |
|---|---|
| `--samples <n>` | Resampling: majority-vote scores + agreement ratio. |
| `--model <id>` | Override the Claude model. |
| `--concurrency <n>` | Parallel API calls per bill (default 4). |
| `--no-thinking` | Disable adaptive extended thinking. |
| `--no-target-groups` | Skip the target-group extraction call. |
| `--no-summary` | Skip the plain-language summary call. |
| `--include-complement` | Also score `score_complement: false` values. |
| `--max-chars <n>` | Safety cap; truncate very long bill text. |

### What a good run looks like

- A real spread in the console summary (some `2`s, some `1`s, many `0`s)
  — **not** every value hedged to `1`.
- `Validation: all sections OK.` — output matched the taxonomy's value
  ids and only used scores in `{0,1,2}`.
- `cache_read > 0` after the first call — the bill text is cached across
  all dimension calls.

## Export the taxonomy for the web app

```bash
python -m pipelines.export_taxonomy --out web/lib/taxonomy.generated.json
```

The frontend must render dimensions/values from this generated file,
never from hand-copied constants.

## Tests

```bash
python -m pytest

# Include the database round-trip tests:
TEST_DATABASE_URL=postgres://... python -m pytest
```

Runs offline — prompt construction, resampling aggregation, stage
derivation, eval comparison, and validation are exercised against the
real `taxonomy.yaml`, with the committed outputs in `out/` as golden
fixtures. Setting `TEST_DATABASE_URL` (a database with the migrations
applied) adds loader/persistence round-trip tests.

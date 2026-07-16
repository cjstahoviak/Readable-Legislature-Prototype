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

## Score bills

```bash
# Defaults to H.R. 2138 (119th Congress), model claude-opus-4-8
python -m pipelines.score_bill

# Any bills (types: hr, s, hjres, sjres, hconres, sconres, hres, sres):
python -m pipelines.score_bill --bills hr-2138,s-129 --samples 3
```

Makes one Claude call per taxonomy dimension (all of a dimension's
values scored together so they calibrate against each other) plus one
call extracting the bill's explicitly targeted groups, validates the
structured output against the taxonomy, and writes one JSON file per
bill to `out/`.

| Flag | Effect |
|---|---|
| `--samples <n>` | Resampling: majority-vote scores + agreement ratio. |
| `--model <id>` | Override the Claude model. |
| `--concurrency <n>` | Parallel API calls per bill (default 4). |
| `--no-thinking` | Disable adaptive extended thinking. |
| `--no-target-groups` | Skip the target-group extraction call. |
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
```

Offline only — prompt construction, resampling aggregation, and
validation are exercised against the real `taxonomy.yaml`, with the
committed outputs in `out/` as golden fixtures.

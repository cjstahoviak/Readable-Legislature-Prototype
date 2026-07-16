-- Initial schema for Readable Legislature.
--
-- Conventions:
--   * dimension_key / value_key columns reference ids in taxonomy.yaml
--     (the single source of truth). They cannot be foreign keys because
--     the taxonomy lives in the repo, not the database — the pipeline
--     validates ids against the YAML before writing.
--   * Enum-like columns use TEXT + CHECK rather than CREATE TYPE so the
--     value set can change in a plain ALTER without type surgery.
--   * Child tables cascade on bill deletion; a bill row owns its data.

-- migrate:up

CREATE TABLE bills (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    congress             SMALLINT NOT NULL,
    bill_type            TEXT NOT NULL CHECK (bill_type IN
                           ('hr','s','hjres','sjres','hconres','sconres','hres','sres')),
    bill_number          INTEGER NOT NULL,
    title                TEXT,

    -- Coarse lifecycle stage driving the UI's status tabs/badges; the
    -- ingestion job derives it from the action history.
    stage                TEXT NOT NULL DEFAULT 'introduced' CHECK (stage IN
                           ('introduced','committee','floor','passed_house',
                            'passed_senate','to_president','enacted','vetoed','failed')),
    policy_area          TEXT,
    introduced_date      DATE,
    source_url           TEXT,        -- congress.gov bill page
    latest_action_text   TEXT,
    latest_action_date   DATE,

    -- Ingestion bookkeeping. congress_update_date is the API's
    -- updateDate, used for incremental sync (skip unchanged bills).
    congress_update_date TIMESTAMPTZ,
    last_fetched_at      TIMESTAMPTZ,

    -- Latest fetched text version; content lives in bill_texts.
    -- text_hash is the sha256 of the extracted plain text.
    text_hash            TEXT,
    text_version_type    TEXT,
    text_source_url      TEXT,

    -- LLM processing state. 'partial' means at least one dimension or
    -- the target-group extraction is missing and should be retried.
    -- Re-scoring is due whenever scored_text_hash <> text_hash or the
    -- prompt/model version moves.
    llm_status           TEXT NOT NULL DEFAULT 'pending' CHECK (llm_status IN
                           ('pending','partial','complete','failed')),
    llm_model            TEXT,
    llm_prompt_version   TEXT,
    llm_samples          SMALLINT,
    llm_processed_at     TIMESTAMPTZ,
    scored_text_hash     TEXT,

    summary_tldr         TEXT,        -- one sentence, bill cards
    summary_overview     TEXT,        -- a few paragraphs, detail page

    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (congress, bill_type, bill_number)
);

CREATE INDEX bills_llm_status_idx ON bills (llm_status);
CREATE INDEX bills_stage_idx ON bills (stage);
CREATE INDEX bills_latest_action_date_idx
    ON bills (latest_action_date DESC NULLS LAST);

-- Extracted plain text of each bill text version we have fetched,
-- keyed by content hash. Keeping the text lets the LLM stage re-run
-- after prompt/model changes without re-fetching from Congress.gov,
-- and scored_text_hash on bills joins to exactly the text scored.
CREATE TABLE bill_texts (
    bill_id           BIGINT NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    text_hash         TEXT NOT NULL,
    text_version_type TEXT,
    source_url        TEXT,
    content           TEXT NOT NULL,
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (bill_id, text_hash)
);

CREATE TABLE legislators (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bioguide_id TEXT NOT NULL UNIQUE,
    full_name   TEXT NOT NULL,
    party       TEXT,
    state       TEXT,
    chamber     TEXT CHECK (chamber IN ('house','senate'))
);

CREATE TABLE bill_sponsors (
    bill_id        BIGINT NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    legislator_id  BIGINT NOT NULL REFERENCES legislators(id) ON DELETE CASCADE,
    role           TEXT NOT NULL CHECK (role IN ('sponsor','cosponsor')),
    sponsored_date DATE,
    PRIMARY KEY (bill_id, legislator_id)
);

-- The bill's action history (status timeline on the detail page).
-- Congress.gov actions carry no stable ids, so the ingestion job
-- replaces a bill's rows wholesale inside a transaction on update.
CREATE TABLE bill_status_history (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bill_id     BIGINT NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    action_date DATE NOT NULL,
    action_text TEXT NOT NULL,
    action_type TEXT,
    chamber     TEXT
);

CREATE INDEX bill_status_history_bill_idx
    ON bill_status_history (bill_id, action_date);

CREATE TABLE bill_committees (
    bill_id        BIGINT NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    committee_name TEXT NOT NULL,
    chamber        TEXT,
    PRIMARY KEY (bill_id, committee_name)
);

-- One row per (bill, taxonomy value): the 0/1/2 relevance score with
-- the user-facing reason. agreement/votes come from resampling
-- (samples > 1): agreement is the share of runs voting for the stored
-- score, votes the full distribution, e.g. {"0": 1, "2": 2}.
CREATE TABLE bill_impact_scores (
    bill_id       BIGINT NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    dimension_key TEXT NOT NULL,
    value_key     TEXT NOT NULL,
    score         SMALLINT NOT NULL CHECK (score IN (0, 1, 2)),
    reason        TEXT NOT NULL DEFAULT '',
    agreement     NUMERIC(3,2) CHECK (agreement BETWEEN 0 AND 1),
    votes         JSONB,
    PRIMARY KEY (bill_id, dimension_key, value_key)
);

-- Ranking query: WHERE (dimension_key, value_key) IN (<selections>)
-- AND score > 0 GROUP BY bill_id — most stored scores are 0, so a
-- partial index keeps it tight.
CREATE INDEX bill_impact_scores_ranking_idx
    ON bill_impact_scores (dimension_key, value_key, bill_id, score)
    WHERE score > 0;

-- Populations the bill explicitly targets, each a conjunction of
-- taxonomy values ("veteran AND has_disability"). A user matches a
-- group when their selections satisfy EVERY condition — the strongest
-- ranking signal. agreement is the share of resampled runs that
-- produced the group.
CREATE TABLE bill_target_groups (
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bill_id   BIGINT NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    reason    TEXT NOT NULL DEFAULT '',
    agreement NUMERIC(3,2) CHECK (agreement BETWEEN 0 AND 1)
);

CREATE INDEX bill_target_groups_bill_idx ON bill_target_groups (bill_id);

-- A group with no condition rows applies to essentially everyone.
CREATE TABLE bill_target_group_conditions (
    group_id      BIGINT NOT NULL REFERENCES bill_target_groups(id) ON DELETE CASCADE,
    dimension_key TEXT NOT NULL,
    value_key     TEXT NOT NULL,
    PRIMARY KEY (group_id, dimension_key, value_key)
);

CREATE INDEX bill_target_group_conditions_value_idx
    ON bill_target_group_conditions (dimension_key, value_key);

-- Targeting constraints the taxonomy cannot express ("receives VA
-- disability compensation"). Display only — never matched.
CREATE TABLE bill_target_group_criteria (
    group_id       BIGINT NOT NULL REFERENCES bill_target_groups(id) ON DELETE CASCADE,
    criterion_text TEXT NOT NULL,
    PRIMARY KEY (group_id, criterion_text)
);

-- migrate:down

DROP TABLE bill_target_group_criteria;
DROP TABLE bill_target_group_conditions;
DROP TABLE bill_target_groups;
DROP TABLE bill_impact_scores;
DROP TABLE bill_committees;
DROP TABLE bill_status_history;
DROP TABLE bill_sponsors;
DROP TABLE legislators;
DROP TABLE bill_texts;
DROP TABLE bills;

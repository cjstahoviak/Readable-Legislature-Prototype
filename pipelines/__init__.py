"""Readable Legislature data pipelines.

Python jobs that fetch congressional bills from the Congress.gov API,
score them against the demographic taxonomy with Claude, and (from
Phase 1) persist the results to PostgreSQL.

Modules:
    taxonomy    -- load taxonomy.yaml, the single source of truth
    congress    -- Congress.gov API client and bill-text extraction
    prompts     -- prompt and JSON-schema construction for LLM calls
    scoring     -- Claude calls, resampling aggregation, orchestration
    validation  -- structured-output checks against the taxonomy
    score_bill  -- CLI: score bills and write JSON files to out/
    export_taxonomy -- emit taxonomy.yaml as JSON for the web app
"""

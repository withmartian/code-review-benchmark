"""Database schema DDL — written in Postgres-native SQL, auto-translated for SQLite."""

from __future__ import annotations

from db.connection import DBAdapter

TABLES = [
    """
    CREATE TABLE IF NOT EXISTS chatbots (
        id              SERIAL PRIMARY KEY,
        github_username TEXT NOT NULL UNIQUE,
        display_name    TEXT,
        created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS prs (
        id                  SERIAL PRIMARY KEY,
        chatbot_id          INTEGER NOT NULL REFERENCES chatbots(id),
        repo_name           TEXT NOT NULL,
        pr_number           INTEGER NOT NULL,
        pr_url              TEXT NOT NULL,
        pr_title            TEXT DEFAULT '',
        pr_author           TEXT,
        pr_created_at       TIMESTAMPTZ,
        pr_merged           BOOLEAN,

        -- Processing status
        status              TEXT NOT NULL DEFAULT 'pending',
        enrichment_step     TEXT DEFAULT NULL,

        -- All data stored as TEXT (JSON strings)
        bq_events           TEXT,
        commits             TEXT,
        reviews             TEXT,
        review_threads      TEXT,
        commit_details      TEXT,
        assembled           TEXT,

        -- Concurrency
        locked_by           TEXT,
        locked_at           TIMESTAMPTZ,
        error_message       TEXT,

        -- Timestamps
        bot_reviewed_at     TIMESTAMPTZ,
        discovered_at       TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        enriched_at         TIMESTAMPTZ,
        assembled_at        TIMESTAMPTZ,
        analyzed_at         TIMESTAMPTZ,

        UNIQUE(chatbot_id, repo_name, pr_number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_analyses (
        id                  SERIAL PRIMARY KEY,
        pr_id               INTEGER NOT NULL REFERENCES prs(id),
        chatbot_id          INTEGER NOT NULL REFERENCES chatbots(id),
        bot_suggestions     TEXT NOT NULL,
        human_actions       TEXT NOT NULL,
        matching_results    TEXT NOT NULL,
        total_bot_comments  INTEGER,
        matched_bot_comments INTEGER,
        precision           REAL,
        recall              REAL,
        f_beta              REAL,
        model_name          TEXT,
        analyzed_at         TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(pr_id, chatbot_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pr_labels (
        id          SERIAL PRIMARY KEY,
        pr_id       INTEGER NOT NULL REFERENCES prs(id),
        chatbot_id  INTEGER NOT NULL REFERENCES chatbots(id),
        labels      TEXT NOT NULL,
        model_name  TEXT,
        labeled_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(pr_id, chatbot_id)
    )
    """,
]

# Indexes for common query patterns
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_prs_status ON prs(status)",
    "CREATE INDEX IF NOT EXISTS idx_prs_chatbot ON prs(chatbot_id)",
    "CREATE INDEX IF NOT EXISTS idx_prs_enrichment ON prs(status, enrichment_step)",
    "CREATE INDEX IF NOT EXISTS idx_llm_analyses_pr ON llm_analyses(pr_id)",
    "CREATE INDEX IF NOT EXISTS idx_llm_analyses_chatbot ON llm_analyses(chatbot_id)",
    "CREATE INDEX IF NOT EXISTS idx_pr_labels_pr ON pr_labels(pr_id)",
]


async def create_tables(db: DBAdapter) -> None:
    """Create all tables and indexes, translating DDL for the target database."""
    for ddl in TABLES:
        await db.execute(db.translate_ddl(ddl))
    for idx in INDEXES:
        await db.execute(idx)

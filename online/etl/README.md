# PR Review Dataset — ETL Pipeline

Data pipeline for the [online code review benchmark](../README.md). Continuously discovers PRs that code review bots have reviewed, fetches full PR data from GitHub, and uses an LLM to analyze whether the bot's suggestions identified real issues and whether developers acted on them.

Discovers merged PRs reviewed by a chatbot via the GitHub Search API (`reviewed-by:<bot> is:merged`), enriches them with GitHub API data, assembles a unified timeline, and runs LLM analysis. A BigQuery / GH Archive fallback is available via `--source bq`. Everything is stored in a database (SQLite or PostgreSQL).

## Setup

```bash
cd etl
uv sync
cp .env.example .env  # fill in values
```

You need:
- A GitHub personal access token with `public_repo` read access
- (Optional) A GCP project with BigQuery access, only needed for `--source bq` fallback and `gcloud auth application-default login`

## Environment

Key variables in `.env`:

| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | SQLite or PostgreSQL URL | `sqlite:///pr_review.db` |
| `GITHUB_TOKEN` | GitHub personal access token | |
| `GCP_PROJECT` | GCP project for BigQuery billing | |
| `MAX_PR_COMMITS` | Skip PRs with more commits | `50` |
| `MAX_PR_CHANGED_LINES` | Skip PRs with more added+deleted lines | `2000` |

## Using PostgreSQL (Cloud SQL)

SQLite works out of the box. For production, use Cloud SQL for PostgreSQL.

### 1. Create a Cloud SQL instance

Create a PostgreSQL instance in GCP with public IP enabled.

### 2. Install and run the Cloud SQL Auth Proxy

The proxy tunnels through your GCP credentials — no IP whitelisting needed.

```bash
# macOS
brew install cloud-sql-proxy

# Linux
curl -o cloud-sql-proxy https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.15.2/cloud-sql-proxy.linux.amd64
chmod +x cloud-sql-proxy

# Run in a separate terminal (keep it running)
cloud-sql-proxy PROJECT:REGION:INSTANCE --port 5433
# e.g. cloud-sql-proxy $GCP_SQL_INSTANCE --port 5433
```

### 3. Update `.env`

```
DATABASE_URL=postgresql://USER:PASSWORD@127.0.0.1:5433/postgres
```

### 4. Test the connection

```bash
psql "host=127.0.0.1 port=5433 dbname=postgres user=USER password=PASSWORD"
```

Both `asyncpg` (pipeline) and `psycopg` (dashboard) connect through the proxy transparently.

## Commands

All commands run from the `etl/` directory.

### Discover PRs

```bash
# All chatbots, last 7 days (default: GitHub Search API)
uv run python main.py discover --all --days-back 7

# Single chatbot, specific date range
uv run python main.py discover \
  --chatbot "coderabbitai[bot]" \
  --start-date 2026-06-01 \
  --end-date 2026-08-01

# Use BigQuery / GH Archive fallback
uv run python main.py discover --all --days-back 7 --source bq
```

### Enrich PRs via GitHub API

```bash
uv run python main.py enrich \
  --chatbot "coderabbitai[bot]" \
  --one-shot --max-prs 50
```

PRs that exceed size limits are automatically marked as `skipped`. Override the defaults per-run:

```bash
uv run python main.py enrich \
  --chatbot "coderabbitai[bot]" \
  --max-pr-commits 100 \
  --max-pr-changed-lines 5000 \
  --one-shot
```

### Run as a daemon (enrich job)

```bash
uv run python -m jobs.enrich_job \
  --chatbot "coderabbitai[bot]" \
  --max-pr-commits 50 \
  --max-pr-changed-lines 2000
```

### Analyze with LLM

```bash
uv run python main.py analyze --chatbot "coderabbitai[bot]"
uv run python main.py analyze --all

# Only PRs reviewed in the last 7 days
uv run python main.py analyze --all --since 7d

# Specific date window
uv run python main.py analyze --all --since 2026-08-01 --until 2026-08-15
```

`--sort sweep` ignores `--since`/`--until` — it orders by `assembled_at DESC` to pick up PRs that were discovered long ago but only recently assembled (e.g., merged weeks after the bot reviewed them).

### Label PRs

```bash
uv run python main.py label --chatbot "coderabbitai[bot]" --limit 5
uv run python main.py label --all
uv run python main.py label --chatbot "coderabbitai[bot]" --since 7d

# Sweep mode: process by analyzed_at DESC to catch stragglers
uv run python main.py label --all --sort sweep --limit 20
```

### Fetch PR volumes

```bash
# All chatbots, last 7 days (uses GitHub Search API by default)
uv run python main.py volumes --all --days-back 7

# All chatbots, specific date range
uv run python main.py volumes --all \
  --start-date 2025-01-01 \
  --end-date 2026-02-25

# Single chatbot
uv run python main.py volumes \
  --chatbot "coderabbitai[bot]" \
  --days-back 30

# Use BigQuery source instead (legacy)
uv run python main.py volumes --all --source bq --days-back 7

# Weekly chunking (fewer API calls, less per-day accuracy)
uv run python main.py volumes --all --weekly --days-back 30
```

Default source is GitHub Search API (`--source search-api`), which counts PRs where the bot left a review. Each PR is counted exactly once on the earliest day the bot touched it. Days with no activity are zero-filled by the API service.

### Import legacy filesystem data

```bash
uv run python main.py import --output-dir output
```

### Dashboard

```bash
uv run python main.py dashboard
```

### Backfill commands

Utility commands for backfilling computed columns on existing data:

```bash
# Backfill diff_lines from stored PR data
uv run python main.py backfill

# Backfill pr_author from BQ events and commits
uv run python main.py backfill-pr-author --use-api

# Backfill pr_merged and repo_id from pr_api_raw
uv run python main.py backfill-metadata

# Fetch pr_api_raw from GitHub API for PRs missing it
uv run python main.py backfill-api-raw --limit 1000

# Compute engagement_signals for assembled PRs missing them
uv run python main.py backfill-engagement

# Recompute after actor-alias fixes (Copilot reviews as copilot-pull-request-reviewer)
uv run python main.py backfill-engagement --chatbot Copilot --force --since 60d
```

## PR Status Flow

```
pending → enriching → enriched → assembled → analyzed → labeled
                ↘ skipped (too large)          ↘ skipped (no completed review by the bot)
                ↘ error
```

## Resumability

Enrichment is resumable per-PR. Each PR tracks its `enrichment_step` — if interrupted, re-run the same command and it picks up where it left off.

## Review coverage

A fix only counts against a bot if the bot had a chance to review the code it changed (see [the methodology](../../methodology/full.md#online-benchmark)). The analysis works this out from the bot's reviews, comments and the commit diffs, with no LLM call:

- A fix of code written in commits the bot had not reviewed before the fix is left out of recall. A suggestion that matched only such fixes is left out of precision.
- A missed fix in a file the bot lists as excluded by the repository's path filters is left out of recall.
- A PR where the bot only posted a notice (rate limit, pause, skip) and never reviewed is marked `skipped`, with a reason in `error_message` that starts with `No completed review`.

To support this, the `threads` enrichment step also stores the PR's conversation comments and the edit history of the scored bot's comments, read in the same GraphQL request.

### Stored data

| Where | Field | Meaning |
|---|---|---|
| `prs` | `issue_comments` | The PR's conversation comments. `NULL` means never read. |
| `prs` | `issue_comment_edits` | Earlier revisions of the scored bot's edited comments. `NULL` means never read. |
| `llm_analyses.human_actions` | `excluded_from_recall` | Why a fix was left out of recall. |
| `llm_analyses.human_actions` | `unreviewed_commits` | The commits that wrote the fixed lines. |
| `llm_analyses.bot_suggestions` | `excluded_from_precision` | Why a suggestion was left out of precision. |

`precision`, `recall` and the comment counts are computed after these exclusions.

### Rows enriched before this change

Rows enriched earlier have no stored comments, so nothing is left out on them, and existing analyses are not re-scored. To apply the rule to an older window, reset those rows, which deletes their existing analyses:

```sql
UPDATE prs SET status = 'pending', enrichment_step = 'bq_events', assembled = NULL, assembled_at = NULL
WHERE chatbot_id = <id> AND bot_reviewed_at >= '<start>';
DELETE FROM llm_analyses WHERE pr_id IN (SELECT id FROM prs WHERE chatbot_id = <id> AND bot_reviewed_at >= '<start>');
```

Then run `main.py enrich` (which also assembles) and `main.py analyze`. This repeats the GitHub requests and the three LLM calls for each PR.

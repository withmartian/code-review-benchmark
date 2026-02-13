# PR Review Dataset Builder

Discovers PRs reviewed by a chatbot (via BigQuery), enriches them with GitHub API data, assembles a unified timeline, and runs LLM analysis. Everything is stored in a database (SQLite or PostgreSQL).

## Setup

```bash
cd pr-review-dataset
uv sync
cp .env.example .env  # fill in values
```

You need:
- A GCP project with BigQuery access (for querying `githubarchive.day.*`)
- A GitHub personal access token with `public_repo` read access
- `gcloud auth application-default login` (for BigQuery auth)

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
# e.g. cloud-sql-proxy feisty-gasket-486610-h2:us-central1:crb-main --port 5433
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

### Discover PRs from BigQuery

```bash
# Single chatbot
uv run python main.py discover \
  --chatbot "coderabbitai[bot]" \
  --start-date 2024-01-01 \
  --end-date 2025-01-01

# All chatbots in one BQ scan (uses DB chatbots, or built-in defaults)
uv run python main.py discover --all --days-back 7
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
```

### Label PRs

```bash
uv run python main.py label --chatbot "coderabbitai[bot]" --limit 5
uv run python main.py label --all
uv run python main.py label --chatbot "coderabbitai[bot]" --since 7d
```

### Import legacy filesystem data

```bash
uv run python main.py import --output-dir output
```

### Dashboard

```bash
uv run python main.py dashboard
```

## PR Status Flow

```
pending → enriching → enriched → assembled → analyzed
                ↘ skipped (too large)
                ↘ error
```

## Resumability

Enrichment is resumable per-PR. Each PR tracks its `enrichment_step` — if interrupted, re-run the same command and it picks up where it left off.

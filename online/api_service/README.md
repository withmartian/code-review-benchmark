# PR Review Dataset — API Service

Dashboard server for the [online code review benchmark](../README.md). Axum-based Rust API that serves the review dashboard. Loads PR analysis data from PostgreSQL into memory at startup and serves it via JSON endpoints with a built-in HTML dashboard.

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection URL | *(required)* |
| `BIND_ADDR` | Address to bind the HTTP server | `0.0.0.0:3000` |
| `RUST_LOG` | Log level filter | `info` |

## Build & Run

```bash
cd api_service

# Development
cargo run

# Release
cargo build --release
./target/release/pr-review-api
```

### Docker

```bash
cd api_service
docker build -t pr-review-api .
docker run -e DATABASE_URL=postgresql://... -p 3000:3000 pr-review-api
```

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | HTML dashboard (embedded) |
| `GET` | `/up` | Health check |
| `GET` | `/api/options` | Available filter options (chatbots, languages, domains, etc.) |
| `GET` | `/api/daily-metrics` | Daily time-series metrics with filtering |
| `GET` | `/api/leaderboard` | Chatbot leaderboard with filtering |
| `GET` | `/api/volumes` | PR volume per tool per day |

### Filter Parameters (for `/api/daily-metrics` and `/api/leaderboard`)

See [`../FILTERS.md`](../FILTERS.md) for the full spec with scopes, defaults, and composition examples.

| Parameter | Description |
|---|---|
| `start_date` | Start date (`YYYY-MM-DD`) |
| `end_date` | End date (`YYYY-MM-DD`) |
| `chatbot` | Comma-separated chatbot names |
| `language` | Comma-separated languages |
| `domain` | Comma-separated domains |
| `pr_type` | Comma-separated PR types |
| `severity` | Comma-separated severities |
| `diff_lines_min` | Minimum diff lines |
| `diff_lines_max` | Maximum diff lines |
| `beta` | F-beta score beta parameter (default: 1.0) |
| `min_total_prs` | Minimum total PR volume (BigQuery count) to include a bot |
| `min_scored_prs` | Minimum scored PRs (after all PR-level filters) to include a bot |
| `min_prs_per_day` | Minimum scored PRs per day to include a day in time series |
| `exclude_bot_authored` | Exclude PRs authored by bots (`true`/`false`) |
| `exclude_self_authored` | Exclude PRs where the bot reviews its own PR (`true`/`false`) |
| `require_solo_bot` | Keep only PRs scored by exactly one bot (`true`/`false`) |
| `require_human_engagement` | Require human comments or commits after bot review (`true`/`false`) |
| `require_reviews` | Require a formal GitHub review event (`true`/`false`) |
| `min_human_reviewers` | Minimum distinct non-bot, non-author commenters after bot review |
| `min_commits_after_review` | Minimum commit events after bot's first review |
| `min_repo_contributors` | Minimum distinct PR authors in the repo |
| `max_author_repo_prs` | Cap PRs per (repo, author, bot) triple via deterministic random sample |
| `include_ignored` | Include bots flagged as ignored in the registry (`true`/`false`) |

### `/api/volumes` Parameters

Returns PR volume counts per tool per day. When populated via the GitHub Search API (default), counts are attributed to the PR's **creation date**. When populated via BigQuery (`--source bq`), counts are attributed to the first day the bot touched the PR. Only date range and chatbot filters apply — label filters are not relevant since this is raw count data, not analyzed data. Days where a bot had no activity are zero-filled in the response so the chart shows gaps correctly.

| Parameter | Description |
|---|---|
| `start_date` | Start date (`YYYY-MM-DD`) |
| `end_date` | End date (`YYYY-MM-DD`) |
| `chatbot` | Comma-separated chatbot names |

```bash
curl 'localhost:3000/api/volumes?start_date=2026-01-01&end_date=2026-02-25'
curl 'localhost:3000/api/volumes?chatbot=coderabbitai%5Bbot%5D,Copilot'
```

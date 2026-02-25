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
| `GET` | `/api/volumes` | PR volume per tool per day (formal reviews from BigQuery) |

### Filter Parameters (for `/api/daily-metrics` and `/api/leaderboard`)

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
| `min_total_prs` | Minimum total PRs to include a chatbot in leaderboard/scatter |
| `min_prs_per_day` | Minimum PRs per day to include in time series |

### `/api/volumes` Parameters

Returns the number of unique PRs each tool first interacted with per day, from GitHub Archive. Each PR is counted exactly once on the earliest day the bot touched it, so summing daily counts gives the true unique total (no double-counting PRs that span multiple days). Only date range and chatbot filters apply — label filters are not relevant since this is raw BigQuery count data, not analyzed data. Days where a bot had no activity are zero-filled in the response so the chart shows gaps correctly.

| Parameter | Description |
|---|---|
| `start_date` | Start date (`YYYY-MM-DD`) |
| `end_date` | End date (`YYYY-MM-DD`) |
| `chatbot` | Comma-separated chatbot names |

```bash
curl 'localhost:3000/api/volumes?start_date=2026-01-01&end_date=2026-02-25'
curl 'localhost:3000/api/volumes?chatbot=coderabbitai%5Bbot%5D,Copilot'
```

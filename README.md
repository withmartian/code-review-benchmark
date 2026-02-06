# PR Review Dataset Builder

Extracts PRs where a GitHub user participated in code review, collects all events into a unified timeline, and enriches with GitHub API data. Outputs JSONL (one JSON object per PR).

Every step saves both the query/request and the result for full auditability.

## Setup

```bash
cd pr-review-dataset
uv sync
```

You need:
- A GCP project with BigQuery access (for querying `githubarchive.day.*`)
- A GitHub personal access token with `public_repo` read access
- `gcloud auth application-default login` (for BigQuery auth)

## Commands

### Dry run (estimate BigQuery cost)

```bash
uv run python main.py \
  --user "target-username" \
  --gcp-project "my-gcp-project" \
  --start 2024-01-01 \
  --end 2025-01-01 \
  --phase bq-extract \
  --bq-dry-run
```

### Full pipeline

```bash
uv run python main.py \
  --user "target-username" \
  --gcp-project "my-gcp-project" \
  --github-token "ghp_..." \
  --start 2024-01-01 \
  --end 2025-01-01
```

### Run phases individually

```bash
# Phase 1: BigQuery extraction
uv run python main.py \
  --user "target-username" \
  --gcp-project "my-gcp-project" \
  --start 2024-01-01 \
  --end 2025-01-01 \
  --phase bq-extract

# Phase 2: GitHub API enrichment
uv run python main.py \
  --user "target-username" \
  --gcp-project "my-gcp-project" \
  --github-token "ghp_..." \
  --start 2024-01-01 \
  --end 2025-01-01 \
  --phase gh-enrich

# Phase 3: Assemble into final JSONL
uv run python main.py \
  --user "target-username" \
  --gcp-project "my-gcp-project" \
  --start 2024-01-01 \
  --end 2025-01-01 \
  --phase assemble
```

### Test with a small sample

```bash
uv run python main.py \
  --user "target-username" \
  --gcp-project "my-gcp-project" \
  --github-token "ghp_..." \
  --start 2024-01-01 \
  --end 2025-01-01 \
  --max-prs 3
```

### Custom output directory

```bash
uv run python main.py \
  --user "target-username" \
  --gcp-project "my-gcp-project" \
  --github-token "ghp_..." \
  --start 2024-01-01 \
  --end 2025-01-01 \
  --output-dir my-output/
```

## Output Structure

All data is organized under `output/{user_id}/` with per-PR directories at `{owner}/{repo}/{pr_number}/`. Every step saves both the query/request and the response for auditability.

```
output/{user_id}/
├── 01_find_prs.sql                              # BQ query with parameter values
├── 01_find_prs.json                             # List of discovered PRs
├── {owner}/{repo}/{pr_number}/
│   ├── 02_fetch_events.sql                      # BQ query with parameter values
│   ├── 02_fetch_events.json                     # BigQuery archive events for this PR
│   ├── 03_commits_request.json                  # REST API request (URL, params)
│   ├── 03_commits_response.json                 # PR commits
│   ├── 04_reviews_request.json                  # REST API request (URL, params)
│   ├── 04_reviews_response.json                 # PR reviews
│   ├── 05_review_threads_request.json           # GraphQL query + variables
│   ├── 05_review_threads_response.json          # Review threads with reactions
│   ├── 06_commit_details_request.json           # REST API requests (one per commit)
│   ├── 06_commit_details_response.json          # File changes with patches
│   └── assembled.json                           # Final combined result for this PR
```

## Resumability

Every phase writes files per-PR. If interrupted, re-run the same command and it skips already-completed work.

Inspect individual PRs:

```bash
cat output/target-username/facebook/react/12345/02_fetch_events.json | jq .
cat output/target-username/facebook/react/12345/03_commits_response.json | jq .
cat output/target-username/facebook/react/12345/05_review_threads_response.json | jq .
cat output/target-username/facebook/react/12345/assembled.json | jq .
```

Audit what queries were actually run:

```bash
cat output/target-username/01_find_prs.sql
cat output/target-username/facebook/react/12345/02_fetch_events.sql
cat output/target-username/facebook/react/12345/03_commits_request.json | jq .
```

## Output

Each PR's final result lives at `output/{user_id}/{owner}/{repo}/{pr}/assembled.json` with:
- PR metadata (title, author, merged status)
- Unified timeline of all events (opens, reviews, comments, commits with full diffs)
- Review thread resolution status and reactions
- Summary stats

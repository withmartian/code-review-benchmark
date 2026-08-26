# Online Code Review Benchmark

Offline benchmarks have a fundamental flaw: they use static datasets of PRs from well-known repositories. Tools may have seen these exact PRs during training, inflating their scores. A benchmark from 2024 tested on PRs from 2023 — tools trained on millions of GitHub PRs likely memorized the answers.

The online benchmark solves this by **continuously sampling fresh PRs from GitHub**. Every week, new PRs appear that no tool has been trained on. This gives an honest measure of how well code review bots actually perform in the wild.

## How it works

```
GitHub Search API
        │
        ▼
    ┌────────┐     ┌─────────┐     ┌──────────┐     ┌─────────┐     ┌────┐     ┌───────────┐
    │Discover│────▶│ Enrich  │────▶│ Assemble │────▶│ Analyze │────▶│ DB │────▶│ Dashboard │
    └────────┘     └─────────┘     └──────────┘     └─────────┘     └────┘     └───────────┘
   Search API      GitHub API     Build unified    LLM 3-step     Postgres    Interactive
   finds merged    fetches full   PR timeline      extraction &   or SQLite   filters &
   bot-reviewed    PR context                      matching                   time series
   PRs
```

### 1. Discover

The GitHub Search API (`reviewed-by:<bot> is:merged`) finds merged PRs where tracked code review bots left reviews. For high-volume bots that exceed the 1,000-result API cap, adaptive time bisection splits the query window until each chunk fits. PRs are randomly sampled and capped per-repo to prevent any single project from dominating. Up to 500 PRs per bot per day. A BigQuery / [GitHub Archive](https://www.gharchive.org/) fallback is available via `--source bq`.

### 2. Enrich

The GitHub API fetches the full PR context in a resumable 6-step process: commits, reviews, review threads (via GraphQL), and per-commit file diffs. PRs exceeding size limits (>50 commits or >2000 changed lines) are automatically skipped. Multi-token rotation handles rate limits.

### 3. Assemble

Raw API data is assembled into a unified chronological timeline: commits, review comments, issue comments, thread resolutions, and merge events. This gives the LLM the full story of what happened in the PR.

### 4. Analyze (LLM 3-step)

This is the core of the benchmark — a three-step LLM analysis:

**Step 1 — Extract bot suggestions**: The LLM reads the code the bot reviewed (pre-review commits + diff) and the bot's comments. It extracts each actionable suggestion with category (bug, security, performance, style, refactor, docs) and severity (low/medium/high/critical).

**Step 2 — Extract human actions**: The LLM reads post-review commits and identifies what the developer actually fixed after the bot commented. This is the ground truth — real issues that required code changes.

**Step 3 — Judge matching**: The LLM determines which bot suggestions correspond to actual human fixes, producing:
- **Precision** = matched suggestions / total suggestions ("what % of the bot's advice was actually useful?")
- **Recall** = matched actions / total actions ("what % of real issues did the bot catch?")
- **F-beta** = adjustable harmonic mean (F1 when beta=1)

### 5. Label (optional)

An LLM classifies each PR by language, domain (frontend/backend/infra), PR type (feature/bugfix/refactor), issue severity, and more. These labels power the dashboard filters.

## Bots tracked

CodeRabbit, GitHub Copilot, Claude, Cursor, Augment, Codex, Gemini, Greptile, Graphite, Qodo, Propel, and others. New bots can be added by name.

## Dashboard

The dashboard shows tool performance over time with filters for:
- **Language**: Python, TypeScript, Go, Rust, Java, etc.
- **Domain**: frontend, backend, infra, fullstack
- **PR type**: feature, bugfix, refactor, chore
- **Severity**: low, medium, high, critical
- **Diff size**: min/max lines changed
- **F-beta**: adjustable weighting between precision and recall
- **Engagement**: require human engagement (comments/commits after the bot's review), solo-bot PRs, minimum reviewer count
- **Sample controls**: min scored PRs, min PRs per day, max PRs per (repo, author, bot) triple

Visualizations include time series of F-beta scores, precision/recall scatter plots, and a filterable leaderboard. See [`FILTERS.md`](FILTERS.md) for the full filter spec.

## Components

| Directory | What | Stack |
|---|---|---|
| [`etl/`](etl/) | Data pipeline: discover, enrich, analyze, label, volumes | Python, asyncio, BigQuery, GitHub API, OpenAI API |
| [`api_service/`](api_service/) | Public dashboard server | Rust, Axum, Plotly.js |

See each subdirectory's README for setup and usage details.

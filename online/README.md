# Online Code Review Benchmark

Offline benchmarks have a fundamental flaw: they use static datasets of PRs from well-known repositories. Tools may have seen these exact PRs during training, inflating their scores. A benchmark from 2024 tested on PRs from 2023 — tools trained on millions of GitHub PRs likely memorized the answers.

The online benchmark solves this by **continuously sampling fresh PRs from GitHub**. Every week, new PRs appear that no tool has been trained on. This gives an honest measure of how well code review bots actually perform in the wild.

## How it works

```
GitHub Archive (BigQuery)
        │
        ▼
    ┌────────┐     ┌─────────┐     ┌──────────┐     ┌─────────┐     ┌────┐     ┌───────────┐
    │Discover│────▶│ Enrich  │────▶│ Assemble │────▶│ Analyze │────▶│ DB │────▶│ Dashboard │
    └────────┘     └─────────┘     └──────────┘     └─────────┘     └────┘     └───────────┘
   BigQuery scan   GitHub API     Build unified    LLM 3-step     Postgres    Interactive
   finds bot PRs   fetches full   PR timeline      extraction &   or SQLite   filters &
                   PR context                      matching                   time series
```

### 1. Discover

A BigQuery scan of [GitHub Archive](https://www.gharchive.org/) finds PRs where tracked code review bots left comments. Sampling is deterministic (FARM_FINGERPRINT-based) so runs are reproducible. Up to 500 PRs per bot per day.

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

#### Honoring tools' own provenance labels

A code review bot is often more than a code reviewer. The same bot account may
also post check-run results, PR assistant chatter, approvability verdicts, or
release notices. Those are distinct product surfaces from code review, and
scoring them as review suggestions is a category error: a style/convention check
run is rarely "fixed" by a developer, so counting it in the precision denominator
unfairly drags the tool's precision.

When a tool tells us which surface produced a comment, the benchmark honors it.
Macroscope stamps every PR comment with a hidden HTML marker recording the
comment's provenance:

```html
<!-- macroscope-meta kind=code_review -->
```

The `kind` distinguishes real review (`code_review`) from non-review surfaces
(`check_run`, `pr_assistant`, `approvability`, `notice`, and any added later).
The benchmark scores **only `code_review`**. Every other kind is *segmented* — recorded
separately as a custom-check comment and excluded from the review-precision
denominator, not silently dropped (see `custom_check` in
`pipeline/analyze.py::_format_bot_comments`).

Two details matter:

- The marker is detected on the **raw comment body**, before hidden HTML comments
  are stripped for the LLM prompt — the marker itself is an HTML comment, so
  checking the cleaned body would never see it.
- Exclusion keys on `kind != code_review` rather than an allowlist of known
  non-review kinds, so a new non-review surface is excluded automatically without
  a benchmark change.

This is a per-tool convention: any bot that labels its own non-review comments can
be scored the same way. The `code_review`-only rule is safe against a tool
labelling its false positives away, because excluded comments are *segmented and
recorded*, not dropped — the exclusions remain auditable.

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

Visualizations include time series of F-beta scores, precision/recall scatter plots, and a filterable leaderboard.

## Components

| Directory | What | Stack |
|---|---|---|
| [`etl/`](etl/) | Data pipeline: discover, enrich, analyze, label | Python, asyncio, BigQuery, OpenAI API |
| [`api_service/`](api_service/) | Public dashboard server | Rust, Axum, Plotly.js |

See each subdirectory's README for setup and usage details.

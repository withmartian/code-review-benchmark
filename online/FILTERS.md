# Online Benchmark Filters

Spec for every filter exposed by the `/api/daily-metrics`, `/api/leaderboard`,
and `/api/volumes` endpoints (served by `online/api_service`).
This file is the source of truth; the dashboard tooltips and
`online/api_service/README.md` mirror it.

For source: query parsing in
`[online/api_service/src/handlers.rs](api_service/src/handlers.rs)`
(see `MetricsQuery`), application logic in
`[online/api_service/src/compute.rs](api_service/src/compute.rs)`
(see `record_matches` and `apply_filters`).

## Filter scopes

Filters apply at one of three scopes:

- **PR** — drop individual PRs from the eligible set before averaging.
- **Bot** — drop an entire bot from the response (no row in leaderboard, no
series in time chart) if it fails the threshold.
- **Day** — drop a `(bot, day)` cell from the daily series if it fails the
threshold (the bot still appears, just with a gap on that day).

The three "min sample" filters look similar but differ in scope and which  
counter they read:


| Filter            | Scope | Counter                                                               | Default |
| ----------------- | ----- | --------------------------------------------------------------------- | ------- |
| `min_total_prs`   | Bot   | **Volume** (BigQuery `pr_count`, every PR the bot ever touched)       | `0`     |
| `min_scored_prs`  | Bot   | **Scored** (PRs with a non-null precision after all PR-level filters) | `0`     |
| `min_prs_per_day` | Day   | **Scored** (per-day count after all PR-level filters)                 | `0`     |


## Defaults

Every filter is `0` / `false` / unset by default. The dashboard ships with
empty placeholders so the unfiltered baseline is the API default. The locked
"Engaged" and "Gold" tier definitions used by the validation pipeline live in
`[analysis/validation/config.py](../analysis/validation/config.py)`.

## Date range


| Param        | Type         | Description                                                                     |
| ------------ | ------------ | ------------------------------------------------------------------------------- |
| `start_date` | `YYYY-MM-DD` | Inclusive start of date range. Filters on the date the bot's review was posted. |
| `end_date`   | `YYYY-MM-DD` | Inclusive end of date range.                                                    |


## Identification filters

Comma-separated, case-insensitive. Empty value = no filter (all values pass).


| Param      | Description                                                                       |
| ---------- | --------------------------------------------------------------------------------- |
| `chatbot`  | Restrict to specific chatbot GitHub usernames (e.g. `coderabbitai[bot],Copilot`). |
| `language` | Restrict to PRs labeled with these languages.                                     |
| `domain`   | Restrict to PRs labeled with these domains (web, infra, etc.).                    |
| `pr_type`  | Restrict to PRs labeled with these types (feature, bugfix, etc.).                 |
| `severity` | Restrict to PRs labeled with these severities.                                    |


## Score configuration


| Param  | Default | Description                                            |
| ------ | ------- | ------------------------------------------------------ |
| `beta` | `1.0`   | Beta parameter for F-beta score. `1.0` is standard F1. |


## PR size


| Param            | Default | Description                                                                                                                                                  |
| ---------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `diff_lines_min` | unset   | Minimum total `additions + deletions`. PRs with NULL `diff_lines` always pass.                                                                               |
| `diff_lines_max` | unset   | Maximum total `additions + deletions`. Note: ETL hard-caps at `MAX_PR_CHANGED_LINES=2000` at enrich time, so larger PRs aren't in the dataset to begin with. |


## Bot-author filters


| Param                   | Default | Scope | Description                                                                                                                                                                                                |
| ----------------------- | ------- | ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `exclude_bot_authored`  | `false` | PR    | Drop PRs where the human-author check fails (`pr_author` ends with `[bot]` or matches the known-bot allowlist in `[online/etl/pipeline/quality.py](etl/pipeline/quality.py)`). Used by Engaged/Gold tiers. |
| `exclude_self_authored` | `false` | PR    | Drop PRs where the chatbot is reviewing its own PR. Edge case; effectively zero impact on most bots.                                                                                                       |
| `require_solo_bot`      | `false` | PR    | Keep only PRs scored by exactly one chatbot in our snapshot (same `(repo_name, pr_number)`). Multi-bot PRs confuse the judge's matching of comments to human follow-up. Counts original chatbot IDs, so display-merged accounts (e.g. the two Qodo bots) still count as two. Untracked reviewers are not detected. |


## Repo / author shape filters


| Param                   | Default | Scope | Description                                                                                                                                                                                                                                                         |
| ----------------------- | ------- | ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `min_repo_contributors` | unset   | PR    | Drop PRs whose repo has fewer than N distinct PR authors across the dataset. Used to filter out solo-dev repos. Used by Gold tier (`= 2`).                                                                                                                          |
| `max_author_repo_prs`   | unset   | PR    | For every `(repo, author, bot)` triple with more than N PRs, randomly sample N to keep. Triples with `<= N` PRs pass through entirely. Random seed is derived from the full filter param set, so the sample is deterministic per query. Used by Gold tier (`= 50`). |


## Engagement filters

All read from the `engagement_signals` JSONB column written by
`[online/etl/pipeline/quality.py](etl/pipeline/quality.py)`. Engagement is
scoped to events strictly after the bot's first review timestamp. PRs with
NULL `engagement_signals` are treated as "not engaged" (filter excludes them).
Recompute with `backfill-engagement --force` after actor-alias changes; otherwise
bots like Copilot (events as `copilot-pull-request-reviewer`) stay flagged
not-engaged even when the author pushed after the review.


| Param                      | Default | Scope | Description                                                                                                                                                                   |
| -------------------------- | ------- | ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `require_human_engagement` | `false` | PR    | Require `has_human_engagement = true`, defined as `human_comment_count > 0 OR commits_after_review > 0`. Used by Engaged and Gold tiers.                                      |
| `min_human_reviewers`      | unset   | PR    | Require at least N distinct non-bot, non-author commenters after the bot's first review.                                                                                      |
| `min_commits_after_review` | unset   | PR    | Require at least N commit events after the bot's first review. The classic "the developer pushed a fix" signal.                                                               |
| `require_reviews`          | `false` | PR    | Require the PR to have at least one formal GitHub review event (any actor). Stricter than `require_human_engagement` because it doesn't accept comments-only or commits-only. |


### `engagement_signals` JSON schema

```json
{
  "human_reviewer_count": 2,
  "human_comment_count": 5,
  "human_comment_total_length": 412,
  "back_and_forth_rounds": 1,
  "commits_after_review": 3,
  "has_human_engagement": true
}
```


| Field                        | Definition                                                |
| ---------------------------- | --------------------------------------------------------- |
| `human_reviewer_count`       | Distinct non-bot, non-author commenters after bot review. |
| `human_comment_count`        | Total human comment events after bot review.              |
| `human_comment_total_length` | Sum of `body` lengths across those human comments.        |
| `back_and_forth_rounds`      | Bot → human → bot review cycles after the first review.   |
| `commits_after_review`       | Commit events after the bot's first review timestamp.     |
| `has_human_engagement`       | `human_comment_count > 0 OR commits_after_review > 0`.    |


## Bot-level sample filters


| Param             | Default | Scope | Counter | Description                                                                                                                                                                                       |
| ----------------- | ------- | ----- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `min_total_prs`   | `0`     | Bot   | Volume  | Hide bots whose total discovered PR volume in the date range is below N. Volume comes from the `pr_volumes` table, populated by the ETL `volumes` command (Search API by default, attributed to PR creation date; BigQuery fallback, attributed to first bot interaction day). Independent of whether PRs were ever scored. |
| `min_scored_prs`  | `0`     | Bot   | Scored  | Hide bots whose total scored-PR count after all PR-level filters is below N. Use this when you want a minimum sample size on the scoring side, not the discovery side.                            |
| `include_ignored` | `false` | Bot   | —       | If `false`, bots flagged `ignored` in the chatbot registry are excluded entirely. The Engaged/Gold tier configs explicitly set this to `true` so the tier is purely defined by the other filters. |


## Day-level sample filter


| Param             | Default | Scope | Counter | Description                                                                                                                                                                                                                                                          |
| ----------------- | ------- | ----- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `min_prs_per_day` | `0`     | Day   | Scored  | For a given bot, drop any day whose scored-PR count is below N from that bot's series. Useful for stripping noise from low-volume days when computing rolling averages or rank stability. Does **not** drop the bot from the response, only the offending day cells. |


## How the pieces compose

A request like the dashboard's "Engaged tier" uses:

```
?require_human_engagement=true
&exclude_bot_authored=true
&min_total_prs=900
&min_prs_per_day=10
&min_scored_prs=100
&include_ignored=true
&beta=1
&diff_lines_min=0&diff_lines_max=9999
```

Order of evaluation in
`[apply_filters](api_service/src/compute.rs)`:

1. **Volume totals** computed from the date range.
2. **Eligible chatbots** = passes `include_ignored`, `chatbot` name filter,
  and `min_total_prs` (volume).
3. **Random sample** for `max_author_repo_prs` is pre-computed once per query
  (deterministic seed derived from the full param set).
4. **Per-PR filtering** via `record_matches` (date, label filters,
  `diff_lines_*`, `exclude_self_authored`, `require_reviews`,
   `exclude_bot_authored`, `require_solo_bot`, `min_repo_contributors`,
   `require_human_engagement`, `min_human_reviewers`,
   `min_commits_after_review`).
5. **Capped sample** check (drop PRs not in the random sample for their
  triple).
6. **Aggregation** (per-day or per-bot accumulators).
7. `**min_scored_prs`** drops bots whose total scored-PR count across the
  filtered records is below threshold.
8. `**min_prs_per_day**` drops `(bot, day)` cells whose scored count is below
  threshold.

## Other current behavior worth knowing

These aren't filter parameters but they shape the dataset every filter
operates on, so they show up in every analysis result.

- **Merged-only**: only PRs with `pr_merged = TRUE` are ever analyzed. See
`[online/etl/db/queries.py](etl/db/queries.py)` lines 58-100. Open PRs and
PRs that closed without merging are not in the scoring dataset at all,
regardless of any API filter.
- **Large-PR cap**: `MAX_PR_CHANGED_LINES = 2000` and `MAX_PR_COMMITS = 50`
are enforced at enrich time
(`[online/etl/pipeline/enrich.py](etl/pipeline/enrich.py)` lines 446-456).
Oversized PRs are marked `skipped` and never reach the analyzer. So
`diff_lines_max` only refines the already-capped set.
- **Bot allowlist for engagement**: the "is this commenter a bot?" check uses
`[bot]` suffix detection plus an explicit allowlist of known automation
accounts (Dependabot, Renovate, etc.). See
`[online/etl/pipeline/quality.py](etl/pipeline/quality.py)` lines 29-58.


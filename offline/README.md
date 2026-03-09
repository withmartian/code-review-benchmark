# Offline Code Review Benchmark

Open replication of the code review benchmark used by companies like [Augment](https://www.augmentcode.com/blog/introducing-augment-code-review) and [Greptile](https://www.greptile.com/blog/code-review-benchmark). 50 PRs across 5 major open-source codebases with human-verified golden comments. An LLM judge evaluates each tool: does it find real issues? Does it generate noise?

## Evaluated tools

| Tool | Type |
|---|---|
| [Augment](https://www.augmentcode.com/) | AI code review |
| [Claude Code](https://claude.ai) | AI assistant |
| [CodeRabbit](https://www.coderabbit.ai/) | AI code review |
| [Codex](https://openai.com/codex) | AI assistant |
| [Cursor Bugbot](https://cursor.com) | AI code review |
| [Gemini](https://gemini.google.com/) | AI assistant |
| [GitHub Copilot](https://github.com/features/copilot) | AI code review |
| [Graphite](https://graphite.dev/) | AI code review |
| [Greptile](https://www.greptile.com/) | AI code review |
| [Propel](https://propelauth.com/) | AI code review |
| [Qodo](https://www.qodo.ai/) | AI code review |

Adding a new tool requires forking the benchmark PRs and collecting the tool's reviews — see Steps 0 and 1 below.

## Methodology

Each of the 50 benchmark PRs has a set of **golden comments**: real issues that a human reviewer identified, with severity labels (Low / Medium / High / Critical). These are the ground truth.

For each tool, the pipeline:
1. **Extracts** individual issues from the tool's review comments (line-specific comments become candidates directly; general comments are sent to an LLM to extract distinct issues)
2. **Judges** each candidate against each golden comment using an LLM: "Do these describe the same underlying issue?"
3. **Computes** precision (what fraction of the tool's comments matched real issues?) and recall (what fraction of real issues did the tool find?)

The judge accepts semantic matches — different wording is fine as long as the underlying issue is the same.

### Judge models used

Results are stored per judge model so you can compare how different judges score:
- `anthropic_claude-opus-4-5-20251101`
- `anthropic_claude-sonnet-4-5-20250929`
- `openai_gpt-5.2`

## Known limitations

- **Static dataset** — PRs are from well-known repos; tools may have seen them during training (training data leakage). See [`online/`](../online/) for a benchmark that avoids this.
- **Golden comments are human-curated** but may miss edge cases or disagree with other reviewers.
- **LLM judge introduces model-dependent variance** — different judge models may score differently. We mitigate this by using consistent prompts and reporting the judge model used.

---

## Setup

1. Install dependencies:
```bash
cd offline
uv sync
```

2. Create `.env` file (see `.env.example`):
```bash
cp .env.example .env
# fill in your tokens
```

## Tests

Run the pytest suite (no network access required):

```bash
pytest
```

## Linting

```bash
ruff check .
```

---

## Pipeline steps

All scripts live in the `code_review_benchmark/` package. Run from the `offline/` directory. Output goes to `results/`.

### 0. Fork PRs

Fork benchmark PRs into a GitHub org where the tool under evaluation is installed:

```bash
uv run python -m code_review_benchmark.step0_fork_prs
```

### 1. Download PR data

Aggregate PR reviews from benchmark repos with golden comments:

```bash
# Full run (incremental - skips already downloaded)
uv run python -m code_review_benchmark.step1_download_prs --output results/benchmark_data.json

# Test mode: 1 PR per tool
uv run python -m code_review_benchmark.step1_download_prs --output results/benchmark_data.json --test

# Force refetch all reviews
uv run python -m code_review_benchmark.step1_download_prs --output results/benchmark_data.json --force

# Force refetch for a specific tool
uv run python -m code_review_benchmark.step1_download_prs --output results/benchmark_data.json --force --tool copilot
```

**Output:** `results/benchmark_data.json`

### 2. Extract comments

Extract individual issues from review comments for matching:

```bash
# Extract for all tools
uv run python -m code_review_benchmark.step2_extract_comments

# Extract for specific tool
uv run python -m code_review_benchmark.step2_extract_comments --tool claude

# Limit extractions (for testing)
uv run python -m code_review_benchmark.step2_extract_comments --tool claude --limit 5
```

Line-specific comments become direct candidates. General comments are sent to the LLM to extract individual issues.

**Output:** Updates `results/benchmark_data.json` with `candidates` field per review.

### 3. Judge comments

Match candidates against golden comments, calculate precision/recall:

```bash
# Evaluate all tools
uv run python -m code_review_benchmark.step3_judge_comments

# Evaluate specific tool
uv run python -m code_review_benchmark.step3_judge_comments --tool claude

# Force re-evaluation
uv run python -m code_review_benchmark.step3_judge_comments --tool claude --force
```

**Output:** `results/{model}/evaluations.json` with TP/FP/FN, precision, recall per review.

### 4. Generate dashboard

Regenerate the dashboard JSON and HTML from evaluation results:

```bash
uv run python analysis/benchmark_dashboard.py
```

**Output:** `analysis/benchmark_dashboard.json` and `analysis/benchmark_dashboard.html`

Open `analysis/benchmark_dashboard.html` in a browser to view results. Run this after adding new tools or re-running the judge to update the dashboard.

### 5. Summary table

Show review counts by tool and repo:

```bash
uv run python -m code_review_benchmark.summary_table
```

**Example output:**
```
Tool        cal_dot_com  discourse    grafana      keycloak     sentry       Total
----------------------------------------------------------------------------------
claude      10           10           10           10           10           50
coderabbit  10           10           10           10           10           50
...
```

### 6. Export by tool

Export tool reviews with evaluation results:

```bash
# Export Claude (default)
uv run python -m code_review_benchmark.step4_export_by_tool

# Export specific tool
uv run python -m code_review_benchmark.step4_export_by_tool --tool greptile
```

**Output:** `results/{tool}_reviews.xlsx`

---

## Data format

### Golden comments (`golden_comments/*.json`)

```json
[
  {
    "pr_title": "Fix race condition in worker pool",
    "url": "https://github.com/getsentry/sentry/pull/93824",
    "comments": [
      {
        "comment": "This lock acquisition can deadlock if the worker is interrupted between acquiring lock A and lock B",
        "severity": "High"
      }
    ]
  }
]
```

Source files: `sentry.json`, `grafana.json`, `keycloak.json`, `discourse.json`, `cal_dot_com.json`

### benchmark_data.json

```json
{
  "https://github.com/getsentry/sentry/pull/93824": {
    "pr_title": "...",
    "original_url": "...",
    "source_repo": "sentry",
    "golden_comments": [
      {"comment": "...", "severity": "High"}
    ],
    "reviews": [
      {
        "tool": "claude",
        "pr_url": "https://github.com/code-review-benchmark/...",
        "review_comments": [
          {"path": "...", "line": 42, "body": "...", "created_at": "..."}
        ],
        "candidates": ["issue description 1", "issue description 2"]
      }
    ]
  }
}
```

### evaluations.json

```json
{
  "https://github.com/getsentry/sentry/pull/93824": {
    "claude": {
      "precision": 0.75,
      "recall": 0.6,
      "true_positives": 3,
      "false_positives": 1,
      "false_negatives": 2,
      "matches": ["..."],
      "false_negatives_detail": ["..."]
    }
  }
}
```

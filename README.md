<div align="center">
    <h1>Code Review Bench</h1>
    <p>
      <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License"></a>
      <a href="https://discord.com/invite/kX6s6nV3zT"><img src="https://img.shields.io/badge/discord-join-5865F2?logo=discord&logoColor=white" alt="Discord"></a>
    </p>
  <picture>
    <source
      srcset="./images/dark.png"
      media="(prefers-color-scheme: dark)"
      width="125" height="125"
    />
    <img
      src="./images/light.png"
      alt="Code Review Benchmark Logo"
      width="125" height="125"
    />
  </picture>
</div>

Open-source [benchmark](https://codereview.withmartian.com) for evaluating AI code review tools — the datasets, the judge, and the pipeline code. Reproduce our results or evaluate your own tool.

## The problem

As AI agents write more code, we need systems to make sure the code they generate is good. This has led to the proliferation of AI code review tools.

Without shared evals for these tools, every company grades its own homework. You can't reproduce the results, compare tools on the same dataset, or verify the methodology. With static evals, agents can game the benchmark. By creating an online and offline benchmark that can check each other, this project allows for robust code review evals.

**We open-source everything**: the PRs, the golden comments, the LLM judge prompts, the evaluation pipeline, and a continuously-updated online benchmark that avoids training data leakage.

## Two benchmarks

### Offline — fixed dataset, reproducible results

**50 PRs** from 5 major open-source projects, each with human-verified golden comments — the real issues a reviewer should catch.

| Repository | Language | Domain |
|---|---|---|
| [Sentry](https://github.com/getsentry/sentry) | Python | Error tracking |
| [Grafana](https://github.com/grafana/grafana) | Go | Observability |
| [Cal.com](https://github.com/calcom/cal.com) | TypeScript | Scheduling |
| [Discourse](https://github.com/discourse/discourse) | Ruby | Forum platform |
| [Keycloak](https://github.com/keycloak/keycloak) | Java | Authentication |

Each PR has curated golden comments (173 total) with severity labels (Low / Medium / High / Critical) and category tags (bug, security, concurrency, data, api, perf, test_gap, doc_defect, style, speculative). An LLM judge matches each tool's review against the golden comments using three judge models (Claude Opus 4.5, GPT-5.2, Claude Sonnet 4.5). Category-based scoring profiles (Strict / Core / All) control which issue types count toward the score, and F-beta weighting lets users prioritize recall over precision.

**Tools evaluated**: Augment, Baz, Claude Code, CodeAnt, CodeRabbit, Cubic, Cursor Bugbot, Devin, Gemini, GitHub Copilot, GitLab Duo, Graphite, Greptile, KG, Kodus, Macroscope, Qodo, Sourcery, and more. Adding a new tool takes an afternoon — fork the benchmark PRs, trigger the tool, run the pipeline.

> **Known limitation**: Static datasets risk training data leakage — tools may have seen these PRs during training. That's why we also run the online benchmark.

See [`offline/README.md`](offline/README.md) for setup and usage.

### Online — continuous, fresh PRs, no data leakage

The online benchmark continuously samples **fresh real-world PRs from GitHub** where code review bots left comments. Because the PRs are recent, tools can't have memorized them during training.

```
GitHub Search API
        │
        ▼
    ┌────────┐     ┌─────────┐     ┌─────────┐     ┌────┐     ┌───────────┐
    │Discover│────▶│ Enrich  │────▶│ Analyze │────▶│ DB │────▶│ Dashboard │
    └────────┘     └─────────┘     └─────────┘     └────┘     └───────────┘
   Search API      GitHub API     LLM 3-step      Postgres    Interactive
   finds merged    fetches full   extraction &    or SQLite   filters &
   bot-reviewed    PR context     matching                    time series
   PRs
```

**How analysis works**:

1. **Extract bot suggestions** — The LLM reads the diff the bot reviewed and the bot's comments, then extracts each actionable suggestion with its category (bug, security, performance, style, ...) and severity.
2. **Extract human actions** — The LLM reads the post-review commits and identifies what the developer actually fixed after the bot commented.
3. **Judge matching** — The LLM determines which bot suggestions correspond to actual fixes, producing per-PR precision (what % of the bot's comments were useful?) and recall (what % of real issues did the bot catch?).

**Bots tracked**: CodeRabbit, GitHub Copilot, Claude, Cursor, Augment, Codex, Gemini, Greptile, Graphite, Qodo, Propel, and others.

**Dashboard features**: Filter by language, project domain, PR type, issue severity, diff size, engagement signals (human comments/commits after bot review), solo-bot PRs, and sample controls. Track performance over time. Adjustable F-beta weighting. See [`online/FILTERS.md`](online/FILTERS.md) for the full filter spec.

See [`online/README.md`](online/README.md) for architecture and setup.

## How the LLM judge works

Both benchmarks use an LLM-as-judge approach, but with different methodologies suited to their data:

| | Offline | Online |
|---|---|---|
| **Ground truth** | Human-curated golden comments (173, categorized) | Developer's post-review fixes |
| **Precision** | Tool comments that match a golden comment / total tool comments | Bot suggestions matched to real fixes / total suggestions |
| **Recall** | Golden comments in active profile found by the tool / total golden in profile | Real fixes caught by the bot / total fixes made |
| **Scoring** | Category-based profiles (Strict/Core/All) + F-beta (0.5–3.0) | F-beta with adjustable weighting |
| **Judge input** | Golden comment + tool candidate | Full PR timeline: diff, bot comments, post-review commits |

In both cases, the judge prompt asks "do these describe the same underlying issue?" — different wording is fine, only the substance matters.

**Judge model variance**: Different LLM judges can score differently. We mitigate this by storing results per judge model and reporting which model was used. The offline benchmark has been evaluated with Claude Opus 4.5, Claude Sonnet 4.5, and GPT-5.2 — the top 5 tools are identical across all three judges, with most tools varying by at most 2 rank positions.

## Repository structure

```
├── offline/                       # Offline benchmark (fixed dataset)
│   ├── golden_comments/           #   Human-curated issues per repo (5 JSON files)
│   ├── code_review_benchmark/     #   Pipeline: fork, download, extract, dedup, judge, export
│   ├── analysis/                  #   Interactive HTML dashboard
│   ├── tests/                     #   Test suite (no network access required)
│   └── results/                   #   Evaluation outputs (per judge model)
│
├── online/                        # Online benchmark (continuous)
│   ├── etl/                       #   Python pipeline
│   │   ├── pipeline/              #     Discover → Enrich → Assemble → Analyze → Label
│   │   ├── llm/                   #     Prompts, schemas, async client
│   │   ├── db/                    #     Database layer (SQLite + PostgreSQL)
│   │   ├── jobs/                  #     Background workers
│   │   └── dashboard/             #     Streamlit dashboard
│   └── api_service/               #   Rust API + embedded HTML dashboard
│
└── LICENSE                        # MIT
```

## Quick start

### Offline benchmark

```bash
cd offline
uv sync
cp .env.example .env               # add GitHub token + LLM API key

# Download reviews for all tools
uv run python -m code_review_benchmark.step1_download_prs --output results/benchmark_data.json

# Extract individual issues from reviews
uv run python -m code_review_benchmark.step2_extract_comments

# Deduplicate candidates (prevents false positives from inline+summary overlap)
uv run python -m code_review_benchmark.step2_5_dedup_candidates

# Run the LLM judge (pass dedup groups to avoid penalising duplicate candidates)
uv run python -m code_review_benchmark.step3_judge_comments \
  --dedup-groups results/${MARTIAN_MODEL}/dedup_groups.json

# View results
open analysis/benchmark_dashboard.html
```

### Online benchmark

```bash
cd online/etl
uv sync
cp .env.example .env               # add GitHub token + GCP project + LLM API key

# Discover recent PRs from BigQuery
uv run python main.py discover --all --days-back 7

# Enrich with GitHub API data
uv run python main.py enrich --chatbot "coderabbitai[bot]" --one-shot

# Run LLM analysis
uv run python main.py analyze --all

# Launch dashboard
uv run python main.py dashboard
```

## Adding a new tool to the offline benchmark

1. Fork the 50 benchmark PRs into a GitHub org where your tool is installed
2. Let the tool review each PR
3. Add the tool name to the download config and run the pipeline
4. Results appear alongside existing tools in the dashboard

See [`offline/README.md`](offline/README.md) for detailed instructions.

## Contributing

We welcome contributions — new tools, better golden comments, improved judge prompts, additional datasets. Open an issue or PR.

## Citation

If you use this benchmark in your research or product evaluation, please cite:

```bibtex
@misc{code_review_benchmark,
  title   = {Code Review Bench},
  author  = {Aleksandr Zverianskii and Ashley Zhang and Jacob Clyne and Antía Garcia and Fazl Barez and Shriyash Upadhyay},
  url     = {https://github.com/withmartian/code-review-benchmark},
  year    = {2026},
  license = {MIT}
}
```

## License

MIT — see [LICENSE](LICENSE).

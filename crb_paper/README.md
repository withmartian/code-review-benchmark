# Fine-tuning experiment

## Goal

Test whether revealed-preference labels carry real review-quality signal
that a model can learn and generalize.

## Training data

- **Format:** within-PR preference pairs (accepted vs. rejected
  suggestion, same PR, different tools when possible).
- **Targets:** the judge's extracted suggestion abstractions, not raw
  bot text.
- **Hygiene:**
  - Length-match pairs within ±20% tokens.
  - Down-sample high-volume tools so no single tool dominates.

## Dataset schemas

Both datasets are emitted as JSONL. Bookkeeping fields (`pr_id`,
`repo_name`, `pr_created_at`, `chatbot`) are kept on every row so the
trainer can do the repo-held-out split, the 2026-04-20 date cutoff, and
the per-tool sanity-check breakdown without re-joining to the DB.

### I/O contract

The model is a **single-suggestion generator**. Given a PR diff, it
produces **one** suggestion in plain text (1–3 sentences). Multi-suggestion
PR review at inference is built by sampling the model k times per PR
(k≈5–10, high temperature), deduplicating near-duplicates, and feeding
the resulting list through the existing CRB matcher for scoring.

**Prompt template** (same shape for SFT and DPO; instruction-formatted
for the chosen instruct base):

    You are reviewing a pull request. Identify one specific issue with
    the change below and describe it in 1–3 sentences.

    PR title: <title>
    Diff:
    <unified diff>

**Output:** plain `description` text only. No JSON. The eval matcher
already takes plain description strings, so structured output
(category, severity, file_path, line_number) buys nothing for v1 and
adds a parsing failure mode.

**Long-diff handling:** drop rows whose tokenized prompt exceeds 12 000
tokens. Documented as known truncation; revisit if it removes too many
rows in step 3.

### SFT (warm-start)

One row per accepted suggestion. The model learns "given this diff,
produce a suggestion that a human would act on."

| Field | Type | Notes |
|---|---|---|
| `pr_id` | int | `prs.id` — for split bookkeeping |
| `repo_name` | str | drives the repo-held-out split |
| `pr_created_at` | timestamp | drives the 2026-04-20 train/val cutoff |
| `chatbot` | str | bot the suggestion came from — for per-tool breakdown and Catapult/CR carveout |
| `prompt` | str | unified diff (assembled from `prs.commits` patches) |
| `response` | str | judge-extracted suggestion abstraction (from `llm_analyses.bot_suggestions`, filtered to ones marked accepted in `matching_results`) |
| `response_tokens` | int | tokenizer length — used for length stratification |

### DPO (preference pairs)

One row per within-PR pair. `prompt` is shared; `chosen` and `rejected`
are two different suggestions on the same PR, one acted on by humans,
one not. When possible they come from different tools.

| Field | Type | Notes |
|---|---|---|
| `pr_id` | int | shared by both sides of the pair |
| `repo_name` | str | repo-held-out split |
| `pr_created_at` | timestamp | 2026-04-20 cutoff |
| `prompt` | str | unified diff (same for chosen and rejected) |
| `chatbot` | str | bot both suggestions came from (within-bot pairs only) |
| `chosen` | str | accepted suggestion (judge abstraction) |
| `chosen_tokens` | int | tokenizer length |
| `rejected` | str | ignored suggestion (judge abstraction) |
| `rejected_tokens` | int | tokenizer length |
| `condition` | enum | `filtered` / `unfiltered` / `inverted` / `random` — selects ablation. `inverted` swaps chosen↔rejected; `random` reassigns labels uniformly within-PR. Same row shape across all four. |

Pair construction guarantees:

- `chosen` and `rejected` are from the **same** `pr_id` **and the
  same bot** (within-bot pairs only). Two suggestions on the same
  PR×bot — one acted on, one ignored.
- `abs(chosen_tokens - rejected_tokens) / max(...) <= 0.20` (the ±20 %
  length match).
- **Bot-balanced at the dataset level:** cap each bot's contribution to
  ~p75 of the per-bot pair counts so no single bot dominates.
- A given PR contributes at most N pairs (TBD after step 3 inspection)
  to keep the dataset diverse across PRs.

Within-bot pairing is deliberate: it removes the bot-style confound
(CodeRabbit prose vs Copilot bullets), so the inverted-labels-fail
result proves "the suggestion's substance carries signal," not "tool
identity carries signal." Tradeoff: PR×bot combos where all suggestions
were accepted (or all ignored) yield zero pairs, so the yield is lower
than a cross-bot pairing scheme would give. Confirm the yield hit is
acceptable in step 3 inspection.

## Method

- SFT → DPO on a small open model (e.g. `Qwen2.5-Coder-7B`).
- Split held-out by **repository**, not randomly.

## Conditions (critical ablations)

| Condition | Expected result |
|---|---|
| Quality-filtered labels | Best |
| Unfiltered | Worse → validates §4 filters |
| Inverted labels | Worse than base → validates signal |
| Random labels | ≈ base model |

## Evaluation — three test sets

| Test set | Question it answers |
|---|---|
| Held-out online PRs (new repos, later time window, different judge model) | Does it generalize in-distribution? |
| Offline 50-PR gold set | Does it transfer across ground-truth definitions? (killer eval) |
| Blind human pairwise, 3–5 reviewers | Does it actually feel better? |

## Reporting (per condition)

- Online F1 with 95% bootstrap CIs.
- Offline P / R / F1.
- Human win rate vs. base.
- Reference points: base model, one frontier model.

## Sanity checks

- Per-tool breakdown — no single-tool mimicry.
- Stratified by PR size and severity.

## Headline result

**Inverted labels failing across all three test sets** = cleanest
evidence the signal is real.

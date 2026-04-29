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

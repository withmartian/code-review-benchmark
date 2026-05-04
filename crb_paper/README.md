# CRB finetuning experiment

## What this experiment tests

The Code Review Benchmark (CRB) records, for each suggestion an automated
review tool makes on a PR, whether a human actually acted on it. That
gives every suggestion a binary label — *accepted* or *ignored* — based
on what the human did, not on what someone thought the suggestion was
worth.

We want to know: **does that accepted/ignored label carry real
information about review quality?** Or is it noise?

The test is to finetune a small language model on those labels and see
if it learns something useful. Critically, we also flip the labels and
finetune again — if the labels carry signal, the flipped run should make
the model *worse*, not better. That's the negative control that turns a
vibe into a finding.

## Setup

- **Base model:** `Qwen/Qwen2.5-Coder-7B-Instruct`, finetuned with LoRA
  adapters (r=8, ~2.5 M trainable params, 0.03 % of total).
- **Context window:** 32 768 tokens (Qwen's native). At 32K seqlen,
  the cross-entropy logits alone take ~9.7 GB
  (`32K × 152K_vocab × 2 bytes`). Hardware-driven prompt cap is
  **28 000 BPE tokens for `prompt + response`**, leaving ~4K headroom
  for the chat template + assistant turn.
- **Two training stages, in order:**
  1. **SFT (warm-start)** — supervised finetuning on accepted
     suggestions. Teaches the model what a "human-actionable" review
     comment looks like at all.
  2. **DPO** — preference optimization on (accepted, ignored) pairs.
     Teaches the model to prefer the kind humans act on.
- **Held-out split** by repository (not random rows). Trained on PRs
  with `bot_reviewed_at` ≤ 2026-03-31; everything later is val/eval.
- **CodeRabbit excluded** (separate compliance reason, not a research
  choice).
- **Hardware:** NVIDIA A100 40 GB (one VM per prompt shape, run in
  parallel). L4 24 GB was insufficient — long-row cross-entropy logits
  OOM'd at step ~250.

## The four ablations

The whole experimental design hinges on running DPO four times with
different label assignments and comparing.

| Run | Labels used | Predicted outcome |
|---|---|---|
| **filtered** | Real labels, with the silver-tier quality filters from the CRB paper | Best — model improves over base |
| **unfiltered** | Real labels, no quality filtering | Worse than `filtered` (validates the filters) |
| **inverted** | **Flipped** — chosen=ignored, rejected=accepted | **Worse than the base model** — validates the signal |
| **random** | Within-PR random reassignment | ≈ base model — validates structure |

If `inverted` makes the model meaningfully worse and `random` doesn't
move it much, the labels carry signal. That's the headline claim.

## What the model sees and produces

**Input (same template at training and inference):**

```
You are reviewing a pull request. Identify one specific issue with the
change below and describe it in 1–3 sentences.

PR title: <title>
Diff:
<unified diff>
```

**Output:** one suggestion in plain text, 1–3 sentences. No JSON. To
produce a multi-suggestion review at inference, we sample the model
*k* times per PR (k≈5, temperature 0.7) and dedup near-duplicates.

**Two prompt shapes** are evaluated in parallel:

- **PR-level** — the prompt's `<unified diff>` is the whole PR's diff.
  Higher-context, more compute, training/inference truncation
  pressure.
- **Hunk-level** — the prompt is just the file's hunk that the
  suggestion anchors to (via the suggestion's `file_path` and
  `line_number`). Way smaller prompts, cleaner train/inference match.

Both are run end-to-end so we can compare which framing actually
helps. See `CHANGELOG.md` for why both exist.

## Pair construction (for DPO)

Each preference pair has the form `(prompt, chosen, rejected)`:

- **Same PR, same bot** — both suggestions come from the same review
  tool on the same PR. This holds the writing style constant so the
  signal we measure is about *substance*, not about which tool's
  prose the model learns to imitate.
- **Length-matched** — `|chosen| − |rejected|` within ±20 % tokens, so
  the model can't cheat by always picking the longer (or shorter) one.
- **Bot-balanced at the dataset level** — each bot's contribution
  capped at ~p75 of per-bot counts, so no single tool dominates.

For hunk-level pairing, "same PR, same bot" is tightened to "same PR,
same bot, **same file**" so chosen and rejected literally share the
hunk in the prompt. This makes the inverted-labels-fail test even
cleaner: prompt is identical between the two sides, the only
difference is the suggestion's substance.

## Evaluation

Three independent test sets, all evaluated by the same matching
judge that scored the original CRB benchmark.

| Test set | Question it answers |
|---|---|
| Held-out online PRs (post-cutoff, different repos, fresh judge) | Does it generalize in-distribution? |
| Offline 50-PR gold set | Does it transfer across ground-truth definitions? (the killer eval) |
| Blind human pairwise, 3–5 reviewers | Does it actually *feel* better? |

**Per-condition metrics reported:**

- Precision, recall, F1 with 95 % bootstrap CIs (1 000 resamples).
- Per-bot breakdown — no single-tool mimicry should dominate.
- Stratified by PR size and severity.
- Reference points: base model, one frontier model.

For paper figures, the live W&B metrics also show:

- `train/loss` and `eval/loss` per run.
- `eval/rewards/accuracies` from DPO — the proportion of held-out
  pairs where the model assigns higher logprob to `chosen` than
  `rejected`. This is the live signal-validation curve.

## Dataset sizes per run

Generated from a 150 000-row random sample of CRB analyses, after
CodeRabbit drop and silver-tier filtering. Train/val split at
`bot_reviewed_at ≤ 2026-03-31`. Long-row drop uses the real Qwen BPE
tokenizer at the cap above.

### PR-level run (whole-PR diff in prompt)

| Dataset | Train | Val |
|---|---|---|
| SFT rows (1 per accepted suggestion) | 12 731 | 1 946 |
| DPO pairs (within-PR, within-bot) | 4 185 | 540 |
| Eval rows (held-out PRs with gold human_actions) | — | 1 719 |

Total ~770 MB JSONL on disk. Lives at
`gs://martian-research-crb-finetune/v2/pr-level/data/`.

### Hunk-level run (per-file hunk in prompt)

| Dataset | Train | Val |
|---|---|---|
| SFT rows (1 per accepted suggestion, hunk-anchored) | 14 601 | 2 328 |
| DPO pairs (within-PR, within-bot, within-file) | 2 001 | 201 |
| Eval rows (per-(PR, bot, file) with gold actions for that file) | — | 6 670 |

Total ~110 MB JSONL on disk. Lives at
`gs://martian-research-crb-finetune/v2/hunk-level/data/`.

DPO yields are smaller for hunk-level because the within-file
constraint requires ≥1 accepted *and* ≥1 ignored suggestion on the
same file from the same bot. Compensated for somewhat by faster
training (smaller prompts ≈ 5× the throughput).

## Files in this folder

| File | What it does |
|---|---|
| `README.md` | This document. |
| `CHANGELOG.md` | Dated record of every meaningful pipeline change, for paper traceability. |
| `filters.py` | Silver-preset filter logic, ported from the dashboard's Rust source. |
| `pairs.py` | Builds SFT rows and DPO pairs from PR analyses. Both PR-level and hunk-level shapes. |
| `db.py` | Read-only Cloud SQL connector + unified-diff assembly from `commit_details`. |
| `prepare_jsonl.py` | End-to-end dataset prep: connect, filter, build pairs, write JSONL. `--mode {pr-level, hunk-level}`. |
| `inspect_dataset.py` | Sanity-check report — counts, length quantiles, per-bot/per-repo distribution. |
| `train_sft.py` | SFT trainer (TRL `SFTTrainer` + LoRA). |
| `train_dpo.py` | DPO trainer with the four-condition flag. |
| `evaluate.py` | Matcher-based eval against gold human actions; bootstrap CIs; logs to W&B. |
| `run_training.sh` | PR-level pipeline runner: SFT → 4 DPO ablations → 6 evals. |
| `run_training_hunk.sh` | Hunk-level pipeline runner (same shape, `hunk-` W&B prefix). |

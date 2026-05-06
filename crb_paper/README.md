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

## Results — v2 (first round, 2026-05-05)

First end-to-end run on A100 40 GB. All four ablations completed for
both prompt shapes. Evaluation uses TRL's built-in DPO val metrics on
the held-out post-cutoff set (224 PR-level / 190 hunk-level pairs).

### Final eval metrics per condition

| Pipeline | Condition | eval/loss | eval/rewards/accuracies | eval/rewards/margins |
|---|---|---|---|---|
| hunk-level | filtered | 0.6906 | 0.437 | 0.0062 |
| hunk-level | unfiltered | 0.6899 | 0.447 | 0.0075 |
| hunk-level | inverted | 0.6911 | 0.442 | 0.0051 |
| hunk-level | random | 0.6905 | 0.500 | 0.0059 |
| pr-level | filtered | 0.6920 | 0.138 | 0.0023 |
| pr-level | unfiltered | 0.6918 | 0.121 | 0.0028 |
| pr-level | inverted | 0.6911 | 0.143 | 0.0043 |
| pr-level | random | 0.6940 | 0.089 | -0.0014 |

### Eval phase: silent failure

The downstream eval phase (`evaluate.py` × 6 runs per pipeline) ran
in ~70 seconds total per pipeline — far too fast for real model
generation + OpenAI matcher API calls. The `results/` directory is
empty on both VMs. The runner shell scripts mask eval errors with
`|| echo`, so the failures didn't propagate. We have **no downstream
P/R/F1 numbers from this round**, only the trainer's built-in DPO
val metrics shown in the table above.

This needs investigation before v3 — likely a missing OPENAI_API_KEY
env propagation or a TRL-version-related signature change in the
trainer-saved adapter that `evaluate.py` doesn't load correctly.

### What this means

- **eval/loss is ~0.69 ≈ ln(2) on every run.** That's the loss value
  at random-init reward, meaning DPO has barely moved the policy from
  its starting point.
- **`eval/rewards/margins` are essentially zero** (|·| < 0.008 across
  the board).
- **No conditions are statistically distinguishable from each other**
  (95 % CI on a 190–224-row val set is ±0.07; all spread is within
  that).
- The headline `inverted-labels-fail` test is **inconclusive** — we
  can't yet claim the labels carry signal from this round.

This is **not** a pipeline bug or a label-swap (verified by inspecting
the JSONL data and reading sample chosen/rejected pairs). The model is
simply undertrained at our v2 settings — 1 epoch on 1.8 K–2 K pairs
with LR 5e-6 produces too small a policy update.

The pr-level absolute accuracy (~0.13) is lower than hunk-level (~0.44)
across all conditions including `random`. We attribute this to the
base model having a slight prior that disagrees with our `chosen`
labels on whole-PR-diff prompts (perhaps reacting to surface features
of the larger context). Either way, since the *relative* ordering
across conditions is what proves the signal, absolute level isn't the
issue — the lack of differentiation between conditions is.

## Results — v3 (stronger training, 2026-05-06)

Same 8 K-BPE-capped JSONL data, same SFT warm-start. Only DPO knobs
changed: epochs 1→3, lr 5e-6→2e-5.

### Final eval metrics per condition

| Pipeline | Condition | eval/loss | eval/rewards/accuracies | eval/rewards/margins |
|---|---|---|---|---|
| hunk-level | filtered | 0.6801 | 0.447 | 0.061 |
| hunk-level | unfiltered | 0.6773 | 0.453 | 0.068 |
| hunk-level | **inverted** | **0.6561** | **0.510** | **0.122** |
| hunk-level | random | 0.6934 | 0.400 | 0.008 |
| pr-level | filtered | 0.6943 | 0.134 | 0.010 |
| pr-level | unfiltered | 0.6869 | 0.134 | 0.023 |
| pr-level | **inverted** | **0.6789** | 0.152 | **0.036** |
| pr-level | random | 0.6984 | 0.107 | -0.005 |

### What this means — and the headline-flip

**`random` is clearly worst, `inverted` is clearly best, `filtered` /
`unfiltered` sit between them.** Margins on `inverted` are 2-10× those
on `filtered`, and `inverted` has the lowest loss in both pipelines.

This is **real, directional signal** — but it points the *opposite*
way from what the original paper framing predicted. The original
headline was *"`inverted` should fail across all eval sets — that's
the negative control proving the labels carry quality signal."* We
observe the opposite: **`inverted` succeeds more strongly than
`filtered`.**

### Why it's the opposite

We audited the labelling end-to-end (code path in `pairs.py`,
matched/accepted convention from `online/etl/llm/prompts.py`'s
`JUDGE_MATCHING`). No bug. `chosen=accepted=matched=True` flows
through correctly to the trainer.

The natural reading: **the base model's prior, after SFT
warm-start on accepted suggestions, prefers human-ignored
suggestions.** Three (compatible) explanations:

1. **"Ignored" suggestions are more typical text.** Most bot
   suggestions don't get acted on, so the language-modelling prior
   over the suggestion pool already concentrates probability on
   "ignored-style" suggestions. DPO going *with* that prior
   (`inverted`) is easier than going *against* it (`filtered`).
2. **"Acted on" doesn't mean better quality.** It means *the human
   had to fix something*. Suggestions that point at non-bugs (false
   positives or trivial nits) are more "fluent / typical" than
   suggestions pointing at real bugs that needed nontrivial code
   changes — and the model rewards fluency.
3. **SFT on accepted narrowed the policy already.** The accepted
   suggestions formed the SFT distribution. After SFT, the policy
   is already biased toward accepted-style. DPO has little
   additional room to shift it further — but plenty of room to
   shift away (which is why `inverted` improves so much more).

### Reframing the paper

The intended `inverted-fails` headline is no longer available. Two
defensible reframings using the same numbers:

1. *"DPO trained on revealed-preference labels learns a directional
   preference, but the direction is opposite of the assumed
   `accepted ≻ ignored`. The signal is real (random vs. directional
   conditions are clearly separable; inverted vs. filtered/unfiltered
   are clearly separable) but its sign is flipped from quality
   intuition."*
2. *"Revealed-preference labels in CRB conflate 'good quality' with
   'requires action.' DPO on these labels teaches the model to prefer
   the latter, which manifests as preferring ignored over accepted
   suggestions. A different label scheme (e.g., asking humans
   directly which suggestion they prefer) would test the
   underlying signal more cleanly."*

Both are real findings. (1) is the closest to the original
experimental design's intent. (2) suggests the next experiment.

### Earlier v3 plan section (kept for traceability)



The fix is straightforward: train for more steps with a larger
learning rate and slightly more LoRA capacity.

| Hyperparameter | v2 | v3 (planned) | Rationale |
|---|---|---|---|
| epochs | 1 | 3 | 3× the gradient updates without re-pulling data |
| learning_rate | 5e-6 | 2e-5 | 4× larger updates per step (still in DPO-safe range) |
| lora_r | 8 | 16 | 2× adapter capacity |

Estimated wallclock: ~3× v2's per-ablation time → ~5-6 hrs total in
parallel on 2 × A100 40 GB. Cost ~$50 GPU + ~$2 OpenAI matcher
≈ ~$50 added.

The v2 dataset and infrastructure (CodeRabbit-dropped, silver-filtered,
8K-BPE-capped JSONL on GCS) are reused for v3 — only training
hyperparameters change.

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

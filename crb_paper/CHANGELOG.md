# Changelog

Documents notable changes to the CRB finetuning pipeline so the paper can
trace what was tried, what worked, and what didn't.

Format: dated entries, newest first. Each bullet links to the commit SHA
when relevant. "Why" line where the rationale isn't obvious from the
change itself.

---

## 2026-05-05 — DPO eval-time OOM fix via length-aware data filter

DPO crashed at periodic-eval step (`eval_steps=250`) on both VMs with
an 18.56 GiB single-tensor allocation. Cause: outlier rows with very
long `prompt + chosen + rejected` concatenations whose attention /
logit memory exceeded what 40 GB A100 can hold even with gradient
checkpointing enabled.

- Considered disabling periodic eval; rejected because the live
  `eval/rewards/accuracies` curve is the headline DPO metric for the
  inverted-labels-fail story in the paper.
- **Wrote `/tmp/filter_dpo_lengths.py`** — uses real Qwen tokenizer to
  drop rows where `tokenize(prompt) + tokenize(chosen) +
  tokenize(rejected) > 8000` (well below the empirical 18K-token
  failure threshold). Applied in-place to `data/dpo_train.jsonl` and
  `data/dpo_val.jsonl` on each VM.
- Yields:
    - PR-level: train 1764/4185 (42 %), val 224/540 (42 %). Heavy
      prr-level pruning because whole-PR prompts run long.
    - Hunk-level: train 1976/2001 (99 %), val 190/201 (94 %). Hunk
      prompts are short by construction; only a few outliers dropped.
- Commit `5a7c908` re-enables `eval_strategy="steps"` after the data
  filter is in place.

## 2026-05-04 — DPO trainer fixes (TRL 1.x API drift + 2× memory)

- **`max_prompt_length` removed from `DPOConfig`** in TRL 1.x. Dropped
  the kwarg in `train_dpo.py` (commit `358df4c`).
- **`gradient_checkpointing` enabled** in DPOConfig (commit `fd8b745`).
  DPO does two forward passes per step (chosen + rejected), so
  activation memory is ~2× SFT.
- **Idempotent runner**: `run_training.sh` and `run_training_hunk.sh`
  now skip SFT if `runs/sft/adapter_model.safetensors` exists, so
  restarts from a DPO failure don't waste 90 minutes redoing SFT
  (commit `2c53300`).

## 2026-05-04 — switch from L4 (24 GB) to A100 (40 GB)

L4's 24 GB couldn't hold cross-entropy logits at full Qwen 2.5 vocab
(152 K) for sequences much over 12 K BPE — even after enabling
gradient checkpointing, the logits live in the forward pass and aren't
helped by activation checkpointing. After ~5 hours of OOM iteration
(28 K cap → 10 K cap → checkpointing → still OOM at eval step 250),
moved to A100 40 GB.

- **Two A100s in parallel** in `us-central1-b` (other zones stocked
  out): `crb-finetune-a100-pr` (pr-level) and `crb-finetune-a100-hunk`
  (hunk-level).
- L4 instances deleted to stop the meter.
- Cost: ~$3.50/hr × 2 VMs × ~8 h ≈ $56 for the full pipeline (vs
  $18 estimated on L4 if the L4 had worked).
- Data unchanged — both A100s pull from the existing GCS objects.

## 2026-05-04 — unified W&B logging (eval metrics on training run)

- **Trainers write `<output_dir>/wandb_run_id.txt`** when training
  finishes (commit `2270a7c`).
- **`evaluate.py` reads that file** and resumes the same W&B run via
  `wandb.init(id=..., resume="allow")`. Eval P/R/F1 + bootstrap CIs
  + per-PR table land on the same run as training curves.
- Caveat: the v1 SFT runs that already finished pre-deploy don't have
  the run-id file, so their eval metrics will appear on sibling
  `eval-runs/sft` runs. New SFT/DPO runs are unified.

## 2026-05-04 — `gradient_checkpointing=True` default in SFT trainer

- Added a `gradient_checkpointing` field to `train_sft.py`'s
  `Hyperparameters`; defaults to True and feeds into `SFTConfig`
  (commit `3880764`). Required for L4-fit; benign on A100.

## 2026-05-04 — autonomous run, partially blocked

Authorised to run autonomously while user away ~5 hrs. Got partway
through v2/pr-level data prep before both `gcloud` CLI auth and ADC
expired (RAPT cycle ~1 hr for sensitive APIs).

- **Re-filtered v1 data** with the real Qwen 2.5 BPE tokenizer (cap
  28K tokens for prompt+response) instead of the old whitespace
  heuristic. Produces v2/pr-level by post-filtering — no DB pull
  needed. Yields:
    - SFT train 12,731 / val 1,946 (96.4 % / 95.8 % of v1)
    - DPO train 4,185 / val 540 (94.5 % / 94.7 %)
    - eval 1,719 (84.6 %)
- **Local artifact** at `/tmp/v2_pr/` with `manifest.json`. Not yet
  uploaded to GCS — auth needed.
- **Hunk-level path: deferred.** Needs fresh DB pull for
  `commit_details` (existing JSONL is post-pairs, no anchors).
- VM `crb-finetune-l4` left running idle, ~$0.70/hr.

Resume path: refresh BOTH `gcloud auth login` (CLI) AND
`gcloud auth application-default login` (ADC). Then upload v2/pr-level
to GCS, VM pulls, restart.

## 2026-05-02 — context-length fix v2: hunk-level prompts + tokenizer filter

Two parallel branches of the pipeline to compare head-to-head. Both share
SFT/DPO trainers and the eval harness; they differ only in dataset shape.

- **Added `crb_paper/pairs.py::extract_hunk_around()`** — given a row's
  `commit_details` JSON plus a suggestion's `file_path` / `line_number`,
  returns the unified-diff hunk (`@@ ... @@` block) the suggestion
  anchors to. Standard `unidiff` parsing, stdlib only.
- **Added `crb_paper/pairs.py::build_hunk_prompt()` + `HUNK_PROMPT_TEMPLATE`** —
  hunk-level prompt format. Just `"File: X\nDiff: <hunk>"` instead of
  the whole-PR template. Each prompt is 100–500 tokens instead of
  thousands.
- **Added `crb_paper/pairs.py::build_hunk_dpo_dataset()` and
  `build_hunk_sft_dataset()`** — pair within `(PR, bot, file)`. Both
  sides of every DPO pair anchor to the same file's hunk, so chosen and
  rejected literally share the prompt.
- **Added `crb_paper/pairs.py::make_qwen_len_fn()`** — factory that
  returns a real-tokenizer length function (Qwen BPE) for the
  drop-overlong path. Replaces the `whitespace_len` heuristic which
  under-counted by 2–3× for code-heavy prompts.
- **Added `--mode {pr-level,hunk-level}` to `prepare_jsonl.py`** —
  `pr-level` uses the existing PR-diff prompts but with the new
  tokenizer-aware filter (cap 28K BPE tokens for prompt+response).
  `hunk-level` uses the new anchored prompts.
- **Why:** the v1 SFT run crashed at step 250 because right-truncation
  cut the assistant response when prompt+response exceeded Qwen's 32K
  context. The whitespace-token filter at 12K let through prompts that
  tokenized to 41K BPE. Both fixes address this from different angles —
  v1 (drop overlong) is a workaround; v2 (hunk-level) addresses the
  root cause of input/task granularity mismatch.

## 2026-05-01 — TRL 1.x migration

- Updated `train_sft.py` to TRL 1.x API: `messages` dataset format
  (let TRL apply chat template), `processing_class=` instead of
  `tokenizer=`, removed `max_seq_length` from SFTConfig (handled by
  tokenizer in 1.x).
- Updated `train_dpo.py` similarly: `processing_class=`.
- Commit `8a3fa2b`.

## 2026-05-01 — initial dataset materialisation

- Generated 150K-row sample, applied silver filter and CodeRabbit
  drop, produced 5K DPO pairs / 13K SFT rows / 2K eval rows.
- Materialised as immutable JSONL artifacts at
  `gs://martian-research-crb-finetune/20260501T205759Z/data/`
  with a `manifest.json` capturing run params + sha256s.
- **Why:** decouple training from DB access. Once data is on GCS, no
  training run needs the proxy or the user's laptop online.

## 2026-04-30 — train/val cutoff on `bot_reviewed_at`

- Changed split field from `pr_created_at` (74 % NULL) to
  `bot_reviewed_at` (always set). Cutoff `2026-03-31` gives ~83/17.
- Added `random_sample` option to `db.fetch_pr_analyses()` so LIMIT
  pulls a date-diverse slice instead of earliest-first.
- Commit `06789b7`, `6d18241`.

## 2026-04-30 — diff loading from `commit_details`

- `db._normalize_row()` now constructs the unified diff from
  `prs.commit_details` JSON and injects it as `row["diff"]`.
- **Why:** initial inspection report showed prompt length uniformly
  23 tokens (just template + title). The DB SELECT was returning
  `assembled` (timeline JSON) instead of a unified diff string. Fixed
  by parsing per-file patches out of `commit_details`.
- Commit `0793384`.

## 2026-04-30 — within-bot DPO pairs + bot balance

- Pair construction restricted to same `(PR, bot)`: chosen and rejected
  always come from the same bot. Removes bot-style confound.
- Added dataset-wide bot balancing — cap each bot's contribution at
  the p75 of per-bot pair counts.
- **Why:** to make inverted-labels-fail prove "the suggestion's
  substance carries signal," not "tool identity carries signal."
- Commit `de7a2c3`.

## 2026-04-29 — initial pipeline

- Ported silver-preset filters from `compute.rs` (Rust dashboard) to
  `crb_paper/filters.py`.
- Built preference-pair extractor (`crb_paper/pairs.py`).
- Read-only Cloud SQL connector (`crb_paper/db.py`).
- SFT trainer scaffold (`crb_paper/train_sft.py`).
- DPO trainer scaffold with 4 ablation conditions (`crb_paper/train_dpo.py`).
- Evaluation harness with bootstrap CIs (`crb_paper/evaluate.py`).
- Drop CodeRabbit row-level (`coderabbitai[bot]`) per CLAUDE.md
  carveout. Catapult is not in CRB chatbots.
- Commits `562df9b` … `de7a2c3`, `c405c0e`, `c975252`, `b82c832`,
  `efb7806`, `2c91080`, `9b393ca`.


# Changelog

Documents notable changes to the CRB finetuning pipeline so the paper can
trace what was tried, what worked, and what didn't.

Format: dated entries, newest first. Each bullet links to the commit SHA
when relevant. "Why" line where the rationale isn't obvious from the
change itself.

---

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


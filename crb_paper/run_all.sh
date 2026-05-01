#!/usr/bin/env bash
# Full CRB finetuning experiment runner.
#
# Runs the gating plan end-to-end on a single GPU host:
#   1. dataset prep (queries crb-main via the cloud-sql-proxy)
#   2. SFT warm-start (Qwen2.5-Coder-7B + LoRA)
#   3. four DPO ablations (filtered / unfiltered / inverted / random)
#   4. evaluation per condition (P/R/F1 + 95% bootstrap CIs)
#
# Prerequisites on the host:
#   - cloud-sql-proxy installed and running on localhost:15432
#   - .env (in repo root) populated — GCP_SQL_*, WANDB_API_KEY, OPENAI_API_KEY
#   - pip install transformers peft trl accelerate datasets torch wandb \
#                 python-dotenv psycopg2-binary openai
#   - GPU with ≥24GB VRAM (L4 or A100)
#
# Usage (from the repo root):
#   bash crb_paper/run_all.sh
#
# Override knobs via env vars:
#   LIMIT=200000 MAX_PAIRS=8000 K=10 bash crb_paper/run_all.sh

set -euo pipefail

# ---- Config (override via env) -------------------------------------------
DATA_DIR="${DATA_DIR:-data}"
RUN_DIR="${RUN_DIR:-runs}"
RESULTS_DIR="${RESULTS_DIR:-results}"
LIMIT="${LIMIT:-150000}"
MAX_PAIRS="${MAX_PAIRS:-5000}"
K="${K:-5}"
CONDITIONS=(filtered unfiltered inverted random)

mkdir -p "$DATA_DIR" "$RUN_DIR" "$RESULTS_DIR"
log() { printf '\n=== %s ===\n' "$*"; }

# ---- 1. Dataset prep -----------------------------------------------------
log "1/4 prepare_jsonl  (LIMIT=$LIMIT MAX_PAIRS=$MAX_PAIRS)"
python3 crb_paper/prepare_jsonl.py \
    --limit "$LIMIT" \
    --max-pairs "$MAX_PAIRS" \
    --output-dir "$DATA_DIR"

# ---- 2. SFT warm-start ---------------------------------------------------
log "2/4 SFT warm-start"
python3 crb_paper/train_sft.py \
    --train-file "$DATA_DIR/sft_train.jsonl" \
    --eval-file  "$DATA_DIR/sft_val.jsonl" \
    --output-dir "$RUN_DIR/sft" \
    --run-name   "sft-$(date +%Y%m%d-%H%M%S)"

# ---- 3. DPO ablations ----------------------------------------------------
log "3/4 DPO ablations  (${CONDITIONS[*]})"
for cond in "${CONDITIONS[@]}"; do
    log "  DPO --condition=$cond"
    python3 crb_paper/train_dpo.py \
        --train-file       "$DATA_DIR/dpo_train.jsonl" \
        --eval-file        "$DATA_DIR/dpo_val.jsonl" \
        --sft-checkpoint   "$RUN_DIR/sft" \
        --condition        "$cond" \
        --output-dir       "$RUN_DIR/dpo_$cond"
done

# ---- 4. Eval -------------------------------------------------------------
log "4/4 Evaluate each condition  (k=$K)"
for cond in "${CONDITIONS[@]}"; do
    log "  eval $cond"
    python3 crb_paper/evaluate.py \
        --checkpoint "$RUN_DIR/dpo_$cond" \
        --eval-file  "$DATA_DIR/eval.jsonl" \
        --k          "$K" \
        --output     "$RESULTS_DIR/eval_$cond.csv"
done

# Also evaluate the SFT-only checkpoint (sanity baseline) and the base
# model (no LoRA) for reference points.
log "  eval sft (baseline)"
python3 crb_paper/evaluate.py \
    --checkpoint "$RUN_DIR/sft" \
    --eval-file  "$DATA_DIR/eval.jsonl" \
    --k          "$K" \
    --output     "$RESULTS_DIR/eval_sft.csv"

log "  eval base model (no LoRA)"
python3 crb_paper/evaluate.py \
    --no-lora \
    --eval-file  "$DATA_DIR/eval.jsonl" \
    --k          "$K" \
    --output     "$RESULTS_DIR/eval_base.csv"

log "DONE. Per-condition CSVs in $RESULTS_DIR/."

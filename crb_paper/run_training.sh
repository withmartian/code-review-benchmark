#!/usr/bin/env bash
# PR-level v2 training runner. SFT + 4 DPO ablations + 6 evals,
# all on data already pulled into ./data/ (no prepare_jsonl step).
# Default W&B run names — pair with run_training_hunk.sh for the
# parallel comparison.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

DATA=data
RUN=runs
RES=results
K=5
TS=$(date +%Y%m%d-%H%M%S)
mkdir -p "$RUN" "$RES"
log() { printf "\n=== %s === %s\n" "$(date -u +%H:%M:%SZ)" "$*"; }

log "1/3 SFT warm-start"
# Idempotent skip: top-level adapter or any saved checkpoint adapter counts.
if [ -f "$RUN/sft/adapter_model.safetensors" ] || [ -f "$RUN/sft/adapter_model.bin" ] \
   || ls "$RUN"/sft/checkpoint-*/adapter_model.safetensors >/dev/null 2>&1; then
    log "  SFT checkpoint exists — skipping."
    # If the adapter only lives in checkpoint-*/, copy it to top-level so
    # downstream tooling (train_dpo.py --sft-checkpoint, evaluate.py) finds
    # it where they look.
    if [ ! -f "$RUN/sft/adapter_model.safetensors" ] && [ ! -f "$RUN/sft/adapter_model.bin" ]; then
        latest=$(ls -d "$RUN"/sft/checkpoint-*/ 2>/dev/null | sort -V | tail -1)
        if [ -n "$latest" ]; then
            cp "$latest"/adapter_*.safetensors "$RUN/sft/" 2>/dev/null || true
            cp "$latest"/adapter_config.json "$RUN/sft/" 2>/dev/null || true
            log "  copied adapter from $latest to $RUN/sft/"
        fi
    fi
else
    python3 crb_paper/train_sft.py \
        --train-file "$DATA/sft_train.jsonl" --eval-file "$DATA/sft_val.jsonl" \
        --output-dir "$RUN/sft" --run-name "sft-$TS"
fi

log "2/3 DPO ablations"
for cond in filtered unfiltered inverted random; do
    log "  DPO --condition=$cond"
    python3 crb_paper/train_dpo.py \
        --train-file "$DATA/dpo_train.jsonl" --eval-file "$DATA/dpo_val.jsonl" \
        --sft-checkpoint "$RUN/sft" --condition "$cond" \
        --output-dir "$RUN/dpo_$cond" --run-name "dpo-$cond-$TS"
done

log "3/3 Evaluation"
for cond in filtered unfiltered inverted random; do
    log "  eval $cond"
    python3 crb_paper/evaluate.py \
        --checkpoint "$RUN/dpo_$cond" --eval-file "$DATA/eval.jsonl" \
        --k $K --output "$RES/eval_$cond.csv" || echo "(eval $cond failed)"
done
log "  eval sft baseline"
python3 crb_paper/evaluate.py --checkpoint "$RUN/sft" \
    --eval-file "$DATA/eval.jsonl" --k $K --output "$RES/eval_sft.csv" || true
log "  eval base"
python3 crb_paper/evaluate.py --no-lora --eval-file "$DATA/eval.jsonl" \
    --k $K --output "$RES/eval_base.csv" || true
log "DONE."

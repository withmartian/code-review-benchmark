#!/usr/bin/env bash
# Hunk-level v2 training runner. Mirrors run_training.sh but with
# `hunk-` prefixes on W&B run names so the two pipelines can be
# compared in the same project.
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
python3 crb_paper/train_sft.py \
    --train-file "$DATA/sft_train.jsonl" --eval-file "$DATA/sft_val.jsonl" \
    --output-dir "$RUN/sft" --run-name "hunk-sft-$TS"

log "2/3 DPO ablations"
for cond in filtered unfiltered inverted random; do
    log "  DPO --condition=$cond"
    python3 crb_paper/train_dpo.py \
        --train-file "$DATA/dpo_train.jsonl" --eval-file "$DATA/dpo_val.jsonl" \
        --sft-checkpoint "$RUN/sft" --condition "$cond" \
        --output-dir "$RUN/dpo_$cond" --run-name "hunk-dpo-$cond-$TS"
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

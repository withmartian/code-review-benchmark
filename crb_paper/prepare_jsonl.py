"""Produce train/val SFT and DPO JSONL files for the CRB finetuning pipeline.

Pipeline (same as `inspect_dataset.py`, but writes data instead of a report):

  1. db.fetch_pr_analyses(random_sample=True) — date-diverse rows.
  2. Drop rows from `coderabbitai[bot]` (CodeRabbit exclusion).
  3. filters.apply_filters(silver) — drops ~95% of rows.
  4. pairs.build_sft_dataset() and pairs.build_dpo_dataset() with default
     PairConfig (within-bot, length-matched, bot-balanced, 12k-token
     prompt cutoff).
  5. Optional cap on DPO pairs.
  6. Split each row by `bot_reviewed_at <= cutoff` into train/val.
  7. Write four files into `--output-dir`:
        sft_train.jsonl, sft_val.jsonl, dpo_train.jsonl, dpo_val.jsonl

Run:

    python3 crb_paper/prepare_jsonl.py \\
        --limit 150000 --max-pairs 5000 \\
        --output-dir data/

Then point the trainers at the output:

    python3 crb_paper/train_sft.py \\
        --train-file data/sft_train.jsonl --eval-file data/sft_val.jsonl \\
        --output-dir runs/sft

    python3 crb_paper/train_dpo.py \\
        --train-file data/dpo_train.jsonl --eval-file data/dpo_val.jsonl \\
        --condition filtered --output-dir runs/dpo_filtered
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# Allow `python3 crb_paper/prepare_jsonl.py` to find sibling modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import filters  # noqa: E402
import pairs  # noqa: E402


DEFAULT_CUTOFF = date(2026, 3, 31)
EXCLUDE_CHATBOTS = {"coderabbitai[bot]"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=150_000,
                   help="Max (PR × bot) rows to fetch. 0 = no limit (~660k).")
    p.add_argument("--max-pairs", type=int, default=5_000,
                   help="Max DPO pairs (post-balance). 0 = keep all.")
    p.add_argument("--cutoff", type=lambda s: date.fromisoformat(s),
                   default=DEFAULT_CUTOFF,
                   help="Train/val cutoff on bot_reviewed_at (YYYY-MM-DD).")
    p.add_argument("--output-dir", type=Path, required=True,
                   help="Output directory for the 4 JSONL files.")
    p.add_argument("--seed", type=int, default=0xC0DECAFE,
                   help="Sampling seed (deterministic).")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Train/val split
# ---------------------------------------------------------------------------

def _row_date(row: dict) -> Optional[date]:
    """Extract the canonical split date from a row (bot_reviewed_at,
    falling back to pr_created_at)."""
    ts = row.get("bot_reviewed_at") or row.get("pr_created_at")
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, date):
        return ts
    return None


def split_rows(rows: list[dict], cutoff: date) -> tuple[list[dict], list[dict]]:
    """Split into (train, val) using `bot_reviewed_at <= cutoff`. Undated
    rows go to val to avoid leaking into train."""
    train, val = [], []
    for r in rows:
        d = _row_date(r)
        (train if d is not None and d <= cutoff else val).append(r)
    return train, val


# ---------------------------------------------------------------------------
# JSONL writer
# ---------------------------------------------------------------------------

def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str))
            f.write("\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    print("connecting to Cloud SQL...")
    conn = db.connect_readonly()
    try:
        limit = args.limit if args.limit > 0 else None
        print(f"fetching pr_analyses (limit={limit}, random_sample=True)...")
        raw = db.fetch_pr_analyses(conn, limit=limit, random_sample=True)
        ignored = db.fetch_ignored_chatbots(conn)
    finally:
        conn.close()
    print(f"  fetched {len(raw):,} rows")

    after_cr = [r for r in raw if r["github_username"] not in EXCLUDE_CHATBOTS]
    print(f"  after CodeRabbit drop: {len(after_cr):,}")

    print("applying silver-preset filters...")
    silver = filters.apply_filters(
        after_cr, filters.FilterParams.silver(), ignored_chatbots=ignored,
    )
    print(f"  after silver: {len(silver):,}")

    print("building SFT rows...")
    sft_all = pairs.build_sft_dataset(silver)
    print(f"  SFT rows: {len(sft_all):,}")

    print("building DPO pairs...")
    dpo_all = pairs.build_dpo_dataset(silver)
    print(f"  DPO pairs: {len(dpo_all):,}")

    if args.max_pairs > 0 and len(dpo_all) > args.max_pairs:
        rng = random.Random(args.seed)
        dpo_all = list(dpo_all)
        rng.shuffle(dpo_all)
        dpo_all = dpo_all[: args.max_pairs]
        print(f"  DPO pairs after --max-pairs sample: {len(dpo_all):,}")

    sft_train, sft_val = split_rows(sft_all, args.cutoff)
    dpo_train, dpo_val = split_rows(dpo_all, args.cutoff)
    print(f"split at {args.cutoff.isoformat()}:")
    print(f"  SFT  train={len(sft_train):,}  val={len(sft_val):,}")
    print(f"  DPO  train={len(dpo_train):,}  val={len(dpo_val):,}")

    out = args.output_dir
    write_jsonl(out / "sft_train.jsonl", sft_train)
    write_jsonl(out / "sft_val.jsonl", sft_val)
    write_jsonl(out / "dpo_train.jsonl", dpo_train)
    write_jsonl(out / "dpo_val.jsonl", dpo_val)
    print(f"wrote 4 files to {out}/")


if __name__ == "__main__":
    main()

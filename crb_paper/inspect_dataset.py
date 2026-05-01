"""Dataset inspection for the CRB finetuning pipeline.

Step 3 of the gating plan in `crb_paper/README.md`.

Pulls PR analyses from the live Cloud SQL DB, applies the silver
preset (with CodeRabbit excluded), runs the within-bot pair extractor,
and writes a markdown report covering counts, the 2026-04-20 train/val
split, the per-bot DPO yield, length quantiles, and the top repos by
pair contribution.

Defaults are right-sized for the v1 experiment: pull 50k analyses,
cap the final DPO set at ~5k pairs. Both knobs are CLI flags; pass
`--limit 0` for the full join and `--max-pairs 0` to skip the cap.

Run:

    python3 crb_paper/inspect.py
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# Allow `python3 crb_paper/inspect.py` to find sibling modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import filters  # noqa: E402
import pairs  # noqa: E402


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DEFAULT_REPORT = Path(__file__).parent / "inspection_report.md"
DEFAULT_CUTOFF = date(2026, 3, 31)
"""Train/val cutoff on `bot_reviewed_at`. 2026-03-31 yields ~83/17
given the current DB snapshot (44.9% of analyzed-merged rows had
bot_reviewed_at by end-of-Feb 2026, 83.2% by end-of-March)."""
EXCLUDE_CHATBOTS = {"coderabbitai[bot]"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=50_000,
                   help="Max (PR × bot) rows to fetch. 0 = no limit (~660k).")
    p.add_argument("--max-pairs", type=int, default=5_000,
                   help="Max final DPO pairs after sampling. 0 = keep all.")
    p.add_argument("--cutoff", type=lambda s: date.fromisoformat(s),
                   default=DEFAULT_CUTOFF,
                   help="Train/val cutoff on bot_reviewed_at (YYYY-MM-DD).")
    p.add_argument("--report-path", type=Path, default=DEFAULT_REPORT,
                   help="Output path for the markdown report.")
    p.add_argument("--seed", type=int, default=0xC0DECAFE,
                   help="Sampling seed (deterministic).")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def percentile(values: list[int], p: float) -> int:
    """Linear-interpolated percentile, stdlib-only. `p` in [0, 100]."""
    if not values:
        return 0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (p / 100.0) * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return int(round(s[lo] * (1 - frac) + s[hi] * frac))


def quantile_row(label: str, values: list[int]) -> str:
    """One markdown table row of [p25, p50, p75, p95, max] for `values`."""
    if not values:
        return f"| {label} | — | — | — | — | — |"
    return (
        f"| {label} "
        f"| {percentile(values, 25)} "
        f"| {percentile(values, 50)} "
        f"| {percentile(values, 75)} "
        f"| {percentile(values, 95)} "
        f"| {max(values)} |"
    )


# ---------------------------------------------------------------------------
# Train/val split
# ---------------------------------------------------------------------------

def split_train_val(rows: list[dict], cutoff: date) -> tuple[int, int]:
    """Return (train_count, val_count). Splits on `bot_reviewed_at`
    (canonical, ~always set). Falls back to `pr_created_at` for the
    rare row where bot_reviewed_at is missing. Undated rows go to val
    to avoid leaking into train."""
    train, val = 0, 0
    for r in rows:
        ts = r.get("bot_reviewed_at") or r.get("pr_created_at")
        if isinstance(ts, str):
            try:
                d = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
            except ValueError:
                d = None
        elif isinstance(ts, datetime):
            d = ts.date()
        elif isinstance(ts, date):
            d = ts
        else:
            d = None
        if d is None:
            val += 1  # treat undated as val to avoid leaking into train
        elif d <= cutoff:
            train += 1
        else:
            val += 1
    return train, val


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def render_report(
    *,
    limit: int,
    max_pairs: int,
    cutoff: date,
    raw_count: int,
    after_cr_drop: int,
    after_silver: int,
    sft_rows: list[dict],
    dpo_pairs_full: list[dict],
    dpo_pairs_sampled: list[dict],
) -> str:
    sft_train, sft_val = split_train_val(sft_rows, cutoff)
    dpo_train, dpo_val = split_train_val(dpo_pairs_full, cutoff)

    bot_counts = Counter(p["chatbot"] for p in dpo_pairs_full)
    repo_counts = Counter(p["repo_name"] for p in dpo_pairs_full)

    prompt_tokens = [pairs.whitespace_len(p["prompt"]) for p in dpo_pairs_full]
    chosen_tokens = [p["chosen_tokens"] for p in dpo_pairs_full]
    rejected_tokens = [p["rejected_tokens"] for p in dpo_pairs_full]

    out: list[str] = []
    out.append(f"# Dataset inspection report\n")
    out.append(f"_Generated by `crb_paper/inspect.py`. Run knobs: "
               f"`--limit {limit}`, `--max-pairs {max_pairs}`._\n")

    # ---- Total counts -----------------------------------------------------
    out.append("## Total counts\n")
    out.append("| Stage | Count |")
    out.append("|---|---|")
    out.append(f"| Raw (PR × bot) rows fetched | {raw_count:,} |")
    out.append(f"| After CodeRabbit drop | {after_cr_drop:,} |")
    out.append(f"| After silver-preset filter | {after_silver:,} |")
    out.append(f"| SFT rows (accepted suggestions) | {len(sft_rows):,} |")
    out.append(f"| DPO pairs (within-bot, length-matched, balanced) | {len(dpo_pairs_full):,} |")
    out.append(f"| DPO pairs after `--max-pairs` sample | {len(dpo_pairs_sampled):,} |")
    out.append("")

    # ---- Train/val split --------------------------------------------------
    out.append(f"## Train/val split at {cutoff.isoformat()}\n")
    out.append("Split by `bot_reviewed_at` (falls back to `pr_created_at`). Undated rows go to val.\n")
    out.append("| Dataset | Train | Val |")
    out.append("|---|---|---|")
    out.append(f"| SFT | {sft_train:,} | {sft_val:,} |")
    out.append(f"| DPO (pre-sample) | {dpo_train:,} | {dpo_val:,} |")
    out.append("")

    # ---- Per-bot DPO distribution -----------------------------------------
    out.append("## Per-bot DPO pair distribution\n")
    out.append("Counts on the **pre-sample** DPO set (post silver-filter, post CodeRabbit drop, post bot-balance).\n")
    out.append("| Bot | Pairs | Share |")
    out.append("|---|---|---|")
    total = sum(bot_counts.values()) or 1
    for bot, n in bot_counts.most_common():
        out.append(f"| `{bot}` | {n:,} | {100*n/total:.1f}% |")
    out.append("")

    # ---- Length distribution ----------------------------------------------
    out.append("## Length distribution (whitespace tokens)\n")
    out.append("On the **pre-sample** DPO set.\n")
    out.append("| Field | p25 | p50 | p75 | p95 | max |")
    out.append("|---|---|---|---|---|---|")
    out.append(quantile_row("`prompt`", prompt_tokens))
    out.append(quantile_row("`chosen`", chosen_tokens))
    out.append(quantile_row("`rejected`", rejected_tokens))
    out.append("")

    # ---- Top repos --------------------------------------------------------
    out.append("## Top 20 repos by DPO pair count\n")
    out.append("| Repo | Pairs |")
    out.append("|---|---|")
    for repo, n in repo_counts.most_common(20):
        out.append(f"| `{repo}` | {n:,} |")
    out.append("")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    # 1. Connect + fetch
    print(f"connecting to Cloud SQL...")
    conn = db.connect_readonly()
    try:
        limit = args.limit if args.limit > 0 else None
        print(f"fetching pr_analyses (limit={limit})...")
        raw = db.fetch_pr_analyses(conn, limit=limit)
        ignored = db.fetch_ignored_chatbots(conn)
    finally:
        conn.close()
    raw_count = len(raw)
    print(f"  fetched {raw_count:,} rows")

    # 2. Drop CodeRabbit
    after_cr = [r for r in raw if r["github_username"] not in EXCLUDE_CHATBOTS]
    print(f"  after CodeRabbit drop: {len(after_cr):,}")

    # 3. Silver-preset filter
    print("applying silver-preset filters...")
    silver = filters.apply_filters(
        after_cr,
        filters.FilterParams.silver(),
        ignored_chatbots=ignored,
    )
    print(f"  after silver: {len(silver):,}")

    # 4. Build SFT and DPO sets (full, before per-pair cap)
    print("building SFT rows...")
    sft_rows = pairs.build_sft_dataset(silver)
    print(f"  SFT rows: {len(sft_rows):,}")

    print("building DPO pairs...")
    dpo_full = pairs.build_dpo_dataset(silver)
    print(f"  DPO pairs (pre-sample): {len(dpo_full):,}")

    # 5. Optionally cap pairs (random sample, deterministic)
    if args.max_pairs > 0 and len(dpo_full) > args.max_pairs:
        rng = random.Random(args.seed)
        dpo_sampled = list(dpo_full)
        rng.shuffle(dpo_sampled)
        dpo_sampled = dpo_sampled[: args.max_pairs]
        print(f"  DPO pairs after --max-pairs sample: {len(dpo_sampled):,}")
    else:
        dpo_sampled = dpo_full

    # 6. Render + write report
    print(f"writing report to {args.report_path}...")
    report = render_report(
        limit=args.limit,
        max_pairs=args.max_pairs,
        cutoff=args.cutoff,
        raw_count=raw_count,
        after_cr_drop=len(after_cr),
        after_silver=len(silver),
        sft_rows=sft_rows,
        dpo_pairs_full=dpo_full,
        dpo_pairs_sampled=dpo_sampled,
    )
    args.report_path.write_text(report)
    print("done.")


if __name__ == "__main__":
    main()

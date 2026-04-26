"""Stratified sampler: pull 100 diverse PRs from the online DB for judge validation.

Stratification axes:
  - Tools: proportional to scored-PR volume, every tool with >=50 scored PRs gets >=2
  - Outcome types: high-precision, low-precision, zero-suggestion PRs
  - Languages/domains: via pr_labels if available

Output: a JSON manifest with PR metadata + raw columns needed to reconstruct
the 3-step judge inputs (commits, commit_details, reviews, assembled).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Repo root is 3 levels up: judge_agreement -> validation -> analysis -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "online" / "etl"))

from config import DBConfig  # noqa: E402
from db.connection import DBAdapter  # noqa: E402

logger = logging.getLogger(__name__)

OUT_DIR = Path(__file__).resolve().parent / "results"

TARGET_SAMPLE_SIZE = 200
MIN_TOOL_SAMPLES = 5
MIN_TOOL_SCORED_PRS = 50
RNG_SEED = 42

# Tools to exclude: too small, not in the paper leaderboard, or not a real code review bot
EXCLUDED_TOOLS = frozenset({
    "sentry[bot]",
    "propel-code-bot[bot]",
    "mesa-dot-dev[bot]",
    "macroscopeapp[bot]",
    "kody-ai[bot]",
    "factory-droid[bot]",
    "kiloconnect[bot]",
    "bito-code-review[bot]",
    "entelligence-ai-pr-reviews[bot]",
    "linearb[bot]",
})


def _stratified_tool_allocation(tool_counts: dict[str, int], target: int) -> dict[str, int]:
    """Allocate sample slots per tool, proportional to volume with a minimum floor."""
    eligible = {t: n for t, n in tool_counts.items() if n >= MIN_TOOL_SCORED_PRS}
    total = sum(eligible.values())
    if total == 0:
        return {}

    alloc: dict[str, int] = {}
    for tool, count in eligible.items():
        alloc[tool] = max(MIN_TOOL_SAMPLES, round(target * count / total))

    # Normalize to target
    current = sum(alloc.values())
    if current > target:
        sorted_tools = sorted(alloc, key=lambda t: alloc[t], reverse=True)
        for t in sorted_tools:
            if current <= target:
                break
            reduce = min(alloc[t] - MIN_TOOL_SAMPLES, current - target)
            alloc[t] -= reduce
            current -= reduce
    elif current < target:
        sorted_tools = sorted(alloc, key=lambda t: eligible[t], reverse=True)
        for t in sorted_tools:
            if current >= target:
                break
            alloc[t] += 1
            current += 1

    return alloc


async def _sample_prs(db: DBAdapter, target: int = TARGET_SAMPLE_SIZE) -> list[dict]:
    """Sample PRs from the DB with stratification across tools and outcome types."""
    import random
    rng = random.Random(RNG_SEED)

    # Default filter SQL fragment:
    #   - Exclude bot-authored PRs (pr_author ending in [bot])
    #   - Require human engagement (engagement_signals JSON has_human_engagement)
    #   - Only scored PRs (precision IS NOT NULL)
    # Concentration cap (max 50 PRs per author-repo-bot triple) is applied post-query
    # since it requires ranking and is hard to express in portable SQL.
    is_pg = db.is_postgres
    engagement_filter = (
        "(p.engagement_signals::jsonb->>'has_human_engagement')::boolean = true"
        if is_pg else
        "json_extract(p.engagement_signals, '$.has_human_engagement') = 1"
    )
    default_filter = f"""
        la.precision IS NOT NULL
        AND p.pr_author NOT LIKE '%%[bot]'
        AND p.engagement_signals IS NOT NULL
        AND {engagement_filter}
    """

    tool_counts_rows = await db.fetchall(f"""
        SELECT c.github_username, COUNT(*) as n
        FROM llm_analyses la
        JOIN prs p ON la.pr_id = p.id
        JOIN chatbots c ON la.chatbot_id = c.id
        WHERE {default_filter}
        GROUP BY c.github_username
    """)
    tool_counts = {
        r["github_username"]: r["n"]
        for r in tool_counts_rows
        if r["github_username"] not in EXCLUDED_TOOLS
    }
    logger.info(f"Found {len(tool_counts)} tools with scored PRs (default filters): {tool_counts}")

    alloc = _stratified_tool_allocation(tool_counts, target)
    logger.info(f"Allocation: {alloc}")

    sampled: list[dict] = []

    for tool, n_needed in alloc.items():
        # Pull scored PRs for this tool that pass default filters
        rows = await db.fetchall(f"""
            SELECT
                p.id as pr_id,
                p.repo_name,
                p.pr_number,
                p.pr_url,
                p.pr_title,
                p.pr_author,
                c.github_username as tool,
                la.precision,
                la.recall,
                la.f_beta,
                la.total_bot_comments as n_suggestions,
                la.matched_bot_comments as n_matched,
                la.model_name
            FROM llm_analyses la
            JOIN prs p ON la.pr_id = p.id
            JOIN chatbots c ON la.chatbot_id = c.id
            WHERE c.github_username = $1
              AND {default_filter}
        """, (tool,))

        if not rows:
            continue

        # Apply concentration cap: max 50 PRs per (repo, author, bot) triple
        # Deterministic sampling via sorted order (matches API behavior)
        triple_counts: dict[tuple[str, str], int] = {}
        capped_rows: list[dict] = []
        for r in sorted(rows, key=lambda x: x["pr_id"]):
            triple = (r["repo_name"], r["pr_author"])
            triple_counts[triple] = triple_counts.get(triple, 0) + 1
            if triple_counts[triple] <= 50:
                capped_rows.append(r)
        rows = capped_rows

        # Stratify by outcome: split into terciles by precision, but only
        # among PRs that have at least 1 suggestion (nonzero denominator).
        has_suggestions = [r for r in rows if (r["n_suggestions"] or 0) > 0]
        zero_sugg = [r for r in rows if (r["n_suggestions"] or 0) == 0]

        if not has_suggestions:
            # Edge case: tool has no PRs with suggestions at all
            picked = rng.sample(rows, min(n_needed, len(rows)))
        else:
            rows_sorted = sorted(has_suggestions, key=lambda r: r["precision"] or 0)
            tercile_size = max(1, len(rows_sorted) // 3)
            low = rows_sorted[:tercile_size]
            mid = rows_sorted[tercile_size:2 * tercile_size]
            high = rows_sorted[2 * tercile_size:]

            # Allocate: 1 zero-suggestion (if any), rest split evenly across terciles
            n_zero = min(1, len(zero_sugg)) if zero_sugg else 0
            remaining = n_needed - n_zero
            n_per_tercile = max(1, remaining // 3)

            picked: list[dict] = []
            if n_zero > 0:
                picked.extend(rng.sample(zero_sugg, n_zero))
            picked.extend(rng.sample(low, min(n_per_tercile, len(low))))
            picked.extend(rng.sample(mid, min(n_per_tercile, len(mid))))
            picked.extend(rng.sample(high, min(n_per_tercile, len(high))))

        # Deduplicate and trim to n_needed
        seen_ids = set()
        unique_picked = []
        for r in picked:
            if r["pr_id"] not in seen_ids:
                seen_ids.add(r["pr_id"])
                unique_picked.append(r)
        unique_picked = unique_picked[:n_needed]
        sampled.extend(unique_picked)

    logger.info(f"Sampled {len(sampled)} PRs across {len(alloc)} tools")
    return sampled


async def _enrich_with_raw_data(db: DBAdapter, sampled: list[dict]) -> list[dict]:
    """Pull raw columns needed for re-judging: commits, commit_details, reviews, assembled."""
    enriched = []
    for pr in sampled:
        row = await db.fetchone("""
            SELECT
                p.commits,
                p.commit_details,
                p.reviews,
                p.assembled,
                la.bot_suggestions,
                la.human_actions,
                la.matching_results
            FROM prs p
            JOIN llm_analyses la ON la.pr_id = p.id
            JOIN chatbots c ON la.chatbot_id = c.id
            WHERE p.id = $1 AND c.github_username = $2
        """, (pr["pr_id"], pr["tool"]))

        if row is None:
            logger.warning(f"Could not fetch raw data for pr_id={pr['pr_id']}")
            continue

        pr_enriched = {
            **pr,
            "commits": row["commits"],
            "commit_details": row["commit_details"],
            "reviews": row["reviews"],
            "assembled": row["assembled"],
            # Existing GPT-5 Nano results (no need to re-run)
            "baseline_bot_suggestions": row["bot_suggestions"],
            "baseline_human_actions": row["human_actions"],
            "baseline_matching_results": row["matching_results"],
        }
        enriched.append(pr_enriched)

    logger.info(f"Enriched {len(enriched)}/{len(sampled)} PRs with raw data")
    return enriched


async def _enrich_with_labels(db: DBAdapter, sampled: list[dict]) -> list[dict]:
    """Attach pr_labels (language, domain) if available."""
    for pr in sampled:
        labels_row = await db.fetchone("""
            SELECT labels FROM pr_labels
            WHERE pr_id = $1
        """, (pr["pr_id"],))
        if labels_row and labels_row["labels"]:
            labels = labels_row["labels"]
            if isinstance(labels, str):
                labels = json.loads(labels)
            pr["language"] = labels.get("language")
            pr["domain"] = labels.get("domain")
        else:
            pr["language"] = None
            pr["domain"] = None
    return sampled


async def main(database_url: str | None = None) -> int:
    cfg = DBConfig()
    if database_url:
        cfg.database_url = database_url
    db = DBAdapter(cfg.database_url)
    await db.connect()

    try:
        sampled = await _sample_prs(db)
        sampled = await _enrich_with_raw_data(db, sampled)
        sampled = await _enrich_with_labels(db, sampled)

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        manifest_path = OUT_DIR / "judge_sample_manifest.json"

        # Write a summary (without raw data) for inspection
        summary = [{
            "pr_id": pr["pr_id"],
            "tool": pr["tool"],
            "repo_name": pr["repo_name"],
            "pr_number": pr["pr_number"],
            "pr_url": pr["pr_url"],
            "precision": pr["precision"],
            "recall": pr["recall"],
            "f_beta": pr["f_beta"],
            "n_suggestions": pr["n_suggestions"],
            "language": pr.get("language"),
            "domain": pr.get("domain"),
        } for pr in sampled]

        summary_path = OUT_DIR / "judge_sample_summary.json"
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        logger.info(f"Wrote summary: {summary_path}")

        # Write full manifest (with raw data for re-judging)
        with open(manifest_path, "w") as f:
            json.dump(sampled, f, indent=2, default=str)
        logger.info(f"Wrote manifest: {manifest_path} ({len(sampled)} PRs)")

        # Print distribution stats
        tools = {}
        for pr in sampled:
            tools.setdefault(pr["tool"], []).append(pr)
        print(f"\n=== Judge Validation Sample: {len(sampled)} PRs ===\n")
        print(f"{'Tool':<40} {'Count':>5}  {'Median P':>8}  {'Median R':>8}")
        print("-" * 70)
        for tool in sorted(tools):
            prs = tools[tool]
            precisions = [p["precision"] for p in prs if p["precision"] is not None]
            recalls = [p["recall"] for p in prs if p["recall"] is not None]
            med_p = sorted(precisions)[len(precisions) // 2] if precisions else None
            med_r = sorted(recalls)[len(recalls) // 2] if recalls else None
            p_str = f"{med_p:.3f}" if med_p is not None else "N/A"
            r_str = f"{med_r:.3f}" if med_r is not None else "N/A"
            print(f"{tool:<40} {len(prs):>5}  {p_str:>8}  {r_str:>8}")

        langs = {}
        for pr in sampled:
            lang = pr.get("language") or "unknown"
            langs[lang] = langs.get(lang, 0) + 1
        print(f"\nLanguages: {dict(sorted(langs.items(), key=lambda x: -x[1]))}")

        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Sample PRs for judge validation")
    parser.add_argument("--database-url", help="Override DATABASE_URL")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.database_url)))

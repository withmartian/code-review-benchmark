"""Count stored analyses whose only extracted suggestions are coverage-gate items, on your DB.

A PR whose bot posted only PR-level content (for example a walkthrough with a docstring-coverage
pre-merge warning) gets one extracted suggestion from the current prompt, the coverage item, which
no human action matches: precision 0. This PR's prompt extracts nothing there, so precision becomes
undefined and the PR leaves both precision means (the daily series, over PRs with a precision, and
the leaderboard, over PRs with both precision and recall). The measurement in the PR that added
this script has no such PR in its CodeRabbit sample, so this script sizes that part on the stored
analyses instead: it counts them and prints both means with and without them.

Nothing is written and no LLM is called. A coverage item is an unlocated suggestion (no file path)
whose description mentions docstrings together with coverage, a threshold or a percentage: the same
fixed pattern as the type table of that PR.

    uv run python -m prompt_eval.coverage_only --bot "coderabbitai[bot]"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re

from config import DBConfig
from db.connection import DBAdapter

ANALYSES_SQL = """
    SELECT la.precision, la.recall, la.bot_suggestions
    FROM llm_analyses la
    JOIN prs p ON la.pr_id = p.id
    JOIN chatbots c ON la.chatbot_id = c.id
    WHERE c.github_username = $1 AND p.pr_merged = TRUE
"""
_DOCSTRING = re.compile(r"docstring", re.I)
_GATE = re.compile(r"coverage|threshold|\d+(\.\d+)?\s*%", re.I)


def is_coverage_item(suggestion: dict) -> bool:
    text = suggestion.get("description") or ""
    return not suggestion.get("file_path") and bool(_DOCSTRING.search(text)) and bool(_GATE.search(text))


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(rows: list[dict]) -> dict:
    """Counts and precision means with and without the coverage-only analyses."""
    with_p, cov_only, rest = [], [], []
    for r in rows:
        raw = r["bot_suggestions"]
        suggestions = json.loads(raw) if isinstance(raw, str) else (raw or [])
        if r["precision"] is None:
            continue
        with_p.append(r)
        only_coverage = bool(suggestions) and all(is_coverage_item(s) for s in suggestions)
        (cov_only if only_coverage else rest).append(r)

    def both(rs: list[dict]) -> list[float]:
        return [r["precision"] for r in rs if r["recall"] is not None]

    return {
        "analyses": len(rows),
        "with_precision": len(with_p),
        "coverage_only": len(cov_only),
        "coverage_only_with_recall": len(both(cov_only)),
        "daily_mean": _mean([r["precision"] for r in with_p]),
        "daily_mean_without": _mean([r["precision"] for r in rest]),
        "leaderboard_mean": _mean(both(with_p)),
        "leaderboard_mean_without": _mean(both(rest)),
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}"


def report(bot: str, s: dict) -> str:
    share = s["coverage_only"] / s["with_precision"] if s["with_precision"] else 0.0
    return "\n".join(
        [
            f"{bot}: {s['analyses']} analyses of merged PRs, {s['with_precision']} with a precision",
            f"coverage-only analyses: {s['coverage_only']} ({100 * share:.1f}% of those with a precision), "
            f"{s['coverage_only_with_recall']} of them with a recall",
            f"daily-series precision: {_pct(s['daily_mean'])} now, {_pct(s['daily_mean_without'])} without them",
            f"leaderboard precision: {_pct(s['leaderboard_mean'])} now, "
            f"{_pct(s['leaderboard_mean_without'])} without them",
        ]
    )


async def run(bot: str, database_url: str) -> dict:
    db = DBAdapter(database_url)
    await db.connect()
    try:
        return summarize(await db.fetchall(ANALYSES_SQL, (bot,)))
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bot", default="coderabbitai[bot]")
    ap.add_argument("--database-url")
    args = ap.parse_args(argv)
    cfg = DBConfig()
    print(report(args.bot, asyncio.run(run(args.bot, args.database_url or cfg.database_url))))


if __name__ == "__main__":
    main()

"""Count stored analyses whose only extracted suggestions are items this prompt no longer extracts.

A PR whose bot posted only PR-level content (for example a walkthrough with a docstring-coverage
pre-merge warning) gets suggestions from the current prompt that no human action matches: precision
0, and recall 0 when the PR has human actions. This PR's prompt extracts nothing there, so precision
becomes undefined. The API then drops the PR from every mean: daily_metrics skips a record without a
precision (its recall goes with it), and the leaderboard averages precision and recall only over PRs
that have both. So such a PR raises precision, recall and the F score of its bot. The measurement in
the PR that added this script has no such PR in its CodeRabbit sample, so this script sizes that
part on the stored analyses instead.

An excluded item is a suggestion matching the fixed text patterns of that PR's type table: a
coverage gate (docstring or test coverage against a percentage or threshold, located or not), or,
when it names no file, a rating, run status, product tip or offer to generate code. The patterns
miss other phrasings and cannot see quoted tool output, so the counts are a lower bound. Nothing is
written and no LLM is called.

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
_COVERAGE = re.compile(
    r"(docstring.*(coverage|threshold|\d+(\.\d+)?\s*%))|((coverage|threshold|\d+(\.\d+)?\s*%).*docstring)"
    r"|((test|code|line|branch) coverage.*(\d+(\.\d+)?\s*%|threshold|required))",
    re.I | re.S,
)
_UNLOCATED_KINDS = {
    "generation_offer": re.compile(r"generate (unit )?tests?|generate docstrings|unit test generation", re.I),
    "rating": re.compile(
        r"review effort|estimated (code )?review|effort (is|rating|estimate)|merge risk (is|rating|level)"
        r"|rated (low|moderate|high)|priority (is|rating|:)",
        re.I,
    ),
    "run_status": re.compile(
        r"actionable comments|no actionable|files? selected|review profile|rate[- ]limit|allowance"
        r"|run configuration|commits? reviewed|review (was )?(skipped|paused)",
        re.I,
    ),
    "product_tip": re.compile(r"autofix|customi[sz]e your|chat with|share on", re.I),
}


def is_coverage_item(suggestion: dict) -> bool:
    return bool(_COVERAGE.search(suggestion.get("description") or ""))


def excluded_kind(suggestion: dict) -> str | None:
    """The excluded kind a suggestion matches, or None for anything else (a finding)."""
    if is_coverage_item(suggestion):
        return "coverage"
    if suggestion.get("file_path"):
        return None
    text = suggestion.get("description") or ""
    return next((kind for kind, pattern in _UNLOCATED_KINDS.items() if pattern.search(text)), None)


def f_beta(precision: float | None, recall: float | None, beta: float = 1.0) -> float | None:
    """F-beta of the two means, as the API's compute.rs computes the leaderboard F score."""
    if precision is None or recall is None:
        return None
    b2 = beta * beta
    denom = b2 * precision + recall
    return None if denom <= 0 else (1 + b2) * precision * recall / denom


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _means(rows: list[dict], beta: float) -> dict:
    with_p = [r for r in rows if r["precision"] is not None]
    both = [r for r in with_p if r["recall"] is not None]
    p, rc = _mean([r["precision"] for r in both]), _mean([r["recall"] for r in both])
    return {
        "daily_precision": _mean([r["precision"] for r in with_p]),
        "daily_recall": _mean([r["recall"] for r in both]),  # daily_metrics: recall of records with a precision
        "leaderboard_precision": p,
        "leaderboard_recall": rc,
        "leaderboard_f": f_beta(p, rc, beta),
    }


def summarize(rows: list[dict], beta: float = 1.0) -> dict:
    """Counts and means with and without the analyses whose only suggestions are excluded items."""
    kept, excluded, coverage = [], [], []
    for r in rows:
        raw = r["bot_suggestions"]
        suggestions = json.loads(raw) if isinstance(raw, str) else (raw or [])
        kinds = [excluded_kind(s) for s in suggestions]
        if r["precision"] is not None and suggestions and all(kinds):
            excluded.append(r)
            if all(k == "coverage" for k in kinds):
                coverage.append(r)
        else:
            kept.append(r)
    with_p = [r for r in rows if r["precision"] is not None]
    both = [r for r in with_p if r["recall"] is not None]
    return {
        "analyses": len(rows),
        "with_precision": len(with_p),
        "with_both": len(both),
        "excluded_only": len(excluded),
        "coverage_only": len(coverage),
        "excluded_only_with_recall": sum(1 for r in excluded if r["recall"] is not None),
        "now": _means(rows, beta),
        "without": _means(kept, beta),
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}"


def report(bot: str, s: dict) -> str:
    f = s["excluded_only"] / s["with_precision"] if s["with_precision"] else 0.0
    f2 = s["excluded_only_with_recall"] / s["with_both"] if s["with_both"] else 0.0
    lines = [
        f"{bot}: {s['analyses']} analyses of merged PRs, {s['with_precision']} with a precision, "
        f"{s['with_both']} with both precision and recall",
        f"analyses whose only suggestions are excluded items (a lower bound): {s['excluded_only']} "
        f"({s['coverage_only']} of them coverage items only)",
        f"f  = {s['excluded_only']}/{s['with_precision']} = {100 * f:.1f}% of analyses with a precision",
        f"f' = {s['excluded_only_with_recall']}/{s['with_both']} = {100 * f2:.1f}% of analyses with both",
        "mean                   now    without them",
    ]
    for key, label in (
        ("daily_precision", "daily precision"),
        ("daily_recall", "daily recall"),
        ("leaderboard_precision", "leaderboard precision"),
        ("leaderboard_recall", "leaderboard recall"),
        ("leaderboard_f", "leaderboard F"),
    ):
        lines.append(f"{label:<22} {_pct(s['now'][key]):>5}  {_pct(s['without'][key]):>5}")
    return "\n".join(lines)


async def run(bot: str, database_url: str, beta: float = 1.0) -> dict:
    db = DBAdapter(database_url)
    await db.connect()
    try:
        return summarize(await db.fetchall(ANALYSES_SQL, (bot,)), beta)
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bot", default="coderabbitai[bot]")
    ap.add_argument("--beta", type=float, default=1.0, help="F-beta weight, as the API's beta parameter")
    ap.add_argument("--database-url")
    args = ap.parse_args(argv)
    cfg = DBConfig()
    print(report(args.bot, asyncio.run(run(args.bot, args.database_url or cfg.database_url, args.beta))))


if __name__ == "__main__":
    main()

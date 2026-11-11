"""Estimate how many stored analyses have only suggestions that this prompt no longer extracts.

A PR whose bot posted only PR-level content (for example a walkthrough with a docstring-coverage
pre-merge warning, or a note that the review was paused) gets suggestions from the current prompt
that no human action matches: precision 0, and recall 0 when the PR has human actions. This PR's
prompt extracts nothing there, so precision becomes undefined. The API then drops the PR from every
mean: daily_metrics skips a record without a precision (its recall goes with it), and the leaderboard
averages precision and recall only over PRs that have both. So such a PR raises precision, recall and
the F score of its bot, whichever bot it is. The measurement in the PR that added this script has
almost no such PR in its sample, so this script estimates that part on the stored analyses.

Only unambiguous aggregate statements count as excluded, and only when the suggestion names no file:
a coverage gate (docstring or test coverage stated with a percentage, in gate or status form) and
review status (comments posted, no actionable comments, review paused or skipped, free review limits),
plus effort ratings and offers to generate code. Anything ambiguous is left as a finding, so the
figures are an estimate; --show prints the classified items for a hand check. The population is the
API's base one (merged PRs with a review date), optionally limited with --since/--until; "daily"
figures are pooled over that window, not per day. Nothing is written and no LLM is called.

    uv run python -m prompt_eval.coverage_only --bot all --since 2026-10-01 --show 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re

from config import DBConfig
from db.connection import DBAdapter

ANALYSES_SQL = """
    SELECT c.github_username AS bot, p.pr_url, la.precision, la.recall, la.bot_suggestions
    FROM llm_analyses la
    JOIN prs p ON la.pr_id = p.id
    JOIN chatbots c ON la.chatbot_id = c.id
    WHERE p.pr_merged = TRUE
      AND p.bot_reviewed_at IS NOT NULL
      AND ($1::text IS NULL OR c.github_username = $1)
      AND ($2::timestamptz IS NULL OR p.bot_reviewed_at >= $2)
      AND ($3::timestamptz IS NULL OR p.bot_reviewed_at < $3)
"""
# Whole-text templates (anchored, matched on the whitespace-normalized description) of the fixed gate,
# rating and status lines bots post, as the extraction step paraphrases them. Anything that does not
# match a whole template is a finding.
_N = r"~?\d+(?:\.\d+)?\s*%"
_SCOPE = r"\(?(?:for|on|in) [^.;:()]*?(?:functions?|diff|changes?)[^.;:()]*\)?"
_COVERAGE_TEMPLATE = (
    r"(?:\(?(?:ci/)?(?:pre-merge|ci|doc) check\)?(?: (?:failure|failed|finding|warning|reports|indicates|flagged|warns))?:? )?"
    r"(?:insufficient |only )?(?:the )?(?:ci )?(?:docstring|test) coverage(?: pre-merge| ci)?(?: check| gate| warning)?"
    rf"(?: {_SCOPE})?"
    r"(?: (?:is failing|is below(?: the)?(?: required)?(?: threshold)?|failed|fails|failing|is insufficient"
    r"|is far below(?: the)?(?: required)? threshold|is only|is|flagged|warns(?: that)?|is inconclusive/failing(?: threshold)?"
    r"|warns coverage is (?:too low|(?:far )?below(?: the)?(?: required)? threshold)|reported as|at))?:?"
    rf"(?: (?:docstring |test )?coverage(?: {_SCOPE})? is)?(?: only)? \(?{_N}(?: (?:docstring |test )?coverage)?\)?"
    rf"(?:,? \(?(?:vs\.?|<|versus|below)(?: the)?(?: required)?(?: threshold)?(?: of)? {_N}(?: threshold)?\)?"
    rf"| which is insufficient(?: \(required {_N}\))?)?"
    rf"(?:,? {_SCOPE})?"
    r"(?:[,.;:] (?:so |add|please add|missing|consider adding|bot (?:requests|recommends))[^.]*)?\.?"
    r"(?: (?:add|missing)[^.]*\.?)?"
)
_TEMPLATES = {
    "coverage": re.compile(_COVERAGE_TEMPLATE, re.I),
    # "Estimated code review effort: 3 (Moderate) | ~20 minutes", "Confidence Score: 4/5", "Priority: Normal"
    "rating": re.compile(
        r"(?:(?:the )?(?:estimated )?(?:code )?(?:review effort|risk|impact|priority|confidence score)(?: rating)?"
        r"(?: is|:)? \(?(?:\d+(?:/\d+)?|low|medium|moderate|high|normal|simple|complex)\)?"
        r"(?: \((?:low|medium|moderate|high|simple|complex)\))?(?:[ ,;|]+~?\d+\+? ?minutes)?"
        r"|\[?(?:low|medium|moderate|high) (?:impact|risk|priority)\]?(?: rating)?)\.?",
        re.I,
    ),
    # "Actionable comments posted: 2", "Bot review was paused for this user, so no ... were produced ..."
    "run_status": re.compile(
        r"(?:actionable comments posted: \d+|no actionable comments(?: were generated)?"
        r"|(?:the )?(?:bot )?review (?:was|has been) (?:paused|skipped)(?: for this (?:user|pull request|pr))?"
        r"(?:,? so no (?:automated )?(?:code-review )?findings were produced(?: in the pr comments)?)?)\.?",
        re.I,
    ),
    # "Offers to generate unit tests (beta)", "The bot offers to generate docstrings for this PR"
    "generation_offer": re.compile(
        r"(?:the bot )?offers? to generate (?:unit tests|docstrings)(?: \(beta\))?(?: for (?:this|the) (?:pr|pull request))?\.?",
        re.I,
    ),
}


def excluded_kind(suggestion: dict) -> str | None:
    """The template an unlocated suggestion's whole text matches, else None (a finding)."""
    if suggestion.get("file_path"):
        return None
    text = " ".join((suggestion.get("description") or "").split())
    return next((kind for kind, pattern in _TEMPLATES.items() if pattern.fullmatch(text)), None)


def is_coverage_item(suggestion: dict) -> bool:
    return excluded_kind(suggestion) == "coverage"


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
    kept, excluded, coverage, classified = [], [], [], []
    for r in rows:
        raw = r["bot_suggestions"]
        suggestions = json.loads(raw) if isinstance(raw, str) else (raw or [])
        kinds = [excluded_kind(s) for s in suggestions]
        classified += [
            (k, s.get("description") or "", r.get("pr_url")) for k, s in zip(kinds, suggestions, strict=True) if k
        ]
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
        "classified": classified,
        "now": _means(rows, beta),
        "without": _means(kept, beta),
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}"


def report(bot: str, s: dict, show: int = 0) -> str:
    f = s["excluded_only"] / s["with_precision"] if s["with_precision"] else 0.0
    f2 = s["excluded_only_with_recall"] / s["with_both"] if s["with_both"] else 0.0
    lines = [
        f"{bot}: {s['analyses']} analyses of merged PRs with a review date, {s['with_precision']} with a "
        f"precision, {s['with_both']} with both precision and recall",
        f"analyses whose only suggestions are excluded items (an estimate): {s['excluded_only']} "
        f"({s['coverage_only']} of them coverage items only)",
        f"f  = {s['excluded_only']}/{s['with_precision']} = {100 * f:.1f}% of analyses with a precision",
        f"f' = {s['excluded_only_with_recall']}/{s['with_both']} = {100 * f2:.1f}% of analyses with both",
        "mean (pooled over the window)  now    without them",
    ]
    for key, label in (
        ("daily_precision", "daily precision"),
        ("daily_recall", "daily recall"),
        ("leaderboard_precision", "leaderboard precision"),
        ("leaderboard_recall", "leaderboard recall"),
        ("leaderboard_f", "leaderboard F"),
    ):
        lines.append(f"{label:<30} {_pct(s['now'][key]):>5}  {_pct(s['without'][key]):>5}")
    for kind, text, url in s["classified"][:show]:
        lines.append(f"  [{kind}] {url}: {text[:160]}")
    return "\n".join(lines)


async def run(
    bot: str | None, database_url: str, since: str | None = None, until: str | None = None, beta: float = 1.0
) -> dict[str, dict]:
    """One summary per bot ({bot: summary}); bot None means every bot."""
    db = DBAdapter(database_url)
    await db.connect()
    try:
        rows = await db.fetchall(ANALYSES_SQL, (bot, since, until))
    finally:
        await db.close()
    by_bot: dict[str, list[dict]] = {}
    for r in rows:
        by_bot.setdefault(r["bot"], []).append(r)
    return {b: summarize(rs, beta) for b, rs in sorted(by_bot.items())}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bot", default="coderabbitai[bot]", help='a bot username, or "all" for one block per bot')
    ap.add_argument("--since", help="bot_reviewed_at lower bound (inclusive), e.g. 7d or 2026-10-01")
    ap.add_argument("--until", help="bot_reviewed_at upper bound (exclusive)")
    ap.add_argument("--beta", type=float, default=1.0, help="F-beta weight, as the API's beta parameter")
    ap.add_argument("--show", type=int, default=0, help="print this many classified items per bot for a hand check")
    ap.add_argument("--database-url")
    args = ap.parse_args(argv)
    cfg = DBConfig()
    bot = None if args.bot == "all" else args.bot
    from main import _parse_time_bound  # the CLI's own parsing: "7d", "2026-10-01" or an ISO timestamp

    since, until = _parse_time_bound(args.since), _parse_time_bound(args.until)
    summaries = asyncio.run(run(bot, args.database_url or cfg.database_url, since, until, args.beta))
    print("\n\n".join(report(b, s, args.show) for b, s in summaries.items()) or "no analyses in this window")


if __name__ == "__main__":
    main()

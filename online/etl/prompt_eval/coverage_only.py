"""Estimate how many stored analyses have only suggestions that this prompt no longer extracts.

A PR whose bot posted only PR-level content (for example a walkthrough with a docstring-coverage
pre-merge warning, or a note that the review was paused) gets suggestions from the current prompt
that no human action matches: precision 0, and recall 0 when the PR has human actions. This PR's
prompt extracts nothing there, so precision becomes undefined. The API then drops the PR from every
mean: daily_metrics skips a record without a precision (its recall goes with it), and the leaderboard
averages precision and recall only over PRs that have both. So such a PR raises precision, recall and
the F score of its bot, whichever bot it is. The measurement in the PR that added this script has
almost no such PR in its sample, so this script estimates that part on the stored analyses.

A suggestion counts as excluded only when it names no file and its whole whitespace-normalized text
matches a template of a fixed line as the extraction step writes it: a coverage gate with its
percentage, an effort / risk / impact / priority / confidence rating, a review status line, or an
offer to generate code. Everything else is a finding, so the figures are an estimate; --show prints
the classified items for a hand check. The templates follow the lines CodeRabbit writes, so f is
understated more for the other bots than for CodeRabbit and is not comparable across bots. The population follows the API: merged PRs with a review
date, the secondary Qodo account shown under the primary one, bots in ignored_tools hidden unless
--include-ignored, and the bundled dashboard's default of at most 2000 changed lines
(--diff-lines-max). --since / --until bound the review date, --until including its whole day as the
API's end_date does; "daily" figures are pooled over that window, not per day (min_scored_prs in
daily_metrics counts PRs with a precision, not PRs with both). Nothing is written and no LLM is called.

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
    SELECT c.github_username AS bot, p.pr_url, p.diff_lines, la.precision, la.recall, la.bot_suggestions
    FROM llm_analyses la
    JOIN prs p ON la.pr_id = p.id
    JOIN chatbots c ON la.chatbot_id = c.id
    WHERE p.pr_merged = TRUE
      AND p.bot_reviewed_at IS NOT NULL
      AND ($1::timestamptz IS NULL OR p.bot_reviewed_at >= $1)
      AND ($2::timestamptz IS NULL OR p.bot_reviewed_at < $2)
"""
# The API's presentation rules (api_service/src/db.rs, compute.rs): a secondary account is shown under
# its primary bot, bots in ignored_tools are hidden unless include_ignored, and the bundled dashboard
# (static/index.html) filters diff_lines to at most 2000 (a PR without diff_lines passes).
MERGE_INTO = {"qodo-free-for-open-source-projects[bot]": "qodo-code-review[bot]"}
DEFAULT_DIFF_LINES_MAX = 2000
# Whole-text templates (anchored, matched on the whitespace-normalized description) of the fixed gate,
# rating and status lines bots post, as the extraction step paraphrases them. Anything that does not
# match a whole template is a finding.
_N = r"~?\d+(?:\.\d+)?\s*%"
# Scope and remediation tails are fixed phrases too: a tail that goes on to name anything else (a
# path, a bug, a behavior) makes the whole text fail to match, so the item stays a finding.
_FUNCS = (
    r"(?:the )?(?:(?:touched|changed|modified|affected)(?: complex| higher-complexity)? functions?"
    r"|functions? (?:touched|changed|modified)(?: (?:by|in) (?:this|the) (?:diff|pr|pull request|change))?)"
    r"(?: with (?:cyclomatic )?complexity ?(?:>=|≥) ?\d+)?"
)
_SCOPE = rf"\(?(?:for|on|in) (?:{_FUNCS}|(?:this|the) (?:diff|pr|pull request))\)?"
_REMEDY = (
    r"(?:so |please |bot (?:requests|recommends) )?(?:add(?:ing)?|consider adding|missing) (?:the )?(?:missing )?"
    rf"(?:docstrings?|tests?)(?: (?:need to be|should be) added)?(?: (?:for|to) {_FUNCS}| for (?:the )?missing functions?)?"
    r"(?: (?:to|so (?:as )?to) (?:meet|satisfy|reach|pass) (?:the )?(?:required |configured )?(?:\d+% )?"
    r"(?:threshold|check|coverage(?: threshold)?|ci(?: threshold)?|pre-merge check))?"
)
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
    rf"(?:[,.;:] {_REMEDY})?\.?"
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
        r"|(?:the )?(?:\w+ ){0,3}(?:code )?review (?:(?:was|has been) (?:paused|skipped|not run)|did not (?:complete|run)|failed)"
        r"(?: for this (?:user|pull request|pr))?(?: \(review status: \w+\))?"
        r"(?:,? so no (?:automated |substantive )?(?:code-review )?findings were produced(?: in the (?:pr|bot) comments)?)?"
        r"|(?:the )?bot(?:'s)? review comments contained no (?:substantive )?(?:code-review )?findings"
        r"|(?:\w+ ){1,2}reviewed \d+ (?:out )?of \d+ changed files(?: in this pull request)? and generated (?:no|\d+)"
        r" (?:new )?comments?)\.?",
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


async def _ignored_tools(db: DBAdapter) -> set[str] | None:
    """Usernames in the API's ignored_tools table, or None when this DB has no such table."""
    try:
        return {r["github_username"] for r in await db.fetchall("SELECT github_username FROM ignored_tools")}
    except Exception:
        return None


async def run(
    bot: str | None,
    database_url: str,
    since: str | None = None,
    until: str | None = None,
    beta: float = 1.0,
    diff_lines_max: int | None = DEFAULT_DIFF_LINES_MAX,
    include_ignored: bool = False,
) -> dict[str, dict]:
    """One summary per bot as the API shows it ({bot: summary}); bot None means every bot."""
    db = DBAdapter(database_url)
    await db.connect()
    try:
        rows = await db.fetchall(ANALYSES_SQL, (since, until))
        ignored = await _ignored_tools(db)
    finally:
        await db.close()
    by_bot: dict[str, list[dict]] = {}
    for r in rows:
        shown = MERGE_INTO.get(r["bot"], r["bot"])  # db.rs: the effective (merged) username
        if not include_ignored and ignored and shown in ignored:
            continue
        if diff_lines_max is not None and r.get("diff_lines") is not None and r["diff_lines"] > diff_lines_max:
            continue
        if bot is None or shown == MERGE_INTO.get(bot, bot):
            by_bot.setdefault(shown, []).append(r)
    return {b: summarize(rs, beta) for b, rs in sorted(by_bot.items())}


def _until_inclusive(value: str | None) -> str | None:
    """--until as the API's end_date: a date is included whole (the bound becomes the next midnight)."""
    from datetime import date
    from datetime import timedelta
    import re

    if value and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return f"{(date.fromisoformat(value) + timedelta(days=1)).isoformat()}T00:00:00+00:00"
    from main import _parse_time_bound

    return _parse_time_bound(value)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bot", default="coderabbitai[bot]", help='a bot username, or "all" for one block per bot')
    ap.add_argument("--since", help="bot_reviewed_at lower bound (inclusive), e.g. 7d or 2026-10-01")
    ap.add_argument("--until", help="last bot_reviewed_at date included, as the API's end_date (e.g. 2026-11-06)")
    ap.add_argument("--beta", type=float, default=1.0, help="F-beta weight, as the API's beta parameter")
    ap.add_argument(
        "--diff-lines-max",
        type=int,
        default=DEFAULT_DIFF_LINES_MAX,
        help="drop PRs with more changed lines, as the bundled dashboard does by default (0: no limit)",
    )
    ap.add_argument("--include-ignored", action="store_true", help="keep bots listed in ignored_tools")
    ap.add_argument("--show", type=int, default=0, help="print this many classified items per bot for a hand check")
    ap.add_argument("--database-url")
    args = ap.parse_args(argv)
    cfg = DBConfig()
    bot = None if args.bot == "all" else args.bot
    from main import _parse_time_bound  # the CLI's own parsing: "7d", "2026-10-01" or an ISO timestamp

    since, until = _parse_time_bound(args.since), _until_inclusive(args.until)
    summaries = asyncio.run(
        run(
            bot,
            args.database_url or cfg.database_url,
            since,
            until,
            args.beta,
            args.diff_lines_max or None,
            args.include_ignored,
        )
    )
    print("\n\n".join(report(b, s, args.show) for b, s in summaries.items()) or "no analyses in this window")


if __name__ == "__main__":
    main()

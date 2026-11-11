"""Compare two versions of EXTRACT_BOT_SUGGESTIONS on PRs that are already analyzed in the DB.

For each analyzed PR (optionally restricted to a list of PR URLs), the script rebuilds step 1's
input exactly as `analyze_single_pr` does, runs step 1 with the baseline prompt (read from a git
ref) and with the current prompt, DRAWS times each, and runs step 3 (JUDGE_MATCHING) against the
human actions already stored for that PR, so both prompts are scored against the same actions.
It prints, per bot: suggestions/PR, matched suggestions/PR, leaderboard precision and recall
(mean over PRs where both are defined), and paired PR bootstrap intervals for each arm minus the
baseline's first draw. The baseline's second draw is the run-to-run noise floor.

Nothing is written to the DB. Results go to --out (JSONL). Its first line is a fingerprint of both
extraction prompts, the matching prompt, the model configuration and every input and action set;
a rerun resumes that file only when the fingerprint matches and otherwise refuses it, so rows from
a different prompt, model or input set are never mixed in. With --pr-list, a CSV that has a bot
column selects (bot, pr_url) pairs; one with only pr_url selects every bot's analysis of the PR.

    uv run python -m prompt_eval.compare_prompts --dry-run
    uv run python -m prompt_eval.compare_prompts --baseline-ref origin/main \\
        --pr-list prompt_eval/extraction_scope_prs.csv --draws 2 --out compare.jsonl
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import csv
import hashlib
import json
import logging
from pathlib import Path
import random
import subprocess
import sys

from config import DBConfig
from db.connection import DBAdapter
from db.repository import PRRepository
from llm.client import LLMClient
from llm.prompts import EXTRACT_BOT_SUGGESTIONS
from llm.prompts import JUDGE_MATCHING
from llm.schemas import BotSuggestionsResponse
from llm.schemas import MatchingResponse
from pipeline.actors import normalize_github_actor
from pipeline.analyze import _build_details_by_sha
from pipeline.analyze import _find_bot_review_commit
from pipeline.analyze import _format_actions
from pipeline.analyze import _format_bot_comments
from pipeline.analyze import _format_commits_with_diffs
from pipeline.analyze import _format_suggestions
from pipeline.analyze import _split_commits_at_hash

logger = logging.getLogger(__name__)


def prompt_at_ref(ref: str) -> str:
    """EXTRACT_BOT_SUGGESTIONS as committed at a git ref."""
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
    src = subprocess.run(
        ["git", "-C", root.stdout.strip(), "show", f"{ref}:online/etl/llm/prompts.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return prompt_in_source(src)


def prompt_in_source(src: str) -> str:
    """EXTRACT_BOT_SUGGESTIONS from the source text of a prompts.py."""
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "EXTRACT_BOT_SUGGESTIONS":
            return node.value.value
    raise KeyError("EXTRACT_BOT_SUGGESTIONS not found")


def _col(pr_row: dict, name: str) -> list[dict]:
    raw = pr_row.get(name)
    if raw is None:
        return []
    return json.loads(raw) if isinstance(raw, str) else raw


def step1_fields(pr_row: dict, chatbot_username: str) -> dict | None:
    """The format() fields analyze_single_pr passes to EXTRACT_BOT_SUGGESTIONS, or None."""
    assembled = pr_row.get("assembled")
    if assembled is None:
        return None
    if isinstance(assembled, str):
        assembled = json.loads(assembled)
    events = assembled.get("events", [])
    commits = _col(pr_row, "commits")
    if not events or not commits:
        return None
    hash_x = _find_bot_review_commit(_col(pr_row, "reviews"), events, commits, chatbot_username)
    pre_commits, _ = _split_commits_at_hash(commits, hash_x)
    details = _build_details_by_sha(_col(pr_row, "commit_details"))
    return {
        "bot_username": chatbot_username,
        "pr_title": assembled.get("pr_title", ""),
        "pr_author": assembled.get("pr_author", "unknown"),
        "repo_name": pr_row["repo_name"],
        "commits_under_review": _format_commits_with_diffs(pre_commits, details),
        "bot_comments": _format_bot_comments(events, chatbot_username),
    }


def metrics(suggestions: list[dict], actions: list[dict], matches: list[dict]) -> dict:
    """The analyze_single_pr arithmetic, plus a count of matched pairs naming an unknown id.

    analyze_single_pr checks each endpoint of a matched pair on its own: a pair whose suggestion
    id does not exist still credits its action toward recall, and a pair whose action id does not
    exist still credits its suggestion toward precision. This script keeps that arithmetic so its
    numbers stay comparable with the leaderboard, and reports how many such pairs each arm has
    (``invalid_id_matches``) instead of dropping them silently.
    """
    sid = {s["issue_id"] for s in suggestions}
    aid = {a["action_id"] for a in actions}
    ms = {m["bot_issue_id"] for m in matches if m["matched"] and m.get("bot_issue_id") in sid}
    ma = {m["human_action_id"] for m in matches if m["matched"] and m.get("human_action_id") in aid}
    invalid = sum(
        1 for m in matches if m["matched"] and (m.get("bot_issue_id") not in sid or m.get("human_action_id") not in aid)
    )
    return {
        "total_suggestions": len(suggestions),
        "matched_suggestions": len(ms),
        "precision": len(ms) / len(suggestions) if suggestions else None,
        "recall": len(ma) / len(actions) if actions else None,
        "invalid_id_matches": invalid,
    }


async def score_arm(llm, template: str, fields: dict, actions: list[dict]) -> dict:
    resp = await llm.structured_completion(template.format(**fields), BotSuggestionsResponse)
    suggestions = [s.model_dump() for s in resp.suggestions]
    matches = []
    if suggestions and actions:
        m = await llm.structured_completion(
            JUDGE_MATCHING.format(
                bot_username=fields["bot_username"],
                bot_suggestions=_format_suggestions(suggestions),
                human_actions=_format_actions(actions),
            ),
            MatchingResponse,
        )
        matches = [x.model_dump() for x in m.matches]
    return {"suggestions": suggestions, "matches": matches, **metrics(suggestions, actions, matches)}


def leaderboard(rows: list[dict]) -> tuple[float | None, float | None, int]:
    both = [r for r in rows if r["precision"] is not None and r["recall"] is not None]
    if not both:
        return None, None, 0
    return sum(r["precision"] for r in both) / len(both), sum(r["recall"] for r in both) / len(both), len(both)


def paired_ci(pairs: list[tuple[dict, dict]], stat, n: int = 2000, seed: int = 7) -> tuple[float, float] | None:
    """95% paired PR bootstrap interval of stat(arm rows) - stat(baseline rows)."""
    rng = random.Random(seed)
    vals = []
    for _ in range(n):
        s = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        a, b = stat([x for x, _ in s]), stat([y for _, y in s])
        if a is not None and b is not None:
            vals.append(b - a)
    if not vals:
        return None
    vals.sort()
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]


def report(rows: list[dict]) -> str:
    """Markdown table per bot from --out rows (the fingerprint header line is skipped).

    Each arm's columns use every PR scored in that arm ("PRs scored"). Deltas against the
    baseline's first draw are paired: they use only the PRs scored in both arms ("paired PRs").
    """
    rows = [r for r in rows if "arm" in r]
    by: dict[tuple[str, str], dict[str, dict]] = {}
    for r in rows:
        by.setdefault((r["bot"], r["pr_url"]), {})[r["arm"]] = r
    arms = sorted({r["arm"] for r in rows}, key=lambda a: (not a.startswith("baseline"), a))
    if not arms:
        return ""
    base = arms[0]
    fmt = lambda v: "n/a" if v is None else f"{v * 100:.1f}"  # noqa: E731
    out = []
    for bot in sorted({b for b, _ in by}):
        keys = [k for k in by if k[0] == bot]
        out.append(f"\n### {bot}\n")
        out.append(
            "| arm | PRs scored | suggestions/PR | matched/PR | P | R | paired PRs "
            "| dP vs baseline draw 1 | dR vs baseline draw 1 | invalid-id matches |"
        )
        out.append("|---|---|---|---|---|---|---|---|---|---|")
        for a in arms:
            rs = [by[k][a] for k in keys if a in by[k]]
            if not rs:
                continue
            p, rc, _ = leaderboard(rs)
            spr = sum(r["total_suggestions"] for r in rs) / len(rs)
            mpr = sum(r["matched_suggestions"] for r in rs) / len(rs)
            bad = sum(r.get("invalid_id_matches", 0) for r in rs)
            cells, n_pairs = ["", ""], ""
            if a != base:
                pairs = [(by[k][base], by[k][a]) for k in keys if base in by[k] and a in by[k]]
                n_pairs = str(len(pairs))
                for i, f in enumerate((lambda x: leaderboard(x)[0], lambda x: leaderboard(x)[1])):
                    if not pairs:
                        break
                    d0, d1 = f([x for x, _ in pairs]), f([y for _, y in pairs])
                    ci = paired_ci(pairs, f)
                    if d0 is not None and d1 is not None and ci:
                        cells[i] = f"{(d1 - d0) * 100:+.1f} [{ci[0] * 100:+.1f}, {ci[1] * 100:+.1f}]"
            out.append(
                f"| {a} | {len(rs)} | {spr:.2f} | {mpr:.2f} | {fmt(p)} | {fmt(rc)} | {n_pairs} "
                f"| {cells[0]} | {cells[1]} | {bad} |"
            )
    return "\n".join(out)


def read_pr_list(path: str) -> tuple[set[str], set[tuple[str, str]] | None]:
    """PR URLs from a CSV with a pr_url column, and (bot, pr_url) pairs when it has a bot column.

    With a bot column, a PR is selected only for the bots it is listed with: a PR listed for one
    bot does not admit another bot's stored analysis of the same PR.
    """
    with open(path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        has_bot = "bot" in (reader.fieldnames or [])
    urls = {r["pr_url"] for r in rows}
    pairs = {(normalize_github_actor(r["bot"]), r["pr_url"]) for r in rows} if has_bot else None
    return urls, pairs


def is_selected(bot: str, pr_url: str, urls: set[str] | None, pairs: set[tuple[str, str]] | None) -> bool:
    if pairs is not None:
        return (normalize_github_actor(bot), pr_url) in pairs
    return urls is None or pr_url in urls


def _actions(an: dict) -> list[dict]:
    raw = an["human_actions"]
    return json.loads(raw) if isinstance(raw, str) else raw


def _sha(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def experiment_fingerprint(templates: dict[str, str], model: dict, units: list[dict]) -> str:
    """Hash of everything a stored row depends on: both extraction prompts, the matching prompt,
    the model configuration (no credentials), and each unit's step 1 input and stored actions."""
    return _sha(
        {
            "extraction_prompts": {k: _sha(v) for k, v in sorted(templates.items())},
            "matching_prompt": _sha(JUDGE_MATCHING),
            "model": model,
            "units": sorted([u["bot"], u["pr_url"], _sha(u["fields"]), _sha(u["actions"])] for u in units),
        }
    )


def open_output(out: Path, fingerprint: str) -> set[tuple[str, str, str]]:
    """Start --out with a fingerprint header, or resume it when its fingerprint matches.

    Returns the (arm, bot, pr_url) keys already scored. An existing file written for a different
    prompt, model or input set is refused, so stale rows are never mixed into a new comparison.
    """
    if not out.exists() or out.stat().st_size == 0:
        out.write_text(json.dumps({"fingerprint": fingerprint}) + "\n")
        return set()
    lines = out.read_text().splitlines()
    header = json.loads(lines[0]) if lines else {}
    if header.get("fingerprint") != fingerprint:
        raise SystemExit(
            f"{out} was written for a different experiment (prompts, model or inputs changed); pass a new --out path"
        )
    return {(r["arm"], r["bot"], r["pr_url"]) for r in map(json.loads, lines[1:])}


async def run(args: argparse.Namespace) -> int:
    """Returns the number of jobs that failed."""
    cfg = DBConfig()
    db = DBAdapter(cfg.database_url)
    await db.connect()
    try:
        return await _run(args, cfg, PRRepository(db))
    finally:  # an open DB connection keeps the process alive after an exception
        await db.close()


async def _run(args: argparse.Namespace, cfg: DBConfig, repo: PRRepository) -> int:
    urls, pairs = read_pr_list(args.pr_list) if args.pr_list else (None, None)
    bots = {normalize_github_actor(b) for b in args.bots.split(",")} if args.bots else None
    units = []
    for cb in await repo.get_all_chatbots():
        bot = cb["github_username"]
        if bots and normalize_github_actor(bot) not in bots:
            continue
        n = 0
        for an in await repo.get_analyses(cb["id"]):
            if not is_selected(bot, an["pr_url"], urls, pairs) or not _actions(an):
                continue
            pr_row = await repo.get_pr_by_id(an["pr_id"])
            fields = step1_fields(pr_row, bot) if pr_row else None
            if fields is None:
                continue
            units.append({"bot": bot, "pr_url": an["pr_url"], "fields": fields, "actions": _actions(an)})
            n += 1
            if args.limit_per_bot and n >= args.limit_per_bot:
                break
    print(f"{len(units)} analyzed PRs with stored actions; {len(units) * args.draws * 4} LLM calls at most")
    if args.dry_run:
        return 0
    baseline = (
        prompt_in_source(Path(args.baseline_file).read_text())
        if args.baseline_file
        else prompt_at_ref(args.baseline_ref)
    )
    templates = {"baseline": baseline, "candidate": EXTRACT_BOT_SUGGESTIONS}
    model = {"base_url": cfg.martian_base_url, "model_name": cfg.martian_model_name}
    out = Path(args.out)
    done = open_output(out, experiment_fingerprint(templates, model, units))
    llm = LLMClient(base_url=cfg.martian_base_url, api_key=cfg.martian_api_key, model_name=cfg.martian_model_name)
    try:
        failures = await run_jobs(llm, units, templates, args.draws, done, out, args.concurrency)
    finally:
        await llm.close()
    print(report([json.loads(x) for x in out.read_text().splitlines()]))
    if failures:
        print(f"\n{len(failures)} job(s) failed and were not scored; rerun with the same --out to retry them.")
    return len(failures)


async def run_jobs(
    llm, units: list[dict], templates: dict[str, str], draws: int, done: set, out: Path, concurrency: int
) -> list[dict]:
    """Score every (unit, arm) not in `done`, appending each result to `out` as it finishes.

    A job whose LLM call fails is logged and returned in the failure list; it writes no row, so a
    rerun with the same --out retries it, and it is never counted as a scored result.
    """
    sem = asyncio.Semaphore(concurrency)

    async def one(u: dict, arm: str, template: str) -> dict:
        key = {"arm": arm, "bot": u["bot"], "pr_url": u["pr_url"]}
        try:
            async with sem:
                res = await score_arm(llm, template, u["fields"], u["actions"])
        except Exception as exc:
            logger.warning("job failed %s: %s: %s", key, type(exc).__name__, exc)
            return {**key, "error": f"{type(exc).__name__}: {exc}"}
        return {**key, **res}

    jobs = [
        one(u, f"{name} draw {d + 1}", tmpl)
        for u in units
        for name, tmpl in templates.items()
        for d in range(draws)
        if (f"{name} draw {d + 1}", u["bot"], u["pr_url"]) not in done
    ]
    failures = []
    with out.open("a") as f:
        for coro in asyncio.as_completed(jobs):
            r = await coro
            if "error" in r:
                failures.append(r)
                continue
            f.write(json.dumps(r) + "\n")
            f.flush()
    return failures


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline-ref", default="origin/main")
    ap.add_argument("--baseline-file", help="a prompts.py to read the baseline prompt from, instead of --baseline-ref")
    ap.add_argument(
        "--pr-list",
        help="CSV with a pr_url column (and optionally bot: then only those pairs); default: every analyzed PR",
    )
    ap.add_argument("--bots", help="comma-separated bot usernames; default: all")
    ap.add_argument("--limit-per-bot", type=int)
    ap.add_argument("--draws", type=int, default=2)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--out", default="compare_prompts.jsonl")
    ap.add_argument("--report-only", action="store_true", help="print the table from --out and exit")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.report_only:
        print(report([json.loads(x) for x in Path(args.out).read_text().splitlines()]))
        return
    if asyncio.run(run(args)):
        sys.exit(1)  # some jobs failed; rerunning with the same --out retries only those


if __name__ == "__main__":
    main()

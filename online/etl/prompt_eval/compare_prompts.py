"""Compare two versions of EXTRACT_BOT_SUGGESTIONS on PRs that are already analyzed in the DB.

For each analyzed PR (optionally restricted to a list of PR URLs), the script rebuilds step 1's
input exactly as `analyze_single_pr` does, runs step 1 with the baseline prompt (read from a git
ref) and with the current prompt, DRAWS times each, and runs step 3 (JUDGE_MATCHING) against the
human actions already stored for that PR, so both prompts are scored against the same actions.
It prints, per bot: suggestions/PR, matched suggestions/PR, leaderboard precision and recall
(mean over PRs where both are defined), and paired PR bootstrap intervals for each arm minus the
baseline's first draw. The baseline's second draw is the run-to-run noise floor.

Nothing is written to the DB. Results go to --out (JSONL, resumable).

    uv run python -m prompt_eval.compare_prompts --dry-run
    uv run python -m prompt_eval.compare_prompts --baseline-ref origin/main \\
        --pr-list prompt_eval/extraction_scope_prs.csv --draws 2 --out compare.jsonl
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import csv
import json
from pathlib import Path
import random
import subprocess

from config import DBConfig
from db.connection import DBAdapter
from db.repository import PRRepository
from llm.client import LLMClient
from llm.prompts import EXTRACT_BOT_SUGGESTIONS
from llm.prompts import JUDGE_MATCHING
from llm.schemas import BotSuggestionsResponse
from llm.schemas import MatchingResponse
from pipeline.analyze import _build_details_by_sha
from pipeline.analyze import _find_bot_review_commit
from pipeline.analyze import _format_actions
from pipeline.analyze import _format_bot_comments
from pipeline.analyze import _format_commits_with_diffs
from pipeline.analyze import _format_suggestions
from pipeline.analyze import _split_commits_at_hash


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
    """The analyze_single_pr arithmetic."""
    sid = {s["issue_id"] for s in suggestions}
    aid = {a["action_id"] for a in actions}
    ms = {m["bot_issue_id"] for m in matches if m["matched"] and m.get("bot_issue_id") in sid}
    ma = {m["human_action_id"] for m in matches if m["matched"] and m.get("human_action_id") in aid}
    return {
        "total_suggestions": len(suggestions),
        "matched_suggestions": len(ms),
        "precision": len(ms) / len(suggestions) if suggestions else None,
        "recall": len(ma) / len(actions) if actions else None,
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
    """Markdown table per bot from --out rows."""
    by: dict[tuple[str, str], dict[str, dict]] = {}
    for r in rows:
        by.setdefault((r["bot"], r["pr_url"]), {})[r["arm"]] = r
    arms = sorted({r["arm"] for r in rows}, key=lambda a: (not a.startswith("baseline"), a))
    out = []
    for bot in sorted({b for b, _ in by}):
        keys = [k for k in by if k[0] == bot and all(a in by[k] for a in arms)]
        if not keys:
            continue
        out.append(f"\n### {bot} ({len(keys)} PRs)\n")
        out.append("| arm | suggestions/PR | matched/PR | P | R | dP vs baseline draw 1 | dR vs baseline draw 1 |")
        out.append("|---|---|---|---|---|---|---|")
        base = arms[0]
        for a in arms:
            rs = [by[k][a] for k in keys]
            p, rc, _ = leaderboard(rs)
            spr = sum(r["total_suggestions"] for r in rs) / len(rs)
            mpr = sum(r["matched_suggestions"] for r in rs) / len(rs)
            cells = ["", ""]
            if a != base:
                pairs = [(by[k][base], by[k][a]) for k in keys]
                for i, f in enumerate((lambda x: leaderboard(x)[0], lambda x: leaderboard(x)[1])):
                    d0, d1 = f([x for x, _ in pairs]), f([y for _, y in pairs])
                    ci = paired_ci(pairs, f)
                    if d0 is not None and d1 is not None and ci:
                        cells[i] = f"{(d1 - d0) * 100:+.1f} [{ci[0] * 100:+.1f}, {ci[1] * 100:+.1f}]"
            fmt = lambda v: "n/a" if v is None else f"{v * 100:.1f}"  # noqa: E731
            out.append(f"| {a} | {spr:.2f} | {mpr:.2f} | {fmt(p)} | {fmt(rc)} | {cells[0]} | {cells[1]} |")
    return "\n".join(out)


async def run(args: argparse.Namespace) -> None:
    cfg = DBConfig()
    db = DBAdapter(cfg.database_url)
    await db.connect()
    repo = PRRepository(db)
    wanted = None
    if args.pr_list:
        with open(args.pr_list) as f:
            wanted = {r["pr_url"] for r in csv.DictReader(f)}
    bots = set(args.bots.split(",")) if args.bots else None
    units = []
    for cb in await repo.get_all_chatbots():
        if bots and cb["github_username"] not in bots:
            continue
        n = 0
        for an in await repo.get_analyses(cb["id"]):
            if wanted is not None and an["pr_url"] not in wanted:
                continue
            actions = json.loads(an["human_actions"]) if isinstance(an["human_actions"], str) else an["human_actions"]
            if not actions:
                continue
            units.append((cb["github_username"], an))
            n += 1
            if args.limit_per_bot and n >= args.limit_per_bot:
                break
    print(f"{len(units)} analyzed PRs with stored actions; {len(units) * args.draws * 4} LLM calls at most")
    if args.dry_run:
        await db.close()
        return
    baseline = (
        prompt_in_source(Path(args.baseline_file).read_text())
        if args.baseline_file
        else prompt_at_ref(args.baseline_ref)
    )
    templates = {"baseline": baseline, "candidate": EXTRACT_BOT_SUGGESTIONS}
    out = Path(args.out)
    done = set()
    if out.exists():
        done = {(r["arm"], r["bot"], r["pr_url"]) for r in map(json.loads, out.open())}
    llm = LLMClient(base_url=cfg.martian_base_url, api_key=cfg.martian_api_key, model_name=cfg.martian_model_name)
    sem = asyncio.Semaphore(args.concurrency)

    async def one(bot: str, an: dict, arm: str, template: str) -> dict | None:
        if (arm, bot, an["pr_url"]) in done:
            return None
        pr_row = await repo.get_pr_by_id(an["pr_id"])
        fields = step1_fields(pr_row, bot) if pr_row else None
        if fields is None:
            return None
        actions = json.loads(an["human_actions"]) if isinstance(an["human_actions"], str) else an["human_actions"]
        async with sem:
            res = await score_arm(llm, template, fields, actions)
        return {"arm": arm, "bot": bot, "pr_url": an["pr_url"], **res}

    jobs = [
        one(bot, an, f"{name} draw {d + 1}", tmpl)
        for bot, an in units
        for name, tmpl in templates.items()
        for d in range(args.draws)
    ]
    with out.open("a") as f:
        for coro in asyncio.as_completed(jobs):
            r = await coro
            if r:
                f.write(json.dumps(r) + "\n")
                f.flush()
    await llm.close()
    await db.close()
    print(report([json.loads(x) for x in out.open()]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline-ref", default="origin/main")
    ap.add_argument("--baseline-file", help="a prompts.py to read the baseline prompt from, instead of --baseline-ref")
    ap.add_argument("--pr-list", help="CSV with a pr_url column; default: every analyzed PR")
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
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

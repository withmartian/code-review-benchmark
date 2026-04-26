"""CAR judge runner: evaluate bot comments against post-review diffs.

Unlike the matching judge which operates on extracted (S_i, A_j) abstractions,
the CAR judge works on raw artifacts: actual bot comment text + actual code diffs.

Per PR, it evaluates each individual bot comment and determines whether
subsequent code changes addressed the concern raised in that comment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "online" / "etl"))

from llm.client import LLMClient  # noqa: E402
from pipeline.analyze import (  # noqa: E402
    _build_details_by_sha,
    _find_bot_review_commit,
    _format_commits_with_diffs,
    _split_commits_at_hash,
)

from car_prompts import CAR_JUDGE  # noqa: E402

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Use the same production model for CAR to isolate methodology difference
CAR_MODEL = os.environ.get("MARTIAN_MODEL_NAME", "openai/gpt-5-nano")
CONCURRENCY = 10


# Structured output schema for CAR judge
from pydantic import BaseModel, Field  # noqa: E402


class CARResult(BaseModel):
    addressed: bool = Field(description="Whether the bot comment's concern was addressed by code changes")
    confidence: float = Field(description="Confidence score 0.0-1.0")
    reasoning: str = Field(description="Brief explanation of the judgment")


class CARResponse(BaseModel):
    result: CARResult


def _parse_json(raw: str | list | dict | None) -> list | dict:
    if raw is None:
        return []
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _extract_bot_comments(events: list[dict], bot_username: str) -> list[dict]:
    """Extract individual bot comments from assembled events.

    Returns list of dicts with: body, path, line, timestamp, diff_hunk,
    event_type, thread context.
    """
    bot_lower = bot_username.lower()
    comments: list[dict] = []

    for e in events:
        actor = (e.get("actor") or "").lower()
        if actor != bot_lower:
            continue
        etype = e.get("event_type", "")
        if etype not in ("review", "review_comment", "issue_comment"):
            continue

        data = e.get("data", {})
        # Skip reply comments (these are responses to threads, not original suggestions)
        if etype == "review_comment" and data.get("in_reply_to_id"):
            continue

        body = data.get("body") or ""
        if not body.strip():
            continue

        comments.append({
            "body": body,
            "path": data.get("path") or "",
            "line": data.get("line"),
            "timestamp": e.get("timestamp", ""),
            "diff_hunk": data.get("diff_hunk") or "",
            "event_type": etype,
            "is_resolved": data.get("is_resolved", False),
            "comment_id": data.get("id") or data.get("node_id") or "",
        })

    return comments


def _get_thread_for_comment(events: list[dict], comment: dict, bot_username: str) -> str:
    """Get the thread context around a bot comment (human replies, resolution)."""
    bot_lower = bot_username.lower()
    path = comment.get("path", "")
    bot_ts = comment.get("timestamp", "")

    thread_lines: list[str] = []
    for e in events:
        etype = e.get("event_type", "")
        if etype not in ("review_comment", "issue_comment"):
            continue
        data = e.get("data", {})
        ts = e.get("timestamp", "")

        # Only include comments after the bot's comment and on the same file/thread
        if ts <= bot_ts:
            continue
        e_path = data.get("path") or ""
        if path and e_path and e_path != path:
            continue

        actor = e.get("actor", "unknown")
        body = data.get("body") or ""
        resolved = " [RESOLVED]" if data.get("is_resolved") else ""
        is_bot = actor.lower() == bot_lower

        if body.strip():
            role = "Bot" if is_bot else "Human"
            thread_lines.append(f"[{ts}] {role} ({actor}){resolved}: {body}")

    return "\n".join(thread_lines) if thread_lines else "(no thread replies)"


async def _evaluate_pr_car(
    llm: LLMClient,
    pr: dict,
) -> dict:
    """Run the CAR judge on all bot comments in a PR."""
    assembled = _parse_json(pr.get("assembled"))
    if not assembled:
        return {"pr_id": pr["pr_id"], "error": "no assembled data"}

    events = assembled.get("events", [])
    commits = _parse_json(pr.get("commits"))
    commit_details = _parse_json(pr.get("commit_details"))
    reviews = _parse_json(pr.get("reviews"))

    if not commits:
        return {"pr_id": pr["pr_id"], "error": "no commits"}

    bot_username = pr["tool"]
    hash_x = _find_bot_review_commit(reviews, events, commits, bot_username)
    _, post_commits = _split_commits_at_hash(commits, hash_x)
    details_by_sha = _build_details_by_sha(commit_details)

    post_diffs = _format_commits_with_diffs(post_commits, details_by_sha)

    bot_comments = _extract_bot_comments(events, bot_username)
    if not bot_comments:
        return {
            "pr_id": pr["pr_id"],
            "tool": pr["tool"],
            "n_comments": 0,
            "comment_results": [],
            "precision": None,
            "recall": None,
            "f_beta": None,
        }

    comment_results: list[dict] = []
    for comment in bot_comments:
        thread = _get_thread_for_comment(events, comment, bot_username)

        bot_comment_text = comment["body"]
        if comment["path"]:
            loc = f"\nFile: {comment['path']}"
            if comment.get("line"):
                loc += f":{comment['line']}"
            bot_comment_text = loc + "\n" + bot_comment_text
        if comment.get("diff_hunk"):
            bot_comment_text += f"\n\nCode context:\n```\n{comment['diff_hunk']}\n```"

        prompt = CAR_JUDGE.format(
            pr_title=assembled.get("pr_title", ""),
            repo_name=pr["repo_name"],
            pr_author=assembled.get("pr_author", "unknown"),
            bot_username=bot_username,
            bot_comment=bot_comment_text,
            comment_thread=thread,
            post_review_diffs=post_diffs,
        )

        try:
            response = await llm.structured_completion(prompt, CARResponse)
            result = response.result.model_dump()
        except Exception as exc:
            logger.error(f"CAR judge failed for comment in pr_id={pr['pr_id']}: {exc}")
            result = {"addressed": False, "confidence": 0.0, "reasoning": f"error: {exc}"}

        comment_results.append({
            "body_preview": comment["body"][:200],
            "path": comment["path"],
            "timestamp": comment["timestamp"],
            **result,
        })

    # Compute CAR-based metrics
    n_total = len(comment_results)
    n_addressed = sum(1 for r in comment_results if r.get("addressed"))

    # CAR precision analog: fraction of bot comments that were addressed
    # (addressed = the developer acted on it, analogous to a matched suggestion)
    precision = n_addressed / n_total if n_total > 0 else None

    # We can't compute recall the same way (no separate "action" list),
    # but we report the addressed rate as the key metric
    return {
        "pr_id": pr["pr_id"],
        "tool": pr["tool"],
        "n_comments": n_total,
        "n_addressed": n_addressed,
        "addressed_rate": precision,
        "comment_results": comment_results,
    }


async def run_car(
    manifest_path: Path,
    model_name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> None:
    """Run the CAR judge on all PRs in the manifest."""
    base_url = base_url or os.environ.get("MARTIAN_BASE_URL", "")
    api_key = api_key or os.environ.get("MARTIAN_API_KEY", "")
    model_name = model_name or CAR_MODEL

    with open(manifest_path) as f:
        manifest = json.load(f)

    logger.info(f"Running CAR judge on {len(manifest)} PRs with {model_name}")

    llm = LLMClient(base_url=base_url, api_key=api_key, model_name=model_name)
    sem = asyncio.Semaphore(CONCURRENCY)

    results: list[dict] = []

    async def _process(pr: dict) -> dict:
        async with sem:
            return await _evaluate_pr_car(llm, pr)

    tasks = [asyncio.create_task(_process(pr)) for pr in manifest]
    for i, coro in enumerate(asyncio.as_completed(tasks)):
        try:
            result = await coro
            results.append(result)
        except Exception as exc:
            results.append({"pr_id": manifest[i]["pr_id"], "error": str(exc)})
        if (i + 1) % 10 == 0:
            logger.info(f"Progress: {i + 1}/{len(manifest)}")

    await llm.close()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / "car_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info(f"Wrote {len(results)} results to {out_path}")

    # Summary
    valid = [r for r in results if "error" not in r and r.get("n_comments", 0) > 0]
    rates = [r["addressed_rate"] for r in valid if r.get("addressed_rate") is not None]
    print(f"\nCAR Results: {len(valid)}/{len(results)} PRs evaluated")
    if rates:
        print(f"  Median addressed rate: {sorted(rates)[len(rates)//2]:.3f}")
        print(f"  Mean addressed rate: {sum(rates)/len(rates):.3f}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Run CAR judge on sampled PRs")
    parser.add_argument("--manifest", type=Path, default=RESULTS_DIR / "judge_sample_manifest.json")
    parser.add_argument("--model", help="Override model name")
    parser.add_argument("--base-url", help="Override MARTIAN_BASE_URL")
    parser.add_argument("--api-key", help="Override MARTIAN_API_KEY")
    args = parser.parse_args()

    asyncio.run(run_car(args.manifest, args.model, args.base_url, args.api_key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

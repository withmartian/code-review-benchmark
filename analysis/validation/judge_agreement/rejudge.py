"""Re-judge sampled PRs with alternative models.

Runs the same 3-step pipeline (extract suggestions, extract actions, match)
using the same prompts but different LLM models. Also runs a "controlled step 3"
variant where all models match against the *same* GPT-5 Nano extractions.

Models (via Martian API):
  - anthropic/claude-sonnet-4-5-20250929  (Claude 4.5 Sonnet)
  - google/gemini-3-flash               (Gemini 3 Flash)
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
from llm.prompts import EXTRACT_BOT_SUGGESTIONS, EXTRACT_HUMAN_ACTIONS, JUDGE_MATCHING  # noqa: E402
from llm.schemas import BotSuggestionsResponse, HumanActionsResponse, MatchingResponse  # noqa: E402
from pipeline.analyze import (  # noqa: E402
    _build_details_by_sha,
    _find_bot_review_commit,
    _format_actions,
    _format_bot_comments,
    _format_commits_with_diffs,
    _format_post_review_activity,
    _format_suggestions,
    _split_commits_at_hash,
)

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).resolve().parent / "results"

ALT_MODELS = {
    "claude-sonnet": "anthropic/claude-sonnet-4-5-20250929",
    "gemini-flash": "google/gemini-flash-latest",
}

CONCURRENCY = 10


def _parse_json(raw: str | list | dict | None) -> list | dict:
    if raw is None:
        return []
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _reconstruct_inputs(pr: dict) -> dict | None:
    """Reconstruct the formatted LLM inputs from raw PR data.

    Returns dict with keys: commits_under_review, bot_comments,
    post_review_commits, post_review_activity, pr_title, pr_author,
    repo_name, bot_username.
    """
    assembled = _parse_json(pr.get("assembled"))
    if not assembled:
        return None

    events = assembled.get("events", [])
    commits = _parse_json(pr.get("commits"))
    commit_details = _parse_json(pr.get("commit_details"))
    reviews = _parse_json(pr.get("reviews"))

    if not commits:
        return None

    bot_username = pr["tool"]
    hash_x = _find_bot_review_commit(reviews, events, commits, bot_username)
    pre_commits, post_commits = _split_commits_at_hash(commits, hash_x)
    details_by_sha = _build_details_by_sha(commit_details)

    return {
        "commits_under_review": _format_commits_with_diffs(pre_commits, details_by_sha),
        "bot_comments": _format_bot_comments(events, bot_username),
        "post_review_commits": _format_commits_with_diffs(post_commits, details_by_sha),
        "post_review_activity": _format_post_review_activity(
            post_commits, details_by_sha, events, bot_username, hash_x
        ),
        "pr_title": assembled.get("pr_title", ""),
        "pr_author": assembled.get("pr_author", "unknown"),
        "repo_name": pr["repo_name"],
        "bot_username": bot_username,
        "events": events,
        "pre_commits": pre_commits,
        "post_commits": post_commits,
        "details_by_sha": details_by_sha,
        "hash_x": hash_x,
    }


async def _run_full_pipeline(
    llm: LLMClient, inputs: dict, beta: float = 1.0
) -> dict:
    """Run the full 3-step pipeline and return structured results."""
    # Step 1: Extract bot suggestions
    prompt1 = EXTRACT_BOT_SUGGESTIONS.format(
        bot_username=inputs["bot_username"],
        pr_title=inputs["pr_title"],
        pr_author=inputs["pr_author"],
        repo_name=inputs["repo_name"],
        commits_under_review=inputs["commits_under_review"],
        bot_comments=inputs["bot_comments"],
    )
    suggestions_resp = await llm.structured_completion(prompt1, BotSuggestionsResponse)
    suggestions = [s.model_dump() for s in suggestions_resp.suggestions]

    # Step 2: Extract human actions
    prompt2 = EXTRACT_HUMAN_ACTIONS.format(
        bot_username=inputs["bot_username"],
        pr_title=inputs["pr_title"],
        pr_author=inputs["pr_author"],
        repo_name=inputs["repo_name"],
        post_review_commits=inputs["post_review_commits"],
        post_review_activity=inputs["post_review_activity"],
    )
    actions_resp = await llm.structured_completion(prompt2, HumanActionsResponse)
    actions = [a.model_dump() for a in actions_resp.actions]

    # Step 3: Match
    prompt3 = JUDGE_MATCHING.format(
        bot_username=inputs["bot_username"],
        bot_suggestions=_format_suggestions(suggestions),
        human_actions=_format_actions(actions),
    )
    matching_resp = await llm.structured_completion(prompt3, MatchingResponse)
    matches = [m.model_dump() for m in matching_resp.matches]

    # Compute metrics
    suggestion_ids = {s["issue_id"] for s in suggestions}
    action_ids = {a["action_id"] for a in actions}
    matched_sugg = {m["bot_issue_id"] for m in matches if m["matched"] and m.get("bot_issue_id") in suggestion_ids}
    matched_acts = {m["human_action_id"] for m in matches if m["matched"] and m.get("human_action_id") in action_ids}

    total_s = len(suggestions)
    total_a = len(actions)
    precision = len(matched_sugg) / total_s if total_s > 0 else None
    recall = len(matched_acts) / total_a if total_a > 0 else None
    f_beta = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        beta_sq = beta ** 2
        f_beta = (1 + beta_sq) * precision * recall / (beta_sq * precision + recall)

    return {
        "bot_suggestions": suggestions,
        "human_actions": actions,
        "matching_results": matches,
        "n_suggestions": total_s,
        "n_actions": total_a,
        "n_matched_suggestions": len(matched_sugg),
        "n_matched_actions": len(matched_acts),
        "precision": precision,
        "recall": recall,
        "f_beta": f_beta,
    }


async def _run_controlled_step3(
    llm: LLMClient,
    inputs: dict,
    baseline_suggestions: list[dict],
    baseline_actions: list[dict],
    beta: float = 1.0,
) -> dict:
    """Run only step 3 (matching) using the baseline's extracted suggestions and actions.

    This isolates matching noise from extraction noise.
    """
    prompt3 = JUDGE_MATCHING.format(
        bot_username=inputs["bot_username"],
        bot_suggestions=_format_suggestions(baseline_suggestions),
        human_actions=_format_actions(baseline_actions),
    )
    matching_resp = await llm.structured_completion(prompt3, MatchingResponse)
    matches = [m.model_dump() for m in matching_resp.matches]

    suggestion_ids = {s["issue_id"] for s in baseline_suggestions}
    action_ids = {a["action_id"] for a in baseline_actions}
    matched_sugg = {m["bot_issue_id"] for m in matches if m["matched"] and m.get("bot_issue_id") in suggestion_ids}
    matched_acts = {m["human_action_id"] for m in matches if m["matched"] and m.get("human_action_id") in action_ids}

    total_s = len(baseline_suggestions)
    total_a = len(baseline_actions)
    precision = len(matched_sugg) / total_s if total_s > 0 else None
    recall = len(matched_acts) / total_a if total_a > 0 else None
    f_beta = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        beta_sq = beta ** 2
        f_beta = (1 + beta_sq) * precision * recall / (beta_sq * precision + recall)

    return {
        "matching_results": matches,
        "n_suggestions": total_s,
        "n_actions": total_a,
        "n_matched_suggestions": len(matched_sugg),
        "n_matched_actions": len(matched_acts),
        "precision": precision,
        "recall": recall,
        "f_beta": f_beta,
    }


async def rejudge_all(
    manifest_path: Path,
    models: dict[str, str] | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> None:
    """Re-judge all PRs in the manifest with alternative models."""
    models = models or ALT_MODELS
    base_url = base_url or os.environ.get("MARTIAN_BASE_URL", "")
    api_key = api_key or os.environ.get("MARTIAN_API_KEY", "")

    with open(manifest_path) as f:
        manifest = json.load(f)

    logger.info(f"Loaded {len(manifest)} PRs from manifest")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    for model_key, model_name in models.items():
        logger.info(f"\n{'='*60}\nRe-judging with {model_key} ({model_name})\n{'='*60}")

        llm = LLMClient(base_url=base_url, api_key=api_key, model_name=model_name)
        sem = asyncio.Semaphore(CONCURRENCY)

        results: list[dict] = []
        errors: list[dict] = []

        async def _process_pr(pr: dict) -> dict | None:
            async with sem:
                pr_id = pr["pr_id"]
                inputs = _reconstruct_inputs(pr)
                if inputs is None:
                    logger.warning(f"Could not reconstruct inputs for pr_id={pr_id}")
                    return None

                result = {"pr_id": pr_id, "tool": pr["tool"], "model": model_key}

                try:
                    # Full pipeline (independent extraction + matching)
                    full = await _run_full_pipeline(llm, inputs)
                    result["full_pipeline"] = full
                except Exception as exc:
                    logger.error(f"Full pipeline failed for pr_id={pr_id}: {exc}")
                    result["full_pipeline"] = {"error": str(exc)}

                try:
                    # Controlled step 3 (use baseline extractions)
                    baseline_suggs = _parse_json(pr.get("baseline_bot_suggestions"))
                    baseline_acts = _parse_json(pr.get("baseline_human_actions"))
                    if baseline_suggs and baseline_acts:
                        controlled = await _run_controlled_step3(
                            llm, inputs, baseline_suggs, baseline_acts
                        )
                        result["controlled_step3"] = controlled
                except Exception as exc:
                    logger.error(f"Controlled step3 failed for pr_id={pr_id}: {exc}")
                    result["controlled_step3"] = {"error": str(exc)}

                return result

        tasks = [asyncio.create_task(_process_pr(pr)) for pr in manifest]
        for i, coro in enumerate(asyncio.as_completed(tasks)):
            try:
                result = await coro
                if result:
                    results.append(result)
            except Exception as exc:
                errors.append({"index": i, "error": str(exc)})
            if (i + 1) % 10 == 0:
                logger.info(f"Progress: {i + 1}/{len(manifest)}")

        await llm.close()

        out_path = RESULTS_DIR / f"rejudge_{model_key}.json"
        with open(out_path, "w") as f:
            json.dump(results, f, indent=2, default=str)
        logger.info(f"Wrote {len(results)} results to {out_path}")

        if errors:
            err_path = RESULTS_DIR / f"rejudge_{model_key}_errors.json"
            with open(err_path, "w") as f:
                json.dump(errors, f, indent=2, default=str)

        # Print summary
        valid = [r for r in results if "error" not in r.get("full_pipeline", {})]
        precisions = [r["full_pipeline"]["precision"] for r in valid if r["full_pipeline"].get("precision") is not None]
        f1s = [r["full_pipeline"]["f_beta"] for r in valid if r["full_pipeline"].get("f_beta") is not None]
        print(f"\n{model_key}: {len(valid)}/{len(results)} succeeded")
        if precisions:
            print(f"  Median precision: {sorted(precisions)[len(precisions)//2]:.3f}")
        if f1s:
            print(f"  Median F1: {sorted(f1s)[len(f1s)//2]:.3f}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Re-judge sampled PRs with alternative models")
    parser.add_argument("--manifest", type=Path, default=RESULTS_DIR / "judge_sample_manifest.json")
    parser.add_argument("--models", nargs="*", help="Model keys to run (default: all)")
    parser.add_argument("--base-url", help="Override MARTIAN_BASE_URL")
    parser.add_argument("--api-key", help="Override MARTIAN_API_KEY")
    args = parser.parse_args()

    models = ALT_MODELS
    if args.models:
        models = {k: v for k, v in ALT_MODELS.items() if k in args.models}

    asyncio.run(rejudge_all(args.manifest, models, args.base_url, args.api_key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

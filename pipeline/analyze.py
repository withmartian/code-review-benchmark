"""Pipeline stage: LLM analysis of assembled PRs (3-step: suggestions, actions, matching)."""

from __future__ import annotations

import asyncio
import json
import logging

from config import DBConfig
from db.connection import DBAdapter
from db.repository import PRRepository
from llm.client import LLMClient
from llm.prompts import EXTRACT_BOT_SUGGESTIONS, EXTRACT_HUMAN_ACTIONS, JUDGE_MATCHING
from llm.schemas import (
    BotSuggestionsResponse,
    HumanActionsResponse,
    MatchingResponse,
)

logger = logging.getLogger(__name__)


def _find_bot_review_commit(
    reviews: list[dict],
    events: list[dict],
    commits: list[dict],
    chatbot_username: str,
) -> str | None:
    """Find the commit hash the bot reviewed (hash X).

    Strategy:
    1. First review by chatbot_username in raw reviews → commit_id
    2. Fallback: original_commit_id on review_comment events in assembled timeline
    3. Last resort: last commit before bot's first comment timestamp
    """
    bot_user_lower = chatbot_username.lower()

    # Strategy 1: raw reviews
    for r in reviews:
        author = (r.get("author") or r.get("user", {}).get("login", "")).lower()
        if author == bot_user_lower and r.get("commit_id"):
            return r["commit_id"]

    # Strategy 2: review_comment events with original_commit_id
    for e in events:
        if e.get("event_type") == "review_comment":
            actor = (e.get("actor") or "").lower()
            if actor == bot_user_lower:
                data = e.get("data", {})
                if data.get("original_commit_id"):
                    return data["original_commit_id"]
                if data.get("commit_id"):
                    return data["commit_id"]

    # Strategy 3: last commit before bot's first comment timestamp
    bot_first_ts = None
    for e in events:
        actor = (e.get("actor") or "").lower()
        if actor == bot_user_lower and e.get("event_type") in ("review", "review_comment", "issue_comment"):
            bot_first_ts = e.get("timestamp")
            break

    if bot_first_ts and commits:
        last_before = None
        for c in commits:
            commit_date = c.get("date") or c.get("committed_date", "")
            if commit_date and commit_date <= bot_first_ts:
                last_before = c.get("sha")
        if last_before:
            return last_before

    # Final fallback: if bot only left issue_comments (no review), use last commit
    if commits:
        return commits[-1].get("sha")

    return None


def _split_commits_at_hash(
    commits: list[dict], hash_x: str | None
) -> tuple[list[dict], list[dict]]:
    """Split commits into pre-review (≤ hash X) and post-review (> hash X).

    Tries exact match, then SHA prefix match. If hash_x not found, all commits
    are treated as pre-review (bot saw the final state).
    """
    if not hash_x or not commits:
        return commits, []

    # Try exact match first, then prefix match
    split_idx = None
    for i, c in enumerate(commits):
        sha = c.get("sha", "")
        if sha == hash_x or sha.startswith(hash_x) or hash_x.startswith(sha):
            split_idx = i
            break

    if split_idx is None:
        logger.debug(f"commit_id {hash_x} not found in commit list, using all as pre-review")
        return commits, []

    return commits[: split_idx + 1], commits[split_idx + 1 :]


def _build_details_by_sha(commit_details: list[dict]) -> dict[str, dict]:
    """Build a lookup from SHA → commit detail (with patches)."""
    by_sha = {}
    for d in commit_details:
        sha = d.get("sha", "")
        if sha:
            by_sha[sha] = d
    return by_sha


def _format_commits_with_diffs(
    commits: list[dict], details_by_sha: dict[str, dict]
) -> str:
    """Format commits with full patch diffs for LLM context."""
    if not commits:
        return "(no commits)"
    lines = []
    for c in commits:
        sha = c.get("sha", "")[:12]
        full_sha = c.get("sha", "")
        message = c.get("message", "")
        author = c.get("author", "unknown")
        date = c.get("date", "")
        lines.append(f"COMMIT {sha} by {author} [{date}]")
        lines.append(f"  Message: {message}")

        detail = details_by_sha.get(full_sha)
        if detail:
            files = detail.get("files", [])
            for f in files:
                filename = f.get("filename", "")
                status = f.get("status", "")
                additions = f.get("additions", 0)
                deletions = f.get("deletions", 0)
                lines.append(f"  {status.upper()} {filename} (+{additions}/-{deletions})")
                patch = f.get("patch")
                if patch:
                    lines.append(f"  ```diff\n{patch}\n  ```")
        lines.append("")
    return "\n".join(lines)


def _format_bot_comments(events: list[dict], chatbot_username: str) -> str:
    """Format bot's review/review_comment/issue_comment events with full context."""
    bot_user_lower = chatbot_username.lower()
    lines = []
    for e in events:
        actor = (e.get("actor") or "").lower()
        if actor != bot_user_lower:
            continue
        etype = e.get("event_type", "")
        if etype not in ("review", "review_comment", "issue_comment"):
            continue

        ts = e.get("timestamp", "")
        data = e.get("data", {})

        if etype == "review":
            state = data.get("state", "")
            body = data.get("body") or ""
            lines.append(f"[{ts}] REVIEW ({state}):")
            if body:
                lines.append(f"  {body}")
        elif etype in ("review_comment", "issue_comment"):
            body = data.get("body") or ""
            path = data.get("path") or ""
            line = data.get("line") or ""
            loc = f" ({path}:{line})" if path else ""
            diff_hunk = data.get("diff_hunk") or ""
            resolved = " [RESOLVED]" if data.get("is_resolved") else ""
            lines.append(f"[{ts}] {etype.upper()}{loc}{resolved}:")
            if diff_hunk:
                lines.append(f"  Code context:\n  ```\n{diff_hunk}\n  ```")
            if body:
                lines.append(f"  {body}")
        lines.append("")
    return "\n".join(lines) if lines else "(no bot comments)"


def _format_post_review_activity(
    post_commits: list[dict],
    details_by_sha: dict[str, dict],
    events: list[dict],
    chatbot_username: str,
    hash_x: str | None,
) -> str:
    """Format post-review commits with diffs + all human comments/replies after bot review."""
    bot_user_lower = chatbot_username.lower()
    sections = []

    # Post-review commits with diffs
    if post_commits:
        sections.append("=== Post-Review Commits ===")
        sections.append(_format_commits_with_diffs(post_commits, details_by_sha))

    # Find the timestamp of hash_x to filter events after it
    # We use the bot's first comment as the cutoff for "after bot review"
    bot_first_ts = None
    for e in events:
        actor = (e.get("actor") or "").lower()
        if actor == bot_user_lower and e.get("event_type") in ("review", "review_comment", "issue_comment"):
            bot_first_ts = e.get("timestamp")
            break

    # All human activity after bot review
    human_lines = []
    for e in events:
        actor = (e.get("actor") or "").lower()
        if actor == bot_user_lower:
            continue
        ts = e.get("timestamp", "")
        etype = e.get("event_type", "")
        if bot_first_ts and ts <= bot_first_ts:
            continue
        data = e.get("data", {})
        actor_name = e.get("actor", "unknown")

        if etype in ("review_comment", "issue_comment"):
            body = data.get("body") or ""
            path = data.get("path") or ""
            line = data.get("line") or ""
            loc = f" ({path}:{line})" if path else ""
            resolved = " [RESOLVED]" if data.get("is_resolved") else ""
            human_lines.append(f"[{ts}] {etype.upper()} by {actor_name}{loc}{resolved}:")
            if body:
                human_lines.append(f"  {body}")
        elif etype == "review":
            state = data.get("state", "")
            body = data.get("body") or ""
            human_lines.append(f"[{ts}] REVIEW by {actor_name}: {state}")
            if body:
                human_lines.append(f"  {body}")
        elif etype in ("pr_merged", "pr_closed", "pr_reopened"):
            human_lines.append(f"[{ts}] {etype.upper()} by {actor_name}")
        human_lines.append("")

    if human_lines:
        sections.append("=== Post-Review Comments & Activity ===")
        sections.append("\n".join(human_lines))

    return "\n\n".join(sections) if sections else "(no post-review activity)"


def _format_suggestions(suggestions: list[dict]) -> str:
    """Format bot suggestions for the matching prompt."""
    lines = []
    for s in suggestions:
        loc = ""
        if s.get("file_path"):
            loc = f" ({s['file_path']}"
            if s.get("line_number"):
                loc += f":{s['line_number']}"
            loc += ")"
        lines.append(f"- [{s['issue_id']}] ({s['category']}/{s['severity']}){loc}: {s['description']}")
    return "\n".join(lines) if lines else "(no suggestions)"


def _format_actions(actions: list[dict]) -> str:
    """Format human actions for the matching prompt."""
    lines = []
    for a in actions:
        loc = ""
        if a.get("file_path"):
            loc = f" ({a['file_path']})"
        lines.append(f"- [{a['action_id']}] ({a['category']}/{a['action_type']}){loc}: {a['description']}")
    return "\n".join(lines) if lines else "(no actions)"


async def analyze_single_pr(
    llm: LLMClient,
    repo: PRRepository,
    pr_row: dict,
    chatbot_username: str,
    model_name: str,
    beta: float = 1.0,
) -> bool:
    """Run 3-step LLM analysis on a single assembled PR. Returns True if successful."""
    assembled = pr_row.get("assembled")
    if assembled is None:
        return False

    if isinstance(assembled, str):
        assembled = json.loads(assembled)

    events = assembled.get("events", [])
    if not events:
        logger.warning(f"No events for {pr_row['repo_name']}#{pr_row['pr_number']}")
        return False

    # Parse raw columns
    def _parse_json_col(col_name: str) -> list[dict]:
        raw = pr_row.get(col_name)
        if raw is None:
            return []
        return json.loads(raw) if isinstance(raw, str) else raw

    commits = _parse_json_col("commits")
    if not commits:
        logger.warning(f"No commits data for {pr_row['repo_name']}#{pr_row['pr_number']} — skipping (possible repo rename/301)")
        await repo.mark_skipped(pr_row["id"], "Enrichment incomplete: no commits data (possible repo rename)")
        return False

    commit_details = _parse_json_col("commit_details")
    reviews = _parse_json_col("reviews")

    # Find hash X (the commit the bot reviewed) and split
    hash_x = _find_bot_review_commit(reviews, events, commits, chatbot_username)
    pre_commits, post_commits = _split_commits_at_hash(commits, hash_x)
    details_by_sha = _build_details_by_sha(commit_details)

    logger.debug(
        f"{pr_row['repo_name']}#{pr_row['pr_number']}: "
        f"hash_x={hash_x and hash_x[:12]}, "
        f"pre={len(pre_commits)}, post={len(post_commits)}"
    )

    # Format inputs for LLM
    commits_under_review = _format_commits_with_diffs(pre_commits, details_by_sha)
    bot_comments = _format_bot_comments(events, chatbot_username)
    post_review_activity = _format_post_review_activity(
        post_commits, details_by_sha, events, chatbot_username, hash_x
    )

    pr_title = assembled.get("pr_title", "")
    pr_author = assembled.get("pr_author", "unknown")
    repo_name = pr_row["repo_name"]

    # Step 1: Extract bot suggestions
    prompt1 = EXTRACT_BOT_SUGGESTIONS.format(
        bot_username=chatbot_username,
        pr_title=pr_title,
        pr_author=pr_author,
        repo_name=repo_name,
        commits_under_review=commits_under_review,
        bot_comments=bot_comments,
    )
    suggestions_resp = await llm.structured_completion(prompt1, BotSuggestionsResponse)
    suggestions = [s.model_dump() for s in suggestions_resp.suggestions]

    # Step 2: Extract human actions
    prompt2 = EXTRACT_HUMAN_ACTIONS.format(
        bot_username=chatbot_username,
        pr_title=pr_title,
        pr_author=pr_author,
        repo_name=repo_name,
        post_review_commits=_format_commits_with_diffs(post_commits, details_by_sha),
        post_review_activity=post_review_activity,
    )
    actions_resp = await llm.structured_completion(prompt2, HumanActionsResponse)
    actions = [a.model_dump() for a in actions_resp.actions]

    # Step 3: Judge matching
    prompt3 = JUDGE_MATCHING.format(
        bot_username=chatbot_username,
        bot_suggestions=_format_suggestions(suggestions),
        human_actions=_format_actions(actions),
    )
    matching_resp = await llm.structured_completion(prompt3, MatchingResponse)
    matches = [m.model_dump() for m in matching_resp.matches]

    # Compute metrics — only count IDs that actually exist in the extracted lists
    suggestion_id_set = {s["issue_id"] for s in suggestions}
    action_id_set = {a["action_id"] for a in actions}
    total_suggestions = len(suggestions)
    matched_suggestion_ids = {m["bot_issue_id"] for m in matches if m["matched"] and m.get("bot_issue_id") in suggestion_id_set}
    matched_suggestions = len(matched_suggestion_ids)
    total_actions = len(actions)
    matched_action_ids = {m["human_action_id"] for m in matches if m["matched"] and m.get("human_action_id") in action_id_set}
    matched_actions = len(matched_action_ids)

    precision = matched_suggestions / total_suggestions if total_suggestions > 0 else None
    recall = matched_actions / total_actions if total_actions > 0 else None
    f_beta = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        beta_sq = beta ** 2
        f_beta = (1 + beta_sq) * precision * recall / (beta_sq * precision + recall)

    # Store results
    await repo.insert_analysis(
        pr_id=pr_row["id"],
        chatbot_id=pr_row["chatbot_id"],
        bot_suggestions=suggestions,
        human_actions=actions,
        matching_results=matches,
        total_bot_comments=total_suggestions,
        matched_bot_comments=matched_suggestions,
        precision=precision,
        recall=recall,
        f_beta=f_beta,
        model_name=model_name,
    )

    logger.info(
        f"Analyzed {repo_name}#{pr_row['pr_number']}: "
        f"{total_suggestions} suggestions, {matched_suggestions} acted on, "
        f"P={precision:.2f}, R={recall:.2f}, F{beta}={f_beta:.2f}"
        if precision is not None and recall is not None and f_beta is not None
        else f"Analyzed {repo_name}#{pr_row['pr_number']}: "
             f"{total_suggestions} suggestions, {matched_suggestions} acted on"
    )
    return True


async def analyze_prs(
    cfg: DBConfig,
    db: DBAdapter,
    chatbot_id: int,
    chatbot_username: str,
    limit: int = 100,
    since: str | None = None,
) -> int:
    """Run LLM analysis on all assembled, unanalyzed PRs for a chatbot.

    Returns the number of PRs analyzed.
    """
    repo = PRRepository(db)
    prs = await repo.get_assembled_not_analyzed(chatbot_id=chatbot_id, limit=limit, since=since)

    if not prs:
        logger.info(f"No unanalyzed PRs for {chatbot_username}")
        return 0

    logger.info(f"Analyzing {len(prs)} PRs for {chatbot_username}")

    llm = LLMClient(
        base_url=cfg.martian_base_url,
        api_key=cfg.martian_api_key,
        model_name=cfg.martian_model_name,
    )

    analyzed = 0
    errors = 0
    concurrency = 60
    sem = asyncio.Semaphore(concurrency)

    async def _run(pr_row: dict) -> bool:
        async with sem:
            return await analyze_single_pr(llm, repo, pr_row, chatbot_username, cfg.martian_model_name, beta=cfg.f_beta)

    try:
        tasks = [asyncio.create_task(_run(pr_row)) for pr_row in prs]
        for coro in asyncio.as_completed(tasks):
            try:
                result = await coro
                if result:
                    analyzed += 1
            except Exception as exc:
                errors += 1
                logger.error(f"Error analyzing PR: {exc}")
            if (analyzed + errors) % 20 == 0:
                logger.info(f"Progress: {analyzed} analyzed, {errors} errors / {len(prs)} total")
    finally:
        await llm.close()

    logger.info(f"Analyzed {analyzed}/{len(prs)} PRs for {chatbot_username}")
    return analyzed

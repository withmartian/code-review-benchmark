"""Pipeline stage: LLM labeling of analyzed PRs."""

from __future__ import annotations

import asyncio
import json
import logging

from config import DBConfig
from db.connection import DBAdapter
from db.repository import PRRepository
from llm.client import LLMClient
from llm.prompts import LABEL_PR
from llm.schemas import PRLabelsResponse

logger = logging.getLogger(__name__)


def _extract_file_list(pr_row: dict) -> str:
    """Extract file list from commit_details."""
    raw = pr_row.get("commit_details")
    if raw is None:
        return "(no file data)"
    details = json.loads(raw) if isinstance(raw, str) else raw

    seen = set()
    lines = []
    for commit in details:
        for f in commit.get("files", []):
            filename = f.get("filename", "")
            if filename and filename not in seen:
                seen.add(filename)
                status = f.get("status", "")
                additions = f.get("additions", 0)
                deletions = f.get("deletions", 0)
                lines.append(f"{filename} ({status}, +{additions}/-{deletions})")
    return "\n".join(lines) if lines else "(no files)"


def _extract_suggestion_summary(pr_row: dict) -> str:
    """Build a brief summary of suggestion categories/severities from analysis data."""
    raw_suggestions = pr_row.get("bot_suggestions")
    raw_matches = pr_row.get("matching_results")

    if not raw_suggestions:
        return "(no suggestions)"

    suggestions = json.loads(raw_suggestions) if isinstance(raw_suggestions, str) else raw_suggestions
    matches = json.loads(raw_matches) if isinstance(raw_matches, str) else (raw_matches or [])

    matched_ids = {m["bot_issue_id"] for m in matches if m.get("matched")}

    categories: dict[str, int] = {}
    severities: dict[str, int] = {}
    for s in suggestions:
        cat = s.get("category", "other")
        sev = s.get("severity", "medium")
        categories[cat] = categories.get(cat, 0) + 1
        severities[sev] = severities.get(sev, 0) + 1

    lines = []
    lines.append(f"Total suggestions: {len(suggestions)}, matched: {len(matched_ids)}")
    lines.append(f"Categories: {', '.join(f'{k}({v})' for k, v in sorted(categories.items()))}")
    lines.append(f"Severities: {', '.join(f'{k}({v})' for k, v in sorted(severities.items()))}")
    return "\n".join(lines)


async def label_single_pr(
    llm: LLMClient,
    repo: PRRepository,
    pr_row: dict,
    model_name: str,
) -> bool:
    """Label a single analyzed PR. Returns True if successful."""
    file_list = _extract_file_list(pr_row)
    suggestion_summary = _extract_suggestion_summary(pr_row)

    pr_title = pr_row.get("pr_title", "")
    pr_author = pr_row.get("pr_author", "unknown")
    repo_name = pr_row["repo_name"]

    prompt = LABEL_PR.format(
        pr_title=pr_title,
        repo_name=repo_name,
        pr_author=pr_author,
        file_list=file_list,
        suggestion_summary=suggestion_summary,
    )

    resp = await llm.structured_completion(prompt, PRLabelsResponse)
    labels = resp.labels.model_dump()

    await repo.insert_labels(
        pr_id=pr_row["id"],
        chatbot_id=pr_row["chatbot_id"],
        labels=labels,
        model_name=model_name,
    )

    logger.info(
        f"Labeled {repo_name}#{pr_row['pr_number']}: "
        f"{labels['language']}, {labels['domain']}, {labels['pr_type']}, {labels['severity']}"
    )
    return True


async def label_prs(
    cfg: DBConfig,
    db: DBAdapter,
    chatbot_id: int,
    chatbot_username: str,
    limit: int = 100,
    since: str | None = None,
) -> int:
    """Label all analyzed, unlabeled PRs for a chatbot. Returns count labeled."""
    repo = PRRepository(db)
    prs = await repo.get_analyzed_not_labeled(chatbot_id=chatbot_id, limit=limit, since=since)

    if not prs:
        logger.info(f"No unlabeled PRs for {chatbot_username}")
        return 0

    logger.info(f"Labeling {len(prs)} PRs for {chatbot_username}")

    llm = LLMClient(
        base_url=cfg.martian_base_url,
        api_key=cfg.martian_api_key,
        model_name=cfg.martian_model_name,
    )

    labeled = 0
    errors = 0
    concurrency = 60
    sem = asyncio.Semaphore(concurrency)

    async def _run(pr_row: dict) -> bool:
        async with sem:
            return await label_single_pr(llm, repo, pr_row, cfg.martian_model_name)

    try:
        tasks = [asyncio.create_task(_run(pr_row)) for pr_row in prs]
        for coro in asyncio.as_completed(tasks):
            try:
                result = await coro
                if result:
                    labeled += 1
            except Exception as exc:
                errors += 1
                logger.error(f"Error labeling PR: {exc}")
            if (labeled + errors) % 20 == 0:
                logger.info(f"Progress: {labeled} labeled, {errors} errors / {len(prs)} total")
    finally:
        await llm.close()

    logger.info(f"Labeled {labeled}/{len(prs)} PRs for {chatbot_username}")
    return labeled

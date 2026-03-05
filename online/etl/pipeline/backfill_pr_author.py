"""Backfill pr_author for rows where it is NULL.

Fetches PR metadata from GitHub REST API and updates the prs table.
Uses the same TokenPool / GitHubEnrichClient from enrich.py for
efficient async requests with rate-limit handling.

Usage:
    python main.py backfill-pr-author [--batch-size 5000] [--max-prs 100000]
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from config import DBConfig
from db.connection import DBAdapter
from pipeline.enrich import GitHubEnrichClient, RateLimitExhaustedError, TokenPool

logger = logging.getLogger(__name__)


async def _fetch_pr_author(
    gh: GitHubEnrichClient,
    owner: str,
    repo: str,
    pr_number: int,
) -> tuple[str | None, dict | None]:
    """Fetch a PR and return (pr_author, raw_response_dict).

    Returns (None, None) on 404/403/error.
    """
    resp = await gh.rest_get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
    if resp is None:
        return None, None
    data = resp.json()
    pr_author = (data.get("user") or {}).get("login")
    return pr_author, data


async def backfill_pr_authors(
    cfg: DBConfig,
    db: DBAdapter,
    batch_size: int = 5000,
    max_prs: int | None = None,
) -> int:
    """Backfill pr_author for all rows where it is NULL.

    Returns total number of rows updated.
    """
    tokens = cfg.github_tokens if cfg.github_tokens else [cfg.github_token]
    pool = TokenPool(tokens)
    n_tokens = pool.size
    n_workers = n_tokens * 10
    logger.info(f"Using {n_tokens} GitHub token(s), {n_workers} workers")

    # Count total work
    count_row = await db.fetchone(
        "SELECT COUNT(*) as cnt FROM prs WHERE pr_author IS NULL AND bq_events IS NOT NULL AND status NOT IN ('skipped', 'error')"
    )
    total_remaining = count_row["cnt"] if count_row else 0
    if total_remaining == 0:
        logger.info("Nothing to backfill — all PRs already have pr_author")
        return 0
    effective_limit = min(total_remaining, max_prs) if max_prs else total_remaining
    logger.info(f"Backfilling pr_author for {effective_limit} / {total_remaining} PRs")

    updated_count = 0
    skipped_count = 0
    error_count = 0

    while updated_count + skipped_count < effective_limit:
        # Fetch a batch of rows missing pr_author
        fetch_limit = min(batch_size, effective_limit - updated_count - skipped_count)
        rows = await db.fetchall(
            *db._translate_params(
                "SELECT id, repo_name, pr_number FROM prs "
                "WHERE pr_author IS NULL AND bq_events IS NOT NULL AND status NOT IN ('skipped', 'error') "
                "ORDER BY id LIMIT $1",
                (fetch_limit,),
            )
        )
        if not rows:
            break

        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        stop_event = asyncio.Event()
        batch_updated = 0
        batch_skipped = 0
        batch_errors = 0

        async def _worker(worker_id: int) -> None:
            nonlocal batch_updated, batch_skipped, batch_errors
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    break

                pr_id = item["id"]
                repo_name = item["repo_name"]
                pr_number = item["pr_number"]

                try:
                    owner, repo = repo_name.split("/", 1)
                except ValueError:
                    logger.warning(f"Invalid repo_name: {repo_name}, skipping")
                    batch_skipped += 1
                    queue.task_done()
                    continue

                gh = None
                try:
                    while True:
                        gh = pool.get()
                        if gh is None:
                            wait = max(0, pool.earliest_reset() - time.time()) + 5
                            logger.warning(
                                f"Worker {worker_id}: all tokens rate-limited, "
                                f"sleeping {wait:.0f}s"
                            )
                            await asyncio.sleep(wait)
                            continue
                        try:
                            pr_author, raw_data = await _fetch_pr_author(
                                gh, owner, repo, pr_number
                            )
                            if pr_author is not None:
                                # Save raw PR data and pr_author
                                await db.execute(
                                    *db._translate_params(
                                        "UPDATE prs SET pr_author = $1, "
                                        "pr_api_raw = $2 WHERE id = $3",
                                        (pr_author, json.dumps(raw_data), pr_id),
                                    )
                                )
                                batch_updated += 1
                            else:
                                # API returned but no user — mark as empty string
                                # so we don't re-fetch
                                await db.execute(
                                    *db._translate_params(
                                        "UPDATE prs SET pr_author = '' WHERE id = $1",
                                        (pr_id,),
                                    )
                                )
                                batch_skipped += 1
                            break
                        except RateLimitExhaustedError as e:
                            pool.mark_limited(gh, e.reset_at)
                            logger.info(
                                f"Worker {worker_id}: token rate-limited, "
                                f"rotating ({pool.status_summary()})"
                            )
                            gh = None
                            continue
                finally:
                    if gh is not None:
                        pool.release(gh)
                    queue.task_done()

        async def _progress_logger() -> None:
            while not stop_event.is_set():
                await asyncio.sleep(15)
                total_done = updated_count + batch_updated + skipped_count + batch_skipped
                pct = total_done * 100 // effective_limit if effective_limit else 0
                logger.info(
                    f"Progress: {total_done}/{effective_limit} ({pct}%) "
                    f"[updated={updated_count + batch_updated} "
                    f"skipped={skipped_count + batch_skipped} "
                    f"errors={error_count + batch_errors}] "
                    f"| Tokens: {pool.status_summary()}"
                )

        # Fill queue
        for row in rows:
            await queue.put(row)
        for _ in range(n_workers):
            await queue.put(None)

        # Run workers
        workers = [asyncio.create_task(_worker(i)) for i in range(n_workers)]
        progress_task = asyncio.create_task(_progress_logger())

        await queue.join()
        stop_event.set()
        await asyncio.gather(*workers)
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

        updated_count += batch_updated
        skipped_count += batch_skipped
        error_count += batch_errors

        logger.info(
            f"Batch done: +{batch_updated} updated, +{batch_skipped} skipped "
            f"| Total: {updated_count} updated, {skipped_count} skipped"
        )

        if len(rows) < fetch_limit:
            break

    await pool.close()
    logger.info(
        f"Backfill complete: {updated_count} updated, "
        f"{skipped_count} skipped, {error_count} errors"
    )
    return updated_count

"""Backfill missing pr_author from stored bq_events, commits, and GitHub API.

Phase 1: Re-parse bq_events with updated _extract_pr_metadata (handles IssueCommentEvent).
Phase 2: Fall back to first commit author from stored commits data.
Phase 3: For remaining PRs, fetch from GitHub REST API (stores raw response in pr_api_raw).
All phases: Re-compute target_user_roles in assembled JSON for affected PRs.

Usage:
    # Dry run: show what would change
    uv run python -m pipeline.backfill_pr_author --dry-run

    # Small sample for verification
    uv run python -m pipeline.backfill_pr_author --limit 10

    # Full backfill (BQ + commits only, no API calls)
    uv run python -m pipeline.backfill_pr_author

    # Full backfill including GitHub API fallback for remaining PRs
    uv run python -m pipeline.backfill_pr_author --use-api

    # With custom DB URL
    uv run python -m pipeline.backfill_pr_author --database-url postgresql://...
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import time

from config import DBConfig
from db.connection import DBAdapter
from db.schema import create_tables
from pipeline.assemble import TimelineEvent
from pipeline.assemble import _determine_roles
from pipeline.assemble import _extract_pr_metadata
from pipeline.enrich import RateLimitExhaustedError
from pipeline.enrich import TokenPool

logger = logging.getLogger(__name__)


def _extract_author_from_bq_events(bq_events_raw: str | list | None) -> str | None:
    """Re-parse bq_events with updated _extract_pr_metadata (handles IssueCommentEvent)."""
    if bq_events_raw is None:
        return None
    events = json.loads(bq_events_raw) if isinstance(bq_events_raw, str) else bq_events_raw
    meta = _extract_pr_metadata(events)
    return meta.get("pr_author")


def _extract_author_from_commits(commits_raw: str | list | None) -> str | None:
    """Fall back to first commit author if bq_events don't yield an author."""
    if commits_raw is None:
        return None
    commits = json.loads(commits_raw) if isinstance(commits_raw, str) else commits_raw
    if commits:
        return commits[0].get("author")
    return None


def _recompute_target_user_roles(
    assembled_raw: str | dict | None, pr_author: str, chatbot_username: str
) -> dict | None:
    """Re-compute target_user_roles in assembled JSON with the new pr_author.

    Returns the updated assembled dict, or None if no update needed.
    """
    if assembled_raw is None:
        return None
    assembled = json.loads(assembled_raw) if isinstance(assembled_raw, str) else assembled_raw

    old_roles = assembled.get("target_user_roles", [])
    events = assembled.get("events", [])

    timeline = [
        TimelineEvent(
            timestamp=e.get("timestamp", ""),
            event_type=e.get("event_type", ""),
            actor=e.get("actor", ""),
            data=e.get("data", {}),
        )
        for e in events
    ]
    new_roles = _determine_roles(chatbot_username, timeline, pr_author)

    if new_roles != old_roles:
        assembled["target_user_roles"] = new_roles
        assembled["pr_author"] = pr_author
        return assembled

    if assembled.get("pr_author") != pr_author:
        assembled["pr_author"] = pr_author
        return assembled

    return None


async def _update_pr_and_roles(
    db: DBAdapter,
    row: dict,
    author: str,
    chatbot_username: str,
    *,
    dry_run: bool,
    pr_api_raw: dict | None = None,
) -> bool:
    """Update pr_author (and optionally pr_api_raw) and recompute roles.

    Returns True if roles were updated.
    """
    pr_id = row["id"]

    if not dry_run:
        if pr_api_raw is not None:
            pr_merged = pr_api_raw.get("merged")
            repo_id = (pr_api_raw.get("base") or {}).get("repo", {}).get("id")
            await db.execute(*db._translate_params(
                "UPDATE prs SET pr_author = $1, pr_api_raw = $2, "
                "pr_merged = COALESCE($3, pr_merged), "
                "repo_id = COALESCE(repo_id, $4) WHERE id = $5",
                (author, json.dumps(pr_api_raw), pr_merged, repo_id, pr_id),
            ))
        else:
            await db.execute(*db._translate_params(
                "UPDATE prs SET pr_author = $1 WHERE id = $2",
                (author, pr_id),
            ))

    updated_assembled = _recompute_target_user_roles(
        row.get("assembled"), author, chatbot_username
    )
    if updated_assembled is not None:
        if dry_run:
            old_roles = (
                json.loads(row["assembled"]) if isinstance(row["assembled"], str) else row["assembled"] or {}
            ).get("target_user_roles", [])
            new_roles = updated_assembled.get("target_user_roles", [])
            if old_roles != new_roles:
                logger.info(f"    roles: {old_roles} -> {new_roles}")
        else:
            await db.execute(*db._translate_params(
                "UPDATE prs SET assembled = $1 WHERE id = $2",
                (json.dumps(updated_assembled), pr_id),
            ))
        return True
    return False


async def _backfill_from_local_data(
    db: DBAdapter,
    chatbot_map: dict[int, str],
    *,
    limit: int | None = None,
    dry_run: bool = False,
    use_commits: bool = False,
) -> tuple[dict[str, int], list[dict]]:
    """Phase 1 (+ optional Phase 2): backfill from bq_events (and optionally commits).

    Commits fallback is off by default because git author names != GitHub usernames.

    Returns (stats, still_missing_rows).
    """
    limit_clause = f"LIMIT {limit}" if limit else ""
    query, params = db._translate_params(
        f"""
        SELECT id, chatbot_id, repo_name, pr_number, pr_author,
               bq_events, commits, assembled
        FROM prs
        WHERE (pr_author IS NULL OR pr_author = '')
        ORDER BY id
        {limit_clause}
        """,
        (),
    )
    rows = await db.fetchall(query, params)

    stats = {
        "total_missing": len(rows),
        "updated_from_bq": 0,
        "updated_from_commits": 0,
        "still_missing": 0,
        "roles_updated": 0,
        "updated_from_api": 0,
    }

    if not rows:
        logger.info("No PRs with missing pr_author found")
        return stats, []

    logger.info(f"Found {len(rows)} PRs with missing pr_author")

    still_missing: list[dict] = []

    for i, row in enumerate(rows):
        pr_id = row["id"]
        repo_name = row["repo_name"]
        pr_number = row["pr_number"]
        chatbot_id = row["chatbot_id"]
        chatbot_username = chatbot_map.get(chatbot_id, "")

        author = _extract_author_from_bq_events(row["bq_events"])
        source = "bq_events"

        # Commits fallback is low-confidence: git author names don't reliably
        # match GitHub usernames (e.g. "Copilot" vs "copilot[bot]")
        if not author and use_commits:
            author = _extract_author_from_commits(row["commits"])
            source = "commits (low-confidence)"

        if not author:
            stats["still_missing"] += 1
            still_missing.append(row)
            if dry_run:
                logger.debug(f"  [SKIP] {repo_name}#{pr_number} (id={pr_id}): no author in bq_events")
            continue

        if dry_run:
            logger.info(f"  [DRY] {repo_name}#{pr_number} (id={pr_id}): pr_author={author!r} (from {source})")

        roles_updated = await _update_pr_and_roles(
            db, row, author, chatbot_username, dry_run=dry_run
        )

        if source == "bq_events":
            stats["updated_from_bq"] += 1
        else:
            stats["updated_from_commits"] += 1
        if roles_updated:
            stats["roles_updated"] += 1

        if (i + 1) % 200 == 0:
            logger.info(f"  Local phase progress: {i + 1}/{len(rows)}")

    return stats, still_missing


async def _backfill_from_api(
    cfg: DBConfig,
    db: DBAdapter,
    rows: list[dict],
    chatbot_map: dict[int, str],
    stats: dict[str, int],
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Phase 3: fetch pr_author from GitHub API for remaining PRs.

    Stores raw API response in pr_api_raw. Mutates and returns stats.
    """
    if not rows:
        return stats

    tokens = cfg.github_tokens if cfg.github_tokens else [cfg.github_token]
    pool = TokenPool(tokens)
    n_tokens = pool.size
    n_workers = n_tokens * 10
    logger.info(
        f"API fallback for {len(rows)} PRs using {n_tokens} token(s), {n_workers} workers"
    )

    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    stop_event = asyncio.Event()
    batch_updated = 0
    batch_skipped = 0
    roles_updated = 0

    async def _worker(worker_id: int) -> None:
        nonlocal batch_updated, batch_skipped, roles_updated
        while True:
            item = await queue.get()
            if item is None:
                queue.task_done()
                break

            pr_id = item["id"]
            repo_name = item["repo_name"]
            pr_number = item["pr_number"]
            chatbot_id = item["chatbot_id"]
            chatbot_username = chatbot_map.get(chatbot_id, "")

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
                        resp = await gh.rest_get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
                        if resp is None:
                            if not dry_run:
                                # Mark as empty string so we don't re-fetch
                                await db.execute(*db._translate_params(
                                    "UPDATE prs SET pr_author = '' WHERE id = $1",
                                    (pr_id,),
                                ))
                            batch_skipped += 1
                            break

                        data = resp.json()
                        author = (data.get("user") or {}).get("login")

                        if author:
                            if dry_run:
                                logger.info(
                                    f"  [DRY/API] {repo_name}#{pr_number} "
                                    f"(id={pr_id}): pr_author={author!r}"
                                )

                            role_changed = await _update_pr_and_roles(
                                db, item, author, chatbot_username,
                                dry_run=dry_run, pr_api_raw=data,
                            )
                            batch_updated += 1
                            if role_changed:
                                roles_updated += 1
                        else:
                            if not dry_run:
                                await db.execute(*db._translate_params(
                                    "UPDATE prs SET pr_author = '' WHERE id = $1",
                                    (pr_id,),
                                ))
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
            total_done = batch_updated + batch_skipped
            pct = total_done * 100 // len(rows) if rows else 0
            logger.info(
                f"API progress: {total_done}/{len(rows)} ({pct}%) "
                f"[updated={batch_updated} skipped={batch_skipped}] "
                f"| Tokens: {pool.status_summary()}"
            )

    for row in rows:
        await queue.put(row)
    for _ in range(n_workers):
        await queue.put(None)

    workers = [asyncio.create_task(_worker(i)) for i in range(n_workers)]
    progress_task = asyncio.create_task(_progress_logger())

    await queue.join()
    stop_event.set()
    await asyncio.gather(*workers)
    progress_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await progress_task

    await pool.close()

    stats["updated_from_api"] = batch_updated
    # Reduce still_missing by the ones we resolved via API
    stats["still_missing"] = stats["still_missing"] - batch_updated - batch_skipped
    stats["roles_updated"] += roles_updated

    logger.info(
        f"API fallback done: {batch_updated} updated, {batch_skipped} skipped"
    )
    return stats


async def backfill_pr_author(
    cfg: DBConfig,
    db: DBAdapter,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    use_api: bool = False,
    use_commits: bool = False,
) -> dict[str, int]:
    """Backfill missing pr_author for PRs in the database.

    Phase 1: Parse stored bq_events (free, instant).
    Phase 2 (if use_commits=True): Fall back to git commit author (low-confidence).
    Phase 3 (if use_api=True): Hit GitHub REST API for remaining PRs.
    All phases: Recompute target_user_roles for updated PRs.

    Returns counts dict with keys:
        total_missing, updated_from_bq, updated_from_commits,
        updated_from_api, still_missing, roles_updated
    """
    chatbot_rows = await db.fetchall("SELECT id, github_username FROM chatbots")
    chatbot_map = {r["id"]: r["github_username"] for r in chatbot_rows}

    stats, still_missing = await _backfill_from_local_data(
        db, chatbot_map, limit=limit, dry_run=dry_run, use_commits=use_commits
    )

    if use_api and still_missing:
        stats = await _backfill_from_api(
            cfg, db, still_missing, chatbot_map, stats, dry_run=dry_run
        )

    return stats


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill missing pr_author from stored BQ events, commits, and optionally GitHub API"
    )
    parser.add_argument("--database-url", help="Override DATABASE_URL")
    parser.add_argument("--limit", type=int, help="Limit number of PRs to process (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    parser.add_argument("--use-api", action="store_true", help="Also fetch from GitHub API for PRs not resolved locally")
    parser.add_argument("--use-commits", action="store_true", help="Also try git commit author (low-confidence, off by default)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cfg = DBConfig()
    if args.database_url:
        cfg.database_url = args.database_url

    db = DBAdapter(cfg.database_url)
    await db.connect()
    try:
        await create_tables(db)

        stats = await backfill_pr_author(
            cfg,
            db,
            limit=args.limit,
            dry_run=args.dry_run,
            use_api=args.use_api,
            use_commits=args.use_commits,
        )

        mode = "DRY RUN" if args.dry_run else "DONE"
        print(f"\n{mode} — backfill_pr_author results:")
        print(f"  Total missing:        {stats['total_missing']}")
        print(f"  Updated from BQ:      {stats['updated_from_bq']}")
        print(f"  Updated from commits: {stats['updated_from_commits']}")
        print(f"  Updated from API:     {stats['updated_from_api']}")
        print(f"  Still missing:        {stats['still_missing']}")
        print(f"  Roles updated:        {stats['roles_updated']}")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

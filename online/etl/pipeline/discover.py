"""Pipeline stage: Discover PRs from BigQuery and insert into database."""

from __future__ import annotations

import json
import logging

from google.cloud import bigquery

from config import DBConfig
from db.connection import DBAdapter
from db.repository import PRRepository

logger = logging.getLogger(__name__)

# Same combined query from bq_extract.py, with per-day random sampling.
# The all_target_prs CTE finds every PR the bot touched, grouped by first-seen day.
# The sampled_prs CTE uses RAND() for random ordering (different sample each run)
# and keeps at most @max_prs_per_day PRs per day.
# If the total PRs across all days is <= @max_prs_per_day, sampling is skipped
# and all PRs are returned (no point sampling when we have fewer than the target).
COMBINED_QUERY = """
WITH raw_target_prs AS (
  SELECT
    repo.name AS repo_name,
    CASE
      WHEN type = 'IssueCommentEvent'
        THEN CAST(JSON_EXTRACT_SCALAR(payload, '$.issue.number') AS INT64)
      ELSE CAST(JSON_EXTRACT_SCALAR(payload, '$.pull_request.number') AS INT64)
    END AS pr_number,
    MAX(COALESCE(
      JSON_EXTRACT_SCALAR(payload, '$.pull_request.html_url'),
      JSON_EXTRACT_SCALAR(payload, '$.issue.pull_request.html_url')
    )) AS pr_url,
    MIN(_TABLE_SUFFIX) AS first_seen_day
  FROM `githubarchive.day.20*`
  WHERE
    actor.login = @target_user
    AND _TABLE_SUFFIX BETWEEN @suffix_start AND @suffix_end
    AND (
      type != 'IssueCommentEvent'
      OR JSON_EXTRACT_SCALAR(payload, '$.issue.pull_request.html_url') IS NOT NULL
    )
    AND CASE
      WHEN type = 'IssueCommentEvent'
        THEN CAST(JSON_EXTRACT_SCALAR(payload, '$.issue.number') AS INT64)
      ELSE CAST(JSON_EXTRACT_SCALAR(payload, '$.pull_request.number') AS INT64)
    END >= @min_pr_number
  GROUP BY repo_name, pr_number
),
all_target_prs AS (
  SELECT
    repo_name,
    pr_number,
    COALESCE(pr_url, CONCAT('https://github.com/', repo_name, '/pull/', CAST(pr_number AS STRING))) AS pr_url,
    first_seen_day
  FROM raw_target_prs
  WHERE pr_number IS NOT NULL
),
sampled_prs AS (
  SELECT *,
    ROW_NUMBER() OVER (
      PARTITION BY first_seen_day
      ORDER BY RAND()
    ) AS rn,
    COUNT(*) OVER () AS total_prs
  FROM all_target_prs
)
SELECT
  t.repo_name,
  t.pr_number,
  t.pr_url,
  e.type,
  e.actor.login AS actor,
  e.created_at,
  e.payload,
  e.id AS event_id,
  e.repo.id AS repo_id
FROM `githubarchive.day.20*` e
INNER JOIN sampled_prs t ON e.repo.name = t.repo_name
WHERE
  (t.total_prs <= @max_prs_per_day OR t.rn <= @max_prs_per_day)
  AND e._TABLE_SUFFIX BETWEEN @suffix_start AND @suffix_end
  AND (
    (CAST(JSON_EXTRACT_SCALAR(e.payload, '$.pull_request.number') AS INT64) = t.pr_number)
    OR
    (e.type = 'IssueCommentEvent'
     AND CAST(JSON_EXTRACT_SCALAR(e.payload, '$.issue.number') AS INT64) = t.pr_number
     AND JSON_EXTRACT_SCALAR(e.payload, '$.issue.pull_request.html_url') IS NOT NULL)
  )
ORDER BY t.repo_name, t.pr_number, e.created_at
"""

# Batch variant: single scan for multiple chatbots at once.
# Differences from COMBINED_QUERY:
#   - @target_user (scalar) → @target_users (array) with IN UNNEST(...)
#   - Carries bot_username through CTEs and final SELECT
COMBINED_QUERY_BATCH = """
WITH raw_target_prs AS (
  SELECT
    actor.login AS bot_username,
    repo.name AS repo_name,
    CASE
      WHEN type = 'IssueCommentEvent'
        THEN CAST(JSON_EXTRACT_SCALAR(payload, '$.issue.number') AS INT64)
      ELSE CAST(JSON_EXTRACT_SCALAR(payload, '$.pull_request.number') AS INT64)
    END AS pr_number,
    MAX(COALESCE(
      JSON_EXTRACT_SCALAR(payload, '$.pull_request.html_url'),
      JSON_EXTRACT_SCALAR(payload, '$.issue.pull_request.html_url')
    )) AS pr_url,
    MIN(_TABLE_SUFFIX) AS first_seen_day
  FROM `githubarchive.day.20*`
  WHERE
    actor.login IN UNNEST(@target_users)
    AND _TABLE_SUFFIX BETWEEN @suffix_start AND @suffix_end
    AND (
      type != 'IssueCommentEvent'
      OR JSON_EXTRACT_SCALAR(payload, '$.issue.pull_request.html_url') IS NOT NULL
    )
    AND CASE
      WHEN type = 'IssueCommentEvent'
        THEN CAST(JSON_EXTRACT_SCALAR(payload, '$.issue.number') AS INT64)
      ELSE CAST(JSON_EXTRACT_SCALAR(payload, '$.pull_request.number') AS INT64)
    END >= @min_pr_number
  GROUP BY bot_username, repo_name, pr_number
),
all_target_prs AS (
  SELECT
    bot_username,
    repo_name,
    pr_number,
    COALESCE(pr_url, CONCAT('https://github.com/', repo_name, '/pull/', CAST(pr_number AS STRING))) AS pr_url,
    first_seen_day
  FROM raw_target_prs
  WHERE pr_number IS NOT NULL
),
sampled_prs AS (
  SELECT *,
    ROW_NUMBER() OVER (
      PARTITION BY bot_username, first_seen_day
      ORDER BY RAND()
    ) AS rn,
    COUNT(*) OVER (PARTITION BY bot_username) AS total_prs
  FROM all_target_prs
)
SELECT
  t.bot_username,
  t.repo_name,
  t.pr_number,
  t.pr_url,
  e.type,
  e.actor.login AS actor,
  e.created_at,
  e.payload,
  e.id AS event_id,
  e.repo.id AS repo_id
FROM `githubarchive.day.20*` e
INNER JOIN sampled_prs t ON e.repo.name = t.repo_name
WHERE
  (t.total_prs <= @max_prs_per_day OR t.rn <= @max_prs_per_day)
  AND e._TABLE_SUFFIX BETWEEN @suffix_start AND @suffix_end
  AND (
    (CAST(JSON_EXTRACT_SCALAR(e.payload, '$.pull_request.number') AS INT64) = t.pr_number)
    OR
    (e.type = 'IssueCommentEvent'
     AND CAST(JSON_EXTRACT_SCALAR(e.payload, '$.issue.number') AS INT64) = t.pr_number
     AND JSON_EXTRACT_SCALAR(e.payload, '$.issue.pull_request.html_url') IS NOT NULL)
  )
ORDER BY t.bot_username, t.repo_name, t.pr_number, e.created_at
"""


def _date_to_suffix(date_str: str) -> str:
    """Convert YYYY-MM-DD to BQ table suffix YYMMDD."""
    parts = date_str.split("-")
    return f"{parts[0][2:]}{parts[1]}{parts[2]}"


def _extract_pr_metadata(events: list[dict]) -> dict:
    """Extract PR title, author, created_at, merged status from BQ events."""
    meta = {"pr_title": "", "pr_author": None, "pr_created_at": None, "pr_merged": None}
    for event in events:
        payload = event.get("payload", {})
        if event["type"] == "PullRequestEvent":
            pr_obj = payload.get("pull_request", {})
            if not meta["pr_title"]:
                meta["pr_title"] = pr_obj.get("title", "")
            if meta["pr_author"] is None:
                meta["pr_author"] = (pr_obj.get("user") or {}).get("login")
            if meta["pr_created_at"] is None:
                meta["pr_created_at"] = pr_obj.get("created_at")
            if payload.get("action") == "closed":
                if pr_obj.get("merged"):
                    meta["pr_merged"] = True
                elif meta["pr_merged"] is None:
                    meta["pr_merged"] = False
        elif event["type"] in ("PullRequestReviewEvent", "PullRequestReviewCommentEvent"):
            pr_obj = payload.get("pull_request", {})
            if not meta["pr_title"]:
                meta["pr_title"] = pr_obj.get("title", "")
            if meta["pr_author"] is None:
                meta["pr_author"] = (pr_obj.get("user") or {}).get("login")
            if meta["pr_created_at"] is None:
                meta["pr_created_at"] = pr_obj.get("created_at")
        elif event["type"] == "IssueCommentEvent":
            issue_obj = payload.get("issue", {})
            pr_obj = issue_obj.get("pull_request", {})
            if not meta["pr_title"]:
                meta["pr_title"] = issue_obj.get("title", "")
            if meta["pr_author"] is None:
                meta["pr_author"] = (issue_obj.get("user") or {}).get("login")
            if meta["pr_created_at"] is None:
                meta["pr_created_at"] = issue_obj.get("created_at")
    return meta


async def discover_prs(
    cfg: DBConfig,
    db: DBAdapter,
    chatbot_username: str,
    start_date: str,
    end_date: str,
    min_pr_number: int = 0,
    max_prs_per_day: int = 500,
    display_name: str | None = None,
) -> int:
    """Run BQ discovery for a chatbot and insert new PRs into the database.

    Randomly samples at most max_prs_per_day PRs per day (different sample each run).
    If the total PRs across all days is <= max_prs_per_day, all PRs are kept without sampling.
    Returns the number of new PRs inserted.
    """
    repo = PRRepository(db)
    chatbot_id = await repo.upsert_chatbot(chatbot_username, display_name)

    client = bigquery.Client(project=cfg.gcp_project)
    try:
        suffix_start = _date_to_suffix(start_date)
        suffix_end = _date_to_suffix(end_date)

        params = [
            bigquery.ScalarQueryParameter("target_user", "STRING", chatbot_username),
            bigquery.ScalarQueryParameter("suffix_start", "STRING", suffix_start),
            bigquery.ScalarQueryParameter("suffix_end", "STRING", suffix_end),
            bigquery.ScalarQueryParameter("min_pr_number", "INT64", min_pr_number),
            bigquery.ScalarQueryParameter("max_prs_per_day", "INT64", max_prs_per_day),
        ]

        # Dry run for cost estimation
        dry_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, query_parameters=params)
        dry_job = client.query(COMBINED_QUERY, job_config=dry_config)
        logger.info(f"BQ estimated scan: {dry_job.total_bytes_processed / 1024**3:.2f} GB")

        # Execute query
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        job = client.query(COMBINED_QUERY, job_config=job_config)
        rows = [dict(row) for row in job]
        logger.info(f"BQ query returned {len(rows)} events")
    finally:
        client.close()

    # Group events by PR
    events_by_key: dict[tuple[str, int], list[dict]] = {}
    pr_urls: dict[tuple[str, int], str] = {}
    repo_ids: dict[tuple[str, int], int | None] = {}

    for row in rows:
        repo_name = row["repo_name"]
        pr_number = row["pr_number"]
        key = (repo_name, pr_number)
        pr_urls.setdefault(key, row.get("pr_url") or f"https://github.com/{repo_name}/pull/{pr_number}")
        # BQ repo.id is stable across renames
        if row.get("repo_id") is not None:
            repo_ids[key] = int(row["repo_id"])

        payload_str = row.get("payload")
        payload = json.loads(payload_str) if isinstance(payload_str, str) else payload_str

        created_at = row["created_at"]
        if hasattr(created_at, "isoformat"):
            created_at = created_at.isoformat()

        event = {
            "event_id": str(row["event_id"]),
            "type": row["type"],
            "actor": row["actor"],
            "created_at": created_at,
            "repo_name": repo_name,
            "pr_number": pr_number,
            "payload": payload,
        }
        events_by_key.setdefault(key, []).append(event)

    # Insert PRs into database
    inserted = 0
    total = len(events_by_key)
    async with db.transaction():
        for i, ((repo_name, pr_number), events) in enumerate(events_by_key.items()):
            if i % 100 == 0 and i > 0:
                logger.info(f"  Inserting PRs: {i}/{total}...")
            pr_url = pr_urls[(repo_name, pr_number)]
            meta = _extract_pr_metadata(events)

            bot_events = [e for e in events if e.get("actor") == chatbot_username and e.get("created_at")]
            bot_reviewed_at = min(e["created_at"] for e in bot_events) if bot_events else None

            was_inserted = await repo.insert_pr(
                chatbot_id=chatbot_id,
                repo_name=repo_name,
                pr_number=pr_number,
                pr_url=pr_url,
                pr_title=meta["pr_title"],
                pr_author=meta["pr_author"],
                pr_created_at=meta["pr_created_at"],
                pr_merged=meta["pr_merged"],
                status="pending",
                bq_events=events,
                bot_reviewed_at=bot_reviewed_at,
                repo_id=repo_ids.get((repo_name, pr_number)),
            )
            if was_inserted:
                inserted += 1

    logger.info(f"Discovered {len(events_by_key)} PRs, inserted {inserted} new ({total - inserted} already existed)")
    return inserted


async def discover_prs_batch(
    cfg: DBConfig,
    db: DBAdapter,
    chatbot_usernames: list[str],
    start_date: str,
    end_date: str,
    min_pr_number: int = 0,
    max_prs_per_day: int = 500,
) -> int:
    """Run a single BQ discovery for multiple chatbots and insert new PRs.

    Scans BigQuery once instead of N times, saving cost.
    Returns the total number of new PRs inserted across all chatbots.
    """
    repo = PRRepository(db)

    # Upsert all chatbots upfront and build username → chatbot_id map
    username_to_id: dict[str, int] = {}
    for username in chatbot_usernames:
        cid = await repo.upsert_chatbot(username)
        username_to_id[username] = cid

    client = bigquery.Client(project=cfg.gcp_project)
    try:
        suffix_start = _date_to_suffix(start_date)
        suffix_end = _date_to_suffix(end_date)

        params = [
            bigquery.ArrayQueryParameter("target_users", "STRING", chatbot_usernames),
            bigquery.ScalarQueryParameter("suffix_start", "STRING", suffix_start),
            bigquery.ScalarQueryParameter("suffix_end", "STRING", suffix_end),
            bigquery.ScalarQueryParameter("min_pr_number", "INT64", min_pr_number),
            bigquery.ScalarQueryParameter("max_prs_per_day", "INT64", max_prs_per_day),
        ]

        # Dry run for cost estimation
        dry_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, query_parameters=params)
        dry_job = client.query(COMBINED_QUERY_BATCH, job_config=dry_config)
        logger.info(f"BQ batch estimated scan: {dry_job.total_bytes_processed / 1024**3:.2f} GB")

        # Execute query
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        job = client.query(COMBINED_QUERY_BATCH, job_config=job_config)
        rows = [dict(row) for row in job]
        logger.info(f"BQ batch query returned {len(rows)} events for {len(chatbot_usernames)} chatbots")
    finally:
        client.close()

    # Group events by (bot_username, repo_name, pr_number)
    events_by_key: dict[tuple[str, str, int], list[dict]] = {}
    pr_urls: dict[tuple[str, str, int], str] = {}
    repo_ids: dict[tuple[str, str, int], int | None] = {}

    for row in rows:
        bot_username = row["bot_username"]
        repo_name = row["repo_name"]
        pr_number = row["pr_number"]
        key = (bot_username, repo_name, pr_number)
        pr_urls.setdefault(key, row.get("pr_url") or f"https://github.com/{repo_name}/pull/{pr_number}")
        if row.get("repo_id") is not None:
            repo_ids[key] = int(row["repo_id"])

        payload_str = row.get("payload")
        payload = json.loads(payload_str) if isinstance(payload_str, str) else payload_str

        created_at = row["created_at"]
        if hasattr(created_at, "isoformat"):
            created_at = created_at.isoformat()

        event = {
            "event_id": str(row["event_id"]),
            "type": row["type"],
            "actor": row["actor"],
            "created_at": created_at,
            "repo_name": repo_name,
            "pr_number": pr_number,
            "payload": payload,
        }
        events_by_key.setdefault(key, []).append(event)

    # Insert PRs into database, grouped by chatbot
    inserted = 0
    total = len(events_by_key)
    async with db.transaction():
        for i, ((bot_username, repo_name, pr_number), events) in enumerate(events_by_key.items()):
            if i % 100 == 0 and i > 0:
                logger.info(f"  Inserting PRs: {i}/{total}...")
            chatbot_id = username_to_id[bot_username]
            pr_url = pr_urls[(bot_username, repo_name, pr_number)]
            meta = _extract_pr_metadata(events)

            bot_events = [e for e in events if e.get("actor") == bot_username and e.get("created_at")]
            bot_reviewed_at = min(e["created_at"] for e in bot_events) if bot_events else None

            was_inserted = await repo.insert_pr(
                chatbot_id=chatbot_id,
                repo_name=repo_name,
                pr_number=pr_number,
                pr_url=pr_url,
                pr_title=meta["pr_title"],
                pr_author=meta["pr_author"],
                pr_created_at=meta["pr_created_at"],
                pr_merged=meta["pr_merged"],
                status="pending",
                bq_events=events,
                bot_reviewed_at=bot_reviewed_at,
                repo_id=repo_ids.get((bot_username, repo_name, pr_number)),
            )
            if was_inserted:
                inserted += 1

    logger.info(
        f"Batch discovered {len(events_by_key)} PRs across {len(chatbot_usernames)} chatbots, "
        f"inserted {inserted} new ({total - inserted} already existed)"
    )
    return inserted

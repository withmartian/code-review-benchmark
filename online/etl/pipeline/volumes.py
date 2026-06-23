"""Pipeline stage: Fetch PR volume counts from BigQuery or GitHub Search API."""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from urllib.parse import quote

import httpx

from google.cloud import bigquery

from config import DBConfig
from db.connection import DBAdapter
from db.repository import PRRepository

logger = logging.getLogger(__name__)

GITHUB_SEARCH_URL = "https://api.github.com/search/issues"
# Seconds to sleep between Search API requests to avoid secondary rate limits
SEARCH_API_SLEEP = 6
# Bots whose GH Archive username differs from their Search API username
SEARCH_API_USERNAME_MAP: dict[str, str] = {
    "Copilot": "copilot-pull-request-reviewer[bot]",
}

# Count unique PRs a bot interacted with, assigned to first-seen day.
# Each PR is counted exactly once (on the earliest day the bot touched it),
# so summing daily counts = true unique total. This avoids overcounting PRs
# that span multiple days and ensures total_prs >= sampled_prs holds.
VOLUME_QUERY = """
WITH pr_first_seen AS (
  SELECT
    actor.login AS bot_username,
    CONCAT(repo.name, '/', CAST(
      CASE WHEN type = 'IssueCommentEvent'
          THEN CAST(JSON_EXTRACT_SCALAR(payload, '$.issue.number') AS INT64)
          ELSE CAST(JSON_EXTRACT_SCALAR(payload, '$.pull_request.number') AS INT64)
      END AS STRING)
    ) AS pr_key,
    MIN(_TABLE_SUFFIX) AS first_seen_day
  FROM `githubarchive.day.20*`
  WHERE
    actor.login IN UNNEST(@target_users)
    AND _TABLE_SUFFIX BETWEEN @suffix_start AND @suffix_end
    AND (
      type != 'IssueCommentEvent'
      OR JSON_EXTRACT_SCALAR(payload, '$.issue.pull_request.html_url') IS NOT NULL
    )
  GROUP BY bot_username, pr_key
)
SELECT
  bot_username,
  first_seen_day AS day_suffix,
  COUNT(*) AS pr_count
FROM pr_first_seen
GROUP BY bot_username, first_seen_day
"""


def _date_to_suffix(date_str: str) -> str:
    """Convert YYYY-MM-DD to BQ table suffix YYMMDD."""
    parts = date_str.split("-")
    return f"{parts[0][2:]}{parts[1]}{parts[2]}"


def _suffix_to_date(suffix: str) -> str:
    """Convert BQ table suffix YYMMDD to YYYY-MM-DD."""
    return f"20{suffix[:2]}-{suffix[2:4]}-{suffix[4:6]}"


async def fetch_pr_volumes(
    cfg: DBConfig,
    db: DBAdapter,
    chatbot_usernames: list[str],
    start_date: str,
    end_date: str,
) -> int:
    """Query BigQuery for PR counts per tool per day and upsert into pr_volumes.

    Returns the number of rows upserted.
    """
    repo = PRRepository(db)

    # Upsert all chatbot usernames and build username → id map
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
        ]

        # Dry run for cost estimation
        dry_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, query_parameters=params)
        dry_job = client.query(VOLUME_QUERY, job_config=dry_config)
        logger.info(f"BQ volumes estimated scan: {dry_job.total_bytes_processed / 1024**3:.2f} GB")

        # Execute query
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        job = client.query(VOLUME_QUERY, job_config=job_config)
        rows = [dict(row) for row in job]
        logger.info(f"BQ volumes query returned {len(rows)} rows")
    finally:
        client.close()

    # Upsert each (chatbot_id, date, pr_count)
    upserted = 0
    async with db.transaction():
        for row in rows:
            bot_username = row["bot_username"]
            chatbot_id = username_to_id.get(bot_username)
            if chatbot_id is None:
                continue
            date = _suffix_to_date(row["day_suffix"])
            pr_count = row["pr_count"]
            await repo.upsert_pr_volume(chatbot_id, date, pr_count)
            upserted += 1

    logger.info(f"Upserted {upserted} volume rows for {len(chatbot_usernames)} chatbots")
    return upserted


class SearchError(enum.Enum):
    """Non-count outcomes from _search_api_count."""
    UNSEARCHABLE = "unsearchable"  # 422: username not recognized by Search API
    TRANSIENT = "transient"        # request failed after retries


SearchResult = int | SearchError


async def _search_api_count(
    client: httpx.AsyncClient,
    bot_username: str,
    start_date: str,
    end_date: str | None = None,
    qualifier: str = "reviewed-by",
) -> SearchResult:
    """Query GitHub Search API for PRs involving a bot in a date range.

    Returns the total_count on success, or a SearchError variant on failure.
    Uses `<qualifier>:<bot> type:pr created:<start>..<end>`.
    If end_date is None, queries a single day.
    """
    end = end_date or start_date
    query = f"type:pr {qualifier}:{bot_username} created:{start_date}..{end}"
    for attempt in range(3):
        try:
            resp = await client.get(
                GITHUB_SEARCH_URL,
                params={"q": query, "per_page": "1"},
            )
            if resp.status_code == 422:
                logger.warning(
                    f"Search API 422 for {bot_username} ({qualifier}:) — username not searchable"
                )
                return SearchError.UNSEARCHABLE
            if resp.status_code == 403:
                retry_after = resp.headers.get("Retry-After")
                wait = int(retry_after) if retry_after else 60
                logger.warning(
                    f"Search API rate limited for {bot_username}, "
                    f"waiting {wait}s (attempt {attempt + 1}/3)"
                )
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json().get("total_count", 0)
        except httpx.HTTPError as e:
            if attempt < 2:
                await asyncio.sleep(2 ** (attempt + 1))
            else:
                logger.error(f"Search API failed for {bot_username} on {start_date}..{end}: {e}")
                return SearchError.TRANSIENT
    return SearchError.TRANSIENT


def _date_range(start_date: str, end_date: str) -> list[str]:
    """Generate YYYY-MM-DD strings for each day in [start_date, end_date]."""
    from datetime import date, timedelta

    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    dates: list[str] = []
    current = start
    while current <= end:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


def _weekly_chunks(start_date: str, end_date: str) -> list[tuple[str, str]]:
    """Split a date range into weekly (7-day) chunks.

    Returns list of (chunk_start, chunk_end) pairs covering the full range.
    The last chunk may be shorter than 7 days.
    """
    from datetime import date, timedelta

    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    chunks: list[tuple[str, str]] = []
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=6), end)
        chunks.append((current.isoformat(), chunk_end.isoformat()))
        current = chunk_end + timedelta(days=1)
    return chunks


async def fetch_pr_volumes_search_api(
    cfg: DBConfig,
    db: DBAdapter,
    chatbot_usernames: list[str],
    start_date: str,
    end_date: str,
    weekly: bool = False,
) -> int:
    """Query GitHub Search API for PR counts per bot and upsert into pr_volumes.

    Uses `reviewed-by:<bot>` to count unique PRs, attributed to PR creation date.
    Sleeps between requests to stay under secondary rate limits.

    When weekly=True, queries in 7-day chunks and distributes the count evenly
    across days. This reduces API calls by ~7x for backfills at the cost of
    less accurate per-day granularity (totals remain exact).

    Returns the number of rows upserted.
    """
    repo = PRRepository(db)

    username_to_id: dict[str, int] = {}
    for username in chatbot_usernames:
        cid = await repo.upsert_chatbot(username)
        username_to_id[username] = cid

    token = cfg.github_tokens[0] if cfg.github_tokens else cfg.github_token
    if not token:
        logger.error("No GitHub token available for Search API volumes")
        return 0

    if weekly:
        chunks = _weekly_chunks(start_date, end_date)
        total_queries = len(chatbot_usernames) * len(chunks)
        logger.info(
            f"Fetching Search API volumes (weekly) for {len(chatbot_usernames)} bots "
            f"x {len(chunks)} chunks = {total_queries} queries"
        )
    else:
        dates = _date_range(start_date, end_date)
        total_queries = len(chatbot_usernames) * len(dates)
        logger.info(
            f"Fetching Search API volumes (daily) for {len(chatbot_usernames)} bots "
            f"x {len(dates)} days = {total_queries} queries"
        )

    upserted = 0
    skipped_bots: list[str] = []
    async with httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30.0,
    ) as client:
        for username in chatbot_usernames:
            chatbot_id = username_to_id[username]
            search_username = SEARCH_API_USERNAME_MAP.get(username, username)
            bot_skipped = False

            if weekly:
                for chunk_start, chunk_end in chunks:
                    result = await _search_api_count(client, search_username, chunk_start, chunk_end)
                    if result is SearchError.UNSEARCHABLE:
                        skipped_bots.append(username)
                        bot_skipped = True
                        break
                    if isinstance(result, SearchError):
                        await asyncio.sleep(SEARCH_API_SLEEP)
                        continue
                    assert isinstance(result, int)

                    chunk_days = _date_range(chunk_start, chunk_end)
                    num_days = len(chunk_days)
                    base = result // num_days
                    remainder = result % num_days

                    async with db.transaction():
                        for i, day in enumerate(chunk_days):
                            day_count = base + (1 if i >= num_days - remainder else 0)
                            await repo.upsert_pr_volume(chatbot_id, day, day_count)
                            upserted += 1

                    await asyncio.sleep(SEARCH_API_SLEEP)
            else:
                for day in dates:
                    result = await _search_api_count(client, search_username, day)
                    if result is SearchError.UNSEARCHABLE:
                        skipped_bots.append(username)
                        bot_skipped = True
                        break
                    if isinstance(result, SearchError):
                        await asyncio.sleep(SEARCH_API_SLEEP)
                        continue
                    assert isinstance(result, int)

                    async with db.transaction():
                        await repo.upsert_pr_volume(chatbot_id, day, result)
                    upserted += 1

                    await asyncio.sleep(SEARCH_API_SLEEP)

            if bot_skipped:
                logger.info(f"  {username}: skipped (unsearchable)")
            else:
                logger.info(f"  {username}: upserted so far: {upserted}")

    if skipped_bots:
        logger.warning(
            f"Skipped {len(skipped_bots)} unsearchable bot(s) (use --source bq for these): "
            f"{', '.join(skipped_bots)}"
        )

    logger.info(f"Search API volumes: upserted {upserted} rows for {len(chatbot_usernames)} bots")
    return upserted

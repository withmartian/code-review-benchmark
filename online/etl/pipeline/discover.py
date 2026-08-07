"""Pipeline stage: Discover PRs from BigQuery or GitHub Search API and insert into database."""

from __future__ import annotations

import asyncio
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import logging
import random

import httpx

from config import DBConfig
from db.connection import DBAdapter
from db.repository import PRRepository
from pipeline.volumes import GITHUB_SEARCH_URL
from pipeline.volumes import SEARCH_API_SLEEP
from pipeline.volumes import SEARCH_API_USERNAME_MAP
from pipeline.volumes import SearchError
from pipeline.volumes import SearchResult

logger = logging.getLogger(__name__)

# Max results the GitHub Search API will return per query (hard API limit)
_SEARCH_MAX_RESULTS = 1_000
_SEARCH_PER_PAGE = 100
# Max PRs from a single repo in a daily sample (prevents repo dominance)
_MAX_PRS_PER_REPO = 10

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
    from google.cloud import bigquery

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
    from google.cloud import bigquery

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


# ---------------------------------------------------------------------------
# Search API discovery
# ---------------------------------------------------------------------------


def _parse_search_item(item: dict) -> dict:
    """Extract PR metadata from a GitHub Search API result item."""
    repo_url = item.get("repository_url", "")
    repo_name = repo_url.split("/repos/", 1)[-1] if "/repos/" in repo_url else ""
    pr_data = item.get("pull_request", {}) or {}
    return {
        "repo_name": repo_name,
        "pr_number": item["number"],
        "pr_url": item.get("html_url", f"https://github.com/{repo_name}/pull/{item['number']}"),
        "pr_title": item.get("title", ""),
        "pr_author": (item.get("user") or {}).get("login"),
        "pr_created_at": item.get("created_at"),
        "pr_merged": True,  # query uses is:merged
        # merged_at is the best proxy for bot_reviewed_at from search results;
        # the bot must have reviewed before the merge happened
        "bot_reviewed_at": pr_data.get("merged_at"),
    }


async def _search_api_count(
    client: httpx.AsyncClient,
    query: str,
) -> SearchResult:
    """Get total_count for a search query. Returns int or SearchError."""
    for attempt in range(3):
        try:
            resp = await client.get(
                GITHUB_SEARCH_URL,
                params={"q": query, "per_page": "1"},
            )
            if resp.status_code == 422:
                return SearchError.UNSEARCHABLE
            if resp.status_code == 403:
                retry_after = resp.headers.get("Retry-After")
                wait = int(retry_after) if retry_after else 60
                logger.warning(f"Search API rate limited, waiting {wait}s (attempt {attempt + 1}/3)")
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json().get("total_count", 0)
        except httpx.HTTPError as e:
            if attempt < 2:
                await asyncio.sleep(2 ** (attempt + 1))
            else:
                logger.error(f"Search API count failed: {e}")
                return SearchError.TRANSIENT
    return SearchError.TRANSIENT


async def _search_api_fetch_page(
    client: httpx.AsyncClient,
    query: str,
    page: int,
) -> list[dict] | SearchError:
    """Fetch a single page of search results."""
    for attempt in range(3):
        try:
            resp = await client.get(
                GITHUB_SEARCH_URL,
                params={"q": query, "per_page": str(_SEARCH_PER_PAGE), "page": str(page)},
            )
            if resp.status_code == 422:
                return SearchError.UNSEARCHABLE
            if resp.status_code == 403:
                retry_after = resp.headers.get("Retry-After")
                wait = int(retry_after) if retry_after else 60
                logger.warning(f"Search API rate limited on page {page}, waiting {wait}s")
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json().get("items", [])
        except httpx.HTTPError as e:
            if attempt < 2:
                await asyncio.sleep(2 ** (attempt + 1))
            else:
                logger.error(f"Search API page {page} failed: {e}")
                return SearchError.TRANSIENT
    return SearchError.TRANSIENT


async def _search_api_fetch_all(
    client: httpx.AsyncClient,
    query: str,
    total_count: int,
) -> list[dict]:
    """Paginate through all results for a query (up to 1,000)."""
    capped = min(total_count, _SEARCH_MAX_RESULTS)
    pages_needed = (capped + _SEARCH_PER_PAGE - 1) // _SEARCH_PER_PAGE
    all_items: list[dict] = []

    for page in range(1, pages_needed + 1):
        await asyncio.sleep(SEARCH_API_SLEEP)
        result = await _search_api_fetch_page(client, query, page)
        if isinstance(result, SearchError):
            logger.warning(f"Stopping pagination at page {page} due to {result}")
            break
        all_items.extend(result)
        if len(result) < _SEARCH_PER_PAGE:
            break

    return all_items


def _format_dt(dt: datetime) -> str:
    """Format a datetime for GitHub Search API merged: qualifier."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


async def _fetch_window(
    client: httpx.AsyncClient,
    search_username: str,
    window_start: datetime,
    window_end: datetime,
    depth: int = 0,
) -> list[dict]:
    """Fetch all merged PRs reviewed by a bot in a time window.

    Recursively bisects the window if total_count exceeds 1,000.
    """
    merged_range = f"merged:{_format_dt(window_start)}..{_format_dt(window_end)}"
    query = f"type:pr reviewed-by:{search_username} is:merged {merged_range}"

    await asyncio.sleep(SEARCH_API_SLEEP)
    count = await _search_api_count(client, query)

    if isinstance(count, SearchError):
        logger.warning(f"Search API error for {search_username} in {merged_range}: {count}")
        return []

    if count == 0:
        return []

    if count <= _SEARCH_MAX_RESULTS:
        logger.debug(f"  Window {merged_range}: {count} results, fetching all")
        return await _search_api_fetch_all(client, query, count)

    # Bisect: window has >1,000 results
    if depth > 8:
        # Safety valve: stop recursing, take what we can get
        logger.warning(
            f"  Window {merged_range}: {count} results at max depth {depth}, "
            f"fetching first {_SEARCH_MAX_RESULTS}"
        )
        return await _search_api_fetch_all(client, query, _SEARCH_MAX_RESULTS)

    mid = window_start + (window_end - window_start) / 2
    logger.debug(f"  Window {merged_range}: {count} results, bisecting at {_format_dt(mid)}")
    left = await _fetch_window(client, search_username, window_start, mid, depth + 1)
    right = await _fetch_window(client, search_username, mid, window_end, depth + 1)
    return left + right


def _sample_prs(
    prs: list[dict],
    max_prs_per_day: int,
    max_per_repo: int = _MAX_PRS_PER_REPO,
) -> list[dict]:
    """Apply repo cap and random sampling to a list of parsed PR dicts."""
    # Cap per repo
    repo_counts: dict[str, int] = {}
    capped: list[dict] = []
    for pr in prs:
        repo = pr["repo_name"]
        repo_counts[repo] = repo_counts.get(repo, 0) + 1
        if repo_counts[repo] <= max_per_repo:
            capped.append(pr)

    if len(capped) <= max_prs_per_day:
        return capped

    return random.sample(capped, max_prs_per_day)


async def discover_prs_search_api(
    cfg: DBConfig,
    db: DBAdapter,
    chatbot_username: str,
    start_date: str,
    end_date: str,
    max_prs_per_day: int = 500,
    display_name: str | None = None,
) -> int:
    """Discover merged PRs reviewed by a bot via GitHub Search API.

    Queries by merge date, bisects time windows that exceed 1,000 results,
    caps per-repo representation, and random-samples to max_prs_per_day.
    Returns the number of new PRs inserted.
    """
    repo = PRRepository(db)
    chatbot_id = await repo.upsert_chatbot(chatbot_username, display_name)
    search_username = SEARCH_API_USERNAME_MAP.get(chatbot_username, chatbot_username)

    token = cfg.github_tokens[0] if cfg.github_tokens else cfg.github_token
    if not token:
        logger.error("No GitHub token available for Search API discovery")
        return 0

    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)

    inserted = 0
    async with httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30.0,
    ) as client:
        current = start
        while current <= end:
            window_start = datetime(current.year, current.month, current.day, tzinfo=timezone.utc)
            window_end = window_start + timedelta(days=1)

            raw_items = await _fetch_window(client, search_username, window_start, window_end)

            if not raw_items:
                logger.debug(f"  {chatbot_username} on {current}: 0 PRs")
                current += timedelta(days=1)
                continue

            # Deduplicate by (repo_name, pr_number) in case bisection windows overlap
            seen: set[tuple[str, int]] = set()
            parsed: list[dict] = []
            for item in raw_items:
                pr = _parse_search_item(item)
                key = (pr["repo_name"], pr["pr_number"])
                if key not in seen:
                    seen.add(key)
                    parsed.append(pr)

            sampled = _sample_prs(parsed, max_prs_per_day)
            logger.info(
                f"  {chatbot_username} on {current}: {len(parsed)} unique PRs, "
                f"sampled {len(sampled)}"
            )

            async with db.transaction():
                for pr in sampled:
                    was_inserted = await repo.insert_pr(
                        chatbot_id=chatbot_id,
                        repo_name=pr["repo_name"],
                        pr_number=pr["pr_number"],
                        pr_url=pr["pr_url"],
                        pr_title=pr["pr_title"],
                        pr_author=pr["pr_author"],
                        pr_created_at=pr["pr_created_at"],
                        pr_merged=pr["pr_merged"],
                        status="pending",
                        bq_events=None,
                        bot_reviewed_at=pr.get("bot_reviewed_at"),
                    )
                    if was_inserted:
                        inserted += 1

            current += timedelta(days=1)

    logger.info(f"Search API discovered {inserted} new PRs for {chatbot_username}")
    return inserted


async def discover_prs_search_api_batch(
    cfg: DBConfig,
    db: DBAdapter,
    chatbot_usernames: list[str],
    start_date: str,
    end_date: str,
    max_prs_per_day: int = 500,
) -> int:
    """Discover merged PRs for multiple bots via GitHub Search API.

    Iterates over each bot sequentially (Search API rate limits are per-token,
    not per-bot). Returns total new PRs inserted across all bots.
    """
    total_inserted = 0
    for username in chatbot_usernames:
        logger.info(f"Discovering PRs for {username} via Search API")
        count = await discover_prs_search_api(
            cfg, db, username, start_date, end_date,
            max_prs_per_day=max_prs_per_day,
        )
        total_inserted += count

    logger.info(
        f"Search API batch: discovered {total_inserted} new PRs "
        f"across {len(chatbot_usernames)} bots"
    )
    return total_inserted

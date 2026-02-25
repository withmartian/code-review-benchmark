"""Pipeline stage: Fetch PR volume counts from BigQuery and store in database."""

from __future__ import annotations

import logging

from google.cloud import bigquery

from config import DBConfig
from db.connection import DBAdapter
from db.repository import PRRepository

logger = logging.getLogger(__name__)

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

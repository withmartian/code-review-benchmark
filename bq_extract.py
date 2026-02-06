"""Phase 1: BigQuery extraction of PR events from GitHub Archive.

Uses a single combined query: a CTE finds target PRs, then a JOIN fetches
all events for those PRs in one scan.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from google.cloud import bigquery

from config import Config
from models import TargetPR

logger = logging.getLogger(__name__)


def _render_sql_audit(
    query: str,
    params: list[bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter],
    description: str = "",
) -> str:
    """Render a SQL query with parameter values as a header comment for audit."""
    lines = []
    if description:
        lines.append(f"-- {description}")
    lines.append(f"-- Executed at: {datetime.now().isoformat()}")
    lines.append("-- Parameters:")
    for p in params:
        if isinstance(p, bigquery.ArrayQueryParameter):
            values_str = ", ".join(str(v) for v in (p.values or []))
            lines.append(f"--   @{p.name} = [{values_str}]")
        else:
            lines.append(f"--   @{p.name} = {repr(p.value)}")
    lines.append("")
    lines.append(query.strip())
    lines.append("")
    return "\n".join(lines)


def _dry_run_query(client: bigquery.Client, query: str, params: list[bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter]) -> int:
    """Run a dry-run query and return estimated bytes processed."""
    job_config = bigquery.QueryJobConfig(
        dry_run=True,
        use_query_cache=False,
        query_parameters=params,
    )
    job = client.query(query, job_config=job_config)
    return job.total_bytes_processed


def _run_query(client: bigquery.Client, query: str, params: list[bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter]) -> list[dict]:
    """Execute a query and return rows as list of dicts."""
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    job = client.query(query, job_config=job_config)
    rows = []
    for row in job:
        rows.append(dict(row))
    actual_bytes = job.total_bytes_processed
    logger.info(f"Query processed {actual_bytes / 1024**3:.2f} GB")
    return rows


COMBINED_QUERY = """
WITH target_prs AS (
  SELECT DISTINCT
    repo.name AS repo_name,
    CASE
      WHEN type = 'IssueCommentEvent'
        THEN CAST(JSON_EXTRACT_SCALAR(payload, '$.issue.number') AS INT64)
      ELSE CAST(JSON_EXTRACT_SCALAR(payload, '$.pull_request.number') AS INT64)
    END AS pr_number,
    COALESCE(
      JSON_EXTRACT_SCALAR(payload, '$.pull_request.html_url'),
      JSON_EXTRACT_SCALAR(payload, '$.issue.pull_request.html_url')
    ) AS pr_url
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
)
SELECT
  t.repo_name,
  t.pr_number,
  t.pr_url,
  e.type,
  e.actor.login AS actor,
  e.created_at,
  e.payload,
  e.id AS event_id
FROM `githubarchive.day.20*` e
INNER JOIN target_prs t ON e.repo.name = t.repo_name
WHERE
  e._TABLE_SUFFIX BETWEEN @suffix_start AND @suffix_end
  AND (
    (CAST(JSON_EXTRACT_SCALAR(e.payload, '$.pull_request.number') AS INT64) = t.pr_number)
    OR
    (e.type = 'IssueCommentEvent'
     AND CAST(JSON_EXTRACT_SCALAR(e.payload, '$.issue.number') AS INT64) = t.pr_number
     AND JSON_EXTRACT_SCALAR(e.payload, '$.issue.pull_request.html_url') IS NOT NULL)
  )
ORDER BY t.repo_name, t.pr_number, e.created_at
"""


def run_bq_extract(config: Config) -> list[TargetPR]:
    """Run the full BigQuery extraction phase as a single combined query."""
    # Check cache: if target PRs list exists and all event files exist, skip
    if os.path.exists(config.target_prs_path) and not config.force_refetch:
        logger.info(f"Loading cached target PRs from {config.target_prs_path}")
        with open(config.target_prs_path) as f:
            prs = [TargetPR.from_dict(d) for d in json.load(f)]
        if config.max_prs is not None:
            prs = prs[: config.max_prs]
        all_have_events = all(
            os.path.exists(os.path.join(pr.pr_dir(config.user_dir), "02_fetch_events.json"))
            for pr in prs
        )
        if all_have_events:
            logger.info(f"All {len(prs)} PRs already have event files — skipping BQ extraction")
            return prs
        logger.info("Some PRs missing event files — re-running combined query")

    client = bigquery.Client(project=config.gcp_project)
    suffix_start = config.bq_suffix_start()
    suffix_end = config.bq_suffix_end()

    params = [
        bigquery.ScalarQueryParameter("target_user", "STRING", config.target_user),
        bigquery.ScalarQueryParameter("suffix_start", "STRING", suffix_start),
        bigquery.ScalarQueryParameter("suffix_end", "STRING", suffix_end),
        bigquery.ScalarQueryParameter("min_pr_number", "INT64", config.min_pr_number),
    ]

    # Dry run
    estimated_bytes = _dry_run_query(client, COMBINED_QUERY, params)
    logger.info(f"Combined query estimated scan: {estimated_bytes / 1024**3:.2f} GB")

    # Save SQL for audit
    os.makedirs(config.user_dir, exist_ok=True)
    sql_content = _render_sql_audit(
        COMBINED_QUERY, params,
        description=f"Combined: find PRs where {config.target_user} participated + fetch all events",
    )
    with open(os.path.join(config.user_dir, "01_find_prs.sql"), "w") as f:
        f.write(sql_content)

    if config.bq_dry_run:
        logger.info("Dry-run mode — not executing query.")
        return []

    rows = _run_query(client, COMBINED_QUERY, params)

    # Split results: extract target PR list + group events by PR
    seen_prs: dict[tuple[str, int], TargetPR] = {}
    events_by_key: dict[tuple[str, int], list[dict]] = {}

    for row in rows:
        repo_name = row["repo_name"]
        pr_number = row["pr_number"]
        key = (repo_name, pr_number)

        if key not in seen_prs:
            pr_url = row.get("pr_url") or f"https://github.com/{repo_name}/pull/{pr_number}"
            seen_prs[key] = TargetPR(repo_name=repo_name, pr_number=pr_number, pr_url=pr_url)

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

    prs = list(seen_prs.values())
    logger.info(f"Found {len(prs)} target PRs with {len(rows)} total events")

    if config.max_prs is not None:
        prs = prs[: config.max_prs]

    # Save target PRs list
    with open(config.target_prs_path, "w") as f:
        json.dump([p.to_dict() for p in prs], f, indent=2)
    logger.info(f"Saved {len(prs)} target PRs to {config.target_prs_path}")

    # Write per-PR event files
    target_keys = {(p.repo_name, p.pr_number) for p in prs}
    for key, events in events_by_key.items():
        if key not in target_keys:
            continue
        pr = seen_prs[key]
        pr_dir = pr.pr_dir(config.user_dir)
        os.makedirs(pr_dir, exist_ok=True)
        with open(os.path.join(pr_dir, "02_fetch_events.json"), "w") as f:
            json.dump(events, f, indent=2)
        with open(os.path.join(pr_dir, "02_fetch_events.sql"), "w") as f:
            f.write(sql_content)

    return prs

"""Pipeline stage: Enrich PRs with GitHub API data (resumable, rate-limit aware)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

import httpx

from config import DBConfig
from db.connection import DBAdapter
from db.repository import PRRepository

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.github.com/graphql"
REST_BASE = "https://api.github.com"

REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $prNumber: Int!, $threadCursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $prNumber) {
      reviewThreads(first: 100, after: $threadCursor) {
        nodes {
          id
          isResolved
          resolvedBy { login }
          comments(first: 50) {
            nodes {
              databaseId
              body
              path
              line
              originalLine
              diffHunk
              author { login }
              createdAt
              reactionGroups {
                content
                reactors { totalCount }
              }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

# Steps in order
ENRICHMENT_STEPS = ["bq_events", "commits", "reviews", "threads", "details", "done"]


class RateLimitExhaustedError(Exception):
    """Raised when GitHub rate limit is hit, with reset timestamp."""

    def __init__(self, reset_at: int):
        self.reset_at = reset_at
        super().__init__(f"Rate limit exhausted, resets at {reset_at}")


class TokenInvalidError(Exception):
    """Raised when a GitHub token is rejected with 401 — invalid, expired, or revoked."""


class AllTokensInvalidError(Exception):
    """Raised when every token in the pool has been permanently rejected.
    Never causes a PR to be marked as error — the enrichment loop aborts instead."""


class GitHubEnrichClient:
    """Async GitHub API client with rate limiting and retries — adapted from gh_enrich.py."""

    def __init__(self, token: str, concurrency: int = 10):
        self.token = token
        self.semaphore = asyncio.Semaphore(concurrency)
        self.api_calls = 0
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _check_rate_limit(self, response: httpx.Response) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset_time = response.headers.get("X-RateLimit-Reset")
        if remaining is not None and int(remaining) < 10 and reset_time:
            raise RateLimitExhaustedError(int(reset_time))
        self.api_calls += 1
        if self.api_calls % 100 == 0:
            logger.info(f"GitHub API calls: {self.api_calls}, remaining: {remaining}")

    def _is_rate_limited(self, resp: httpx.Response) -> bool:
        """Check if a 403 response is due to rate limiting (vs genuine forbidden)."""
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None and int(remaining) == 0:
            return True
        # Secondary rate limits (abuse detection) use Retry-After
        if resp.headers.get("Retry-After"):
            return True
        body = resp.text.lower()
        return "rate limit" in body or "abuse detection" in body

    async def rest_get(self, path: str, params: dict | None = None) -> httpx.Response | None:
        async with self.semaphore:
            client = await self._get_client()
            url = f"{REST_BASE}{path}"
            for attempt in range(4):
                try:
                    resp = await client.get(url, params=params)
                    if resp.status_code == 401:
                        raise TokenInvalidError(f"Token rejected (401) for {url}: {resp.text[:200]}")
                    if resp.status_code == 403:
                        if self._is_rate_limited(resp):
                            reset_time = resp.headers.get("X-RateLimit-Reset")
                            raise RateLimitExhaustedError(int(reset_time or time.time() + 60))
                        # Genuine forbidden — treat like 404, skip
                        logger.warning(f"403 Forbidden for {url} — skipping")
                        return None
                    await self._check_rate_limit(resp)
                    # 404/410 Gone/422/451 Legal Reasons — content unavailable, skip
                    if resp.status_code in (404, 410, 422, 451):
                        logger.warning(f"{resp.status_code} for {url} — skipping")
                        return None
                    if resp.status_code == 301:
                        location = resp.headers.get("Location")
                        if location:
                            logger.info(f"Following 301 redirect: {url} → {location}")
                            url = location
                            continue
                        return None
                    if resp.status_code >= 500:
                        wait = 2**attempt
                        logger.warning(f"{resp.status_code} on {url}, retrying in {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return resp
                except (RateLimitExhaustedError, TokenInvalidError):
                    raise
                except httpx.HTTPError as e:
                    if attempt < 3:
                        await asyncio.sleep(2**attempt)
                    else:
                        logger.error(f"Failed after 4 attempts: {url}: {e}")
                        return None
        return None

    async def rest_get_paginated(self, path: str, params: dict | None = None) -> list[dict]:
        results: list[dict] = []
        params = dict(params or {})
        params.setdefault("per_page", "100")
        page = 1
        while True:
            params["page"] = str(page)
            resp = await self.rest_get(path, params)
            if resp is None:
                break
            data = resp.json()
            if not isinstance(data, list) or len(data) == 0:
                break
            results.extend(data)
            link = resp.headers.get("Link", "")
            if 'rel="next"' not in link:
                break
            page += 1
        return results

    async def graphql(self, query: str, variables: dict) -> dict | None:
        async with self.semaphore:
            client = await self._get_client()
            for attempt in range(4):
                try:
                    resp = await client.post(
                        GRAPHQL_URL,
                        json={"query": query, "variables": variables},
                    )
                    if resp.status_code == 401:
                        raise TokenInvalidError(f"Token rejected (401) on GraphQL: {resp.text[:200]}")
                    if resp.status_code == 403:
                        if self._is_rate_limited(resp):
                            reset_time = resp.headers.get("X-RateLimit-Reset")
                            raise RateLimitExhaustedError(int(reset_time or time.time() + 60))
                        logger.warning("403 Forbidden on GraphQL query — skipping")
                        return None
                    await self._check_rate_limit(resp)
                    if resp.status_code >= 500:
                        await asyncio.sleep(2**attempt)
                        continue
                    data = resp.json()
                    if "errors" in data:
                        logger.warning(f"GraphQL errors: {data['errors']}")
                        if data.get("data") is not None:
                            return data["data"]
                        return None
                    return data.get("data")
                except (RateLimitExhaustedError, TokenInvalidError):
                    raise
                except httpx.HTTPError as e:
                    if attempt < 3:
                        await asyncio.sleep(2**attempt)
                    else:
                        logger.error(f"GraphQL failed after 4 attempts: {e}")
                        return None
        return None


# -- Enrichment sub-steps (return JSONB-ready data) ----------------------------


async def _fetch_commits(gh: GitHubEnrichClient, owner: str, repo: str, pr_number: int, *, repo_path: str = "") -> list[dict]:
    base = repo_path or f"/repos/{owner}/{repo}"
    path = f"{base}/pulls/{pr_number}/commits"
    raw = await gh.rest_get_paginated(path)
    return [
        {
            "sha": c["sha"],
            "message": c.get("commit", {}).get("message", ""),
            "date": c.get("commit", {}).get("author", {}).get("date", ""),
            "author": (c.get("author") or {}).get("login"),
        }
        for c in raw
    ]


async def _fetch_reviews(gh: GitHubEnrichClient, owner: str, repo: str, pr_number: int, *, repo_path: str = "") -> list[dict]:
    base = repo_path or f"/repos/{owner}/{repo}"
    path = f"{base}/pulls/{pr_number}/reviews"
    raw = await gh.rest_get_paginated(path)
    return [
        {
            "id": r["id"],
            "author": (r.get("user") or {}).get("login"),
            "state": r.get("state", ""),
            "body": r.get("body", ""),
            "submitted_at": r.get("submitted_at"),
            "commit_id": r.get("commit_id"),
            "author_association": r.get("author_association"),
        }
        for r in raw
    ]


async def _fetch_review_threads(gh: GitHubEnrichClient, owner: str, repo: str, pr_number: int) -> list[dict]:
    all_threads: list[dict] = []
    cursor = None
    while True:
        variables = {"owner": owner, "repo": repo, "prNumber": pr_number, "threadCursor": cursor}
        data = await gh.graphql(REVIEW_THREADS_QUERY, variables)
        if data is None:
            break
        pr_data = (data.get("repository") or {}).get("pullRequest")
        if pr_data is None:
            break
        threads_data = pr_data.get("reviewThreads", {})
        for node in threads_data.get("nodes", []):
            thread = {
                "id": node["id"],
                "is_resolved": node["isResolved"],
                "resolved_by": (node.get("resolvedBy") or {}).get("login"),
                "comments": [],
            }
            for comment in (node.get("comments") or {}).get("nodes", []):
                reactions = {}
                for rg in comment.get("reactionGroups") or []:
                    reactions[rg["content"]] = rg["reactors"]["totalCount"]
                thread["comments"].append(
                    {
                        "id": comment["databaseId"],
                        "body": comment["body"],
                        "path": comment.get("path"),
                        "line": comment.get("line"),
                        "original_line": comment.get("originalLine"),
                        "diff_hunk": comment.get("diffHunk"),
                        "author": (comment.get("author") or {}).get("login"),
                        "created_at": comment.get("createdAt"),
                        "reactions": reactions,
                    }
                )
            all_threads.append(thread)
        page_info = threads_data.get("pageInfo", {})
        if page_info.get("hasNextPage"):
            cursor = page_info["endCursor"]
        else:
            break
    return all_threads


async def _fetch_one_commit(gh: GitHubEnrichClient, owner: str, repo: str, sha: str, *, repo_path: str = "") -> dict:
    base = repo_path or f"/repos/{owner}/{repo}"
    resp = await gh.rest_get(f"{base}/commits/{sha}")
    if resp is None:
        return {"sha": sha, "files": []}
    data = resp.json()
    files = []
    for f in data.get("files", []):
        files.append(
            {
                "filename": f["filename"],
                "status": f.get("status", "unknown"),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "patch": f.get("patch", ""),
            }
        )
    return {"sha": sha, "files": files}


async def _fetch_commit_details(gh: GitHubEnrichClient, owner: str, repo: str, commits: list[dict], *, repo_path: str = "") -> list[dict]:
    if not commits:
        return []
    tasks = [_fetch_one_commit(gh, owner, repo, c["sha"], repo_path=repo_path) for c in commits]
    return list(await asyncio.gather(*tasks))


# -- PR summary (lightweight size check) ---------------------------------------


async def _fetch_pr_summary(gh: GitHubEnrichClient, owner: str, repo: str, pr_number: int, *, repo_path: str = "") -> dict | None:
    """Fetch PR summary (1 API call) to check size before full enrichment."""
    base = repo_path or f"/repos/{owner}/{repo}"
    resp = await gh.rest_get(f"{base}/pulls/{pr_number}")
    if resp is None:
        return None
    data = resp.json()
    return {
        "additions": data.get("additions", 0),
        "deletions": data.get("deletions", 0),
        "commits": data.get("commits", 0),
        "changed_files": data.get("changed_files", 0),
        "pr_author": (data.get("user") or {}).get("login"),
        "merged": data.get("merged"),
        "repo_id": (data.get("base") or {}).get("repo", {}).get("id"),
        "raw": data,
    }


# -- Token pool for multi-token rotation ---------------------------------------


class TokenPool:
    """Manages multiple GitHubEnrichClient instances with load-aware distribution."""

    def __init__(self, tokens: list[str], concurrency: int = 10):
        self._entries: list[dict] = [
            {"client": GitHubEnrichClient(t, concurrency), "reset_at": 0, "active": 0, "invalid": False}
            for t in tokens
        ]

    @property
    def size(self) -> int:
        return len(self._entries)

    def get(self) -> GitHubEnrichClient | None:
        """Return the least-loaded non-rate-limited client, or None if all exhausted."""
        now = time.time()
        available = [e for e in self._entries if not e["invalid"] and e["reset_at"] <= now]
        if not available:
            return None
        best = min(available, key=lambda e: e["active"])
        best["active"] += 1
        return best["client"]

    def all_invalid(self) -> bool:
        """True when every token has been permanently rejected — pipeline cannot continue."""
        return all(e["invalid"] for e in self._entries)

    def release(self, client: GitHubEnrichClient) -> None:
        """Decrement active count when a worker finishes using a client."""
        for e in self._entries:
            if e["client"] is client:
                e["active"] = max(0, e["active"] - 1)
                break

    def mark_limited(self, client: GitHubEnrichClient, reset_at: int) -> None:
        for e in self._entries:
            if e["client"] is client:
                e["reset_at"] = reset_at
                e["active"] = 0
                break

    def mark_invalid(self, client: GitHubEnrichClient) -> None:
        """Permanently remove a token from rotation (bad credentials, revoked, etc.)."""
        for e in self._entries:
            if e["client"] is client:
                e["invalid"] = True
                e["active"] = 0
                logger.error(f"Token permanently disabled — {self.status_summary()}")
                break

    def earliest_reset(self) -> float:
        """Return the earliest reset time among rate-limited (non-invalid) tokens."""
        limited = [e["reset_at"] for e in self._entries if not e["invalid"]]
        return min(limited) if limited else float("inf")

    def status_summary(self) -> str:
        now = time.time()
        parts = []
        for i, e in enumerate(self._entries):
            if e["invalid"]:
                parts.append(f"T{i}:invalid")
            elif e["reset_at"] > now:
                parts.append(f"T{i}:limited({int(e['reset_at'] - now)}s)")
            else:
                parts.append(f"T{i}:active={e['active']}")
        return " ".join(parts)

    async def close(self) -> None:
        for e in self._entries:
            await e["client"].close()


# -- Main enrichment logic -----------------------------------------------------


def _step_index(step: str | None) -> int:
    """Return the index of a step in the enrichment sequence, -1 if not started."""
    if step is None:
        return -1
    try:
        return ENRICHMENT_STEPS.index(step)
    except ValueError:
        return -1


async def enrich_single_pr(
    gh: GitHubEnrichClient,
    repo_obj: PRRepository,
    pr_row: dict,
    cfg: DBConfig | None = None,
) -> None:
    """Enrich a single PR, resuming from wherever we left off."""
    pr_id = pr_row["id"]
    repo_name = pr_row["repo_name"]
    pr_number = pr_row["pr_number"]
    current_step = pr_row.get("enrichment_step")
    step_idx = _step_index(current_step)

    owner, repo = repo_name.split("/", 1)
    repo_id = pr_row.get("repo_id")
    repo_path = f"/repositories/{repo_id}" if repo_id else f"/repos/{owner}/{repo}"

    # Size check: fetch PR summary and skip if too large
    if cfg is not None and step_idx < 1:
        summary = await _fetch_pr_summary(gh, owner, repo, pr_number, repo_path=repo_path)
        if summary is not None:
            # Backfill pr_author from GitHub API if missing from BQ events
            if not pr_row.get("pr_author") and summary.get("pr_author"):
                await repo_obj.update_pr_author(pr_id, summary["pr_author"])

            # Store pr_api_raw, pr_merged, repo_id from the API response
            await repo_obj.db.execute(
                *repo_obj.db._translate_params(
                    "UPDATE prs SET pr_api_raw = COALESCE(pr_api_raw, $1), "
                    "pr_merged = COALESCE($2, pr_merged), "
                    "repo_id = COALESCE(repo_id, $3) WHERE id = $4",
                    (json.dumps(summary["raw"]), summary["merged"], summary["repo_id"], pr_id),
                )
            )

            total_lines = summary["additions"] + summary["deletions"]
            if summary["commits"] > cfg.max_pr_commits:
                reason = f"Too many commits: {summary['commits']} > {cfg.max_pr_commits}"
                logger.warning(f"Skipping {repo_name}#{pr_number}: {reason}")
                await repo_obj.mark_skipped(pr_id, reason)
                return
            if total_lines > cfg.max_pr_changed_lines:
                reason = f"Too many changed lines: {total_lines} > {cfg.max_pr_changed_lines}"
                logger.warning(f"Skipping {repo_name}#{pr_number}: {reason}")
                await repo_obj.mark_skipped(pr_id, reason)
                return

    # Step: commits (index 1)
    if step_idx < 1:
        commits = await _fetch_commits(gh, owner, repo, pr_number, repo_path=repo_path)
        await repo_obj.update_commits(pr_id, commits)
        logger.debug(f"  {repo_name}#{pr_number}: commits done ({len(commits)})")

    # Step: reviews (index 2)
    if step_idx < 2:
        reviews = await _fetch_reviews(gh, owner, repo, pr_number, repo_path=repo_path)
        await repo_obj.update_reviews(pr_id, reviews)
        logger.debug(f"  {repo_name}#{pr_number}: reviews done ({len(reviews)})")

    # Step: threads (index 3)
    if step_idx < 3:
        threads = await _fetch_review_threads(gh, owner, repo, pr_number)
        await repo_obj.update_threads(pr_id, threads)
        logger.debug(f"  {repo_name}#{pr_number}: threads done ({len(threads)})")

    # Step: commit details (index 4)
    if step_idx < 4:
        # Need commits data — either from this run or from DB
        commits_json = pr_row.get("commits")
        if commits_json:
            commits_data = json.loads(commits_json) if isinstance(commits_json, str) else commits_json
        else:
            # Re-read from DB since we just wrote it
            refreshed = await repo_obj.get_pr_by_id(pr_id)
            commits_json = refreshed.get("commits") if refreshed else None
            commits_data = json.loads(commits_json) if commits_json else []

        details = await _fetch_commit_details(gh, owner, repo, commits_data, repo_path=repo_path)
        await repo_obj.update_commit_details(pr_id, details)
        logger.debug(f"  {repo_name}#{pr_number}: commit details done")

    # Mark enrichment complete
    await repo_obj.mark_enrichment_done(pr_id)
    logger.info(f"Enriched {repo_name}#{pr_number}")


async def enrich_loop(
    cfg: DBConfig,
    db: DBAdapter,
    chatbot_id: int,
    chatbot_username: str | None = None,
    max_prs: int | None = None,
    one_shot: bool = False,
) -> int:
    """Main enrichment loop. Processes pending PRs until exhausted or rate-limited.

    Uses an asyncio.Queue + worker pool pattern so rate-limited tokens don't
    block workers that still have available tokens.

    If one_shot=True, processes available PRs once and returns.
    If one_shot=False, runs indefinitely (daemon mode), sleeping when idle or rate-limited.
    If chatbot_username is provided, assembles enriched PRs periodically.

    Returns total number of PRs enriched.
    """
    from pipeline.assemble import assemble_enriched_prs

    repo_obj = PRRepository(db)
    tokens = cfg.github_tokens if cfg.github_tokens else [cfg.github_token]
    pool = TokenPool(tokens)
    n_tokens = pool.size
    n_workers = n_tokens * 10
    logger.info(f"Using {n_tokens} GitHub token(s), {n_workers} workers")

    enriched_count = 0
    error_count = 0
    limit = max_prs or 10000
    assemble_interval = 100  # assemble every N enriched PRs
    last_assembled_at = 0

    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    stop_event = asyncio.Event()

    async def _worker(worker_id: int) -> None:
        nonlocal enriched_count, error_count
        while True:
            # Stop early if the pool is dead or another worker requested shutdown —
            # avoids locking more PRs we can't actually process.
            if stop_event.is_set() or pool.all_invalid():
                # Drain remaining items (including sentinels) so queue.join() unblocks
                try:
                    pr_row = queue.get_nowait()
                    queue.task_done()
                    continue
                except asyncio.QueueEmpty:
                    break

            pr_row = await queue.get()
            if pr_row is None:
                queue.task_done()
                break

            pr_id = pr_row["id"]
            pr_label = f"{pr_row['repo_name']}#{pr_row['pr_number']}"
            try:
                locked = await repo_obj.lock_pr(pr_id, cfg.worker_id, cfg.lock_timeout_minutes)
                if not locked:
                    queue.task_done()
                    continue

                gh = None
                try:
                    while True:
                        if pool.all_invalid():
                            raise AllTokensInvalidError("All GitHub tokens are invalid — aborting enrichment")
                        gh = pool.get()
                        if gh is None:
                            wait = max(0, pool.earliest_reset() - time.time()) + 5
                            logger.warning(f"Worker {worker_id}: all tokens rate-limited, sleeping {wait:.0f}s")
                            await asyncio.sleep(wait)
                            continue
                        try:
                            await enrich_single_pr(gh, repo_obj, pr_row, cfg)
                            enriched_count += 1
                            break
                        except TokenInvalidError:
                            pool.mark_invalid(gh)
                            gh = None
                            logger.warning(f"Worker {worker_id}: token invalid, rotating ({pool.status_summary()})")
                            continue
                        except RateLimitExhaustedError as e:
                            pool.mark_limited(gh, e.reset_at)
                            logger.info(f"Worker {worker_id}: token rate-limited, rotating ({pool.status_summary()})")
                            gh = None
                            continue
                finally:
                    if gh is not None:
                        pool.release(gh)
            except AllTokensInvalidError:
                # Don't mark the PR as error — release the lock so it's picked up
                # immediately on the next run with valid tokens (instead of waiting
                # for the stale-lock timeout). Signal all workers to stop.
                logger.critical(f"Worker {worker_id}: all tokens invalid, stopping enrichment loop")
                with contextlib.suppress(Exception):
                    await repo_obj.unlock_pr(pr_id)
                stop_event.set()
            except Exception as e:
                logger.error(f"Worker {worker_id}: error enriching {pr_label}: {e}")
                await repo_obj.mark_error(pr_id, str(e))
                error_count += 1
            finally:
                queue.task_done()

            if max_prs and enriched_count >= max_prs:
                stop_event.set()

    async def _periodic_assembler() -> None:
        """Assemble enriched PRs periodically while workers are running."""
        nonlocal last_assembled_at
        if not chatbot_username:
            return
        while not stop_event.is_set():
            await asyncio.sleep(15)
            if enriched_count - last_assembled_at >= assemble_interval:
                assembled = await assemble_enriched_prs(db, chatbot_id, chatbot_username)
                if assembled:
                    logger.info(f"Periodic assembly: {assembled} PRs")
                last_assembled_at = enriched_count

    async def _progress_logger() -> None:
        """Log progress periodically while workers are running."""
        while not stop_event.is_set():
            await asyncio.sleep(30)
            logger.info(
                f"Progress: {enriched_count} enriched, {error_count} errors, "
                f"~{queue.qsize()} queued | Tokens: {pool.status_summary()}"
            )

    try:
        while True:
            prs = await repo_obj.get_pending_prs(chatbot_id, limit=limit)
            if not prs:
                if one_shot:
                    logger.info("No pending PRs found.")
                    break
                logger.info("No pending PRs, sleeping 5 minutes...")
                await asyncio.sleep(300)
                continue

            logger.info(f"Queuing {len(prs)} PRs for enrichment")

            # Fill queue
            for pr_row in prs:
                await queue.put(pr_row)

            # Sentinel values to stop workers
            for _ in range(n_workers):
                await queue.put(None)

            # Start workers + background tasks
            stop_event.clear()
            workers = [asyncio.create_task(_worker(i)) for i in range(n_workers)]
            progress_task = asyncio.create_task(_progress_logger())
            assembler_task = asyncio.create_task(_periodic_assembler())

            # Wait for all work to complete
            await queue.join()
            stop_event.set()
            await asyncio.gather(*workers)
            for task in (progress_task, assembler_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            logger.info(
                f"Pass complete: {enriched_count} enriched, {error_count} errors | Tokens: {pool.status_summary()}"
            )

            # Assemble enriched PRs
            if chatbot_username and enriched_count > last_assembled_at:
                assembled = await assemble_enriched_prs(db, chatbot_id, chatbot_username)
                if assembled:
                    logger.info(f"Assembled {assembled} PRs")
                last_assembled_at = enriched_count

            if max_prs and enriched_count >= max_prs:
                logger.info(f"Reached max_prs limit ({max_prs})")
                break

            if one_shot:
                break

    finally:
        await pool.close()

    return enriched_count

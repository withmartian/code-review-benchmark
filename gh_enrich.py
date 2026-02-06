"""Phase 2: GitHub API enrichment for PR data."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx
from tqdm import tqdm

from config import Config
from models import TargetPR

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.github.com/graphql"
REST_BASE = "https://api.github.com"

# GraphQL query for review threads with reactions and resolution status
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


class GitHubClient:
    """Async GitHub API client with rate limiting and retries."""

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
        if remaining is not None and int(remaining) < 100:
            if reset_time:
                wait = max(0, int(reset_time) - int(time.time())) + 1
                logger.warning(f"Rate limit low ({remaining} remaining), sleeping {wait}s")
                await asyncio.sleep(wait)

        self.api_calls += 1
        if self.api_calls % 100 == 0:
            logger.info(f"GitHub API calls made: {self.api_calls}, rate limit remaining: {remaining}")

    async def rest_get(self, path: str, params: dict | None = None) -> httpx.Response | None:
        """GET request to REST API with retries."""
        async with self.semaphore:
            client = await self._get_client()
            url = f"{REST_BASE}{path}"
            for attempt in range(4):
                try:
                    resp = await client.get(url, params=params)
                    await self._check_rate_limit(resp)

                    if resp.status_code == 404:
                        logger.warning(f"404 for {url} — skipping")
                        return None
                    if resp.status_code == 422:
                        logger.warning(f"422 for {url} — skipping")
                        return None
                    if resp.status_code == 403:
                        # Rate limited
                        reset_time = resp.headers.get("X-RateLimit-Reset")
                        wait = max(0, int(reset_time or 0) - int(time.time())) + 1 if reset_time else 60
                        logger.warning(f"403 rate limit on {url}, sleeping {wait}s (attempt {attempt + 1})")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status_code >= 500:
                        wait = 2 ** attempt
                        logger.warning(f"{resp.status_code} on {url}, retrying in {wait}s (attempt {attempt + 1})")
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return resp
                except httpx.HTTPError as e:
                    if attempt < 3:
                        wait = 2 ** attempt
                        logger.warning(f"HTTP error on {url}: {e}, retrying in {wait}s")
                        await asyncio.sleep(wait)
                    else:
                        logger.error(f"Failed after 4 attempts: {url}: {e}")
                        return None
        return None

    async def rest_get_paginated(self, path: str, params: dict | None = None) -> tuple[list[dict], int]:
        """GET all pages from a paginated REST endpoint. Returns (results, pages_fetched)."""
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
            # Check for next page via Link header
            link = resp.headers.get("Link", "")
            if 'rel="next"' not in link:
                break
            page += 1
        return results, page

    async def graphql(self, query: str, variables: dict) -> dict | None:
        """Execute a GraphQL query with retries."""
        async with self.semaphore:
            client = await self._get_client()
            for attempt in range(4):
                try:
                    resp = await client.post(
                        GRAPHQL_URL,
                        json={"query": query, "variables": variables},
                    )
                    await self._check_rate_limit(resp)

                    if resp.status_code == 403:
                        reset_time = resp.headers.get("X-RateLimit-Reset")
                        wait = max(0, int(reset_time or 0) - int(time.time())) + 1 if reset_time else 60
                        logger.warning(f"403 on GraphQL, sleeping {wait}s (attempt {attempt + 1})")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status_code >= 500:
                        wait = 2 ** attempt
                        logger.warning(f"{resp.status_code} on GraphQL, retrying in {wait}s")
                        await asyncio.sleep(wait)
                        continue

                    data = resp.json()
                    if "errors" in data:
                        logger.warning(f"GraphQL errors for {variables}: {data['errors']}")
                        # Some errors are non-fatal (e.g., old PRs)
                        if data.get("data") is not None:
                            return data["data"]
                        return None
                    return data.get("data")
                except httpx.HTTPError as e:
                    if attempt < 3:
                        wait = 2 ** attempt
                        logger.warning(f"GraphQL HTTP error: {e}, retrying in {wait}s")
                        await asyncio.sleep(wait)
                    else:
                        logger.error(f"GraphQL failed after 4 attempts: {e}")
                        return None
        return None


async def enrich_pr_commits(gh: GitHubClient, pr: TargetPR, pr_dir: str) -> None:
    """Fetch PR commits via REST API and save request + response."""
    response_path = os.path.join(pr_dir, "03_commits_response.json")
    if os.path.exists(response_path):
        return

    api_path = f"/repos/{pr.owner()}/{pr.repo()}/pulls/{pr.pr_number}/commits"
    raw_commits, pages_fetched = await gh.rest_get_paginated(api_path)

    # Save request audit
    request_info = {
        "step": "03_commits",
        "description": f"Fetch commits for {pr.repo_name}#{pr.pr_number}",
        "method": "GET",
        "url": f"{REST_BASE}{api_path}",
        "params": {"per_page": 100},
        "pages_fetched": pages_fetched,
    }
    with open(os.path.join(pr_dir, "03_commits_request.json"), "w") as f:
        json.dump(request_info, f, indent=2)

    # Process and save response
    commits = []
    for c in raw_commits:
        commits.append({
            "sha": c["sha"],
            "message": c.get("commit", {}).get("message", ""),
            "date": c.get("commit", {}).get("author", {}).get("date", ""),
            "author": (c.get("author") or {}).get("login"),
        })

    with open(response_path, "w") as f:
        json.dump(commits, f, indent=2)


async def enrich_reviews(gh: GitHubClient, pr: TargetPR, pr_dir: str) -> None:
    """Fetch top-level reviews via REST API and save request + response."""
    response_path = os.path.join(pr_dir, "04_reviews_response.json")
    if os.path.exists(response_path):
        return

    api_path = f"/repos/{pr.owner()}/{pr.repo()}/pulls/{pr.pr_number}/reviews"
    raw_reviews, pages_fetched = await gh.rest_get_paginated(api_path)

    # Save request audit
    request_info = {
        "step": "04_reviews",
        "description": f"Fetch reviews for {pr.repo_name}#{pr.pr_number}",
        "method": "GET",
        "url": f"{REST_BASE}{api_path}",
        "params": {"per_page": 100},
        "pages_fetched": pages_fetched,
    }
    with open(os.path.join(pr_dir, "04_reviews_request.json"), "w") as f:
        json.dump(request_info, f, indent=2)

    # Process and save response
    reviews = []
    for r in raw_reviews:
        reviews.append({
            "id": r["id"],
            "author": (r.get("user") or {}).get("login"),
            "state": r.get("state", ""),
            "body": r.get("body", ""),
            "submitted_at": r.get("submitted_at"),
            "commit_id": r.get("commit_id"),
            "author_association": r.get("author_association"),
        })

    with open(response_path, "w") as f:
        json.dump(reviews, f, indent=2)


async def enrich_review_threads(gh: GitHubClient, pr: TargetPR, pr_dir: str) -> None:
    """Fetch review threads via GraphQL and save request + response."""
    response_path = os.path.join(pr_dir, "05_review_threads_response.json")
    if os.path.exists(response_path):
        return

    all_threads: list[dict] = []
    cursor = None
    all_variables: list[dict] = []

    while True:
        variables = {
            "owner": pr.owner(),
            "repo": pr.repo(),
            "prNumber": pr.pr_number,
            "threadCursor": cursor,
        }
        all_variables.append(variables.copy())
        data = await gh.graphql(REVIEW_THREADS_QUERY, variables)
        if data is None:
            break

        pr_data = (data.get("repository") or {}).get("pullRequest")
        if pr_data is None:
            logger.warning(f"No PR data in GraphQL response for {pr.repo_name}#{pr.pr_number}")
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
                thread["comments"].append({
                    "id": comment["databaseId"],
                    "body": comment["body"],
                    "path": comment.get("path"),
                    "line": comment.get("line"),
                    "original_line": comment.get("originalLine"),
                    "diff_hunk": comment.get("diffHunk"),
                    "author": (comment.get("author") or {}).get("login"),
                    "created_at": comment.get("createdAt"),
                    "reactions": reactions,
                })
            all_threads.append(thread)

        page_info = threads_data.get("pageInfo", {})
        if page_info.get("hasNextPage"):
            cursor = page_info["endCursor"]
        else:
            break

    # Save request audit
    request_info = {
        "step": "05_review_threads",
        "description": f"Fetch review threads for {pr.repo_name}#{pr.pr_number}",
        "method": "POST",
        "url": GRAPHQL_URL,
        "query": REVIEW_THREADS_QUERY.strip(),
        "requests": all_variables,
        "pages_fetched": len(all_variables),
    }
    with open(os.path.join(pr_dir, "05_review_threads_request.json"), "w") as f:
        json.dump(request_info, f, indent=2)

    with open(response_path, "w") as f:
        json.dump(all_threads, f, indent=2)


async def enrich_commit_details(gh: GitHubClient, pr: TargetPR, pr_dir: str) -> None:
    """Fetch file changes for each commit and save request + response."""
    response_path = os.path.join(pr_dir, "06_commit_details_response.json")
    if os.path.exists(response_path):
        return

    commits_path = os.path.join(pr_dir, "03_commits_response.json")
    if not os.path.exists(commits_path):
        return

    with open(commits_path) as f:
        commits = json.load(f)

    # Build request list for audit
    request_urls: list[dict] = []
    details: list[dict] = []

    for commit in commits:
        sha = commit["sha"]
        api_path = f"/repos/{pr.owner()}/{pr.repo()}/commits/{sha}"
        request_urls.append({
            "method": "GET",
            "url": f"{REST_BASE}{api_path}",
        })

        resp = await gh.rest_get(api_path)
        if resp is None:
            details.append({"sha": sha, "files": []})
            continue

        data = resp.json()
        files = []
        for file_info in data.get("files", []):
            entry: dict = {
                "filename": file_info["filename"],
                "status": file_info.get("status", "unknown"),
                "additions": file_info.get("additions", 0),
                "deletions": file_info.get("deletions", 0),
            }
            entry["patch"] = file_info.get("patch", "")
            files.append(entry)
        details.append({"sha": sha, "files": files})

    # Save request audit
    request_info = {
        "step": "06_commit_details",
        "description": f"Fetch file changes for {len(commits)} commits in {pr.repo_name}#{pr.pr_number}",
        "method": "GET",
        "requests": request_urls,
    }
    with open(os.path.join(pr_dir, "06_commit_details_request.json"), "w") as f:
        json.dump(request_info, f, indent=2)

    with open(response_path, "w") as f:
        json.dump(details, f, indent=2)


async def enrich_single_pr(gh: GitHubClient, pr: TargetPR, config: Config) -> None:
    """Run all enrichment steps for a single PR."""
    pr_dir = pr.pr_dir(config.user_dir)
    os.makedirs(pr_dir, exist_ok=True)

    await enrich_pr_commits(gh, pr, pr_dir)
    await enrich_reviews(gh, pr, pr_dir)
    await enrich_review_threads(gh, pr, pr_dir)
    await enrich_commit_details(gh, pr, pr_dir)


async def _run_enrichment(config: Config, prs: list[TargetPR]) -> int:
    """Async entry point for enrichment."""
    gh = GitHubClient(config.github_token)
    try:
        tasks = []
        for pr in prs:
            tasks.append(enrich_single_pr(gh, pr, config))

        # Process with progress bar
        pbar = tqdm(total=len(tasks), desc="Enriching PRs")
        results = []
        # Process in batches to avoid overwhelming the event loop
        batch_size = 50
        for i in range(0, len(tasks), batch_size):
            batch = tasks[i:i + batch_size]
            batch_results = await asyncio.gather(*batch, return_exceptions=True)
            for j, result in enumerate(batch_results):
                if isinstance(result, Exception):
                    pr = prs[i + j]
                    logger.error(f"Error enriching {pr.repo_name}#{pr.pr_number}: {result}")
                pbar.update(1)
            results.extend(batch_results)
        pbar.close()
        return gh.api_calls
    finally:
        await gh.close()


def run_gh_enrich(config: Config) -> int:
    """Run the GitHub API enrichment phase. Returns total API calls made."""
    if not os.path.exists(config.target_prs_path):
        logger.error(f"No target PRs found at {config.target_prs_path}. Run bq-extract first.")
        return 0

    with open(config.target_prs_path) as f:
        prs = [TargetPR.from_dict(d) for d in json.load(f)]

    if config.max_prs is not None:
        prs = prs[: config.max_prs]

    logger.info(f"Enriching {len(prs)} PRs via GitHub API")
    api_calls = asyncio.run(_run_enrichment(config, prs))
    logger.info(f"GitHub enrichment complete. Total API calls: {api_calls}")
    return api_calls

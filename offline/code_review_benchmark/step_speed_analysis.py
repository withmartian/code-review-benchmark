#!/usr/bin/env python3
"""Compute code review tool latency from GitHub PR timelines.

For each benchmark repo (PR #1 in the forked repo), determines when the review
was triggered and when the last review comment arrived, then computes elapsed
time. Each tool has a different triggering mechanism — see the strategy map at
the bottom of this module for details.

Usage:
    uv run python -m code_review_benchmark.step_speed_analysis \\
        --org code-review-benchmark --output results/speed_analysis.json

    # Single tool
    uv run python -m code_review_benchmark.step_speed_analysis --tool coderabbit
"""

import argparse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from tqdm import tqdm

# ── Tool routing ───────────────────────────────────────────────────────────────

# Review triggered by a user comment (from trigger_review.sh).
# Start = last non-bot comment before the bot's last review comment.
# End   = max(created_at, updated_at) across all bot comments.
TRIGGER_COMMENT_TOOLS = frozenset(
    {
        "augment",
        "baz",
        "bugbot",
        "codeant",
        "coderabbit",
        "cubic-v2",
        "entelligence",
        "gemini",
        "greptile-v4",
        "kodus-v2",
        "mesa",
        "propel",
        "qodo-v2",
        "qodo-extended",
        "qodo-extended-v2",
        "sourcery",
        "codeant-v2",
    }
)

# Tools we have intentionally deprecated or duplicated by a newer slug.
IGNORE_TOOLS = frozenset(
    {
        "linearb",
        "vercel",
        "sentry",
        "bito",
        "cubic",   # superseded by cubic-v2
        "kodus",   # superseded by kodus-v2
        "greptile", # superseded by greptile-v4
        "qodo",    # superseded by qodo-v2
        "qodo-v2-2",
        "qodo-v22",
        "inspect",
    }
)

# Tool slugs whose name starts with this prefix are ignored.
_IGNORE_PREFIXES = ("mra-",)

# ── Data types ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Comment:
    login: str
    is_bot: bool
    created_at: datetime
    # updated_at when the comment was edited; equals created_at if never edited
    latest_at: datetime


@dataclass
class PRData:
    repo: str
    pr_url: str
    pr_author: str
    pr_created_at: datetime | None
    issue_comments: list[dict]
    reviews: list[dict]           # top-level PR review bodies
    review_comments: list[dict]   # inline PR review comments
    timeline_events: list[dict]
    # Each entry has {createdAt, editor: {login}} — from GraphQL userContentEdits
    body_edits: list[dict]


@dataclass
class TimingResult:
    repo: str
    pr_url: str
    start: str | None   # ISO-8601 timestamp
    end: str | None     # ISO-8601 timestamp
    duration_seconds: float | None
    notes: str = ""


# ── GitHub helpers ─────────────────────────────────────────────────────────────

# Conservative default — the GitHub REST API allows 5 000 requests/hour for
# authenticated users. With 5 concurrent workers each making ~5 calls per repo
# we stay well within that budget even for large runs.
_MAX_WORKERS = 5
_MAX_RETRIES = 4
_RETRY_BASE_SLEEP = 2.0  # seconds; doubles on each attempt


def _run_gh(args: list[str]) -> subprocess.CompletedProcess:
    """Run a gh command with exponential-backoff retry on rate-limit (403/429) errors."""
    for attempt in range(_MAX_RETRIES):
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode == 0:
            return result
        # Retry on rate limit; give up immediately on other errors
        is_rate_limit = (
            "rate limit" in result.stderr.lower()
            or '"status":"403"' in result.stdout
            or '"status":"429"' in result.stdout
        )
        if not is_rate_limit or attempt == _MAX_RETRIES - 1:
            raise subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr)
        sleep = _RETRY_BASE_SLEEP * (2**attempt)
        time.sleep(sleep)
    raise RuntimeError("unreachable")


def _gh_paginated(endpoint: str, extra_headers: list[str] | None = None) -> list:
    """Call `gh api --paginate` and return all items as a flat list.

    Uses `--jq '.[]'` so each item is emitted as one JSON line, which avoids
    having to stitch together page-boundary JSON blobs manually.
    """
    args = ["gh", "api", "--paginate", "--jq", ".[]", endpoint]
    for header in extra_headers or []:
        args += ["--header", header]
    result = _run_gh(args)
    if not result.stdout.strip():
        return []
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def _gh_single(endpoint: str) -> dict:
    args = ["gh", "api", endpoint]
    result = _run_gh(args)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def _gh_graphql(query: str) -> dict:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    result = _run_gh(args)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _is_bot(user: dict) -> bool:
    return user.get("type") == "Bot" or user.get("login", "").endswith("[bot]")


def _to_comment(user: dict, created_at: str | None, updated_at: str | None = None) -> Comment | None:
    created = _parse_dt(created_at)
    if created is None:
        return None
    updated = _parse_dt(updated_at)
    latest = max(created, updated) if updated else created
    return Comment(
        login=user.get("login", ""),
        is_bot=_is_bot(user),
        created_at=created,
        latest_at=latest,
    )


def _all_comments(pr_data: PRData) -> list[Comment]:
    """Flatten all comment types (issue, review body, inline) into one list."""
    comments: list[Comment] = []

    for c in pr_data.issue_comments:
        item = _to_comment(c.get("user", {}), c.get("created_at"), c.get("updated_at"))
        if item:
            comments.append(item)

    for r in pr_data.reviews:
        # Only include reviews that actually have a body — empty "APPROVED" events etc. are noise
        if r.get("body") and r.get("body", "").strip():
            item = _to_comment(r.get("user", {}), r.get("submitted_at"))
            if item:
                comments.append(item)

    for c in pr_data.review_comments:
        item = _to_comment(c.get("user", {}), c.get("created_at"), c.get("updated_at"))
        if item:
            comments.append(item)

    return comments


def fetch_pr_data(org: str, repo: str, tool: str = "") -> PRData:
    """Fetch all data needed for timing analysis for PR #1 of a forked repo."""
    pr_info = _gh_single(f"/repos/{org}/{repo}/pulls/1")
    issue_comments = _gh_paginated(f"/repos/{org}/{repo}/issues/1/comments")
    reviews = _gh_paginated(f"/repos/{org}/{repo}/pulls/1/reviews")
    review_comments = _gh_paginated(f"/repos/{org}/{repo}/pulls/1/comments")
    # The timeline API requires this preview header to expose all event types.
    timeline_events = _gh_paginated(
        f"/repos/{org}/{repo}/issues/1/timeline",
        extra_headers=["Accept: application/vnd.github.mockingbird-preview+json"],
    )
    # userContentEdits is only needed for Devin — the GraphQL call is expensive
    # so we skip it for all other tools.
    body_edits = _fetch_body_edits(org, repo) if tool == "devin" else []
    return PRData(
        repo=repo,
        pr_url=pr_info.get("html_url", ""),
        pr_author=pr_info.get("user", {}).get("login", ""),
        pr_created_at=_parse_dt(pr_info.get("created_at")),
        issue_comments=issue_comments,
        reviews=reviews,
        review_comments=review_comments,
        timeline_events=timeline_events,
        body_edits=body_edits,
    )


def _fetch_body_edits(org: str, repo: str) -> list[dict]:
    """Return PR body edit history via GraphQL userContentEdits."""
    query = f"""
    {{
      repository(owner: "{org}", name: "{repo}") {{
        pullRequest(number: 1) {{
          userContentEdits(first: 20) {{
            nodes {{ createdAt editor {{ login }} }}
          }}
        }}
      }}
    }}
    """
    try:
        data = _gh_graphql(query)
        nodes = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("userContentEdits", {})
            .get("nodes", [])
        )
        return nodes or []
    except subprocess.CalledProcessError:
        return []


# ── Timing strategies ──────────────────────────────────────────────────────────

# Each strategy returns (start, end, notes) where notes is a non-empty string
# if something was unexpected.
TimingStrategy = Callable[[PRData], tuple[datetime | None, datetime | None, str]]


def _trigger_comment_timing(
    pr_data: PRData, fallback_to_pr_creation: bool = False,
) -> tuple[datetime | None, datetime | None, str]:
    """Trigger-comment strategy: used by most native code-review bots.

    Start: the last non-bot comment that precedes the bot's review (i.e. the
           last trigger attempt — handles cases where the first trigger failed
           and a human retried). Non-bot comments that arrive *after* the bot
           finished are ignored.
    End:   max(created_at, updated_at) across all bot comments (some bots like
           qodo-extended edit their first comment to add content).

    Args:
        fallback_to_pr_creation: If True and no trigger comment is found,
            fall back to PR creation time as start (for tools like entelligence
            that auto-trigger on PR open without needing a human comment).
    """
    comments = _all_comments(pr_data)
    bot_comments = [c for c in comments if c.is_bot]
    if not bot_comments:
        return None, None, "no bot comments found"

    end_time = max(c.latest_at for c in bot_comments)

    # Last non-bot comment strictly before end_time
    triggers = [c for c in comments if not c.is_bot and c.created_at < end_time]
    if not triggers:
        if fallback_to_pr_creation and pr_data.pr_created_at:
            return pr_data.pr_created_at, end_time, "no trigger comment; using PR creation time"
        return None, end_time, "no non-bot trigger comment found before bot review"

    start_time = max(c.created_at for c in triggers)
    return start_time, end_time, ""


def _entelligence_timing(pr_data: PRData) -> tuple[datetime | None, datetime | None, str]:
    """Entelligence auto-triggers on PR creation — fall back to PR creation time."""
    return _trigger_comment_timing(pr_data, fallback_to_pr_creation=True)


def _claude_timing(pr_data: PRData) -> tuple[datetime | None, datetime | None, str]:
    """Claude (via /claude slash command on the PR).

    Start: first comment by the PR author (the /claude invocation).
    End:   latest comment by anyone (Claude's final response).
    Excludes the PR description body itself, which is not an issue comment.
    """
    comments = _all_comments(pr_data)
    if not comments:
        return None, None, "no comments"

    author_comments = [c for c in comments if c.login == pr_data.pr_author]
    if not author_comments:
        return None, None, f"no comments by PR author ({pr_data.pr_author})"

    start_time = min(c.created_at for c in author_comments)
    end_time = max(c.latest_at for c in comments)
    return start_time, end_time, ""


def _claude_code_timing(pr_data: PRData) -> tuple[datetime | None, datetime | None, str]:
    """Claude Code (triggered when PR is marked ready for review).

    Start: the "ready_for_review" timeline event, or PR creation time if absent.
    End:   the last comment by claude[bot].
    """
    start_time: datetime | None = None
    for event in pr_data.timeline_events:
        if event.get("event") == "ready_for_review":
            # Take the last occurrence in case there were multiple
            t = _parse_dt(event.get("created_at"))
            if t and (start_time is None or t > start_time):
                start_time = t

    notes = ""
    if start_time is None:
        start_time = pr_data.pr_created_at
        notes = "no ready_for_review event; using PR creation time"

    claude_comments = [c for c in _all_comments(pr_data) if c.is_bot and "claude" in c.login.lower()]
    if not claude_comments:
        return start_time, None, f"{notes}; no claude[bot] comments found".lstrip("; ")

    end_time = max(c.latest_at for c in claude_comments)
    return start_time, end_time, notes


def _copilot_timing(pr_data: PRData) -> tuple[datetime | None, datetime | None, str]:
    """GitHub Copilot code review.

    Start: the `review_requested` timeline event where the requested reviewer
           is Copilot (login contains "copilot"). This corresponds to the
           "@user requested a review from Copilot" event visible on the PR.
    End:   last comment by any copilot bot.
    """
    start_time: datetime | None = None
    for event in pr_data.timeline_events:
        if event.get("event") != "review_requested":
            continue
        reviewer = event.get("requested_reviewer") or {}
        if "copilot" in reviewer.get("login", "").lower():
            t = _parse_dt(event.get("created_at"))
            if t and (start_time is None or t > start_time):
                start_time = t

    if start_time is None:
        return None, None, "no 'review_requested' event for Copilot found in timeline"

    copilot_comments = [c for c in _all_comments(pr_data) if c.is_bot and "copilot" in c.login.lower()]
    if not copilot_comments:
        return start_time, None, "no copilot[bot] comments found"

    end_time = max(c.latest_at for c in copilot_comments)
    return start_time, end_time, ""


def _kg_timing(pr_data: PRData) -> tuple[datetime | None, datetime | None, str]:
    """Kilocode (kg) — review triggered by reopening the PR.

    Start: the last "reopened" timeline event (the one that successfully
           triggered the review).
    End:   last comment by any bot whose username contains "kilo".
    """
    start_time: datetime | None = None
    for event in pr_data.timeline_events:
        if event.get("event") == "reopened":
            t = _parse_dt(event.get("created_at"))
            if t and (start_time is None or t > start_time):
                start_time = t

    if start_time is None:
        return None, None, "no 'reopened' timeline event found"

    kg_comments = [c for c in _all_comments(pr_data) if c.is_bot and "kilo" in c.login.lower()]
    if not kg_comments:
        return start_time, None, "no kilo[bot] comments found"

    end_time = max(c.latest_at for c in kg_comments)
    return start_time, end_time, ""


def _devin_timing(pr_data: PRData) -> tuple[datetime | None, datetime | None, str]:
    """Devin code review.

    Confirmed with Devin team:
    - Start: Devin edits the PR description to add the "Open in Devin Review"
             button. The exact timestamp comes from the GraphQL userContentEdits
             API — we take the earliest edit attributed to the Devin bot.
    - End:   the last review comment posted by the Devin bot.
    """
    # Start: earliest body edit by the Devin bot (via GraphQL userContentEdits)
    devin_edits = [
        e for e in pr_data.body_edits
        if "devin" in (e.get("editor") or {}).get("login", "").lower()
    ]
    start_time: datetime | None = None
    notes = ""
    if devin_edits:
        start_time = min((_parse_dt(e["createdAt"]) for e in devin_edits), default=None)
    if start_time is None:
        notes = "no Devin body edit found; falling back to PR creation time"
        start_time = pr_data.pr_created_at

    # End: latest comment by the Devin bot
    all_comments = _all_comments(pr_data)
    bot_comments = [c for c in all_comments if c.is_bot and "devin" in c.login.lower()]
    if not bot_comments:
        bot_comments = [c for c in all_comments if c.is_bot]
    if not bot_comments:
        return start_time, None, f"{notes}; no bot comments found".lstrip("; ")

    end_time = max(c.latest_at for c in bot_comments)
    return start_time, end_time, notes


# Map tool slug → timing strategy function
_STRATEGY: dict[str, TimingStrategy] = {
    **dict.fromkeys(TRIGGER_COMMENT_TOOLS, _trigger_comment_timing),
    # Override entelligence — it auto-triggers on PR open, no human comment needed
    "entelligence": _entelligence_timing,
    "devin": _devin_timing,
    "claude": _claude_timing,
    "claude-code": _claude_code_timing,
    "copilot": _copilot_timing,
    "kg": _kg_timing,
}

SUPPORTED_TOOLS = frozenset(_STRATEGY.keys())


# ── Repo helpers ───────────────────────────────────────────────────────────────


def _parse_repo_name(name: str) -> dict | None:
    """Extract components from a benchmark repo name.

    Pattern: {config_prefix}__{original_repo}__{tool}__PR{number}__{date}
    """
    match = re.match(r"^(.+?)__(.+?)__(.+?)__PR(\d+)__(\d+)$", name)
    if not match:
        return None
    return {
        "config_prefix": match.group(1),
        "original_repo": match.group(2),
        "tool": match.group(3),
        "pr_number": int(match.group(4)),
        "date": match.group(5),
    }


def _should_skip(tool: str) -> bool:
    return tool in IGNORE_TOOLS or any(tool.startswith(p) for p in _IGNORE_PREFIXES)


def _process_repo(org: str, repo_name: str, tool: str) -> TimingResult:
    try:
        pr_data = fetch_pr_data(org, repo_name, tool)
    except Exception as exc:
        return TimingResult(
            repo=repo_name, pr_url="", start=None, end=None,
            duration_seconds=None, notes=f"fetch error: {exc}",
        )

    strategy = _STRATEGY.get(tool)
    if strategy is None:
        return TimingResult(
            repo=repo_name,
            pr_url=pr_data.pr_url,
            start=None,
            end=None,
            duration_seconds=None,
            notes=f"no strategy for tool '{tool}'",
        )

    start, end, notes = strategy(pr_data)

    duration: float | None = None
    if start and end and end >= start:
        duration = (end - start).total_seconds()
    elif start and end and end < start:
        notes = f"{notes}; end ({end.isoformat()}) is before start ({start.isoformat()})".lstrip("; ")

    return TimingResult(
        repo=repo_name,
        pr_url=pr_data.pr_url,
        start=start.isoformat() if start else None,
        end=end.isoformat() if end else None,
        duration_seconds=duration,
        notes=notes,
    )


# ── Aggregation ────────────────────────────────────────────────────────────────

@dataclass
class ToolStats:
    count: int
    mean_seconds: float
    median_seconds: float
    p25_seconds: float
    p75_seconds: float
    min_seconds: float
    max_seconds: float


def _compute_stats(durations: list[float]) -> ToolStats | None:
    if not durations:
        return None
    s = sorted(durations)
    n = len(s)
    return ToolStats(
        count=n,
        mean_seconds=sum(s) / n,
        median_seconds=s[n // 2],
        p25_seconds=s[n // 4],
        p75_seconds=s[(3 * n) // 4],
        min_seconds=s[0],
        max_seconds=s[-1],
    )


# ── Main ───────────────────────────────────────────────────────────────────────


def _load_dotenv(filepath: str = ".env") -> None:
    env_path = Path(filepath)
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main() -> None:
    _load_dotenv()

    parser = argparse.ArgumentParser(description="Compute code review tool latency from GitHub PR timelines")
    parser.add_argument("--org", default="code-review-benchmark", help="GitHub organization")
    parser.add_argument("--output", default="results/speed_analysis.json", help="Output JSON file")
    parser.add_argument("--tool", help="Only process a specific tool slug")
    parser.add_argument("--workers", type=int, default=_MAX_WORKERS, help="Concurrent API workers")
    parser.add_argument("--force", action="store_true", help="Re-fetch all repos, ignoring existing results")
    args = parser.parse_args()

    print(f"Listing repos in {args.org}...")
    raw_repos = subprocess.run(
        ["gh", "repo", "list", args.org, "--limit", "5000", "--json", "name"],
        capture_output=True, text=True,
    )
    if raw_repos.returncode != 0:
        print(f"Error listing repos: {raw_repos.stderr}", file=sys.stderr)
        sys.exit(1)

    all_repos: list[dict] = json.loads(raw_repos.stdout)
    print(f"Found {len(all_repos)} repos total")

    # Load existing results for incremental mode
    output_path = Path(args.output)
    existing: dict = {}
    if not args.force and output_path.exists():
        with open(output_path) as f:
            existing = json.load(f)
        print(f"Loaded existing results from {args.output}")

    # Build set of repos that already have a successful result so we can skip them
    already_done: set[str] = set()
    if not args.force:
        for tool_data in existing.values():
            for pr in tool_data.get("per_pr", []):
                if pr.get("duration_seconds") is not None:
                    already_done.add(pr["repo"])

    # Group repos by tool slug, skipping already-successful ones
    tool_repos: dict[str, list[str]] = {}
    for entry in all_repos:
        repo_name = entry["name"]
        parsed = _parse_repo_name(repo_name)
        if not parsed:
            continue
        tool = parsed["tool"]
        if _should_skip(tool):
            continue
        if args.tool and tool != args.tool:
            continue
        if tool not in SUPPORTED_TOOLS:
            continue
        tool_repos.setdefault(tool, []).append(repo_name)

    skipped_count = sum(1 for repos in tool_repos.values() for r in repos if r in already_done)
    all_tasks = [
        (tool, repo)
        for tool, repos in tool_repos.items()
        for repo in repos
        if repo not in already_done
    ]
    print(f"Tools to process: {sorted(tool_repos)}")
    print(f"Skipping {skipped_count} already-successful repos; fetching {len(all_tasks)}")

    if not all_tasks:
        print("Nothing to process.")
        return

    # Fetch + compute timing concurrently
    results_by_tool: dict[str, list[TimingResult]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_task = {
            executor.submit(_process_repo, args.org, repo, tool): (tool, repo)
            for tool, repo in all_tasks
        }
        with tqdm(total=len(all_tasks), desc="Fetching PR timelines") as pbar:
            for future in as_completed(future_to_task):
                tool, repo = future_to_task[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = TimingResult(
                        repo=repo, pr_url="", start=None, end=None,
                        duration_seconds=None, notes=f"unexpected: {exc}",
                    )
                results_by_tool.setdefault(tool, []).append(result)
                pbar.update(1)

    # Merge new results with kept existing ones
    for tool, tool_data in existing.items():
        for pr in tool_data.get("per_pr", []):
            if pr["repo"] in already_done:
                result = TimingResult(
                    repo=pr["repo"],
                    pr_url=pr.get("pr_url", ""),
                    start=pr.get("start"),
                    end=pr.get("end"),
                    duration_seconds=pr.get("duration_seconds"),
                    notes=pr.get("notes", ""),
                )
                results_by_tool.setdefault(tool, []).append(result)

    # Build output
    output: dict = {}
    for tool, results in sorted(results_by_tool.items()):
        durations = [r.duration_seconds for r in results if r.duration_seconds is not None]
        failed = [r for r in results if r.duration_seconds is None]
        stats = _compute_stats(durations)
        output[tool] = {
            "per_pr": sorted(
                [
                    {
                        "repo": r.repo,
                        "pr_url": r.pr_url,
                        "start": r.start,
                        "end": r.end,
                        "duration_seconds": r.duration_seconds,
                        **({"notes": r.notes} if r.notes else {}),
                    }
                    for r in results
                ],
                key=lambda d: d["repo"],
            ),
            "stats": (
                {
                    "count": stats.count,
                    "mean_seconds": stats.mean_seconds,
                    "median_seconds": stats.median_seconds,
                    "p25_seconds": stats.p25_seconds,
                    "p75_seconds": stats.p75_seconds,
                    "min_seconds": stats.min_seconds,
                    "max_seconds": stats.max_seconds,
                }
                if stats
                else None
            ),
            "failed_count": len(failed),
        }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nSaved to {args.output}")
    print("\nSummary:")
    header = f"  {'tool':<30}  {'n':>4}  {'median':>10}  {'mean':>10}  {'failed':>6}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for tool, data in output.items():
        s = data["stats"]
        if s:
            med = s["median_seconds"] / 60
            avg = s["mean_seconds"] / 60
            print(f"  {tool:<30}  {s['count']:>4}  {med:>9.1f}m  {avg:>9.1f}m  {data['failed_count']:>6}")
        else:
            print(f"  {tool:<30}  {'—':>4}  {'—':>10}  {'—':>10}  {data['failed_count']:>6}")


if __name__ == "__main__":
    main()

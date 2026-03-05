"""Pipeline stage: Assemble enriched PR data into unified PRRecord (DB-backed).

Contains the pure assembly functions (originally from old/assemble.py) and the
dataclasses they operate on (originally from old/models.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
import json
import logging
from typing import Any

from db.connection import DBAdapter
from db.repository import PRRepository

logger = logging.getLogger(__name__)


# -- Data models ---------------------------------------------------------------


@dataclass
class TimelineEvent:
    timestamp: str  # ISO8601
    event_type: str
    actor: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "actor": self.actor,
            "data": self.data,
        }


@dataclass
class ReviewThread:
    thread_id: str
    path: str | None
    is_resolved: bool
    resolved_by: str | None
    comments: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "path": self.path,
            "is_resolved": self.is_resolved,
            "resolved_by": self.resolved_by,
            "comments": self.comments,
        }


@dataclass
class PRStats:
    total_events: int = 0
    total_commits: int = 0
    total_review_comments_by_target: int = 0
    total_review_threads: int = 0
    resolved_threads: int = 0
    target_user_comments_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_events": self.total_events,
            "total_commits": self.total_commits,
            "total_review_comments_by_target": self.total_review_comments_by_target,
            "total_review_threads": self.total_review_threads,
            "resolved_threads": self.resolved_threads,
            "target_user_comments_count": self.target_user_comments_count,
        }


# -- Pure assembly functions ---------------------------------------------------


def _parse_timestamp(ts: str | None) -> datetime:
    """Parse an ISO8601 timestamp string to a datetime for sorting."""
    if not ts:
        return datetime.min.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.min.replace(tzinfo=UTC)


def _extract_pr_metadata(bq_events: list[dict]) -> dict:
    """Extract PR title, author, created_at, merged status from BQ events."""
    meta: dict = {
        "pr_title": "",
        "pr_author": None,
        "pr_created_at": None,
        "pr_merged": None,
    }
    for event in bq_events:
        payload = event.get("payload", {})
        if event["type"] == "PullRequestEvent":
            pr_obj = payload.get("pull_request", {})
            if not meta["pr_title"]:
                meta["pr_title"] = pr_obj.get("title", "")
            if meta["pr_author"] is None:
                meta["pr_author"] = (pr_obj.get("user") or {}).get("login")
            if meta["pr_created_at"] is None:
                meta["pr_created_at"] = pr_obj.get("created_at")
            if meta["pr_merged"] is None:
                if payload.get("action") == "closed" and pr_obj.get("merged"):
                    meta["pr_merged"] = True
                elif payload.get("action") == "closed":
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


def _build_timeline_events(
    bq_events: list[dict],
    commits: list[dict] | None,
    commit_details: list[dict] | None,
    reviews: list[dict] | None,
) -> list[TimelineEvent]:
    """Build a unified sorted timeline from BQ events, commits, and API reviews."""
    timeline: list[TimelineEvent] = []

    # Index commit details by SHA
    details_by_sha: dict[str, list[dict]] = {}
    if commit_details:
        for cd in commit_details:
            details_by_sha[cd["sha"]] = cd.get("files", [])

    # Track review IDs seen from BQ to avoid duplicates with API reviews
    seen_review_ids: set[int] = set()

    # Process BQ events
    for event in bq_events:
        payload = event.get("payload", {})
        event_type = event["type"]
        ts = event["created_at"]
        actor = event["actor"]

        if event_type == "PullRequestEvent":
            action = payload.get("action", "")
            pr_obj = payload.get("pull_request", {})
            if action == "opened":
                etype = "pr_opened"
            elif action == "closed":
                etype = "pr_merged" if pr_obj.get("merged") else "pr_closed"
            elif action == "reopened":
                etype = "pr_reopened"
            else:
                etype = f"pr_{action}"
            timeline.append(
                TimelineEvent(
                    timestamp=ts,
                    event_type=etype,
                    actor=actor,
                    data={"action": action, "title": pr_obj.get("title")},
                )
            )

        elif event_type == "PullRequestReviewEvent":
            review = payload.get("review", {})
            review_id = review.get("id")
            if review_id:
                seen_review_ids.add(review_id)
            timeline.append(
                TimelineEvent(
                    timestamp=ts,
                    event_type="review",
                    actor=actor,
                    data={
                        "state": review.get("state", ""),
                        "body": review.get("body"),
                        "review_id": review_id,
                    },
                )
            )

        elif event_type == "PullRequestReviewCommentEvent":
            comment = payload.get("comment", {})
            timeline.append(
                TimelineEvent(
                    timestamp=ts,
                    event_type="review_comment",
                    actor=actor,
                    data={
                        "comment_id": comment.get("id"),
                        "body": comment.get("body"),
                        "path": comment.get("path"),
                        "line": comment.get("original_line") or comment.get("line"),
                        "diff_hunk": comment.get("diff_hunk"),
                        "in_reply_to_id": comment.get("in_reply_to_id"),
                        "original_commit_id": comment.get("original_commit_id"),
                    },
                )
            )

        elif event_type == "IssueCommentEvent":
            comment = payload.get("comment", {})
            timeline.append(
                TimelineEvent(
                    timestamp=ts,
                    event_type="issue_comment",
                    actor=actor,
                    data={
                        "comment_id": comment.get("id"),
                        "body": comment.get("body"),
                    },
                )
            )

    # Add API reviews not already seen in BQ events
    if reviews:
        for r in reviews:
            review_id = r.get("id")
            if review_id and review_id in seen_review_ids:
                continue
            ts = r.get("submitted_at")
            if not ts:
                continue
            timeline.append(
                TimelineEvent(
                    timestamp=ts,
                    event_type="review",
                    actor=r.get("author") or "unknown",
                    data={
                        "state": r.get("state", ""),
                        "body": r.get("body"),
                        "review_id": review_id,
                        "commit_id": r.get("commit_id"),
                        "author_association": r.get("author_association"),
                        "source": "api",
                    },
                )
            )

    # Add commits to timeline
    if commits:
        for i, c in enumerate(commits):
            files_changed = []
            files_detail = []
            sha = c["sha"]
            if sha in details_by_sha:
                for fd in details_by_sha[sha]:
                    files_changed.append(fd["filename"])
                    files_detail.append(
                        {
                            "filename": fd["filename"],
                            "status": fd.get("status", "unknown"),
                            "additions": fd.get("additions", 0),
                            "deletions": fd.get("deletions", 0),
                        }
                    )

            timeline.append(
                TimelineEvent(
                    timestamp=c["date"],
                    event_type="commit",
                    actor=c.get("author") or "unknown",
                    data={
                        "sha": sha,
                        "message": c["message"],
                        "files_changed": files_changed,
                        "files_detail": files_detail,
                        "order_index": i,
                    },
                )
            )

    timeline.sort(key=lambda e: (_parse_timestamp(e.timestamp), e.data.get("order_index", 0)))
    return timeline


def _build_review_threads(raw_threads: list[dict] | None) -> list[ReviewThread]:
    """Convert raw thread data to ReviewThread objects with full comment content."""
    if not raw_threads:
        return []

    threads: list[ReviewThread] = []
    for t in raw_threads:
        path = None
        comments: list[dict] = []
        for c in t.get("comments", []):
            if path is None:
                path = c.get("path")
            comments.append(
                {
                    "comment_id": c.get("id"),
                    "body": c.get("body", ""),
                    "author": c.get("author"),
                    "created_at": c.get("created_at"),
                    "path": c.get("path"),
                    "line": c.get("line"),
                    "original_line": c.get("original_line"),
                    "diff_hunk": c.get("diff_hunk"),
                    "reactions": c.get("reactions", {}),
                }
            )
        threads.append(
            ReviewThread(
                thread_id=t["id"],
                path=path,
                is_resolved=t.get("is_resolved", False),
                resolved_by=t.get("resolved_by"),
                comments=comments,
            )
        )
    return threads


def _enrich_timeline_with_threads(timeline: list[TimelineEvent], raw_threads: list[dict] | None) -> None:
    """Enrich existing review_comment events and add missing thread comments to the timeline."""
    if not raw_threads:
        return

    # Build lookups from thread data
    comment_to_thread: dict[int, dict] = {}
    comment_api_data: dict[int, dict] = {}
    for t in raw_threads:
        for c in t.get("comments", []):
            cid = c.get("id")
            if cid:
                comment_to_thread[cid] = {
                    "is_resolved": t.get("is_resolved", False),
                    "resolved_by": t.get("resolved_by"),
                    "thread_id": t["id"],
                }
                comment_api_data[cid] = c

    # Enrich existing timeline events
    seen_comment_ids: set[int] = set()
    for event in timeline:
        if event.event_type == "review_comment":
            cid = event.data.get("comment_id")
            if cid:
                seen_comment_ids.add(cid)
            if cid and cid in comment_to_thread:
                event.data["is_resolved"] = comment_to_thread[cid]["is_resolved"]
                event.data["resolved_by"] = comment_to_thread[cid]["resolved_by"]
                event.data["thread_id"] = comment_to_thread[cid]["thread_id"]
            if cid and cid in comment_api_data:
                api = comment_api_data[cid]
                if api.get("body"):
                    event.data["body"] = api["body"]
                event.data["reactions"] = api.get("reactions", {})

    # Add thread comments missing from the timeline
    for t in raw_threads:
        thread_id = t["id"]
        is_resolved = t.get("is_resolved", False)
        resolved_by = t.get("resolved_by")
        for c in t.get("comments", []):
            cid = c.get("id")
            if not cid or cid in seen_comment_ids:
                continue
            timeline.append(
                TimelineEvent(
                    timestamp=c.get("created_at") or "",
                    event_type="review_comment",
                    actor=c.get("author") or "unknown",
                    data={
                        "comment_id": cid,
                        "body": c.get("body", ""),
                        "path": c.get("path"),
                        "line": c.get("original_line") or c.get("line"),
                        "diff_hunk": c.get("diff_hunk"),
                        "reactions": c.get("reactions", {}),
                        "thread_id": thread_id,
                        "is_resolved": is_resolved,
                        "resolved_by": resolved_by,
                        "source": "api",
                    },
                )
            )


def _compute_stats(target_user: str, timeline: list[TimelineEvent], threads: list[ReviewThread]) -> PRStats:
    """Compute summary stats for a PR."""
    stats = PRStats()
    stats.total_events = len(timeline)
    stats.total_commits = sum(1 for e in timeline if e.event_type == "commit")
    stats.total_review_comments_by_target = sum(
        1 for e in timeline if e.event_type == "review_comment" and e.actor == target_user
    )
    stats.total_review_threads = len(threads)
    stats.resolved_threads = sum(1 for t in threads if t.is_resolved)
    stats.target_user_comments_count = sum(
        1 for e in timeline if e.actor == target_user and e.event_type in ("review_comment", "issue_comment", "review")
    )
    return stats


def _determine_roles(target_user: str, timeline: list[TimelineEvent], pr_author: str | None) -> list[str]:
    """Determine what roles the target user played in this PR."""
    roles: set[str] = set()
    if pr_author == target_user:
        roles.add("author")
    for e in timeline:
        if e.actor != target_user:
            continue
        if e.event_type in ("review", "review_comment"):
            roles.add("reviewer")
        if e.event_type == "issue_comment":
            roles.add("commenter")
    return sorted(roles)


# -- JSON helpers --------------------------------------------------------------


def _json_load(val: str | list | dict | None) -> list | dict | None:
    """Parse a JSONB column value — may be a string (SQLite) or already parsed (Postgres)."""
    if val is None:
        return None
    if isinstance(val, str):
        return json.loads(val)
    return val


# -- DB-backed assembly --------------------------------------------------------


def assemble_pr_from_row(pr_row: dict, chatbot_username: str) -> dict | None:
    """Assemble a PRRecord dict from a database row.

    Returns the assembled record as a dict, or None if required data is missing.
    """
    bq_events = _json_load(pr_row.get("bq_events"))
    commits = _json_load(pr_row.get("commits"))
    reviews = _json_load(pr_row.get("reviews"))
    raw_threads = _json_load(pr_row.get("review_threads"))
    commit_details = _json_load(pr_row.get("commit_details"))

    if bq_events is None:
        logger.warning(f"No BQ events for PR {pr_row['repo_name']}#{pr_row['pr_number']} — skipping assembly")
        return None

    meta = _extract_pr_metadata(bq_events)
    timeline = _build_timeline_events(bq_events, commits, commit_details, reviews)
    threads = _build_review_threads(raw_threads)
    _enrich_timeline_with_threads(timeline, raw_threads)
    timeline.sort(key=lambda e: (_parse_timestamp(e.timestamp), e.data.get("order_index", 0)))
    stats = _compute_stats(chatbot_username, timeline, threads)
    roles = _determine_roles(chatbot_username, timeline, meta["pr_author"])

    return {
        "pr_url": pr_row["pr_url"],
        "repo_name": pr_row["repo_name"],
        "pr_number": pr_row["pr_number"],
        "pr_title": meta["pr_title"],
        "pr_author": meta["pr_author"],
        "pr_created_at": meta["pr_created_at"],
        "pr_merged": meta["pr_merged"],
        "target_user_roles": roles,
        "events": [e.to_dict() for e in timeline],
        "review_threads": [t.to_dict() for t in threads],
        "stats": stats.to_dict(),
    }


async def assemble_pr(
    repo: PRRepository,
    pr_row: dict,
    chatbot_username: str,
) -> bool:
    """Assemble a single PR and save to DB. Returns True if successful."""
    record = assemble_pr_from_row(pr_row, chatbot_username)
    if record is None:
        return False

    await repo.mark_assembled(pr_row["id"], record)

    # Also update metadata from BQ events
    await repo.update_metadata(
        pr_row["id"],
        pr_title=record["pr_title"],
        pr_author=record["pr_author"],
        pr_created_at=record["pr_created_at"],
        pr_merged=record["pr_merged"],
    )

    logger.debug(f"Assembled {pr_row['repo_name']}#{pr_row['pr_number']}")
    return True


async def assemble_enriched_prs(
    db: DBAdapter,
    chatbot_id: int,
    chatbot_username: str,
) -> int:
    """Assemble all enriched PRs for a chatbot. Returns count of assembled PRs."""
    repo = PRRepository(db)

    # Get all enriched PRs that haven't been assembled yet
    rows = await db.fetchall(
        *db._translate_params(
            "SELECT * FROM prs WHERE chatbot_id = $1 AND status = 'enriched' ORDER BY discovered_at",
            (chatbot_id,),
        )
    )

    assembled = 0
    for row in rows:
        try:
            if await assemble_pr(repo, row, chatbot_username):
                assembled += 1
        except Exception as e:
            logger.error(f"Error assembling {row['repo_name']}#{row['pr_number']}: {e}")
            await repo.mark_error(row["id"], f"Assembly error: {e}")

    logger.info(f"Assembled {assembled}/{len(rows)} enriched PRs for {chatbot_username}")
    return assembled

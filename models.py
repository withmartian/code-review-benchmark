"""Data models for the PR review dataset builder."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TargetPR:
    repo_name: str  # "owner/repo"
    pr_number: int
    pr_url: str

    def owner(self) -> str:
        return self.repo_name.split("/")[0]

    def repo(self) -> str:
        return self.repo_name.split("/")[1]

    def pr_dir(self, user_dir: str) -> str:
        """Return PR directory path: {user_dir}/{owner}/{repo}/{pr_number}"""
        return os.path.join(user_dir, self.owner(), self.repo(), str(self.pr_number))

    def to_dict(self) -> dict[str, Any]:
        return {"repo_name": self.repo_name, "pr_number": self.pr_number, "pr_url": self.pr_url}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TargetPR:
        return cls(repo_name=d["repo_name"], pr_number=d["pr_number"], pr_url=d["pr_url"])


@dataclass
class CommitInfo:
    sha: str
    message: str
    date: str  # ISO8601
    author: str | None  # GitHub login, may be null
    files_changed: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "message": self.message,
            "date": self.date,
            "author": self.author,
            "files_changed": self.files_changed,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CommitInfo:
        return cls(
            sha=d["sha"],
            message=d["message"],
            date=d["date"],
            author=d.get("author"),
            files_changed=d.get("files_changed", []),
        )


@dataclass
class ReviewCommentData:
    comment_id: int
    body: str
    path: str | None
    line: int | None
    diff_hunk: str | None
    in_reply_to_id: int | None
    original_commit_id: str | None
    reactions: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "comment_id": self.comment_id,
            "body": self.body,
            "path": self.path,
            "line": self.line,
            "diff_hunk": self.diff_hunk,
            "in_reply_to_id": self.in_reply_to_id,
            "original_commit_id": self.original_commit_id,
            "reactions": self.reactions,
        }


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

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReviewThread:
        return cls(
            thread_id=d["thread_id"],
            path=d.get("path"),
            is_resolved=d["is_resolved"],
            resolved_by=d.get("resolved_by"),
            comments=d.get("comments", []),
        )


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


@dataclass
class PRRecord:
    pr_url: str
    repo_name: str
    pr_number: int
    pr_title: str
    pr_author: str | None
    pr_created_at: str | None
    pr_merged: bool | None
    target_user_roles: list[str] = field(default_factory=list)
    events: list[TimelineEvent] = field(default_factory=list)
    review_threads: list[ReviewThread] = field(default_factory=list)
    stats: PRStats = field(default_factory=PRStats)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr_url": self.pr_url,
            "repo_name": self.repo_name,
            "pr_number": self.pr_number,
            "pr_title": self.pr_title,
            "pr_author": self.pr_author,
            "pr_created_at": self.pr_created_at,
            "pr_merged": self.pr_merged,
            "target_user_roles": self.target_user_roles,
            "events": [e.to_dict() for e in self.events],
            "review_threads": [t.to_dict() for t in self.review_threads],
            "stats": self.stats.to_dict(),
        }

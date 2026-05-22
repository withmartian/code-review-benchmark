"""Tests for pr_author extraction from BQ events, including IssueCommentEvent fallback.

Tests both discover.py and assemble.py extraction functions, plus the backfill helpers.
"""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

from pipeline.assemble import _extract_pr_metadata as assemble_extract
from pipeline.backfill_pr_author import _extract_author_from_bq_events
from pipeline.backfill_pr_author import _extract_author_from_commits
from pipeline.backfill_pr_author import _recompute_target_user_roles
from pipeline.discover import _extract_pr_metadata as discover_extract


def _make_pr_event(author: str = "alice", title: str = "Fix bug", action: str = "opened") -> dict:
    """Build a minimal PullRequestEvent BQ event."""
    return {
        "type": "PullRequestEvent",
        "actor": author,
        "created_at": "2026-01-15T10:00:00Z",
        "payload": {
            "action": action,
            "pull_request": {
                "title": title,
                "user": {"login": author},
                "created_at": "2026-01-15T09:00:00Z",
            },
        },
    }


def _make_review_event(reviewer: str = "bot[bot]", author: str = "alice") -> dict:
    """Build a minimal PullRequestReviewEvent BQ event."""
    return {
        "type": "PullRequestReviewEvent",
        "actor": reviewer,
        "created_at": "2026-01-15T11:00:00Z",
        "payload": {
            "review": {"id": 1, "state": "COMMENTED", "body": "Looks good"},
            "pull_request": {
                "title": "Fix bug",
                "user": {"login": author},
                "created_at": "2026-01-15T09:00:00Z",
            },
        },
    }


def _make_issue_comment_event(
    commenter: str = "bot[bot]",
    pr_author: str = "alice",
    title: str = "Fix bug",
) -> dict:
    """Build a minimal IssueCommentEvent BQ event (the case that was previously unhandled)."""
    return {
        "type": "IssueCommentEvent",
        "actor": commenter,
        "created_at": "2026-01-15T12:00:00Z",
        "payload": {
            "action": "created",
            "issue": {
                "title": title,
                "user": {"login": pr_author},
                "created_at": "2026-01-15T09:00:00Z",
                "pull_request": {"html_url": "https://github.com/org/repo/pull/42"},
            },
            "comment": {"id": 100, "body": "I will fix this"},
        },
    }


class TestExtractPrMetadataDiscover:
    """Test _extract_pr_metadata from discover.py."""

    def test_pr_event_extracts_author(self) -> None:
        events = [_make_pr_event(author="alice")]
        meta = discover_extract(events)
        assert meta["pr_author"] == "alice"
        assert meta["pr_title"] == "Fix bug"

    def test_review_event_extracts_author(self) -> None:
        events = [_make_review_event(reviewer="coderabbitai[bot]", author="bob")]
        meta = discover_extract(events)
        assert meta["pr_author"] == "bob"

    def test_issue_comment_event_extracts_author(self) -> None:
        """The key fix: IssueCommentEvent should now yield pr_author."""
        events = [_make_issue_comment_event(commenter="bot[bot]", pr_author="charlie")]
        meta = discover_extract(events)
        assert meta["pr_author"] == "charlie"
        assert meta["pr_title"] == "Fix bug"

    def test_issue_comment_only_events(self) -> None:
        """PR discovered solely through IssueCommentEvent — previously returned None."""
        events = [
            _make_issue_comment_event(commenter="bot1[bot]", pr_author="dave"),
            _make_issue_comment_event(commenter="bot2[bot]", pr_author="dave"),
        ]
        meta = discover_extract(events)
        assert meta["pr_author"] == "dave"

    def test_pr_event_takes_precedence_over_issue_comment(self) -> None:
        """PullRequestEvent author should be preferred (it's processed first in typical order)."""
        events = [
            _make_pr_event(author="alice"),
            _make_issue_comment_event(commenter="bot[bot]", pr_author="alice"),
        ]
        meta = discover_extract(events)
        assert meta["pr_author"] == "alice"

    def test_mixed_events_first_author_wins(self) -> None:
        """When events are ordered, the first event with author info wins."""
        events = [
            _make_issue_comment_event(commenter="bot[bot]", pr_author="from_issue"),
            _make_pr_event(author="from_pr"),
        ]
        meta = discover_extract(events)
        # IssueCommentEvent is first, so its author wins
        assert meta["pr_author"] == "from_issue"

    def test_empty_events(self) -> None:
        meta = discover_extract([])
        assert meta["pr_author"] is None

    def test_missing_user_field(self) -> None:
        events = [{"type": "IssueCommentEvent", "actor": "bot", "created_at": "2026-01-15T10:00:00Z",
                    "payload": {"issue": {}}}]
        meta = discover_extract(events)
        assert meta["pr_author"] is None


class TestExtractPrMetadataAssemble:
    """Test _extract_pr_metadata from assemble.py (should behave identically)."""

    def test_issue_comment_event_extracts_author(self) -> None:
        events = [_make_issue_comment_event(commenter="bot[bot]", pr_author="eve")]
        meta = assemble_extract(events)
        assert meta["pr_author"] == "eve"

    def test_pr_event_extracts_author(self) -> None:
        events = [_make_pr_event(author="frank")]
        meta = assemble_extract(events)
        assert meta["pr_author"] == "frank"


class TestBackfillHelpers:
    """Test the backfill script helper functions."""

    def test_extract_author_from_bq_events_string(self) -> None:
        events = [_make_issue_comment_event(pr_author="grace")]
        raw = json.dumps(events)
        assert _extract_author_from_bq_events(raw) == "grace"

    def test_extract_author_from_bq_events_list(self) -> None:
        events = [_make_pr_event(author="heidi")]
        assert _extract_author_from_bq_events(events) == "heidi"

    def test_extract_author_from_bq_events_none(self) -> None:
        assert _extract_author_from_bq_events(None) is None

    def test_extract_author_from_commits(self) -> None:
        commits = [{"sha": "abc123", "author": "ivan", "message": "fix", "date": "2026-01-15T10:00:00Z"}]
        assert _extract_author_from_commits(commits) == "ivan"

    def test_extract_author_from_commits_string(self) -> None:
        commits = [{"sha": "abc123", "author": "judy", "message": "fix", "date": "2026-01-15T10:00:00Z"}]
        assert _extract_author_from_commits(json.dumps(commits)) == "judy"

    def test_extract_author_from_empty_commits(self) -> None:
        assert _extract_author_from_commits([]) is None
        assert _extract_author_from_commits(None) is None

    def test_recompute_roles_adds_author(self) -> None:
        """When pr_author matches chatbot_username, 'author' role should be added."""
        assembled = {
            "pr_author": None,
            "target_user_roles": ["reviewer"],
            "events": [
                {"timestamp": "2026-01-15T11:00:00Z", "event_type": "review", "actor": "devin-ai-integration[bot]",
                 "data": {"state": "COMMENTED", "body": "review"}},
            ],
        }
        result = _recompute_target_user_roles(
            json.dumps(assembled), "devin-ai-integration[bot]", "devin-ai-integration[bot]"
        )
        assert result is not None
        assert "author" in result["target_user_roles"]
        assert "reviewer" in result["target_user_roles"]
        assert result["pr_author"] == "devin-ai-integration[bot]"

    def test_recompute_roles_no_change(self) -> None:
        """When author doesn't match bot, roles stay the same but pr_author still updates."""
        assembled = {
            "pr_author": None,
            "target_user_roles": ["reviewer"],
            "events": [
                {"timestamp": "2026-01-15T11:00:00Z", "event_type": "review", "actor": "coderabbitai[bot]",
                 "data": {"state": "COMMENTED"}},
            ],
        }
        result = _recompute_target_user_roles(
            json.dumps(assembled), "alice", "coderabbitai[bot]"
        )
        # pr_author changed from None to "alice", so assembled gets updated
        assert result is not None
        assert result["pr_author"] == "alice"
        assert result["target_user_roles"] == ["reviewer"]

    def test_recompute_roles_none_assembled(self) -> None:
        assert _recompute_target_user_roles(None, "alice", "bot[bot]") is None


@given(author=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N", "P"))))
def test_issue_comment_roundtrip(author: str) -> None:
    """Property: any non-empty author string survives the IssueCommentEvent extraction roundtrip."""
    events = [_make_issue_comment_event(pr_author=author)]
    meta = discover_extract(events)
    assert meta["pr_author"] == author

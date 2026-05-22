"""Tests for discover.py pure functions: metadata extraction, date conversion, merged status."""

from __future__ import annotations

from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st
import pytest

from pipeline.discover import _date_to_suffix
from pipeline.discover import _extract_pr_metadata

# -- _date_to_suffix ----------------------------------------------------------


@pytest.mark.parametrize("date_str,expected", [
    ("2026-01-15", "260115"),
    ("2025-12-31", "251231"),
    ("2020-01-01", "200101"),
    ("2099-06-09", "990609"),
])
def test_date_to_suffix(date_str: str, expected: str) -> None:
    assert _date_to_suffix(date_str) == expected


@given(
    year=st.integers(min_value=2000, max_value=2099),
    month=st.integers(min_value=1, max_value=12),
    day=st.integers(min_value=1, max_value=28),
)
@settings(max_examples=100)
def test_date_to_suffix_always_6_chars(year: int, month: int, day: int) -> None:
    date_str = f"{year:04d}-{month:02d}-{day:02d}"
    result = _date_to_suffix(date_str)
    assert len(result) == 6
    assert result.isdigit()


# -- _extract_pr_metadata: merged status --------------------------------------

def _make_pr_event(
    author: str = "alice",
    title: str = "Fix bug",
    action: str = "opened",
    merged: bool = False,
) -> dict:
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
                "merged": merged,
            },
        },
    }


class TestMergedStatus:
    def test_merged_true_from_closed_event(self) -> None:
        events = [_make_pr_event(action="closed", merged=True)]
        meta = _extract_pr_metadata(events)
        assert meta["pr_merged"] is True

    def test_merged_false_from_closed_event(self) -> None:
        events = [_make_pr_event(action="closed", merged=False)]
        meta = _extract_pr_metadata(events)
        assert meta["pr_merged"] is False

    def test_no_close_event_merged_is_none(self) -> None:
        events = [_make_pr_event(action="opened")]
        meta = _extract_pr_metadata(events)
        assert meta["pr_merged"] is None

    def test_merged_true_sticky(self) -> None:
        """Once merged=True is set, a subsequent closed(merged=False) shouldn't override it."""
        events = [
            _make_pr_event(action="closed", merged=True),
            _make_pr_event(action="closed", merged=False),
        ]
        meta = _extract_pr_metadata(events)
        assert meta["pr_merged"] is True

    def test_merged_false_then_true(self) -> None:
        """A PR closed without merge, then re-closed with merge: result is True."""
        events = [
            _make_pr_event(action="closed", merged=False),
            _make_pr_event(action="closed", merged=True),
        ]
        meta = _extract_pr_metadata(events)
        assert meta["pr_merged"] is True

    def test_multiple_events_only_closed_sets_merged(self) -> None:
        events = [
            _make_pr_event(action="opened"),
            _make_pr_event(action="reopened"),
        ]
        meta = _extract_pr_metadata(events)
        assert meta["pr_merged"] is None


# -- _extract_pr_metadata: title and author -----------------------------------


class TestTitleAndAuthor:
    def test_pr_event_extracts_title_and_author(self) -> None:
        events = [_make_pr_event(author="alice", title="My PR")]
        meta = _extract_pr_metadata(events)
        assert meta["pr_title"] == "My PR"
        assert meta["pr_author"] == "alice"
        assert meta["pr_created_at"] == "2026-01-15T09:00:00Z"

    def test_review_event_extracts_title_and_author(self) -> None:
        events = [{
            "type": "PullRequestReviewEvent",
            "actor": "reviewer",
            "created_at": "2026-01-15T11:00:00Z",
            "payload": {
                "review": {"id": 1, "state": "COMMENTED"},
                "pull_request": {
                    "title": "From review",
                    "user": {"login": "bob"},
                    "created_at": "2026-01-15T09:00:00Z",
                },
            },
        }]
        meta = _extract_pr_metadata(events)
        assert meta["pr_title"] == "From review"
        assert meta["pr_author"] == "bob"

    def test_review_comment_event_extracts_title_and_author(self) -> None:
        events = [{
            "type": "PullRequestReviewCommentEvent",
            "actor": "reviewer",
            "created_at": "2026-01-15T11:00:00Z",
            "payload": {
                "comment": {"id": 1, "body": "nit"},
                "pull_request": {
                    "title": "From comment",
                    "user": {"login": "carol"},
                    "created_at": "2026-01-15T09:00:00Z",
                },
            },
        }]
        meta = _extract_pr_metadata(events)
        assert meta["pr_title"] == "From comment"
        assert meta["pr_author"] == "carol"

    def test_issue_comment_event_extracts_title_and_author(self) -> None:
        events = [{
            "type": "IssueCommentEvent",
            "actor": "bot[bot]",
            "created_at": "2026-01-15T12:00:00Z",
            "payload": {
                "action": "created",
                "issue": {
                    "title": "From issue",
                    "user": {"login": "dave"},
                    "created_at": "2026-01-15T09:00:00Z",
                    "pull_request": {"html_url": "https://github.com/org/repo/pull/42"},
                },
                "comment": {"id": 100, "body": "test"},
            },
        }]
        meta = _extract_pr_metadata(events)
        assert meta["pr_title"] == "From issue"
        assert meta["pr_author"] == "dave"

    def test_first_title_wins(self) -> None:
        """First event with a non-empty title wins."""
        events = [
            _make_pr_event(title="First"),
            _make_pr_event(title="Second"),
        ]
        meta = _extract_pr_metadata(events)
        assert meta["pr_title"] == "First"

    def test_first_author_wins(self) -> None:
        """First event with a non-None author wins."""
        events = [
            _make_pr_event(author="first"),
            _make_pr_event(author="second"),
        ]
        meta = _extract_pr_metadata(events)
        assert meta["pr_author"] == "first"


# -- Edge cases ---------------------------------------------------------------


class TestEdgeCases:
    def test_empty_events(self) -> None:
        meta = _extract_pr_metadata([])
        assert meta == {
            "pr_title": "",
            "pr_author": None,
            "pr_created_at": None,
            "pr_merged": None,
        }

    def test_missing_payload(self) -> None:
        events = [{"type": "PullRequestEvent", "actor": "x", "created_at": "2026-01-15T10:00:00Z"}]
        meta = _extract_pr_metadata(events)
        assert meta["pr_author"] is None

    def test_missing_user_field(self) -> None:
        events = [{
            "type": "PullRequestEvent",
            "actor": "bot",
            "created_at": "2026-01-15T10:00:00Z",
            "payload": {"action": "opened", "pull_request": {}},
        }]
        meta = _extract_pr_metadata(events)
        assert meta["pr_author"] is None

    def test_user_none(self) -> None:
        """user field explicitly set to None."""
        events = [{
            "type": "PullRequestEvent",
            "actor": "bot",
            "created_at": "2026-01-15T10:00:00Z",
            "payload": {"action": "opened", "pull_request": {"user": None}},
        }]
        meta = _extract_pr_metadata(events)
        assert meta["pr_author"] is None

    def test_unknown_event_type_ignored(self) -> None:
        events = [{
            "type": "WatchEvent",
            "actor": "watcher",
            "created_at": "2026-01-15T10:00:00Z",
            "payload": {},
        }]
        meta = _extract_pr_metadata(events)
        assert meta["pr_author"] is None

    def test_issue_comment_without_pull_request_key(self) -> None:
        """IssueCommentEvent on a non-PR issue has no pull_request key."""
        events = [{
            "type": "IssueCommentEvent",
            "actor": "bot",
            "created_at": "2026-01-15T12:00:00Z",
            "payload": {
                "issue": {"title": "An issue", "user": {"login": "eve"}},
            },
        }]
        meta = _extract_pr_metadata(events)
        assert meta["pr_author"] == "eve"
        assert meta["pr_title"] == "An issue"


# -- Property-based tests -----------------------------------------------------


@given(
    author=st.text(
        min_size=1, max_size=30,
        alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    ),
    title=st.text(min_size=0, max_size=100),
)
@settings(max_examples=200)
def test_metadata_preserves_author_and_title(author: str, title: str) -> None:
    """Any author/title survives extraction roundtrip."""
    events = [_make_pr_event(author=author, title=title)]
    meta = _extract_pr_metadata(events)
    assert meta["pr_author"] == author
    assert meta["pr_title"] == title


@given(merged=st.booleans())
def test_closed_event_merged_matches_input(merged: bool) -> None:
    events = [_make_pr_event(action="closed", merged=merged)]
    meta = _extract_pr_metadata(events)
    assert meta["pr_merged"] == merged

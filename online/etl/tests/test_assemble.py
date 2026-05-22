"""Tests for assemble.py pure functions: timeline, threads, stats, roles, JSON helpers."""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

from pipeline.assemble import TimelineEvent
from pipeline.assemble import _build_review_threads
from pipeline.assemble import _build_timeline_events
from pipeline.assemble import _compute_stats
from pipeline.assemble import _determine_roles
from pipeline.assemble import _enrich_timeline_with_threads
from pipeline.assemble import _extract_pr_metadata
from pipeline.assemble import _json_load
from pipeline.assemble import _parse_timestamp
from pipeline.assemble import assemble_pr_from_row

# -- _parse_timestamp ---------------------------------------------------------


class TestParseTimestamp:
    def test_iso_with_z(self) -> None:
        dt = _parse_timestamp("2026-01-15T10:30:00Z")
        assert dt.year == 2026
        assert dt.month == 1
        assert dt.hour == 10

    def test_iso_with_offset(self) -> None:
        dt = _parse_timestamp("2026-01-15T10:30:00+05:00")
        assert dt.year == 2026

    def test_none_returns_min(self) -> None:
        dt = _parse_timestamp(None)
        assert dt.year == 1

    def test_empty_string_returns_min(self) -> None:
        dt = _parse_timestamp("")
        assert dt.year == 1

    def test_invalid_string_returns_min(self) -> None:
        dt = _parse_timestamp("not-a-date")
        assert dt.year == 1

    def test_iso_without_timezone(self) -> None:
        dt = _parse_timestamp("2026-01-15T10:30:00")
        assert dt.year == 2026


# -- _json_load ---------------------------------------------------------------


class TestJsonLoad:
    def test_none(self) -> None:
        assert _json_load(None) is None

    def test_string_list(self) -> None:
        result = _json_load("[1, 2, 3]")
        assert result == [1, 2, 3]

    def test_string_dict(self) -> None:
        result = _json_load('{"a": 1}')
        assert result == {"a": 1}

    def test_already_list(self) -> None:
        data = [1, 2]
        assert _json_load(data) is data

    def test_already_dict(self) -> None:
        data = {"a": 1}
        assert _json_load(data) is data


# -- _extract_pr_metadata (assemble version) -----------------------------------


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


class TestAssembleExtractMetadata:
    def test_basic_extraction(self) -> None:
        events = [_make_pr_event(author="alice", title="My PR")]
        meta = _extract_pr_metadata(events)
        assert meta["pr_author"] == "alice"
        assert meta["pr_title"] == "My PR"

    def test_merged_from_closed(self) -> None:
        events = [_make_pr_event(action="closed", merged=True)]
        meta = _extract_pr_metadata(events)
        assert meta["pr_merged"] is True

    def test_not_merged_from_closed(self) -> None:
        events = [_make_pr_event(action="closed", merged=False)]
        meta = _extract_pr_metadata(events)
        assert meta["pr_merged"] is False

    def test_no_close_event(self) -> None:
        events = [_make_pr_event(action="opened")]
        meta = _extract_pr_metadata(events)
        assert meta["pr_merged"] is None

    def test_issue_comment_event(self) -> None:
        events = [{
            "type": "IssueCommentEvent",
            "actor": "bot",
            "created_at": "2026-01-15T12:00:00Z",
            "payload": {
                "issue": {"title": "Issue", "user": {"login": "eve"}, "created_at": "2026-01-15T09:00:00Z"},
            },
        }]
        meta = _extract_pr_metadata(events)
        assert meta["pr_author"] == "eve"
        assert meta["pr_title"] == "Issue"


# -- _build_timeline_events ---------------------------------------------------


class TestBuildTimeline:
    def test_pr_opened_event(self) -> None:
        bq_events = [_make_pr_event(action="opened")]
        timeline = _build_timeline_events(bq_events, None, None, None)
        assert len(timeline) == 1
        assert timeline[0].event_type == "pr_opened"
        assert timeline[0].actor == "alice"

    def test_pr_merged_event(self) -> None:
        bq_events = [_make_pr_event(action="closed", merged=True)]
        timeline = _build_timeline_events(bq_events, None, None, None)
        assert timeline[0].event_type == "pr_merged"

    def test_pr_closed_not_merged_event(self) -> None:
        bq_events = [_make_pr_event(action="closed", merged=False)]
        timeline = _build_timeline_events(bq_events, None, None, None)
        assert timeline[0].event_type == "pr_closed"

    def test_review_event(self) -> None:
        bq_events = [{
            "type": "PullRequestReviewEvent",
            "actor": "reviewer",
            "created_at": "2026-01-15T11:00:00Z",
            "payload": {
                "review": {"id": 42, "state": "APPROVED", "body": "LGTM"},
                "pull_request": {},
            },
        }]
        timeline = _build_timeline_events(bq_events, None, None, None)
        assert len(timeline) == 1
        assert timeline[0].event_type == "review"
        assert timeline[0].data["review_id"] == 42

    def test_review_comment_event(self) -> None:
        bq_events = [{
            "type": "PullRequestReviewCommentEvent",
            "actor": "reviewer",
            "created_at": "2026-01-15T11:00:00Z",
            "payload": {
                "comment": {"id": 99, "body": "nit: fix this", "path": "main.py", "line": 10},
            },
        }]
        timeline = _build_timeline_events(bq_events, None, None, None)
        assert timeline[0].event_type == "review_comment"
        assert timeline[0].data["path"] == "main.py"

    def test_issue_comment_event(self) -> None:
        bq_events = [{
            "type": "IssueCommentEvent",
            "actor": "bot",
            "created_at": "2026-01-15T12:00:00Z",
            "payload": {
                "comment": {"id": 100, "body": "Thanks for the review"},
            },
        }]
        timeline = _build_timeline_events(bq_events, None, None, None)
        assert timeline[0].event_type == "issue_comment"
        assert timeline[0].data["body"] == "Thanks for the review"

    def test_commits_added_to_timeline(self) -> None:
        commits = [
            {"sha": "abc123", "author": "alice", "message": "fix bug", "date": "2026-01-15T10:30:00Z"},
            {"sha": "def456", "author": "alice", "message": "address review", "date": "2026-01-15T11:00:00Z"},
        ]
        timeline = _build_timeline_events([], commits, None, None)
        assert len(timeline) == 2
        assert all(e.event_type == "commit" for e in timeline)
        assert timeline[0].data["sha"] == "abc123"

    def test_commit_details_included(self) -> None:
        commits = [{"sha": "abc", "author": "alice", "message": "fix", "date": "2026-01-15T10:30:00Z"}]
        commit_details = [{"sha": "abc", "files": [
            {"filename": "main.py", "status": "modified", "additions": 5, "deletions": 2},
        ]}]
        timeline = _build_timeline_events([], commits, commit_details, None)
        assert timeline[0].data["files_changed"] == ["main.py"]
        assert len(timeline[0].data["files_detail"]) == 1

    def test_api_reviews_added(self) -> None:
        api_reviews = [
            {"id": 1, "submitted_at": "2026-01-15T11:00:00Z", "author": "bob", "state": "APPROVED"},
        ]
        timeline = _build_timeline_events([], None, None, api_reviews)
        assert len(timeline) == 1
        assert timeline[0].event_type == "review"
        assert timeline[0].data["source"] == "api"

    def test_api_reviews_deduplicated_with_bq(self) -> None:
        """API reviews with same ID as BQ reviews should be skipped."""
        bq_events = [{
            "type": "PullRequestReviewEvent",
            "actor": "reviewer",
            "created_at": "2026-01-15T11:00:00Z",
            "payload": {"review": {"id": 42, "state": "APPROVED"}},
        }]
        api_reviews = [{"id": 42, "submitted_at": "2026-01-15T11:00:00Z", "author": "reviewer", "state": "APPROVED"}]
        timeline = _build_timeline_events(bq_events, None, None, api_reviews)
        assert len(timeline) == 1

    def test_timeline_sorted_by_timestamp(self) -> None:
        bq_events = [
            _make_pr_event(action="opened"),
        ]
        commits = [{"sha": "a", "author": "alice", "message": "early", "date": "2026-01-15T08:00:00Z"}]
        api_reviews = [{"id": 1, "submitted_at": "2026-01-15T12:00:00Z", "author": "bob", "state": "APPROVED"}]

        timeline = _build_timeline_events(bq_events, commits, None, api_reviews)
        timestamps = [e.timestamp for e in timeline]
        assert timestamps == sorted(timestamps)


# -- _build_review_threads -----------------------------------------------------


class TestBuildReviewThreads:
    def test_none_input(self) -> None:
        assert _build_review_threads(None) == []

    def test_empty_list(self) -> None:
        assert _build_review_threads([]) == []

    def test_single_thread(self) -> None:
        raw = [{
            "id": "thread-1",
            "is_resolved": True,
            "resolved_by": "alice",
            "comments": [
                {"id": 1, "body": "fix this", "author": "bot", "path": "main.py"},
                {"id": 2, "body": "fixed", "author": "alice", "path": "main.py"},
            ],
        }]
        threads = _build_review_threads(raw)
        assert len(threads) == 1
        assert threads[0].thread_id == "thread-1"
        assert threads[0].is_resolved is True
        assert threads[0].path == "main.py"
        assert len(threads[0].comments) == 2

    def test_thread_path_from_first_comment(self) -> None:
        raw = [{
            "id": "t1",
            "comments": [
                {"id": 1, "body": "nit", "author": "bot", "path": "file_a.py"},
                {"id": 2, "body": "ok", "author": "alice", "path": "file_b.py"},
            ],
        }]
        threads = _build_review_threads(raw)
        assert threads[0].path == "file_a.py"


# -- _enrich_timeline_with_threads --------------------------------------------


class TestEnrichTimeline:
    def test_no_threads_no_change(self) -> None:
        timeline = [TimelineEvent("2026-01-15T10:00:00Z", "review_comment", "bot", {"comment_id": 1})]
        _enrich_timeline_with_threads(timeline, None)
        assert len(timeline) == 1

    def test_enriches_existing_comment(self) -> None:
        timeline = [TimelineEvent("2026-01-15T10:00:00Z", "review_comment", "bot", {"comment_id": 1})]
        raw_threads = [{
            "id": "t1",
            "is_resolved": True,
            "resolved_by": "alice",
            "comments": [{"id": 1, "body": "better body", "author": "bot"}],
        }]
        _enrich_timeline_with_threads(timeline, raw_threads)
        assert timeline[0].data["is_resolved"] is True
        assert timeline[0].data["thread_id"] == "t1"
        assert timeline[0].data["body"] == "better body"

    def test_adds_missing_thread_comments(self) -> None:
        timeline = [TimelineEvent("2026-01-15T10:00:00Z", "review_comment", "bot", {"comment_id": 1})]
        raw_threads = [{
            "id": "t1",
            "is_resolved": False,
            "comments": [
                {"id": 1, "body": "first", "author": "bot"},
                {"id": 2, "body": "reply", "author": "alice", "created_at": "2026-01-15T11:00:00Z"},
            ],
        }]
        _enrich_timeline_with_threads(timeline, raw_threads)
        assert len(timeline) == 2
        assert timeline[1].event_type == "review_comment"
        assert timeline[1].actor == "alice"
        assert timeline[1].data["source"] == "api"


# -- _compute_stats -----------------------------------------------------------


class TestComputeStats:
    def test_empty_timeline(self) -> None:
        stats = _compute_stats("bot[bot]", [], [])
        assert stats.total_events == 0
        assert stats.total_commits == 0

    def test_counts_commits(self) -> None:
        timeline = [
            TimelineEvent("t1", "commit", "alice"),
            TimelineEvent("t2", "commit", "alice"),
            TimelineEvent("t3", "review", "bot[bot]"),
        ]
        stats = _compute_stats("bot[bot]", timeline, [])
        assert stats.total_commits == 2
        assert stats.total_events == 3

    def test_counts_target_review_comments(self) -> None:
        timeline = [
            TimelineEvent("t1", "review_comment", "bot[bot]", {"body": "nit"}),
            TimelineEvent("t2", "review_comment", "alice", {"body": "ok"}),
        ]
        stats = _compute_stats("bot[bot]", timeline, [])
        assert stats.total_review_comments_by_target == 1

    def test_counts_resolved_threads(self) -> None:
        from pipeline.assemble import ReviewThread
        threads = [
            ReviewThread("t1", "main.py", True, "alice"),
            ReviewThread("t2", "util.py", False, None),
            ReviewThread("t3", "test.py", True, "bob"),
        ]
        stats = _compute_stats("bot[bot]", [], threads)
        assert stats.total_review_threads == 3
        assert stats.resolved_threads == 2

    def test_target_user_comment_types(self) -> None:
        timeline = [
            TimelineEvent("t1", "review_comment", "bot[bot]"),
            TimelineEvent("t2", "issue_comment", "bot[bot]"),
            TimelineEvent("t3", "review", "bot[bot]"),
            TimelineEvent("t4", "commit", "bot[bot]"),
            TimelineEvent("t5", "pr_opened", "bot[bot]"),
        ]
        stats = _compute_stats("bot[bot]", timeline, [])
        assert stats.target_user_comments_count == 3


# -- _determine_roles ---------------------------------------------------------


class TestDetermineRoles:
    def test_author_role(self) -> None:
        roles = _determine_roles("alice", [], "alice")
        assert roles == ["author"]

    def test_reviewer_role(self) -> None:
        timeline = [TimelineEvent("t1", "review", "bot[bot]")]
        roles = _determine_roles("bot[bot]", timeline, "alice")
        assert "reviewer" in roles

    def test_commenter_role(self) -> None:
        timeline = [TimelineEvent("t1", "issue_comment", "bot[bot]")]
        roles = _determine_roles("bot[bot]", timeline, "alice")
        assert "commenter" in roles

    def test_author_and_reviewer(self) -> None:
        timeline = [TimelineEvent("t1", "review_comment", "bot[bot]")]
        roles = _determine_roles("bot[bot]", timeline, "bot[bot]")
        assert "author" in roles
        assert "reviewer" in roles

    def test_no_roles_for_non_target(self) -> None:
        timeline = [TimelineEvent("t1", "review", "other")]
        roles = _determine_roles("bot[bot]", timeline, "alice")
        assert roles == []

    def test_roles_sorted(self) -> None:
        timeline = [
            TimelineEvent("t1", "review", "bot[bot]"),
            TimelineEvent("t2", "issue_comment", "bot[bot]"),
        ]
        roles = _determine_roles("bot[bot]", timeline, "bot[bot]")
        assert roles == sorted(roles)


# -- assemble_pr_from_row -----------------------------------------------------


class TestAssemblePrFromRow:
    def test_returns_none_without_bq_events(self) -> None:
        row = {"repo_name": "org/repo", "pr_number": 1, "pr_url": "https://x", "bq_events": None}
        assert assemble_pr_from_row(row, "bot[bot]") is None

    def test_basic_assembly(self) -> None:
        bq_events = [_make_pr_event(author="alice", title="Fix thing", action="closed", merged=True)]
        row = {
            "repo_name": "org/repo",
            "pr_number": 42,
            "pr_url": "https://github.com/org/repo/pull/42",
            "bq_events": json.dumps(bq_events),
            "commits": None,
            "reviews": None,
            "review_threads": None,
            "commit_details": None,
        }
        result = assemble_pr_from_row(row, "bot[bot]")
        assert result is not None
        assert result["pr_author"] == "alice"
        assert result["pr_title"] == "Fix thing"
        assert result["pr_merged"] is True
        assert result["repo_name"] == "org/repo"
        assert result["pr_number"] == 42
        assert "stats" in result
        assert "events" in result
        assert "review_threads" in result
        assert "target_user_roles" in result

    def test_pr_merged_none_when_no_close(self) -> None:
        """Assemble should produce pr_merged=None when BQ events have no close event."""
        bq_events = [_make_pr_event(action="opened")]
        row = {
            "repo_name": "org/repo",
            "pr_number": 1,
            "pr_url": "https://x",
            "bq_events": json.dumps(bq_events),
            "commits": None,
            "reviews": None,
            "review_threads": None,
            "commit_details": None,
        }
        result = assemble_pr_from_row(row, "bot[bot]")
        assert result is not None
        assert result["pr_merged"] is None

    def test_bq_events_as_list(self) -> None:
        """bq_events can be passed as a list (Postgres returns parsed JSON)."""
        bq_events = [_make_pr_event()]
        row = {
            "repo_name": "org/repo",
            "pr_number": 1,
            "pr_url": "https://x",
            "bq_events": bq_events,
            "commits": None,
            "reviews": None,
            "review_threads": None,
            "commit_details": None,
        }
        result = assemble_pr_from_row(row, "bot[bot]")
        assert result is not None


# -- TimelineEvent.to_dict ----------------------------------------------------


class TestTimelineEventSerialization:
    def test_to_dict(self) -> None:
        e = TimelineEvent("2026-01-15T10:00:00Z", "review", "bot", {"state": "APPROVED"})
        d = e.to_dict()
        assert d["timestamp"] == "2026-01-15T10:00:00Z"
        assert d["event_type"] == "review"
        assert d["actor"] == "bot"
        assert d["data"]["state"] == "APPROVED"

    def test_default_data(self) -> None:
        e = TimelineEvent("ts", "type", "actor")
        assert e.data == {}


# -- Property-based -----------------------------------------------------------


_ts_strat = st.datetimes().map(lambda dt: dt.isoformat() + "Z")


@given(ts=_ts_strat)
@settings(max_examples=100)
def test_parse_timestamp_never_raises(ts: str) -> None:
    """_parse_timestamp should never raise, always return a datetime."""
    result = _parse_timestamp(ts)
    assert result is not None


@given(data=st.one_of(
    st.none(),
    st.lists(st.integers(), max_size=5).map(json.dumps),
    st.dictionaries(st.text(max_size=5), st.integers(), max_size=3).map(json.dumps),
))
def test_json_load_roundtrip(data: str | None) -> None:
    """_json_load should parse JSON strings or return None."""
    result = _json_load(data)
    if data is None:
        assert result is None
    else:
        assert result == json.loads(data)

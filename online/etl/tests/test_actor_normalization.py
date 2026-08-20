"""Tests for GitHub App actor normalization across benchmark pipeline stages."""

from __future__ import annotations

from pipeline.actors import normalize_github_actor
from pipeline.actors import same_github_actor
from pipeline.analyze import _find_bot_review_commit
from pipeline.analyze import _format_bot_comments
from pipeline.analyze import _format_post_review_activity
from pipeline.assemble import TimelineEvent
from pipeline.assemble import _compute_stats
from pipeline.assemble import _determine_roles
from pipeline.quality import compute_engagement_signals


def test_normalizes_bot_suffix_for_github_app_actors() -> None:
    assert normalize_github_actor("Cubic-Dev-AI[bot]") == "cubic-dev-ai"
    assert same_github_actor("cubic-dev-ai", "cubic-dev-ai[bot]")
    assert not same_github_actor("cubic-dev-ai", "coderabbitai[bot]")


def test_copilot_aliases_resolve_to_same_actor() -> None:
    assert same_github_actor("Copilot", "copilot-pull-request-reviewer[bot]")
    assert same_github_actor("Copilot", "copilot-pull-request-reviewer")
    assert same_github_actor("Copilot", "copilot-swe-agent[bot]")
    assert same_github_actor("Copilot", "copilot-swe-agent")
    assert normalize_github_actor("copilot-pull-request-reviewer[bot]") == "copilot"
    assert normalize_github_actor("copilot-swe-agent") == "copilot"
    assert not same_github_actor("Copilot", "coderabbitai[bot]")


def test_analyze_includes_graphql_thread_comments_from_bot_slug() -> None:
    events = [
        {
            "timestamp": "2026-06-18T10:00:00Z",
            "event_type": "review_comment",
            "actor": "cubic-dev-ai",
            "data": {"body": "Fix the missing null check.", "path": "main.py", "line": 42},
        }
    ]

    formatted = _format_bot_comments(events, "cubic-dev-ai[bot]")

    assert "Fix the missing null check." in formatted
    assert "main.py:42" in formatted


def test_analyze_excludes_bot_slug_from_human_activity() -> None:
    events = [
        {
            "timestamp": "2026-06-18T10:00:00Z",
            "event_type": "review_comment",
            "actor": "cubic-dev-ai",
            "data": {"body": "Initial bot review."},
        },
        {
            "timestamp": "2026-06-18T10:05:00Z",
            "event_type": "review_comment",
            "actor": "alice",
            "data": {"body": "I pushed the fix."},
        },
        {
            "timestamp": "2026-06-18T10:06:00Z",
            "event_type": "review_comment",
            "actor": "cubic-dev-ai",
            "data": {"body": "Bot follow-up should not be human activity."},
        },
    ]

    formatted = _format_post_review_activity([], {}, events, "cubic-dev-ai[bot]", None)

    assert "I pushed the fix." in formatted
    assert "Bot follow-up should not be human activity." not in formatted


def test_find_bot_review_commit_matches_slug_without_bot_suffix() -> None:
    reviews = [{"author": "cubic-dev-ai", "commit_id": "abc123"}]

    assert _find_bot_review_commit(reviews, [], [], "cubic-dev-ai[bot]") == "abc123"


def test_assemble_stats_and_roles_match_slug_without_bot_suffix() -> None:
    timeline = [
        TimelineEvent("2026-06-18T10:00:00Z", "review_comment", "cubic-dev-ai"),
        TimelineEvent("2026-06-18T10:05:00Z", "issue_comment", "alice"),
    ]

    stats = _compute_stats("cubic-dev-ai[bot]", timeline, [])
    roles = _determine_roles("cubic-dev-ai[bot]", timeline, "alice")

    assert stats.total_review_comments_by_target == 1
    assert stats.target_user_comments_count == 1
    assert roles == ["reviewer"]


def test_engagement_signals_start_after_bot_slug_review() -> None:
    assembled = {
        "events": [
            {
                "timestamp": "2026-06-18T10:00:00Z",
                "event_type": "review_comment",
                "actor": "cubic-dev-ai",
                "data": {"body": "Please fix this."},
            },
            {
                "timestamp": "2026-06-18T10:05:00Z",
                "event_type": "issue_comment",
                "actor": "alice",
                "data": {"body": "Fixed."},
            },
        ],
    }

    signals = compute_engagement_signals(assembled, "cubic-dev-ai[bot]", pr_author="bob")

    assert signals["has_human_engagement"] is True
    assert signals["human_comment_count"] == 1


def test_engagement_signals_detect_copilot_reviewer_alias() -> None:
    """Copilot reviews land as copilot-pull-request-reviewer; chatbot name is Copilot.

    Engagement used to miss the first review (bot_first_ts=None → not engaged)
    even when later commits existed. Aliases must make this count as engaged.
    """
    assembled = {
        "events": [
            {
                "timestamp": "2026-06-18T10:00:00Z",
                "event_type": "review",
                "actor": "copilot-pull-request-reviewer[bot]",
                "data": {"body": "Please add a null check."},
            },
            {
                "timestamp": "2026-06-18T10:10:00Z",
                "event_type": "commit",
                "actor": "alice",
                "data": {"sha": "abc"},
            },
        ],
    }

    signals = compute_engagement_signals(assembled, "Copilot", pr_author="alice")

    assert signals["has_human_engagement"] is True
    assert signals["commits_after_review"] == 1

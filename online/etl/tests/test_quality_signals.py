"""Tests for per-PR quality and engagement signal computation."""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st
import pytest

from pipeline.quality import compute_engagement_signals
from pipeline.quality import is_bot_username
from pipeline.quality import serialize_engagement_signals

# -- is_bot_username ----------------------------------------------------------

@pytest.mark.parametrize("username,expected", [
    ("coderabbitai[bot]", True),
    ("dependabot[bot]", True),
    ("Copilot", True),
    ("copilot", True),
    ("renovate", True),
    ("github-actions", True),
    ("alice", False),
    ("mybot", False),
    ("snyk-bot", True),
    ("DEPENDABOT", True),
    ("GitHub-Actions", True),
    ("cubic-dev-ai", True),
    ("gemini-code-assist", True),
    ("github-advanced-security", True),
    ("copilot-pull-request-review", True),
    ("copilot-pull-request-reviewer", True),
    ("clawdbot", True),
])
def test_is_bot_username(username: str, expected: bool) -> None:
    assert is_bot_username(username) == expected


# -- helpers ------------------------------------------------------------------

BOT = "coderabbitai[bot]"


def _ev(
    event_type: str,
    actor: str,
    timestamp: str,
    body: str = "",
    **extra: object,
) -> dict:
    data: dict = {**extra}
    if body:
        data["body"] = body
    return {"event_type": event_type, "actor": actor, "timestamp": timestamp, "data": data}


def _asm(events: list[dict] | None = None, pr_author: str = "alice") -> dict:
    return {"pr_author": pr_author, "events": events or []}


def _signals(events: list[dict], pr_author: str = "alice") -> dict:
    return compute_engagement_signals(_asm(events, pr_author), BOT, pr_author=pr_author)


# -- compute_engagement_signals: basic cases ----------------------------------

class TestEngagementBasics:
    def test_no_events(self) -> None:
        result = _signals([])
        assert result["has_human_engagement"] is False
        assert result["human_reviewer_count"] == 0
        assert result["human_comment_count"] == 0
        assert result["back_and_forth_rounds"] == 0
        assert result["commits_after_review"] == 0

    def test_only_bot_events(self) -> None:
        events = [
            _ev("review", BOT, "2026-01-01T00:00:00Z"),
            _ev("review_comment", BOT, "2026-01-01T00:01:00Z"),
        ]
        result = _signals(events)
        assert result["has_human_engagement"] is False
        assert result["human_comment_count"] == 0

    def test_bot_no_review_events(self) -> None:
        """Bot only has commits, no review — engagement tracking doesn't start."""
        events = [
            _ev("commit", BOT, "2026-01-01T00:00:00Z"),
            _ev("issue_comment", "alice", "2026-01-01T00:05:00Z"),
        ]
        result = _signals(events)
        assert result["has_human_engagement"] is False

    def test_human_activity_before_bot_review_only(self) -> None:
        events = [
            _ev("issue_comment", "alice", "2026-01-01T00:00:00Z"),
            _ev("review", BOT, "2026-01-01T00:10:00Z"),
        ]
        result = _signals(events)
        assert result["has_human_engagement"] is False

    def test_other_bot_doesnt_count(self) -> None:
        events = [
            _ev("review", BOT, "2026-01-01T00:00:00Z"),
            _ev("issue_comment", "dependabot[bot]", "2026-01-01T00:05:00Z"),
        ]
        result = _signals(events)
        assert result["has_human_engagement"] is False
        assert result["human_comment_count"] == 0

    def test_pr_event_doesnt_count(self) -> None:
        events = [
            _ev("review", BOT, "2026-01-01T00:00:00Z"),
            _ev("pr_merged", "alice", "2026-01-01T01:00:00Z"),
        ]
        result = _signals(events)
        assert result["has_human_engagement"] is False


# -- comment counting and lengths --------------------------------------------

class TestCommentMetrics:
    def test_single_human_comment(self) -> None:
        events = [
            _ev("review", BOT, "2026-01-01T00:00:00Z"),
            _ev("issue_comment", "alice", "2026-01-01T00:05:00Z", body="Looks good, thanks!"),
        ]
        result = _signals(events)
        assert result["has_human_engagement"] is True
        assert result["human_comment_count"] == 1
        assert result["human_comment_total_length"] == len("Looks good, thanks!")

    def test_multiple_comments_multiple_reviewers(self) -> None:
        events = [
            _ev("review", BOT, "2026-01-01T00:00:00Z"),
            _ev("review_comment", "bob", "2026-01-01T00:05:00Z", body="Nice"),
            _ev("issue_comment", "carol", "2026-01-01T00:06:00Z", body="LGTM"),
            _ev("review_comment", "bob", "2026-01-01T00:07:00Z", body="One more thing"),
        ]
        result = _signals(events)
        assert result["human_comment_count"] == 3
        assert result["human_comment_total_length"] == len("Nice") + len("LGTM") + len("One more thing")
        # bob and carol, but NOT alice (pr_author excluded from reviewer count)
        assert result["human_reviewer_count"] == 2


# -- reviewer count with pr_author exclusion ----------------------------------

class TestReviewerCount:
    def test_author_excluded_from_reviewer_count(self) -> None:
        """PR author's comments count toward comment metrics but not reviewer count."""
        events = [
            _ev("review", BOT, "2026-01-01T00:00:00Z"),
            _ev("issue_comment", "alice", "2026-01-01T00:05:00Z", body="Fixed"),
        ]
        result = _signals(events, pr_author="alice")
        assert result["human_comment_count"] == 1
        assert result["human_reviewer_count"] == 0  # only the author engaged
        assert result["has_human_engagement"] is True

    def test_author_plus_reviewer(self) -> None:
        events = [
            _ev("review", BOT, "2026-01-01T00:00:00Z"),
            _ev("issue_comment", "alice", "2026-01-01T00:05:00Z", body="ok"),
            _ev("review_comment", "bob", "2026-01-01T00:06:00Z", body="Looks right"),
        ]
        result = _signals(events, pr_author="alice")
        assert result["human_reviewer_count"] == 1  # bob only
        assert result["human_comment_count"] == 2

    def test_no_pr_author_counts_all(self) -> None:
        """When pr_author is None, all humans count as reviewers."""
        events = [
            _ev("review", BOT, "2026-01-01T00:00:00Z"),
            _ev("issue_comment", "alice", "2026-01-01T00:05:00Z", body="ok"),
        ]
        result = compute_engagement_signals(_asm(events), BOT, pr_author=None)
        assert result["human_reviewer_count"] == 1


# -- commits after review ----------------------------------------------------

class TestCommitsAfterReview:
    def test_commits_counted(self) -> None:
        events = [
            _ev("review", BOT, "2026-01-01T00:00:00Z"),
            _ev("commit", "alice", "2026-01-01T00:30:00Z"),
            _ev("commit", "alice", "2026-01-01T00:45:00Z"),
        ]
        result = _signals(events)
        assert result["commits_after_review"] == 2
        assert result["has_human_engagement"] is True

    def test_commits_before_review_not_counted(self) -> None:
        events = [
            _ev("commit", "alice", "2026-01-01T00:00:00Z"),
            _ev("review", BOT, "2026-01-01T00:10:00Z"),
        ]
        result = _signals(events)
        assert result["commits_after_review"] == 0


# -- back and forth rounds (review cycles) ------------------------------------

class TestBackAndForthRounds:
    def test_zero_rounds_no_human_response(self) -> None:
        """Bot reviews, nobody responds = 0 rounds."""
        events = [
            _ev("review", BOT, "2026-01-01T00:00:00Z"),
        ]
        result = _signals(events)
        assert result["back_and_forth_rounds"] == 0

    def test_zero_rounds_human_responds_bot_doesnt_follow_up(self) -> None:
        """Bot reviews, human responds, bot doesn't review again = 0 rounds."""
        events = [
            _ev("review", BOT, "2026-01-01T00:00:00Z"),
            _ev("commit", "alice", "2026-01-01T00:30:00Z"),
        ]
        result = _signals(events)
        assert result["back_and_forth_rounds"] == 0

    def test_one_round(self) -> None:
        """Bot reviews → human commits → bot reviews again = 1 round."""
        events = [
            _ev("review", BOT, "2026-01-01T00:00:00Z"),
            _ev("commit", "alice", "2026-01-01T00:30:00Z"),
            _ev("review", BOT, "2026-01-01T01:00:00Z"),
        ]
        result = _signals(events)
        assert result["back_and_forth_rounds"] == 1

    def test_two_rounds(self) -> None:
        """Two full cycles of bot→human→bot."""
        events = [
            _ev("review", BOT, "2026-01-01T00:00:00Z"),
            _ev("issue_comment", "alice", "2026-01-01T00:10:00Z", body="fixing"),
            _ev("commit", "alice", "2026-01-01T00:30:00Z"),
            _ev("review_comment", BOT, "2026-01-01T01:00:00Z"),
            _ev("commit", "alice", "2026-01-01T01:30:00Z"),
            _ev("review", BOT, "2026-01-01T02:00:00Z"),
        ]
        result = _signals(events)
        assert result["back_and_forth_rounds"] == 2

    def test_consecutive_bot_reviews_count_once(self) -> None:
        """Multiple bot reviews without human activity in between = still 1 transition."""
        events = [
            _ev("review", BOT, "2026-01-01T00:00:00Z"),
            _ev("commit", "alice", "2026-01-01T00:30:00Z"),
            _ev("review", BOT, "2026-01-01T01:00:00Z"),
            _ev("review_comment", BOT, "2026-01-01T01:01:00Z"),
        ]
        result = _signals(events)
        assert result["back_and_forth_rounds"] == 1

    def test_human_comment_triggers_round(self) -> None:
        """Human comment (not just commit) followed by bot review = round."""
        events = [
            _ev("review", BOT, "2026-01-01T00:00:00Z"),
            _ev("issue_comment", "alice", "2026-01-01T00:10:00Z", body="I disagree"),
            _ev("review_comment", BOT, "2026-01-01T00:20:00Z"),
        ]
        result = _signals(events)
        assert result["back_and_forth_rounds"] == 1


# -- serialization ------------------------------------------------------------

class TestSerializeEngagement:
    def test_round_trip(self) -> None:
        assembled = _asm([
            _ev("review", BOT, "2026-01-01T00:00:00Z"),
            _ev("issue_comment", "alice", "2026-01-01T00:05:00Z", body="Thanks"),
            _ev("commit", "alice", "2026-01-01T00:30:00Z"),
        ])
        serialized = serialize_engagement_signals(assembled, BOT, pr_author="alice")
        parsed = json.loads(serialized)
        assert parsed["has_human_engagement"] is True
        assert parsed["human_comment_count"] == 1
        assert parsed["commits_after_review"] == 1
        assert set(parsed.keys()) == {
            "human_reviewer_count", "human_comment_count",
            "human_comment_total_length", "back_and_forth_rounds",
            "commits_after_review", "has_human_engagement",
        }


# -- property-based tests ----------------------------------------------------

_actor_strat = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_[]"),
    min_size=1,
    max_size=30,
)

_timestamp_strat = st.datetimes().map(lambda dt: dt.isoformat() + "Z")

_event_type_strat = st.sampled_from(["review", "review_comment", "issue_comment", "commit", "pr_merged", "pr_closed"])


@given(
    pr_author=_actor_strat,
    chatbot=_actor_strat,
    events=st.lists(
        st.tuples(_event_type_strat, _actor_strat, _timestamp_strat),
        max_size=20,
    ),
)
@settings(max_examples=200)
def test_engagement_signals_always_valid_structure(
    pr_author: str,
    chatbot: str,
    events: list[tuple[str, str, str]],
) -> None:
    """Engagement signals always return the expected keys with correct types."""
    assembled = _asm(
        events=[_ev(et, actor, ts) for et, actor, ts in events],
        pr_author=pr_author,
    )
    result = compute_engagement_signals(assembled, chatbot, pr_author=pr_author)
    expected_keys = {
        "human_reviewer_count", "human_comment_count",
        "human_comment_total_length", "back_and_forth_rounds",
        "commits_after_review", "has_human_engagement",
    }
    assert set(result.keys()) == expected_keys
    assert isinstance(result["has_human_engagement"], bool)
    assert all(isinstance(result[k], int) for k in expected_keys - {"has_human_engagement"})
    assert all(result[k] >= 0 for k in expected_keys - {"has_human_engagement"})


@given(
    chatbot=_actor_strat,
    n_bot_only=st.integers(min_value=0, max_value=10),
)
@settings(max_examples=100)
def test_bot_only_never_has_engagement(chatbot: str, n_bot_only: int) -> None:
    """If all events are from the chatbot, there can be no human engagement."""
    events = [
        _ev("review_comment", chatbot, f"2026-01-01T00:{i:02d}:00Z")
        for i in range(n_bot_only)
    ]
    result = compute_engagement_signals(_asm(events=events), chatbot)
    assert result["has_human_engagement"] is False
    assert result["human_comment_count"] == 0
    assert result["commits_after_review"] == 0
    assert result["back_and_forth_rounds"] == 0

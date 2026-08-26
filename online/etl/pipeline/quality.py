"""Per-PR quality and engagement signals from assembled timeline data.

Engagement signals (stored in engagement_signals JSON column):
  - human_reviewer_count: distinct non-bot commenters after bot review
  - human_comment_count: total human comments after bot review
  - human_comment_total_length: sum of comment body lengths
  - back_and_forth_rounds: bot→human→bot review cycles
  - commits_after_review: commit events after bot's first review
  - has_human_engagement: convenience bool (any human activity after review)

Signals derivable from existing columns (computed at load time, not stored):
  - pr_author_is_bot: pr_author ends in [bot] or is a known bot
  - repo_unique_contributors: COUNT(DISTINCT pr_author) per repo
  - author_repo_pr_count: count per (repo, author, bot) triple
"""

from __future__ import annotations

import json
import logging
from typing import Any

from config import DEFAULT_CHATBOT_USERNAMES
from pipeline.actors import normalize_github_actor
from pipeline.actors import same_github_actor

logger = logging.getLogger(__name__)

# BQ events often store bot actors without the [bot] suffix, so we build
# a comprehensive set from DEFAULT_CHATBOT_USERNAMES plus other known bots.
_GENERAL_BOT_USERNAMES = frozenset({
    "dependabot", "renovate", "github-actions", "codecov",
    "mergify", "snyk-bot", "greenkeeper", "imgbot",
    "stale", "allcontributors", "semantic-release-bot",
    "github-advanced-security",
    "llamapreview", "ai-coding-guardrails",
    "qodo-free-for-open-source-projects", "amazon-q-developer",
    "sourceryai", "github-code-quality", "copilot-pull-request-review",
    "copilot-pull-request-reviewer", "raycastbot", "clawdbot",
    "cometactions", "kilo-code-bot", "codecov-comment",
})

_KNOWN_BOT_USERNAMES: frozenset[str] = frozenset(
    {name.lower() for name in _GENERAL_BOT_USERNAMES}
    | {name.lower() for name in DEFAULT_CHATBOT_USERNAMES}
    | {name.lower().removesuffix("[bot]") for name in DEFAULT_CHATBOT_USERNAMES}
)

_COMMENT_EVENT_TYPES = frozenset({"review", "review_comment", "issue_comment"})
_REVIEW_EVENT_TYPES = frozenset({"review", "review_comment", "issue_comment"})


def is_bot_username(username: str) -> bool:
    """Heuristic: username is a bot if it ends with [bot] or matches a known bot name.

    Handles BQ event actors that may lack the [bot] suffix (e.g. 'cubic-dev-ai'
    instead of 'cubic-dev-ai[bot]'), and known aliases (e.g. copilot-swe-agent → copilot).
    """
    lower = username.lower()
    if lower.endswith("[bot]") or lower in _KNOWN_BOT_USERNAMES:
        return True
    canonical = normalize_github_actor(username)
    return bool(canonical) and canonical in _KNOWN_BOT_USERNAMES


def _find_bot_first_review_ts(
    events: list[dict[str, Any]],
    chatbot_username: str,
) -> str | None:
    """Find the timestamp of the bot's first review/comment event."""
    for e in events:
        if same_github_actor(e.get("actor"), chatbot_username) and e.get("event_type") in _REVIEW_EVENT_TYPES:
            return e.get("timestamp")
    return None


def compute_engagement_signals(
    assembled: dict[str, Any],
    chatbot_username: str,
    pr_author: str | None = None,
) -> dict[str, Any]:
    """Compute engagement signals from an assembled PR timeline.

    Measures the depth and quality of human interaction with the bot's review.
    All metrics are scoped to activity after the bot's first review event.

    Args:
        assembled: The assembled PR record with events list.
        chatbot_username: The bot's GitHub username.
        pr_author: If provided, excluded from human_reviewer_count to detect
            "self-dealing" (only the PR author engaged with the review).
    """
    events = assembled.get("events", [])
    bot_first_ts = _find_bot_first_review_ts(events, chatbot_username)

    if bot_first_ts is None:
        return {
            "human_reviewer_count": 0,
            "human_comment_count": 0,
            "human_comment_total_length": 0,
            "back_and_forth_rounds": 0,
            "commits_after_review": 0,
            "has_human_engagement": False,
        }

    human_actors: set[str] = set()
    human_comment_count = 0
    human_comment_total_length = 0
    commits_after_review = 0

    # Track review cycles: bot reviews → human responds → bot reviews again = 1 round
    # "last_phase" tracks whether the most recent activity was from the bot or human
    last_phase: str | None = "bot"  # bot just reviewed
    back_and_forth_rounds = 0
    pr_author_lower = pr_author.lower() if pr_author else None

    for e in events:
        ts = e.get("timestamp", "")
        if ts <= bot_first_ts:
            continue

        actor = e.get("actor") or ""
        if not actor:
            continue

        actor_lower = normalize_github_actor(actor)
        etype = e.get("event_type", "")

        if same_github_actor(actor, chatbot_username):
            # Bot activity after human response = start of new round
            if etype in _REVIEW_EVENT_TYPES and last_phase == "human":
                back_and_forth_rounds += 1
                last_phase = "bot"
            continue

        if is_bot_username(actor):
            continue

        # Human activity
        if etype in _COMMENT_EVENT_TYPES:
            human_comment_count += 1
            body = (e.get("data") or {}).get("body") or ""
            human_comment_total_length += len(body)
            human_actors.add(actor_lower)
            last_phase = "human"
        elif etype == "commit":
            commits_after_review += 1
            last_phase = "human"

    # Exclude PR author from reviewer count if requested
    reviewer_actors = human_actors
    if pr_author_lower and pr_author_lower in reviewer_actors:
        reviewer_actors = reviewer_actors - {pr_author_lower}

    has_engagement = human_comment_count > 0 or commits_after_review > 0

    return {
        "human_reviewer_count": len(reviewer_actors),
        "human_comment_count": human_comment_count,
        "human_comment_total_length": human_comment_total_length,
        "back_and_forth_rounds": back_and_forth_rounds,
        "commits_after_review": commits_after_review,
        "has_human_engagement": has_engagement,
    }


def serialize_engagement_signals(
    assembled: dict[str, Any],
    chatbot_username: str,
    pr_author: str | None = None,
) -> str:
    """Compute engagement signals and return as a JSON string."""
    return json.dumps(
        compute_engagement_signals(assembled, chatbot_username, pr_author)
    )

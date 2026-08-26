"""Helpers for comparing GitHub actor usernames across API sources."""

from __future__ import annotations

# Bots whose review activity comes from a different GitHub account than
# their canonical chatbot username.  Maps normalized alias → canonical name.
_ACTOR_ALIASES: dict[str, str] = {
    "copilot-pull-request-reviewer": "copilot",
    "copilot-swe-agent": "copilot",
}


def normalize_github_actor(username: str | None) -> str:
    """Canonicalize GitHub actor names for equality checks.

    GitHub App bot accounts can appear as either ``name[bot]`` or ``name``
    depending on the API/source object. Treat those forms as the same actor.
    Also resolves known aliases (e.g. copilot-pull-request-reviewer → copilot).
    """
    name = (username or "").strip().lower().removesuffix("[bot]")
    return _ACTOR_ALIASES.get(name, name)


def same_github_actor(left: str | None, right: str | None) -> bool:
    """Return whether two GitHub actor names represent the same account/app."""
    left_norm = normalize_github_actor(left)
    right_norm = normalize_github_actor(right)
    return bool(left_norm and right_norm) and left_norm == right_norm

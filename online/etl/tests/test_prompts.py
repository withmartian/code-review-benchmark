"""Tests for llm/prompts.py: the extraction prompt renders and carries its scope rules."""

from __future__ import annotations

from llm.prompts import EXTRACT_BOT_SUGGESTIONS


def _render() -> str:
    return EXTRACT_BOT_SUGGESTIONS.format(
        bot_username="reviewer[bot]",
        pr_title="Add retry",
        pr_author="dev",
        repo_name="org/repo",
        bot_comments="COMMENT C1 [ISSUE_COMMENT timestamp=2026-07-01T12:00:00Z]\nWalkthrough ...",
        commits_under_review="(no commits)",
    )


def test_extract_bot_suggestions_renders_with_all_placeholders() -> None:
    rendered = _render()

    assert "reviewer[bot]" in rendered
    assert "org/repo" in rendered
    assert "{" not in rendered
    assert "}" not in rendered


def test_extract_bot_suggestions_excludes_pr_overviews_check_status_and_controls() -> None:
    rendered = _render()

    assert "walkthroughs, change summaries" in rendered
    assert "A metric below a threshold is not by itself a finding." in rendered
    assert "names a concrete problem in the changed code" in rendered
    assert "checkboxes, buttons, commands the reader can run" in rendered


def test_extract_bot_suggestions_keeps_concrete_problems_explained_under_a_rating() -> None:
    rendered = _render()

    assert "Effort, risk, or confidence ratings themselves." in rendered
    assert "concrete problem in the changed code, that problem is a finding: extract it." in rendered


def test_extract_bot_suggestions_puts_scope_rules_before_the_inputs() -> None:
    rendered = _render()

    assert rendered.index("Results of automated checks") < rendered.index("=== Bot Review Comments ===")

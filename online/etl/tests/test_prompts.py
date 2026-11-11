"""Change detectors for llm/prompts.py: each test pins the wording of one extraction-scope rule.

They fail whenever that rule's text changes, on purpose: a reviewer then checks that the change is
intended and updates the pinned sentence. Whitespace is collapsed, so rewrapping alone does not fail.
Model behavior is measured separately (test_extraction_scope_live.py and prompt_eval/).
"""

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


def flat(text: str) -> str:
    """Collapse every run of whitespace to one space."""
    return " ".join(text.split())


def _rules() -> str:
    """The rendered prompt up to the inputs, whitespace collapsed."""
    rendered = flat(_render())
    return rendered[: rendered.index("=== Bot Review Comments ===")]


def test_extract_bot_suggestions_renders_with_all_placeholders() -> None:
    rendered = _render()

    assert "reviewer[bot]" in rendered
    assert "org/repo" in rendered
    assert "{" not in rendered
    assert "}" not in rendered


def test_overviews_are_not_findings_but_problems_named_inside_them_are() -> None:
    rules = _rules()

    assert "walkthroughs, change summaries, per-file tables, release notes" in rules
    assert "A concrete problem named inside a summary or walkthrough is still a finding: extract it." in rules


def test_ratings_are_not_findings_but_problems_in_their_explanation_are() -> None:
    rules = _rules()

    assert "Effort, risk, or confidence ratings themselves." in rules
    assert (
        "When the text that explains a rating names a concrete problem in the changed code, "
        "that problem is a finding: extract it, even when no other comment states it." in rules
    )


def test_repository_policy_gates_are_not_findings_on_any_surface() -> None:
    rules = _rules()

    assert "Repository-policy gates:" in rules
    assert "coverage thresholds (for example docstring or test coverage percentages)" in rules
    assert "regardless of which bot posts them or where (summary, review body, or inline comment)" in rules
    assert "A metric below a threshold is not by itself a finding." in rules


def test_documentation_requests_about_specific_code_are_still_findings() -> None:
    rules = _rules()

    assert (
        'A request to add docstrings or tests to "the touched functions" so a coverage threshold is met '
        "is a gate result, wherever it appears." in rules
    )
    assert (
        "A comment on a specific location in the changed code (a named function or file and line) that "
        "asks for documentation of that code, for a reason other than meeting a threshold, is a "
        "documentation finding: extract it." in rules
    )


def test_controls_generation_offers_and_run_metadata_are_not_findings() -> None:
    rules = _rules()

    assert "checkboxes, buttons, commands the reader can run" in rules
    assert "offers to generate code for the author, for example to write docstrings or tests" in rules
    assert "Review status and run metadata: counts of comments posted" in rules


def test_tool_output_listed_as_context_is_not_a_finding_but_tool_results_the_bot_presents_are() -> None:
    rules = _rules()

    assert "Tool output the bot lists as context is not a finding" in rules
    assert "Tool results the bot presents as its own findings" in rules
    assert "are findings." in rules


def test_findings_inside_collapsed_sections_are_still_findings() -> None:
    rules = _rules()

    assert "These rules depend on what a piece of text is, not on where it sits." in rules
    assert "A concrete finding inside a collapsed or nested section" in rules


def test_located_rule_violations_are_findings_only_aggregate_results_are_excluded() -> None:
    rules = _rules()

    assert (
        "Only aggregate results (a status, or a metric against a threshold) are excluded: a violation of a "
        "repository rule or guideline reported at a specific location in the changed code is a finding." in rules
    )


def test_remarks_about_the_pull_request_itself_are_not_findings_but_remarks_about_changed_files_are() -> None:
    rules = _rules()

    assert (
        "Remarks about the pull request itself rather than the changed code: its title, description, "
        "template sections, linked issue or ticket, test plan, or review and merge process." in rules
    )
    assert "even when the bot attaches such a remark to a line of the diff as an inline comment" in rules
    assert (
        "A remark about the content of a file the pull request changes is about the code, even when that "
        "file is documentation or configuration: extract it." in rules
    )


def test_tool_output_quoted_inside_a_bot_comment_supports_that_comment() -> None:
    rules = _rules()

    assert (
        "Tool output quoted inside one of the bot's comments (for example a collapsed tool-results section "
        "under an inline comment) is supporting context for that comment, not a separate finding." in rules
    )


def test_prompt_version_is_stamped_into_the_stored_model_name() -> None:
    from llm.prompts import EXTRACTION_PROMPT_VERSION
    from pipeline.analyze import stamped_model_name

    assert stamped_model_name("some-model") == f"some-model+{EXTRACTION_PROMPT_VERSION}"

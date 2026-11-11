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


def test_inputs_the_bot_lists_and_raw_tool_output_are_not_findings() -> None:
    rules = _rules()

    assert "Inputs the bot lists as having used for its review" in rules
    assert "Do not extract findings from these inputs, even when a listed tool result names a file and line" in rules
    assert (
        "Raw tool output quoted in a comment is not its own finding: only problems the bot states in its "
        "own words are findings." in rules
    )
    assert "A linter or static-analysis result is a finding when the bot writes it up as a problem" in rules


def test_findings_in_review_body_sections_or_collapsed_sections_are_still_findings() -> None:
    rules = _rules()

    assert "These rules depend on what a piece of text is, not on where it sits." in rules
    assert (
        "A concrete finding listed in a review-body section rather than as an inline comment, or inside a "
        "collapsed or nested section, is still a finding: extract it." in rules
    )


def _pinned_inputs() -> dict[str, str]:
    """sha256 of everything an analysis depends on: the three step prompts and the code that renders
    step 1 and step 3 input (whitespace and comments in that code count, on purpose)."""
    import hashlib
    import inspect

    from llm import prompts
    from pipeline import analyze

    texts = {
        name: getattr(prompts, name) for name in ("EXTRACT_BOT_SUGGESTIONS", "EXTRACT_HUMAN_ACTIONS", "JUDGE_MATCHING")
    }
    for fn in ("_clean_bot_comment_body", "_format_bot_comments", "_format_suggestions", "_format_actions"):
        texts[f"pipeline.analyze.{fn}"] = inspect.getsource(getattr(analyze, fn))
    return {name: hashlib.sha256(text.encode()).hexdigest() for name, text in texts.items()}


# What each EXTRACTION_PROMPT_VERSION was released with. Never edit an existing entry: when any pinned
# input changes, bump EXTRACTION_PROMPT_VERSION (in llm/prompts.py and api_service/src/db.rs) and add a
# new entry, so stored analyses and the API's served set can tell the two apart. `analyze --reanalyze`
# re-runs steps 1 and 3 only; a change to EXTRACT_HUMAN_ACTIONS needs fresh analyses as well.
PINNED_INPUTS_BY_VERSION = {
    "extract-v2": {
        "EXTRACT_BOT_SUGGESTIONS": "1ed595f2b0a51171a87ae2ab8c392d1fb9080d9541c1339f6501b84033e5ca6b",
        "EXTRACT_HUMAN_ACTIONS": "a205a85d059f54091b2f16ba6ac768f2ec5dab85dac54ac84656ab54241f755f",
        "JUDGE_MATCHING": "b110fde45775b98940a77ddf699fcc312f32fa4eab0588b1f9da489879f376f8",
        "pipeline.analyze._clean_bot_comment_body": "4d586e1efbdb23031f39f2d91a06f4d211b427bd4701ecb49b9cade11a46df87",
        "pipeline.analyze._format_bot_comments": "246149b41308c2626d38ea943fd6caed474dd4ce1981ab0a1cc7e64eabc770a2",
        "pipeline.analyze._format_suggestions": "ddeb73e2e299f01837ab9fa5a445cb0c0284f9c464be3c7cc7b9ce35fc4d795f",
        "pipeline.analyze._format_actions": "29532725a032aed8001f305444060bad0bae12b4833fc144d8462443a2a6f798",
    },
}


def test_prompt_version_is_pinned_to_every_prompt_and_formatter() -> None:
    from llm.prompts import EXTRACTION_PROMPT_VERSION

    pinned = PINNED_INPUTS_BY_VERSION[EXTRACTION_PROMPT_VERSION]
    current = _pinned_inputs()
    changed = sorted(name for name in current if current[name] != pinned.get(name))
    assert not changed, f"{changed} changed: bump EXTRACTION_PROMPT_VERSION and add a new entry here"


def test_no_two_prompt_versions_share_a_pinned_input_set() -> None:
    sets = [tuple(sorted(pins.items())) for pins in PINNED_INPUTS_BY_VERSION.values()]
    assert len(set(sets)) == len(sets)


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


def test_prompt_version_is_stamped_into_the_stored_model_name() -> None:
    from llm.prompts import EXTRACTION_PROMPT_VERSION
    from pipeline.analyze import stamped_model_name

    assert stamped_model_name("some-model") == f"some-model+{EXTRACTION_PROMPT_VERSION}"


def test_api_service_serves_the_same_prompt_version() -> None:
    from pathlib import Path

    from llm.prompts import EXTRACTION_PROMPT_VERSION

    db_rs = (Path(__file__).parents[2] / "api_service" / "src" / "db.rs").read_text()
    assert f'pub const EXTRACTION_PROMPT_VERSION: &str = "{EXTRACTION_PROMPT_VERSION}";' in db_rs

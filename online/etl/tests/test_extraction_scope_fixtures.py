"""Extraction-scope checks on trimmed real review bodies.

Each fixture under tests/fixtures/extraction_scope/ holds one public github.com PR's events for one
bot (its issue comments, a review body, a few inline comments) in the pipeline's event shape,
trimmed by removing hidden HTML comments and whole sections (image markup, static-analysis
transcripts, agent prompts, suggested code, file lists, table rows past the second). Labels are
verbatim quotes: ``non_findings`` is PR-level content that is not a code-review finding (the
prompt states a rule for some kinds; NO_RULE_KINDS in test_extraction_scope_live.py lists the
others, which the live check does not assert), ``findings`` are review findings it must keep,
including ones inside collapsed sections. Each
label's optional ``pattern`` is used by the opt-in model check in test_extraction_scope_live.py. A
finding label stays asserted whatever its pass rate in that check (failures are disclosed, never
hidden by un-asserting); only overview sentences, which no text pattern separates from findings on
the same code, carry no pattern.

These tests are deterministic. They check that the labelled text reaches the extraction input, that
every labelled kind is mapped to the prompt sentence that decides it (or explicitly to no rule) and
that sentence is in the prompt, and that labels of a kind sit on the side (finding or non-finding)
its sentence gives. They cannot show how the model applies the sentences: that is the opt-in live
check's job.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm.prompts import EXTRACT_BOT_SUGGESTIONS
from pipeline.analyze import _format_bot_comments

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "extraction_scope").glob("*.json"))

# The prompt sentence that decides each labelled kind (compared with whitespace collapsed). Findings
# with no rule of their own fall under the general instruction to extract every substantive finding.
# None: the prompt states no rule for this non-finding kind (NO_RULE_KINDS), so nothing is checked here
# and the live check does not assert it.
_ANY_FINDING = "Your job is to identify every distinct substantive finding the bot flagged."
_SPECIFIC_CHECK = (
    "A check that states a specific problem with the pull request, such as with its title or description, "
    "is not aggregate: it is a finding."
)
RULE_FOR_KIND = {
    "coverage_check": "An aggregate check result is not a finding either: a check whose result is a count",
    "pr_description_check": _SPECIFIC_CHECK,
    "linked_issue_check": None,
    "change_overview": "pure summaries without a concrete finding",
    "rating": None,
    "run_status": "status/progress messages",
    "control": None,
    "generation_offer": None,
    "pr_remark": _SPECIFIC_CHECK,
    "resolved_finding": _ANY_FINDING,
    "dismissed_finding": _ANY_FINDING,
    "product_tip": None,
    "tool_context": "Do not extract findings from these inputs",
    "nested_tool_output": "Tool output quoted as supporting context inside a comment",
    "inline": "Top-level inline review comments generally contain substantive findings",
    "body_finding": "review bodies and issue comments may contain findings or summaries",
    "summary_finding": "pure summaries without a concrete finding",
    "collapsed_nitpick": "inside a collapsed or nested section, is still a finding",
    "collapsed_follow_up": "inside a collapsed or nested section, is still a finding",
    "rating_explanation": _ANY_FINDING,
    "tool_finding": "a linter or static-analysis result the bot writes up as a problem in its own comment text",
    "praise": "Skip generic praise",
    "check_finding": _SPECIFIC_CHECK,
    "located_rule_violation": _ANY_FINDING,
}


def flat(text: str) -> str:
    return " ".join(text.split())


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _render(fx: dict) -> str:
    return EXTRACT_BOT_SUGGESTIONS.format(
        bot_username=fx["bot_username"],
        pr_title="",
        pr_author="unknown",
        repo_name="/".join(fx["source"].split("/")[3:5]),
        bot_comments=_format_bot_comments(fx["events"], fx["bot_username"]),
        commits_under_review="(no commits)",
    )


def test_fixtures_are_public_github_prs_with_both_label_kinds() -> None:
    assert len(FIXTURES) >= 3
    for path in FIXTURES:
        fx = _load(path)
        assert fx["source"].startswith("https://github.com/")
        assert fx["non_findings"]
        assert fx["findings"]


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_labelled_text_reaches_the_extraction_input(path: Path) -> None:
    fx = _load(path)
    formatted = _format_bot_comments(fx["events"], fx["bot_username"])

    for item in fx["findings"] + fx["non_findings"] + fx.get("neutral", []):
        assert item["quote"] in formatted, item


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_prompt_states_a_rule_for_every_labelled_kind_before_the_inputs(path: Path) -> None:
    fx = _load(path)
    rendered = flat(_render(fx))
    rules = rendered[: rendered.index("=== Bot Review Comments ===")]

    for item in fx["findings"] + fx["non_findings"] + fx.get("neutral", []):
        assert item["kind"] in RULE_FOR_KIND, item["kind"]
        if RULE_FOR_KIND[item["kind"]] is not None:
            assert RULE_FOR_KIND[item["kind"]] in rules, item["kind"]


def test_check_labels_sit_on_the_side_the_check_sentences_give() -> None:
    """A check stating a specific problem with the PR (title, description) is a finding; an aggregate
    check result (coverage against a threshold) is not. Both rows can sit in one check table."""
    specific = {kind for kind, rule in RULE_FOR_KIND.items() if rule == _SPECIFIC_CHECK}
    both_in_one_fixture = False
    for path in FIXTURES:
        fx = _load(path)
        finding_kinds = {label["kind"] for label in fx["findings"]}
        non_finding_kinds = {label["kind"] for label in fx["non_findings"]}
        assert not specific & non_finding_kinds, path.stem
        assert "coverage_check" not in finding_kinds, path.stem
        both_in_one_fixture |= "pr_description_check" in finding_kinds and "coverage_check" in non_finding_kinds
    assert both_in_one_fixture  # the scylladb check table: a description check row next to a coverage row


def test_kinds_without_a_rule_are_non_finding_kinds_the_live_check_skips() -> None:
    from tests.test_extraction_scope_live import NO_RULE_KINDS

    assert {kind for kind, rule in RULE_FOR_KIND.items() if rule is None} == NO_RULE_KINDS
    finding_kinds = {label["kind"] for path in FIXTURES for label in _load(path)["findings"]}
    assert not finding_kinds & NO_RULE_KINDS


def test_live_check_flags_status_and_coverage_items_and_a_missing_finding() -> None:
    from tests.test_extraction_scope_live import check_labels

    fx = _load(next(p for p in FIXTURES if "trezor" in p.stem))
    kept = [
        {"description": label["quote"], "file_path": label["path"]} for label in fx["findings"] if label.get("pattern")
    ]
    assert check_labels(fx, kept) == []

    status = {"description": "Actionable comments posted: 3", "file_path": None}
    coverage = {"description": "Docstring coverage is 25% vs the required 80%", "file_path": None}
    problems = check_labels(fx, [*kept[1:], status, coverage])
    assert any(p.startswith("non-finding extracted (run_status)") for p in problems)
    assert any(p.startswith("non-finding extracted (coverage_check)") for p in problems)
    assert any(p.startswith("finding not extracted (inline)") for p in problems)


def test_live_check_skips_non_finding_kinds_the_prompt_states_no_rule_for() -> None:
    from tests.test_extraction_scope_live import check_labels

    fx = _load(next(p for p in FIXTURES if "trezor" in p.stem))
    kept = [
        {"description": label["quote"], "file_path": label["path"]} for label in fx["findings"] if label.get("pattern")
    ]
    rating = {"description": "Estimated review effort: 3 (moderate)", "file_path": None}

    assert check_labels(fx, [*kept, rating]) == []
    problems = check_labels(fx, [*kept, rating], include_unasserted=True)
    assert any(p.startswith("non-finding extracted (rating)") for p in problems)


def test_live_check_reports_a_dropped_check_finding_when_only_inline_findings_are_extracted() -> None:
    from tests.test_extraction_scope_live import check_labels

    fx = _load(next(p for p in FIXTURES if "local-search-mcp" in p.stem))
    inline_bodies = [e["data"] for e in fx["events"] if e["event_type"] == "review_comment"]
    only_inline = [{"description": d["body"], "file_path": d["path"]} for d in inline_bodies]

    problems = check_labels(fx, only_inline, include_unasserted=True)

    assert any(p.startswith("finding not extracted (check_finding)") for p in problems)
    assert not any("(inline)" in p for p in problems)


def _other_finding_text(fx: dict, label: dict) -> str:
    """The whole inline comment holding a finding's quote, else the quote itself."""
    inline = [e["data"]["body"] for e in fx["events"] if e["event_type"] == "review_comment"]
    return next((b for b in inline if label["quote"] in b), label["quote"])


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_label_patterns_match_their_own_quote_and_no_other_finding(path: Path) -> None:
    import re

    fx = _load(path)
    for label in fx["findings"] + fx["non_findings"]:
        if label.get("pattern"):
            assert re.search(label["pattern"], label["quote"]), label["quote"]
    for label in fx["findings"]:
        if not label.get("pattern"):
            continue
        for other in fx["findings"]:
            if other is not label:
                assert not re.search(label["pattern"], _other_finding_text(fx, other)), (label["quote"], other["quote"])


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_every_label_has_a_pattern_or_a_reason_it_is_not_asserted(path: Path) -> None:
    fx = _load(path)
    for label in fx["findings"] + fx["non_findings"]:
        assert label.get("pattern") or label.get("not_asserted"), label["quote"]


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_neutral_labels_say_why_and_are_never_asserted(path: Path) -> None:
    fx = _load(path)
    for label in fx.get("neutral", []):
        assert label.get("reason"), label["quote"]
        assert "pattern" not in label, label["quote"]


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_no_non_finding_pattern_matches_a_finding(path: Path) -> None:
    """A non-finding pattern that also matched a finding would fail the live check on a correct
    extraction of that finding."""
    import re

    fx = _load(path)
    for label in fx["non_findings"]:
        if not label.get("pattern"):
            continue
        for finding in fx["findings"]:
            assert not re.search(label["pattern"], finding["quote"]), (label["kind"], finding["quote"])

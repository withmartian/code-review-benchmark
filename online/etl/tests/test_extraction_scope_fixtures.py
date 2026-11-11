"""Extraction-scope checks on trimmed real review bodies.

Each fixture under tests/fixtures/extraction_scope/ holds one public github.com PR's events for one
bot (its issue comments, a review body, a few inline comments) in the pipeline's event shape,
trimmed by removing hidden HTML comments and whole sections (image markup, static-analysis
transcripts, agent prompts, suggested code, file lists, table rows past the second). Labels are
verbatim quotes: ``non_findings`` is PR-level content the prompt says not to extract,
``findings`` are review findings it must keep, including ones inside collapsed sections. Each
label's optional ``pattern`` is used by the opt-in model check in test_extraction_scope_live.py.

These tests are deterministic: they check that both kinds of text reach the extraction prompt and
that the prompt states a rule for every labelled kind.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm.prompts import EXTRACT_BOT_SUGGESTIONS
from pipeline.analyze import _format_bot_comments

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "extraction_scope").glob("*.json"))

# The prompt sentence that covers each labelled kind (compared with whitespace collapsed).
RULE_FOR_KIND = {
    "coverage_check": "A metric below a threshold is not by itself a finding.",
    "description_check": "such as title, description, linked-issue, or scope checks",
    "change_overview": "walkthroughs, change summaries, per-file tables, release notes",
    "rating": "Effort, risk, or confidence ratings themselves.",
    "run_status": "Review status and run metadata: counts of comments posted",
    "control": "Interactive controls and product text: checkboxes, buttons",
    "generation_offer": '"generate docstrings" or "generate unit tests"',
    "product_tip": "usage tips, links to dashboards",
    "tool_context": "Tool output the bot lists as context is not a finding",
    "inline": "Top-level inline review comments generally contain substantive findings",
    "body_finding": "review bodies and issue comments may contain findings or summaries",
    "summary_finding": "A concrete problem named inside a summary or walkthrough is still a finding",
    "collapsed_nitpick": "A concrete finding inside a collapsed or nested section",
    "collapsed_follow_up": "A concrete finding inside a collapsed or nested section",
    "rating_explanation": "that problem is a finding: extract it, even when no other comment states it.",
    "tool_finding": "Tool results the bot presents as its own findings",
    "praise": "Skip generic praise",
    "check_finding": "Extract a finding from a check result only when it names a concrete problem in the changed code",
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

    for item in fx["findings"] + fx["non_findings"]:
        assert item["quote"] in formatted, item


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_prompt_states_a_rule_for_every_labelled_kind_before_the_inputs(path: Path) -> None:
    fx = _load(path)
    rendered = flat(_render(fx))
    rules = rendered[: rendered.index("=== Bot Review Comments ===")]

    for item in fx["findings"] + fx["non_findings"]:
        assert RULE_FOR_KIND[item["kind"]] in rules, item["kind"]


def test_live_check_flags_a_coverage_item_and_a_missing_finding() -> None:
    from tests.test_extraction_scope_live import check_labels

    fx = _load(next(p for p in FIXTURES if "trezor" in p.stem))
    kept = [
        {"description": label["quote"], "file_path": label["path"]} for label in fx["findings"] if label.get("pattern")
    ]
    assert check_labels(fx, kept) == []

    coverage = {"description": "Docstring coverage is 25% vs the required 80%", "file_path": None}
    problems = check_labels(fx, [*kept[1:], coverage])
    assert any(p.startswith("non-finding extracted (coverage_check)") for p in problems)
    assert any(p.startswith("finding not extracted (inline)") for p in problems)

"""Tests for inline comment severity parsing and noise removal."""

from __future__ import annotations

from code_review_benchmark.parsers.coderabbit import clean_inline_body
from code_review_benchmark.parsers.coderabbit import parse_severity


def test_parse_severity_critical():
    body = "_\u26a0\ufe0f Potential issue_ | _\U0001f534 Critical_\n\nSome issue text"
    assert parse_severity(body) == "critical"


def test_parse_severity_major():
    body = "_\u26a0\ufe0f Potential issue_ | _\U0001f7e0 Major_\n\nSome issue text"
    assert parse_severity(body) == "major"


def test_parse_severity_minor():
    body = "_\u26a0\ufe0f Potential issue_ | _\U0001f7e1 Minor_\n\nSome issue text"
    assert parse_severity(body) == "minor"


def test_parse_severity_none():
    body = "Just a regular comment without severity badges"
    assert parse_severity(body) is None


def test_parse_severity_potential_issue_only():
    """If only the type indicator is present without a severity level, return None."""
    body = "_\u26a0\ufe0f Potential issue_\n\nSome text"
    assert parse_severity(body) is None


def test_clean_inline_removes_analysis_chain():
    body = (
        "_\u26a0\ufe0f Potential issue_ | _\U0001f534 Critical_\n\n"
        "<details>\n<summary>\U0001f9e9 Analysis chain</summary>\n\n"
        "\U0001f3c1 Script executed:\n```shell\nrg -n 'foo'\n```\nLength of output: 500\n\n"
        "</details>\n\n"
        "The actual issue is here."
    )
    cleaned = clean_inline_body(body)
    assert "Analysis chain" not in cleaned
    assert "Script executed" not in cleaned
    assert "rg -n" not in cleaned
    assert "The actual issue is here." in cleaned


def test_clean_inline_removes_ai_agent_prompt():
    body = (
        "Some issue text.\n\n"
        "<details>\n<summary>\U0001f916 Prompt for AI Agents</summary>\n\n"
        "Fix the code by doing X\n\n"
        "</details>\n\n"
        "<!-- fingerprinting:phantom:medusa:ocelot -->"
    )
    cleaned = clean_inline_body(body)
    assert "Prompt for AI Agents" not in cleaned
    assert "fingerprinting" not in cleaned
    assert "Some issue text." in cleaned


def test_clean_inline_removes_html_comments():
    body = "Issue text\n<!-- internal state start -->\nlots of data\n<!-- internal state end -->\nMore text"
    cleaned = clean_inline_body(body)
    assert "internal state" not in cleaned
    assert "Issue text" in cleaned
    assert "More text" in cleaned


def test_clean_inline_preserves_code_suggestions():
    body = "Missing null check.\n\n**Suggested fix:**\n```java\nif (x != null) { ... }\n```"
    cleaned = clean_inline_body(body)
    assert "Missing null check." in cleaned
    assert "```java" in cleaned
    assert "if (x != null)" in cleaned


def test_clean_inline_removes_severity_badge_line():
    body = "_\u26a0\ufe0f Potential issue_ | _\U0001f534 Critical_\n\nThe actual issue description."
    cleaned = clean_inline_body(body)
    assert "_\u26a0\ufe0f Potential issue_" not in cleaned
    assert "The actual issue description." in cleaned


def test_clean_inline_strips_whitespace():
    body = "\n\n  Issue text  \n\n"
    cleaned = clean_inline_body(body)
    assert cleaned == "Issue text"

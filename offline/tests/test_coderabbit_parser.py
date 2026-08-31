"""Tests for the full CodeRabbitParser.parse() method."""

from __future__ import annotations

from code_review_benchmark.parsers.coderabbit import CodeRabbitParser


def test_parse_inline_comment():
    comments = [
        {
            "body": "_\u26a0\ufe0f Potential issue_ | _\U0001f534 Critical_\n\nMissing null check.",
            "path": "src/main.py",
            "line": 42,
            "created_at": "2026-01-26T00:00:00Z",
        }
    ]
    parser = CodeRabbitParser()
    result = parser.parse(comments)
    assert "inline" in result.sections
    assert len(result.sections["inline"]) == 1
    c = result.sections["inline"][0]
    assert c.severity == "critical"
    assert c.path == "src/main.py"
    assert c.line == 42
    assert "Missing null check." in c.body
    # Severity badge should be stripped from body
    assert "_\u26a0\ufe0f Potential issue_" not in c.body


def test_parse_walkthrough_classified():
    comments = [
        {
            "body": "<!-- walkthrough_start -->\n<details><summary>\U0001f4dd Walkthrough</summary>\nStuff\n</details>",
            "path": None,
            "line": None,
            "created_at": "2026-01-26T00:00:00Z",
        }
    ]
    parser = CodeRabbitParser()
    result = parser.parse(comments)
    assert "walkthrough" in result.sections
    assert len(result.sections["walkthrough"]) == 1


def test_parse_status_comment_splits_sections():
    comments = [
        {
            "body": (
                "**Actionable comments posted: 1**\n\n"
                "<details>\n<summary>\U0001f916 Fix all issues with AI agents</summary>\n\nFix stuff\n\n</details>\n\n"
                "<details>\n<summary>\U0001f9f9 Nitpick comments (1)</summary><blockquote>\n\n"
                "<details>\n<summary>a.py (1)</summary><blockquote>\n\n"
                "`10-15`: **Style issue**\n\nUse better names.\n\n"
                "</blockquote></details>\n\n"
                "</blockquote></details>"
            ),
            "path": None,
            "line": None,
            "created_at": "2026-01-26T00:00:00Z",
        }
    ]
    parser = CodeRabbitParser()
    result = parser.parse(comments)
    assert "actionable_summary" in result.sections
    assert "nitpick" in result.sections
    assert result.sections["nitpick"][0].path == "a.py"


def test_parse_mixed_comment_types():
    comments = [
        {
            "body": "_\u26a0\ufe0f Potential issue_ | _\U0001f7e0 Major_\n\nBug in handler.",
            "path": "src/handler.py",
            "line": 10,
            "created_at": "2026-01-26T00:00:00Z",
        },
        {
            "body": "<!-- walkthrough_start -->\nWalkthrough text",
            "path": None,
            "line": None,
            "created_at": "2026-01-26T00:01:00Z",
        },
        {
            "body": "Just a random comment.",
            "path": None,
            "line": None,
            "created_at": "2026-01-26T00:02:00Z",
        },
    ]
    parser = CodeRabbitParser()
    result = parser.parse(comments)
    assert "inline" in result.sections
    assert "walkthrough" in result.sections
    assert "unknown" in result.sections
    assert result.sections["inline"][0].severity == "major"


def test_parse_empty_comments():
    parser = CodeRabbitParser()
    result = parser.parse([])
    assert result.sections == {}


def test_parse_preserves_created_at():
    comments = [
        {
            "body": "Some issue.",
            "path": "a.py",
            "line": 1,
            "created_at": "2026-01-26T16:38:36Z",
        }
    ]
    parser = CodeRabbitParser()
    result = parser.parse(comments)
    assert result.sections["inline"][0].created_at == "2026-01-26T16:38:36Z"

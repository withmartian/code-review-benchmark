"""Tests for parser base dataclasses."""

from __future__ import annotations

from code_review_benchmark.parsers.base import ParsedComment
from code_review_benchmark.parsers.base import ParsedReview


def test_parsed_comment_creation():
    comment = ParsedComment(
        body="Missing null check",
        path="src/main.py",
        line=42,
        created_at="2026-01-26T16:38:36Z",
        section="inline",
        severity="critical",
    )
    assert comment.body == "Missing null check"
    assert comment.path == "src/main.py"
    assert comment.line == 42
    assert comment.section == "inline"
    assert comment.severity == "critical"


def test_parsed_comment_nullable_fields():
    comment = ParsedComment(
        body="General issue",
        path=None,
        line=None,
        created_at="2026-01-26T16:38:36Z",
        section="nitpick",
        severity=None,
    )
    assert comment.path is None
    assert comment.line is None
    assert comment.severity is None


def test_parsed_review_sections():
    inline = ParsedComment(
        body="Issue A",
        path="a.py",
        line=1,
        created_at="2026-01-26T00:00:00Z",
        section="inline",
        severity="major",
    )
    nitpick = ParsedComment(
        body="Style issue",
        path=None,
        line=None,
        created_at="2026-01-26T00:00:00Z",
        section="nitpick",
        severity=None,
    )
    review = ParsedReview(sections={"inline": [inline], "nitpick": [nitpick]})
    assert len(review.sections["inline"]) == 1
    assert len(review.sections["nitpick"]) == 1


def test_parsed_review_to_markdown_inline():
    comment = ParsedComment(
        body="Missing null check on user input",
        path="src/main.py",
        line=42,
        created_at="2026-01-26T00:00:00Z",
        section="inline",
        severity="critical",
    )
    review = ParsedReview(sections={"inline": [comment]})
    md = review.to_markdown()
    assert "## Inline" in md
    assert "src/main.py:42" in md
    assert "[critical]" in md
    assert "Missing null check on user input" in md


def test_parsed_review_to_markdown_no_severity():
    comment = ParsedComment(
        body="Some issue found",
        path="src/util.py",
        line=10,
        created_at="2026-01-26T00:00:00Z",
        section="inline",
        severity=None,
    )
    review = ParsedReview(sections={"inline": [comment]})
    md = review.to_markdown()
    assert "src/util.py:10" in md
    assert "[critical]" not in md
    assert "[major]" not in md


def test_parsed_review_to_markdown_multiple_sections():
    inline = ParsedComment(
        body="Null check missing",
        path="a.py",
        line=1,
        created_at="2026-01-26T00:00:00Z",
        section="inline",
        severity="major",
    )
    outside = ParsedComment(
        body="Race condition in init",
        path="b.py",
        line=10,
        created_at="2026-01-26T00:00:00Z",
        section="outside_diff",
        severity=None,
    )
    review = ParsedReview(sections={"inline": [inline], "outside_diff": [outside]})
    md = review.to_markdown()
    assert "Null check missing" in md
    assert "Race condition in init" in md
    assert "---" in md


def test_parsed_review_to_markdown_empty():
    review = ParsedReview(sections={})
    md = review.to_markdown()
    assert md == ""


def test_parsed_comment_to_dict():
    comment = ParsedComment(
        body="Issue",
        path="a.py",
        line=1,
        created_at="2026-01-26T00:00:00Z",
        section="inline",
        severity="critical",
    )
    d = comment.to_dict()
    assert d == {
        "body": "Issue",
        "path": "a.py",
        "line": 1,
        "created_at": "2026-01-26T00:00:00Z",
        "section": "inline",
        "severity": "critical",
    }

"""Tests for DefaultParser passthrough."""

from __future__ import annotations

from code_review_benchmark.parsers.default import DefaultParser


def test_default_parser_returns_raw_section():
    comments = [
        {"body": "Issue A", "path": "a.py", "line": 1, "created_at": "2026-01-26T00:00:00Z"},
        {"body": "Issue B", "path": None, "line": None, "created_at": "2026-01-26T00:01:00Z"},
    ]
    parser = DefaultParser()
    result = parser.parse(comments)
    assert "raw" in result.sections
    assert len(result.sections["raw"]) == 2
    assert result.sections["raw"][0].body == "Issue A"
    assert result.sections["raw"][0].section == "raw"
    assert result.sections["raw"][1].path is None


def test_default_parser_empty_comments():
    parser = DefaultParser()
    result = parser.parse([])
    assert result.sections == {"raw": []}


def test_default_parser_missing_fields():
    comments = [{"body": "Only body"}]
    parser = DefaultParser()
    result = parser.parse(comments)
    assert len(result.sections["raw"]) == 1
    assert result.sections["raw"][0].path is None
    assert result.sections["raw"][0].line is None
    assert result.sections["raw"][0].created_at == ""

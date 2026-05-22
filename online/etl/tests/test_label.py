"""Tests for label.py pure functions: file list extraction, suggestion summary."""

from __future__ import annotations

import json

from pipeline.label import _extract_file_list
from pipeline.label import _extract_suggestion_summary

# -- _extract_file_list -------------------------------------------------------


class TestExtractFileList:
    def test_none_commit_details(self) -> None:
        assert _extract_file_list({}) == "(no file data)"
        assert _extract_file_list({"commit_details": None}) == "(no file data)"

    def test_empty_commits(self) -> None:
        row = {"commit_details": json.dumps([])}
        assert _extract_file_list(row) == "(no files)"

    def test_single_file(self) -> None:
        details = [{"files": [
            {"filename": "main.py", "status": "modified", "additions": 10, "deletions": 3},
        ]}]
        row = {"commit_details": json.dumps(details)}
        result = _extract_file_list(row)
        assert "main.py" in result
        assert "+10/-3" in result
        assert "modified" in result

    def test_deduplication(self) -> None:
        """Same file across commits should appear only once."""
        details = [
            {"files": [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0}]},
            {"files": [{"filename": "a.py", "status": "modified", "additions": 2, "deletions": 0}]},
        ]
        row = {"commit_details": json.dumps(details)}
        result = _extract_file_list(row)
        assert result.count("a.py") == 1

    def test_multiple_files(self) -> None:
        details = [{"files": [
            {"filename": "a.py", "status": "added", "additions": 5, "deletions": 0},
            {"filename": "b.py", "status": "modified", "additions": 3, "deletions": 1},
        ]}]
        row = {"commit_details": json.dumps(details)}
        result = _extract_file_list(row)
        assert "a.py" in result
        assert "b.py" in result

    def test_already_parsed_list(self) -> None:
        """commit_details can be a list (already parsed, e.g. from Postgres)."""
        details = [{"files": [
            {"filename": "x.py", "status": "modified", "additions": 1, "deletions": 1},
        ]}]
        row = {"commit_details": details}
        result = _extract_file_list(row)
        assert "x.py" in result

    def test_empty_filename_skipped(self) -> None:
        details = [{"files": [
            {"filename": "", "status": "modified", "additions": 0, "deletions": 0},
            {"filename": "real.py", "status": "modified", "additions": 1, "deletions": 0},
        ]}]
        row = {"commit_details": json.dumps(details)}
        result = _extract_file_list(row)
        assert "real.py" in result
        lines = result.strip().split("\n")
        assert len(lines) == 1

    def test_missing_status_defaults(self) -> None:
        details = [{"files": [{"filename": "f.py"}]}]
        row = {"commit_details": json.dumps(details)}
        result = _extract_file_list(row)
        assert "+0/-0" in result

    def test_no_files_key_in_commit(self) -> None:
        details = [{}]
        row = {"commit_details": json.dumps(details)}
        assert _extract_file_list(row) == "(no files)"


# -- _extract_suggestion_summary ---------------------------------------------


class TestExtractSuggestionSummary:
    def test_no_suggestions(self) -> None:
        assert _extract_suggestion_summary({}) == "(no suggestions)"
        assert _extract_suggestion_summary({"bot_suggestions": None}) == "(no suggestions)"

    def test_basic_summary(self) -> None:
        suggestions = [
            {"issue_id": "S1", "category": "bug", "severity": "high"},
            {"issue_id": "S2", "category": "style", "severity": "low"},
        ]
        matches = [
            {"bot_issue_id": "S1", "matched": True},
            {"bot_issue_id": "S2", "matched": False},
        ]
        row = {
            "bot_suggestions": json.dumps(suggestions),
            "matching_results": json.dumps(matches),
        }
        result = _extract_suggestion_summary(row)
        assert "Total suggestions: 2" in result
        assert "matched: 1" in result
        assert "bug(1)" in result
        assert "style(1)" in result
        assert "high(1)" in result
        assert "low(1)" in result

    def test_no_matches(self) -> None:
        suggestions = [{"issue_id": "S1", "category": "bug", "severity": "medium"}]
        row = {"bot_suggestions": json.dumps(suggestions), "matching_results": None}
        result = _extract_suggestion_summary(row)
        assert "matched: 0" in result

    def test_already_parsed(self) -> None:
        """Inputs as lists instead of JSON strings."""
        suggestions = [{"issue_id": "S1", "category": "refactor", "severity": "medium"}]
        matches = [{"bot_issue_id": "S1", "matched": True}]
        row = {"bot_suggestions": suggestions, "matching_results": matches}
        result = _extract_suggestion_summary(row)
        assert "matched: 1" in result

    def test_missing_category_defaults(self) -> None:
        suggestions = [{"issue_id": "S1"}]
        row = {"bot_suggestions": json.dumps(suggestions)}
        result = _extract_suggestion_summary(row)
        assert "other(1)" in result
        assert "medium(1)" in result

    def test_multiple_same_category(self) -> None:
        suggestions = [
            {"issue_id": "S1", "category": "bug", "severity": "high"},
            {"issue_id": "S2", "category": "bug", "severity": "high"},
            {"issue_id": "S3", "category": "bug", "severity": "low"},
        ]
        row = {"bot_suggestions": json.dumps(suggestions)}
        result = _extract_suggestion_summary(row)
        assert "bug(3)" in result
        assert "high(2)" in result
        assert "low(1)" in result

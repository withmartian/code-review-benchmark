"""Tests for step1_5_parse_reviews CLI."""

from __future__ import annotations

import json

from code_review_benchmark import step1_5_parse_reviews as step1_5
from code_review_benchmark.parsers import get_parser


# Helpers ----------------------------------------------------------------

def _coderabbit_defaults() -> dict[str, bool]:
    return get_parser("coderabbit").default_sections()


# Tests ------------------------------------------------------------------

def test_parser_declares_default_sections():
    """Each parser must declare its own default sections."""
    defaults = _coderabbit_defaults()
    assert defaults["inline"] is True
    assert defaults["actionable_summary"] is False
    assert defaults["outside_diff"] is True
    assert defaults["nitpick"] is False
    assert defaults["walkthrough"] is False


def test_default_parser_declares_sections():
    defaults = get_parser("unknown_tool").default_sections()
    assert defaults == {"raw": True}


def test_resolve_sections_defaults():
    defaults = _coderabbit_defaults()
    result = step1_5.resolve_sections(include=None, exclude=None, only=None, defaults=defaults)
    assert result["inline"] is True
    assert result["nitpick"] is False


def test_resolve_sections_include():
    defaults = _coderabbit_defaults()
    result = step1_5.resolve_sections(include=["nitpick"], exclude=None, only=None, defaults=defaults)
    assert result["nitpick"] is True
    assert result["inline"] is True  # default still on


def test_resolve_sections_exclude():
    defaults = _coderabbit_defaults()
    result = step1_5.resolve_sections(include=None, exclude=["outside_diff"], only=None, defaults=defaults)
    assert result["outside_diff"] is False
    assert result["inline"] is True


def test_resolve_sections_only():
    defaults = _coderabbit_defaults()
    result = step1_5.resolve_sections(include=None, exclude=None, only=["inline"], defaults=defaults)
    assert result["inline"] is True
    assert result["actionable_summary"] is False
    assert result["outside_diff"] is False


def test_build_output_for_review():
    """Test building the output structure for a single review."""
    sections_config = {
        "inline": True,
        "actionable_summary": False,
        "nitpick": False,
        "walkthrough": False,
        "pre_merge_checks": False,
        "finishing_touches": False,
        "review_details": False,
        "additional_comments": False,
        "additional_context": False,
        "outside_diff": False,
        "unknown": False,
    }
    comments = [
        {
            "body": "_\u26a0\ufe0f Potential issue_ | _\U0001f534 Critical_\n\nNull check missing.",
            "path": "a.py",
            "line": 10,
            "created_at": "2026-01-26T00:00:00Z",
        },
        {
            "body": "<!-- walkthrough_start -->\nWalkthrough text",
            "path": None,
            "line": None,
            "created_at": "2026-01-26T00:01:00Z",
        },
    ]
    result = step1_5.build_review_output("coderabbit", comments, sections_config)
    assert result["tool"] == "coderabbit"
    # Inline is included
    assert len(result["review_comments"]) == 1
    assert result["review_comments"][0]["section"] == "inline"
    # Walkthrough is excluded
    assert len(result["excluded_comments"]) == 1
    assert result["excluded_comments"][0]["section"] == "walkthrough"
    # Rendered markdown contains only included comments
    assert "Null check missing" in result["rendered_markdown"]
    assert "Walkthrough" not in result["rendered_markdown"]


def test_run_parser_end_to_end(tmp_path, monkeypatch):
    """Integration test: parse benchmark data and produce output file."""
    data = {
        "https://example/pr1": {
            "reviews": [
                {
                    "tool": "coderabbit",
                    "review_comments": [
                        {
                            "body": "_\u26a0\ufe0f Potential issue_ | _\U0001f7e0 Major_\n\nBug found.",
                            "path": "src/main.py",
                            "line": 5,
                            "created_at": "2026-01-26T00:00:00Z",
                        },
                    ],
                },
                {
                    "tool": "claude",
                    "review_comments": [
                        {
                            "body": "Some claude comment",
                            "path": None,
                            "line": None,
                            "created_at": "2026-01-26T00:00:00Z",
                        },
                    ],
                },
            ],
        },
    }
    results_dir = tmp_path
    benchmark_file = results_dir / "benchmark_data.json"
    benchmark_file.write_text(json.dumps(data))

    monkeypatch.setattr(step1_5, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(step1_5, "BENCHMARK_DATA_FILE", benchmark_file)

    defaults = get_parser("coderabbit").default_sections()
    sections_config = step1_5.resolve_sections(include=None, exclude=None, only=None, defaults=defaults)
    step1_5.run_parser("coderabbit", sections_config)

    output_file = results_dir / "parsed_coderabbit.json"
    assert output_file.exists()

    output = json.loads(output_file.read_text())
    assert output["config"]["tool"] == "coderabbit"
    assert "inline" in output["config"]["included_sections"]
    assert "https://example/pr1" in output["reviews"]
    review = output["reviews"]["https://example/pr1"]
    assert review["tool"] == "coderabbit"
    assert len(review["review_comments"]) >= 1
    assert review["review_comments"][0]["section"] == "inline"
    assert "Bug found" in review["rendered_markdown"]

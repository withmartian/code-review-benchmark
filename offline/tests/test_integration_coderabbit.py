"""Integration tests: run CodeRabbitParser on real benchmark data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_review_benchmark.parsers.coderabbit import CodeRabbitParser

BENCHMARK_FILE = Path(__file__).resolve().parents[1] / "results" / "benchmark_data.json"


@pytest.mark.skipif(not BENCHMARK_FILE.exists(), reason="benchmark_data.json not available")
def test_parse_all_coderabbit_reviews():
    """Parse every CodeRabbit review in the benchmark and verify no crashes."""
    with open(BENCHMARK_FILE) as f:
        data = json.load(f)

    parser = CodeRabbitParser()
    total_reviews = 0
    total_inline = 0
    total_nitpick = 0
    total_actionable = 0
    total_outside = 0

    for golden_url, entry in data.items():
        for review in entry.get("reviews", []):
            if review["tool"] != "coderabbit":
                continue
            total_reviews += 1
            comments = review.get("review_comments", [])
            result = parser.parse(comments)

            # Every review should produce some sections
            assert isinstance(result.sections, dict)

            for section_name, parsed_comments in result.sections.items():
                for pc in parsed_comments:
                    # Every parsed comment should have a non-empty body
                    assert pc.body, f"Empty body in {section_name} for {golden_url}"
                    assert pc.section == section_name

            total_inline += len(result.sections.get("inline", []))
            total_nitpick += len(result.sections.get("nitpick", []))
            total_actionable += len(result.sections.get("actionable_summary", []))
            total_outside += len(result.sections.get("outside_diff", []))

    print(f"\nParsed {total_reviews} CodeRabbit reviews:")
    print(f"  Inline comments: {total_inline}")
    print(f"  Nitpick comments: {total_nitpick}")
    print(f"  Actionable summaries: {total_actionable}")
    print(f"  Outside-diff comments: {total_outside}")

    assert total_reviews > 0, "No CodeRabbit reviews found"
    assert total_inline > 0, "Expected some inline comments"


@pytest.mark.skipif(not BENCHMARK_FILE.exists(), reason="benchmark_data.json not available")
def test_to_markdown_produces_clean_output():
    """Verify rendered markdown doesn't contain noise patterns."""
    with open(BENCHMARK_FILE) as f:
        data = json.load(f)

    parser = CodeRabbitParser()
    noise_patterns = [
        "<!-- internal state",
        "<!-- fingerprinting:",
        "\U0001f9e9 Analysis chain",
        "\U0001f916 Prompt for AI Agents",
        "\U0001f3c1 Script executed:",
    ]

    for golden_url, entry in data.items():
        for review in entry.get("reviews", []):
            if review["tool"] != "coderabbit":
                continue
            comments = review.get("review_comments", [])
            result = parser.parse(comments)

            # Only render inline (the section most likely to have noise)
            from code_review_benchmark.parsers.base import ParsedReview

            inline_only = ParsedReview(sections={"inline": result.sections.get("inline", [])})
            md = inline_only.to_markdown()
            for pattern in noise_patterns:
                assert pattern not in md, (
                    f"Noise pattern '{pattern}' found in rendered markdown for {golden_url}"
                )

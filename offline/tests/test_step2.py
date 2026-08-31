"""Tests for step2_extract_comments module."""

from __future__ import annotations

import argparse
import asyncio
import json

from code_review_benchmark import step2_extract_comments as step2


def test_get_all_comment_text():
    comments = [
        {"body": "Inline comment", "path": "code.py", "line": 1},
        {"body": "General one"},
        {"body": "General two", "path": None},
    ]
    # All comments combined with delimiter
    result = step2.get_all_comment_text(comments)
    assert "Inline comment" in result
    assert "General one" in result
    assert "General two" in result
    assert "---" in result  # delimiter present


def test_process_batch_handles_batches():
    async def runner():
        async def sleeper(value):
            return value * 2

        tasks = [sleeper(i) for i in range(5)]
        return await step2.process_batch(tasks, batch_size=2)

    result = asyncio.run(runner())
    assert result == [0, 2, 4, 6, 8]


def test_main_creates_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("MARTIAN_MODEL", "test-model")
    monkeypatch.setenv("MARTIAN_API_KEY", "dummy")

    data = {
        "https://example/pr": {
            "golden_comments": [],
            "reviews": [
                {
                    "tool": "tool-inline",
                    "review_comments": [
                        {"path": "a.py", "line": 1, "body": "Inline issue: missing null check on user input"}
                    ],
                },
                {
                    "tool": "tool-general",
                    "review_comments": [
                        {"body": "General issue: the function does not validate parameters"}
                    ],
                },
            ],
        }
    }

    results_dir = tmp_path
    benchmark_file = results_dir / "benchmark_data.json"
    benchmark_file.write_text(json.dumps(data))

    monkeypatch.setattr(step2, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(step2, "BENCHMARK_DATA_FILE", benchmark_file)
    monkeypatch.setattr(step2, "load_dotenv", lambda: None)

    class DummyExtractor:
        def __init__(self):
            self.calls: list[str] = []

        async def extract_from_comment(self, text: str):
            self.calls.append(text)
            return {"issues": [f"Issue: {text}"]}

    extractor = DummyExtractor()

    monkeypatch.setattr(step2, "CandidateExtractor", lambda: extractor)

    class DummyParser:
        def __init__(self, *_, **__):
            pass

        def add_argument(self, *_, **__):
            return None

        def parse_args(self):
            return SimpleNamespace(tool=None, limit=None, force=False)

    monkeypatch.setattr(argparse, "ArgumentParser", DummyParser)

    asyncio.run(step2.main())

    model_dir = results_dir / "test-model"
    with (model_dir / "candidates.json").open() as fh:
        candidates = json.load(fh)

    assert "https://example/pr" in candidates

    # Both tools now go through LLM extraction
    assert "tool-inline" in candidates["https://example/pr"]
    inline = candidates["https://example/pr"]["tool-inline"]
    assert "missing null check" in inline[0]["text"]
    assert inline[0]["source"] == "extracted"

    assert "tool-general" in candidates["https://example/pr"]
    general = candidates["https://example/pr"]["tool-general"]
    assert "validate parameters" in general[0]["text"]
    assert general[0]["source"] == "extracted"

    # Both reviews should have been processed by LLM
    assert len(extractor.calls) == 2
    assert any("missing null check" in call for call in extractor.calls)
    assert any("validate parameters" in call for call in extractor.calls)


class SimpleNamespace:
    """Lightweight drop-in replacement for argparse Namespace."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_get_comment_text_for_review_uses_parsed_file(tmp_path, monkeypatch):
    """When a parsed file exists, use rendered_markdown from it."""
    monkeypatch.setattr(step2, "RESULTS_DIR", tmp_path)

    parsed = {
        "config": {"tool": "coderabbit"},
        "reviews": {
            "https://example/pr": {
                "tool": "coderabbit",
                "review_comments": [],
                "excluded_comments": [],
                "rendered_markdown": "## Inline comment — a.py:1 [critical]\n\nClean issue text",
            }
        },
    }
    parsed_file = tmp_path / "parsed_coderabbit.json"
    parsed_file.write_text(json.dumps(parsed))

    result = step2.get_comment_text_for_review(
        "https://example/pr",
        "coderabbit",
        [{"body": "Raw noisy comment", "path": None, "line": None}],
    )
    assert result == "## Inline comment — a.py:1 [critical]\n\nClean issue text"


def test_get_comment_text_for_review_fallback(tmp_path, monkeypatch):
    """When no parsed file exists, fall back to raw comments."""
    monkeypatch.setattr(step2, "RESULTS_DIR", tmp_path)

    result = step2.get_comment_text_for_review(
        "https://example/pr",
        "unknown-tool",
        [{"body": "Raw comment A"}, {"body": "Raw comment B"}],
    )
    assert "Raw comment A" in result
    assert "Raw comment B" in result
    assert "---" in result


def test_get_comment_text_for_review_missing_pr(tmp_path, monkeypatch):
    """When parsed file exists but PR not in it, fall back to raw comments."""
    monkeypatch.setattr(step2, "RESULTS_DIR", tmp_path)

    parsed = {
        "config": {"tool": "coderabbit"},
        "reviews": {},
    }
    parsed_file = tmp_path / "parsed_coderabbit.json"
    parsed_file.write_text(json.dumps(parsed))

    result = step2.get_comment_text_for_review(
        "https://example/missing-pr",
        "coderabbit",
        [{"body": "Fallback text"}],
    )
    assert "Fallback text" in result

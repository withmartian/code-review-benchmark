"""Tests for step3_judge_comments module."""

from __future__ import annotations

import argparse
import asyncio
import json

import pytest

from code_review_benchmark import step3_judge_comments as step3


def test_get_candidates_prefers_cached():
    cached = {
        "https://example/pr": {
            "tool-a": [
                {"text": "cached comment"},
                {"text": "/propel review"},
                {"text": "Baz review"},
                {"text": "Bugbot review"},
                {"text": "@greptile"},
            ],
        }
    }
    review = {"tool": "tool-a", "review_comments": [{"body": "raw"}]}

    result = step3.get_candidates(review, cached, "https://example/pr")
    assert result == ["cached comment"]


def test_get_candidates_fallback_to_comments():
    cached = {}
    review = {
        "tool": "tool-b",
        "review_comments": [
            {"body": "first"},
            {"body": "/propel review"},
            {"body": "Baz review"},
            {"body": "Bugbot review"},
            {"body": "@greptile"},
            {"body": "second"},
        ],
    }
    result = step3.get_candidates(review, cached, "https://example/pr")
    assert result == ["first", "second"]


def test_evaluate_review_matches_and_metrics(monkeypatch):
    golden_comments = [
        {"comment": "Issue A", "severity": "High"},
        {"comment": "Issue B", "severity": "Low"},
    ]
    candidates = ["Issue A fix", "Unrelated"]

    responses = [
        {"match": True, "confidence": 0.9, "reasoning": "same"},
        {"match": False, "confidence": 0.1},
        {"match": False, "confidence": 0.2},
        {"match": False, "confidence": 0.3},
    ]

    async def fake_process(tasks):
        results = []
        for task in tasks:
            results.append(await task)
        return results

    monkeypatch.setattr(step3, "process_batch", fake_process)

    class DummyJudge:
        async def match_comment(self, _golden_comment, _candidate):
            return responses.pop(0)

    result = asyncio.run(step3.evaluate_review(DummyJudge(), golden_comments, candidates))

    assert result["tp"] == 1
    assert result["fp"] == 1
    assert result["fn"] == 1
    assert pytest.approx(result["precision"], 0.01) == 0.5
    assert pytest.approx(result["recall"], 0.01) == 0.5
    assert result["true_positives"][0]["golden_comment"] == "Issue A"


def test_evaluate_review_no_candidates():
    class DummyJudge:
        async def match_comment(self, _golden_comment, _candidate):
            return {"match": False}

    result = asyncio.run(step3.evaluate_review(DummyJudge(), [{"comment": "Issue"}], []))
    assert result["tp"] == 0
    assert result["fn"] == 1
    assert result["precision"] == 0.0
    assert result["recall"] == 0.0


def test_evaluate_review_ignores_propel_review_candidate(monkeypatch):
    golden_comments = [{"comment": "Issue A", "severity": "High"}]
    candidates = ["/propel review", "Issue A fix"]

    responses = [
        {"match": True, "confidence": 0.9, "reasoning": "same"},
    ]

    async def fake_process(tasks):
        results = []
        for task in tasks:
            results.append(await task)
        return results

    monkeypatch.setattr(step3, "process_batch", fake_process)

    class DummyJudge:
        async def match_comment(self, _golden_comment, _candidate):
            return responses.pop(0)

    result = asyncio.run(step3.evaluate_review(DummyJudge(), golden_comments, candidates))
    assert result["total_candidates"] == 1
    assert result["fp"] == 0
    assert result["precision"] == 1.0


def test_evaluation_state_roundtrip(tmp_path):
    state = step3.EvaluationState()
    state.mark_done("url", "tool", {"tp": 1, "errors_count": 0})
    assert state.is_done("url", "tool")

    path = tmp_path / "state.json"
    state.save(path)

    loaded = step3.EvaluationState.load(path)
    assert loaded.is_done("url", "tool")


def test_process_batch_respects_batches():
    async def square(x):
        await asyncio.sleep(0)
        return x * x

    result = asyncio.run(step3.process_batch([square(i) for i in range(4)], batch_size=2))
    assert result == [0, 1, 4, 9]


def test_main_writes_evaluations(monkeypatch, tmp_path):
    monkeypatch.setenv("MARTIAN_MODEL", "judge-model")
    monkeypatch.setenv("MARTIAN_API_KEY", "dummy")

    data = {
        "https://example/pr": {
            "golden_comments": [{"comment": "Issue", "severity": "High"}],
            "reviews": [
                {
                    "tool": "tool-a",
                    "repo_name": "repo",
                    "pr_url": "https://github.com/org/repo/pull/1",
                    "review_comments": [{"body": "Issue"}],
                }
            ],
        }
    }

    results_dir = tmp_path
    benchmark_file = results_dir / "benchmark_data.json"
    benchmark_file.write_text(json.dumps(data))

    monkeypatch.setattr(step3, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(step3, "BENCHMARK_DATA_FILE", benchmark_file)
    monkeypatch.setattr(step3, "load_dotenv", lambda: None)

    class DummyJudge:
        def __init__(self, *_args, **_kwargs):
            pass

        async def match_comment(self, _golden_comment, _candidate):
            return {"match": True, "confidence": 0.9, "reasoning": "same"}

    def stub_judge(structured_output=False):  # noqa: ARG001
        return DummyJudge()

    def stub_process(tasks, batch_size=step3.BATCH_SIZE):  # noqa: ARG001
        return asyncio.gather(*tasks)

    monkeypatch.setattr(step3, "LLMJudge", stub_judge)
    monkeypatch.setattr(step3, "process_batch", stub_process)

    class DummyParser:
        def __init__(self, *_, **__):
            pass

        def add_argument(self, *_, **__):
            return None

        def parse_args(self):
            return SimpleNamespace(tool=None, limit=None, force=False, structured=False)

    monkeypatch.setattr(argparse, "ArgumentParser", DummyParser)

    asyncio.run(step3.main())

    model_dir = results_dir / "judge-model"
    evaluations_path = model_dir / "evaluations.json"
    assert evaluations_path.exists()

    with evaluations_path.open() as fh:
        saved = json.load(fh)

    tool_result = saved["https://example/pr"]["tool-a"]
    assert tool_result["tp"] == 1
    assert tool_result["precision"] == 1.0


class SimpleNamespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

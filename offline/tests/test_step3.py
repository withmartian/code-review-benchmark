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
            "tool-a": [{"text": "cached comment"}],
        }
    }
    review = {"tool": "tool-a", "review_comments": [{"body": "raw"}]}

    result = step3.get_candidates(review, cached, "https://example/pr")
    assert result == ["cached comment"]


def test_get_candidates_fallback_to_comments():
    cached = {}
    review = {"tool": "tool-b", "review_comments": [{"body": "first"}, {"body": "second"}]}
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


def _run_with_confidences(monkeypatch, confidences, candidates, dedup_groups=None):
    """Judge every candidate listed in `confidences` as a match for the single golden comment."""

    async def fake_process(tasks):
        return [await task for task in tasks]

    monkeypatch.setattr(step3, "process_batch", fake_process)

    class DummyJudge:
        async def match_comment(self, _golden_comment, candidate):
            if candidate in confidences:
                return {"match": True, "confidence": confidences[candidate], "reasoning": "same"}
            return {"match": False, "confidence": 0.1}

    return asyncio.run(step3.evaluate_review(DummyJudge(), [{"comment": "Issue A"}], candidates, dedup_groups))


@pytest.mark.parametrize("candidates", [["vague", "precise"], ["precise", "vague"]])
def test_evaluate_review_fp_count_independent_of_candidate_order(monkeypatch, candidates):
    """Two candidates matching the same golden: neither is a false positive, in any order."""
    result = _run_with_confidences(monkeypatch, {"precise": 0.9, "vague": 0.7}, [*candidates, "unrelated"])

    assert result["tp"] == 1
    assert result["true_positives"][0]["matched_candidate"] == "precise"
    assert result["false_positives"] == [{"candidate": "unrelated"}]
    assert result["fp"] == 1


def test_evaluate_review_equal_confidence_matches_are_not_false_positives(monkeypatch):
    result = _run_with_confidences(monkeypatch, {"first": 0.9, "second": 0.9}, ["first", "second"])

    assert result["tp"] == 1
    assert result["true_positives"][0]["matched_candidate"] == "first"
    assert result["fp"] == 0


def test_evaluate_review_zero_confidence_match_counts_as_true_positive(monkeypatch):
    result = _run_with_confidences(monkeypatch, {"only": 0}, ["only"])

    assert (result["tp"], result["fp"], result["fn"]) == (1, 0, 0)


def test_evaluate_review_siblings_of_lower_confidence_match_are_not_false_positives(monkeypatch):
    """The dedup sibling of a matched candidate is covered even when that match is not the best one."""
    result = _run_with_confidences(
        monkeypatch,
        {"precise": 0.9, "vague": 0.7},
        ["precise", "vague", "vague restated"],
        dedup_groups=[[0], [1, 2]],
    )

    assert result["tp"] == 1
    assert result["fp"] == 0


def test_evaluate_review_no_candidates():
    class DummyJudge:
        async def match_comment(self, _golden_comment, _candidate):
            return {"match": False}

    result = asyncio.run(step3.evaluate_review(DummyJudge(), [{"comment": "Issue"}], []))
    assert result["tp"] == 0
    assert result["fn"] == 1
    assert result["precision"] == 0.0
    assert result["recall"] == 0.0


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
            return SimpleNamespace(
                tool=None, limit=None, force=False, structured=False,
                evaluations_file=None, dedup_groups=None, no_dedup=False,
            )

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

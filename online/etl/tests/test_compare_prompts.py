"""Tests for prompt_eval/compare_prompts.py (no network, no DB)."""

from __future__ import annotations

import json

from llm.schemas import BotSuggestionsResponse
from llm.schemas import MatchingResponse
from prompt_eval.compare_prompts import leaderboard
from prompt_eval.compare_prompts import metrics
from prompt_eval.compare_prompts import paired_ci
from prompt_eval.compare_prompts import report
from prompt_eval.compare_prompts import score_arm
from prompt_eval.compare_prompts import step1_fields


def _pr_row() -> dict:
    events = [
        {
            "actor": "reviewer[bot]",
            "event_type": "review_comment",
            "timestamp": "2026-07-01T12:00:00Z",
            "data": {"path": "src/app.py", "line": 3, "body": "This can throw when value is null."},
        }
    ]
    return {
        "repo_name": "org/repo",
        "assembled": json.dumps({"events": events, "pr_title": "Add retry", "pr_author": "dev"}),
        "commits": json.dumps([{"sha": "a" * 40, "message": "add retry", "author": "dev", "date": "2026-07-01"}]),
        "reviews": json.dumps([{"author": "reviewer[bot]", "commit_id": "a" * 40}]),
        "commit_details": json.dumps([]),
    }


def test_step1_fields_match_what_analyze_single_pr_formats() -> None:
    fields = step1_fields(_pr_row(), "reviewer[bot]")

    assert fields is not None
    assert fields["repo_name"] == "org/repo"
    assert fields["pr_title"] == "Add retry"
    assert "This can throw when value is null." in fields["bot_comments"]
    assert step1_fields({**_pr_row(), "assembled": None}, "reviewer[bot]") is None


class _StubLLM:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def structured_completion(self, prompt: str, response_model):
        self.prompts.append(prompt)
        if response_model is BotSuggestionsResponse:
            return BotSuggestionsResponse.model_validate(
                {
                    "suggestions": [
                        {"issue_id": "S1", "description": "null deref", "category": "bug", "severity": "high"},
                        {"issue_id": "S2", "description": "coverage 40% < 80%", "category": "other", "severity": "low"},
                    ]
                }
            )
        assert response_model is MatchingResponse
        return MatchingResponse.model_validate(
            {
                "matches": [
                    {
                        "bot_issue_id": "S1",
                        "human_action_id": "A1",
                        "matched": True,
                        "confidence": 0.9,
                        "reasoning": "same",
                    }
                ]
            }
        )


async def test_score_arm_uses_the_template_and_scores_against_the_given_actions() -> None:
    llm = _StubLLM()
    fields = step1_fields(_pr_row(), "reviewer[bot]")
    actions = [{"action_id": "A1", "description": "guard null", "category": "bug", "action_type": "fix"}]

    res = await score_arm(llm, "TEMPLATE {bot_username} {repo_name} {bot_comments}", fields, actions)

    assert llm.prompts[0].startswith("TEMPLATE reviewer[bot] org/repo")
    assert res["total_suggestions"] == 2
    assert res["matched_suggestions"] == 1
    assert res["precision"] == 0.5
    assert res["recall"] == 1.0


def test_metrics_ignore_matches_to_unknown_ids() -> None:
    m = metrics(
        [{"issue_id": "S1"}], [{"action_id": "A1"}], [{"bot_issue_id": "S9", "human_action_id": "A1", "matched": True}]
    )

    assert m["matched_suggestions"] == 0
    assert m["recall"] == 1.0


def test_leaderboard_paired_ci_and_report() -> None:
    rows = []
    for i in range(20):
        for arm, p in (("baseline draw 1", 0.5), ("baseline draw 2", 0.5), ("candidate draw 1", 0.75)):
            rows.append(
                {
                    "arm": arm,
                    "bot": "reviewer[bot]",
                    "pr_url": f"https://github.com/o/r/pull/{i}",
                    "total_suggestions": 4,
                    "matched_suggestions": int(4 * p),
                    "precision": p,
                    "recall": 0.5,
                }
            )
    base = [r for r in rows if r["arm"] == "baseline draw 1"]
    cand = [r for r in rows if r["arm"] == "candidate draw 1"]

    assert leaderboard(base) == (0.5, 0.5, 20)
    lo, hi = paired_ci(list(zip(base, cand, strict=True)), lambda x: leaderboard(x)[0])
    assert lo == hi == 0.25
    table = report(rows)
    assert "### reviewer[bot] (20 PRs)" in table
    assert "| candidate draw 1 | 4.00 | 3.00 | 75.0 | 50.0 | +25.0 [+25.0, +25.0] |" in table

"""prompt_eval/coverage_only.py: sizing analyses whose only suggestions are excluded items."""

from __future__ import annotations

from pathlib import Path

import pytest

from prompt_eval.coverage_only import excluded_kind
from prompt_eval.coverage_only import f_beta
from prompt_eval.coverage_only import is_coverage_item
from prompt_eval.coverage_only import report
from prompt_eval.coverage_only import run
from prompt_eval.coverage_only import summarize

COVERAGE = {"description": "Docstring coverage is 25% vs the required 80%", "file_path": None}
FINDING = {"description": "Null dereference when value is missing", "file_path": "src/app.py"}
RATING = {"description": "Estimated code review effort is moderate (~20 minutes)", "file_path": None}


def test_coverage_items_include_test_coverage_and_located_gates() -> None:
    assert is_coverage_item(COVERAGE)
    assert is_coverage_item({"description": "Test coverage is 61% below the required 80%", "file_path": None})
    assert is_coverage_item({**COVERAGE, "file_path": "src/app.py"})  # a gate posted on a line is still a gate
    assert not is_coverage_item({"description": "Add a docstring explaining the retry", "file_path": "src/a.py"})
    assert not is_coverage_item(FINDING)


def test_other_excluded_kinds_count_only_when_unlocated() -> None:
    assert excluded_kind(RATING) == "rating"
    assert excluded_kind({"description": "Actionable comments posted: 3", "file_path": None}) == "run_status"
    assert excluded_kind({**RATING, "file_path": "src/app.py"}) is None
    assert excluded_kind(FINDING) is None


def test_f_beta_matches_the_api_formula() -> None:
    assert f_beta(0.5, 0.5) == pytest.approx(0.5)
    assert f_beta(0.6, 0.3, beta=2.0) == pytest.approx(5 * 0.6 * 0.3 / (4 * 0.6 + 0.3))
    assert f_beta(0.0, 0.0) is None and f_beta(None, 0.5) is None


def test_excluded_only_prs_leave_every_precision_and_recall_mean() -> None:
    rows = [
        {"precision": 0.0, "recall": 0.0, "bot_suggestions": [COVERAGE]},  # coverage-only, with actions
        {"precision": 0.0, "recall": None, "bot_suggestions": [COVERAGE, RATING]},  # excluded-only, no actions
        {"precision": 0.5, "recall": 0.5, "bot_suggestions": [COVERAGE, FINDING]},
        {"precision": 1.0, "recall": None, "bot_suggestions": [FINDING]},
        {"precision": None, "recall": 0.0, "bot_suggestions": []},
    ]
    s = summarize(rows)

    assert (s["analyses"], s["with_precision"], s["with_both"]) == (5, 4, 2)
    assert (s["excluded_only"], s["coverage_only"], s["excluded_only_with_recall"]) == (2, 1, 1)
    now, without = s["now"], s["without"]
    assert now["daily_precision"] == pytest.approx(1.5 / 4) and without["daily_precision"] == pytest.approx(1.5 / 2)
    assert now["leaderboard_precision"] == pytest.approx(0.25) and without["leaderboard_precision"] == pytest.approx(
        0.5
    )
    assert now["leaderboard_recall"] == pytest.approx(0.25) and without["leaderboard_recall"] == pytest.approx(0.5)
    assert now["daily_recall"] == now["leaderboard_recall"]
    assert now["leaderboard_f"] == pytest.approx(0.25) and without["leaderboard_f"] == pytest.approx(0.5)
    text = report("b", s)
    assert "f  = 2/4 = 50.0% of analyses with a precision" in text
    assert "f' = 1/2 = 50.0% of analyses with both" in text
    assert "leaderboard recall      25.0   50.0" in text


async def test_run_reads_merged_analyses_of_the_bot_from_the_db(tmp_path: Path) -> None:
    from db.connection import DBAdapter
    from db.repository import PRRepository
    from db.schema import create_tables

    url = f"sqlite:///{tmp_path / 'x.db'}"
    db = DBAdapter(url)
    await db.connect()
    await create_tables(db)
    repo = PRRepository(db)
    cid = await repo.upsert_chatbot("coderabbitai[bot]")
    for n, merged, sugg, p in ((1, True, [COVERAGE], 0.0), (2, True, [FINDING], 1.0), (3, False, [COVERAGE], 0.0)):
        await repo.insert_pr(
            chatbot_id=cid, repo_name="o/r", pr_number=n, pr_url=f"https://github.com/o/r/pull/{n}", pr_merged=merged
        )
        pr = await repo.get_pr(cid, "o/r", n)
        await repo.insert_analysis(
            pr_id=pr["id"],
            chatbot_id=cid,
            bot_suggestions=sugg,
            human_actions=[],
            matching_results=[],
            total_bot_comments=1,
            matched_bot_comments=0,
            precision=p,
            recall=None,
            f_beta=None,
            model_name="m",
        )
    await db.close()

    s = await run("coderabbitai[bot]", url)
    assert (s["analyses"], s["excluded_only"]) == (2, 1)  # the unmerged PR is not on the leaderboard

"""prompt_eval/coverage_only.py: counting analyses whose only suggestions are coverage-gate items."""

from __future__ import annotations

from pathlib import Path

from prompt_eval.coverage_only import is_coverage_item
from prompt_eval.coverage_only import report
from prompt_eval.coverage_only import run
from prompt_eval.coverage_only import summarize

COVERAGE = {"description": "Docstring coverage is 25% vs the required 80%", "file_path": None}
FINDING = {"description": "Null dereference when value is missing", "file_path": "src/app.py"}


def test_a_coverage_item_is_an_unlocated_docstring_gate() -> None:
    assert is_coverage_item(COVERAGE)
    assert not is_coverage_item({**COVERAGE, "file_path": "src/app.py"})  # located: a documentation finding
    assert not is_coverage_item({"description": "Add a docstring explaining the retry", "file_path": None})
    assert not is_coverage_item(FINDING)


def test_summary_removes_coverage_only_prs_from_both_means() -> None:
    rows = [
        {"precision": 0.0, "recall": 0.0, "bot_suggestions": [COVERAGE]},  # coverage-only, with recall
        {"precision": 0.0, "recall": None, "bot_suggestions": [COVERAGE]},  # coverage-only, no actions
        {"precision": 0.5, "recall": 0.5, "bot_suggestions": [COVERAGE, FINDING]},
        {"precision": 1.0, "recall": None, "bot_suggestions": [FINDING]},
        {"precision": None, "recall": 0.0, "bot_suggestions": []},
    ]
    s = summarize(rows)

    assert (s["analyses"], s["with_precision"], s["coverage_only"], s["coverage_only_with_recall"]) == (5, 4, 2, 1)
    assert s["daily_mean"] == 1.5 / 4 and s["daily_mean_without"] == 1.5 / 2
    assert s["leaderboard_mean"] == 0.5 / 2 and s["leaderboard_mean_without"] == 0.5
    assert "coverage-only analyses: 2 (50.0% of those with a precision), 1 of them with a recall" in report("b", s)


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
    assert (s["analyses"], s["coverage_only"]) == (2, 1)  # the unmerged PR is not on the leaderboard

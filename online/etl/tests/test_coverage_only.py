"""prompt_eval/coverage_only.py: estimating analyses whose only suggestions are excluded items."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prompt_eval.coverage_only import excluded_kind
from prompt_eval.coverage_only import f_beta
from prompt_eval.coverage_only import is_coverage_item
from prompt_eval.coverage_only import report
from prompt_eval.coverage_only import run
from prompt_eval.coverage_only import summarize

COVERAGE = {"description": "Docstring coverage is 25.00% which is insufficient (required 80%)", "file_path": None}
FINDING = {"description": "Null dereference when value is missing", "file_path": "src/app.py"}
PAUSED = {"description": "Bot review was paused for this user, so no findings were produced", "file_path": None}

FOOFTILLY = json.loads(
    (Path(__file__).parent / "fixtures" / "extraction_scope" / "Fooftilly__local-search-mcp__27.json").read_text()
)
FOOFTILLY_FINDING = next(f["quote"] for f in FOOFTILLY["findings"] if f["kind"] == "check_finding")


@pytest.mark.parametrize(
    ("description", "kind"),
    [
        # the forms the extraction step writes for the fixed gate, rating and status lines (results data)
        ("Docstring coverage check failed (52.63% vs required 80%) for functions touched by the diff.", "coverage"),
        (
            "Docstring coverage check is failing (28% vs required 80%) for touched functions with cyclomatic "
            "complexity >= 2; add missing docstrings to meet the threshold.",
            "coverage",
        ),
        ("Pre-merge check failure: docstring coverage is 0% for functions touched by this diff.", "coverage"),
        ("Docstring coverage is 25.00% which is insufficient", "coverage"),
        ("Test coverage is 61% below the required 80%", "coverage"),
        ("Estimated code review effort: 3 (Moderate) | ~20 minutes", "rating"),
        ("Confidence Score: 4/5", "rating"),
        ("Priority: Normal", "rating"),
        ("Medium impact", "rating"),
        ("Actionable comments posted: 2", "run_status"),
        ("Bot review was paused for this user, so no automated code-review findings were produced.", "run_status"),
        ("Review was skipped.", "run_status"),
        (
            "Codex code review did not complete (review status: Failed), so no substantive findings were produced"
            " in the bot comments.",
            "run_status",
        ),
        ("Bot review comments contained no substantive code-review findings.", "run_status"),
        ("Qodo Merge review was paused for this user.", "run_status"),
        ("Qodo Merge Pro review was paused.", "run_status"),
        ("Copilot reviewed 3 out of 3 changed files in this pull request and generated no comments.", "run_status"),
        ("Copilot reviewed 5 out of 7 changed files and generated 2 comments", "run_status"),
        (
            "Docstring coverage check failed (42.86% vs required 80%) for touched functions; bot requests adding"
            " missing docstrings to meet the threshold.",
            "coverage",
        ),
        ("Offers to generate unit tests (beta)", "generation_offer"),
    ],
)
def test_whole_text_templates_of_fixed_lines_are_excluded(description: str, kind: str) -> None:
    assert excluded_kind({"description": description, "file_path": None}) == kind


@pytest.mark.parametrize(
    "description",
    [
        FOOFTILLY_FINDING,  # names "rate-limit", but is a concrete finding
        "Add test coverage for the required null-input error path in parse().",
        "Add test coverage for the required-field validation in parse_config",
        "The docstring for retry() says the threshold defaults to 5 but the code uses 3",
        "Docstring claims the function returns a value in 0-100% but it returns a fraction",
        "Missing test coverage for the error path; this branch is required for the fallback",
        "The test coverage gate passes even when pytest exits nonzero; propagate the failure.",
        "Generate unit tests for parse_token() to cover expired tokens.",
        "The confidence score returned by classify() is never clamped to [0, 1]",
        "Splitting this PR would reduce review effort; the migration and refactor are unrelated",
        "The PR lowers the test coverage threshold in jest.config.js from 80% to 50%",
        "Test coverage is missing for the retry path, which handles 100% of transient failures",
        "Review was skipped for generated files, but src/gen/handler.py contains hand-written logic with a bug",
        "The CLI's --generate docstrings flag crashes when the output directory does not exist",
        # a gate line followed by a concrete problem: the tail is not a fixed remediation phrase
        "Test coverage is 61% below the required 80%; add a regression test because parse_token() accepts expired"
        " tokens.",
        "Test coverage is 0%. Add tests for the token-expiry path, which silently accepts expired tokens.",
        "Docstring coverage check failed (42.86% vs required 80%); missing input validation in upload handler"
        " allows path traversal.",
        "Docstring coverage is 25% vs 80%; missing null check in parse() causes a crash.",
        "The review failed to flag that retry() never resets its backoff",
        "Copilot reviewed 3 out of 3 changed files but missed the race in worker.py",
        "The Qodo Merge review was paused mid-run, so the retry() bug went unreported",
    ],
)
def test_anything_else_is_a_finding_even_without_a_file(description: str) -> None:
    item = {"description": description, "file_path": None}
    assert excluded_kind(item) is None
    assert not is_coverage_item(item)


def test_a_located_gate_is_left_as_a_finding() -> None:
    assert excluded_kind({**COVERAGE, "file_path": "src/app.py"}) is None


def test_f_beta_matches_the_api_formula() -> None:
    assert f_beta(0.5, 0.5) == pytest.approx(0.5)
    assert f_beta(0.6, 0.3, beta=2.0) == pytest.approx(5 * 0.6 * 0.3 / (4 * 0.6 + 0.3))
    assert f_beta(0.0, 0.0) is None and f_beta(None, 0.5) is None


def test_excluded_only_prs_leave_every_precision_and_recall_mean() -> None:
    rows = [
        {"pr_url": "u1", "precision": 0.0, "recall": 0.0, "bot_suggestions": [COVERAGE]},  # coverage-only, actions
        {"pr_url": "u2", "precision": 0.0, "recall": None, "bot_suggestions": [COVERAGE, PAUSED]},  # no actions
        {"pr_url": "u3", "precision": 0.5, "recall": 0.5, "bot_suggestions": [COVERAGE, FINDING]},
        {"pr_url": "u4", "precision": 1.0, "recall": None, "bot_suggestions": [FINDING]},
        {"pr_url": "u5", "precision": None, "recall": 0.0, "bot_suggestions": []},
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
    text = report("b", s, show=2)
    assert "(an estimate): 2" in text
    assert "f  = 2/4 = 50.0% of analyses with a precision" in text
    assert "f' = 1/2 = 50.0% of analyses with both" in text
    assert "leaderboard recall              25.0   50.0" in text
    assert "  [coverage] u1: Docstring coverage is 25.00%" in text
    assert text.count("\n  [") == 2  # --show 2 prints two classified items


def test_the_cross_bot_caveat_prints_once_unless_the_bot_is_coderabbit(monkeypatch, capsys) -> None:
    import prompt_eval.coverage_only as co

    s = summarize([{"pr_url": "u1", "precision": 0.5, "recall": 0.5, "bot_suggestions": []}])
    every = co.render_all({"a[bot]": s, "b[bot]": s}, caveat=True)
    assert every.startswith(co.CROSS_BOT_CAVEAT) and every.count(co.CROSS_BOT_CAVEAT) == 1
    assert "not comparable across bots" in every and "understated more for the other bots" in every
    assert co.CROSS_BOT_CAVEAT not in co.render_all({"a[bot]": s})
    assert co.render_all({}, caveat=True) == "no analyses in this window"

    async def fake_run(bot, *_):
        return {bot or "a[bot]": s}

    monkeypatch.setattr(co, "run", fake_run)
    for bot, shown in (("all", True), ("qodo-code-review[bot]", True), ("coderabbitai[bot]", False)):
        co.main(["--bot", bot, "--database-url", "sqlite:///unused.db"])
        assert (co.CROSS_BOT_CAVEAT in capsys.readouterr().out) is shown, bot


async def test_run_reads_merged_reviewed_analyses_per_bot_and_window(tmp_path: Path) -> None:
    from db.connection import DBAdapter
    from db.repository import PRRepository
    from db.schema import create_tables

    url = f"sqlite:///{tmp_path / 'x.db'}"
    db = DBAdapter(url)
    await db.connect()
    await create_tables(db)
    repo = PRRepository(db)
    prs = (
        # bot, number, merged, reviewed at, suggestions, precision
        ("coderabbitai[bot]", 1, True, "2026-10-02T00:00:00+00:00", [COVERAGE], 0.0),
        ("coderabbitai[bot]", 2, True, "2026-10-03T00:00:00+00:00", [FINDING], 1.0),
        ("coderabbitai[bot]", 3, False, "2026-10-03T00:00:00+00:00", [COVERAGE], 0.0),  # not merged
        ("coderabbitai[bot]", 4, True, None, [COVERAGE], 0.0),  # no review date: not in the API
        ("coderabbitai[bot]", 5, True, "2026-09-01T00:00:00+00:00", [COVERAGE], 0.0),  # before --since
        ("other[bot]", 6, True, "2026-10-02T00:00:00+00:00", [PAUSED], 0.0),
    )
    for bot, n, merged, reviewed, sugg, p in prs:
        cid = await repo.upsert_chatbot(bot)
        await repo.insert_pr(
            chatbot_id=cid,
            repo_name="o/r",
            pr_number=n,
            pr_url=f"https://github.com/o/r/pull/{n}",
            pr_merged=merged,
            bot_reviewed_at=reviewed,
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

    one = await run("coderabbitai[bot]", url, since="2026-10-01T00:00:00+00:00")
    assert list(one) == ["coderabbitai[bot]"]
    assert (one["coderabbitai[bot]"]["analyses"], one["coderabbitai[bot]"]["excluded_only"]) == (2, 1)
    every = await run(None, url, since="2026-10-01T00:00:00+00:00")
    assert sorted(every) == ["coderabbitai[bot]", "other[bot]"]
    assert every["other[bot]"]["excluded_only"] == 1  # the dropout is not specific to one bot


async def test_run_applies_the_api_presentation_rules(tmp_path: Path) -> None:
    from db.connection import DBAdapter
    from db.repository import PRRepository
    from db.schema import create_tables

    url = f"sqlite:///{tmp_path / 'x.db'}"
    db = DBAdapter(url)
    await db.connect()
    await create_tables(db)
    repo = PRRepository(db)
    prs = (
        # bot, number, diff lines
        ("qodo-code-review[bot]", 1, 100),
        ("qodo-free-for-open-source-projects[bot]", 2, 100),  # shown under qodo-code-review (MERGE_INTO)
        ("qodo-code-review[bot]", 3, 5000),  # above the dashboard's 2000-line default
        ("hidden[bot]", 4, 100),  # in ignored_tools
    )
    for bot, n, lines in prs:
        cid = await repo.upsert_chatbot(bot)
        await repo.insert_pr(
            chatbot_id=cid,
            repo_name="o/r",
            pr_number=n,
            pr_url=f"https://github.com/o/r/pull/{n}",
            pr_merged=True,
            bot_reviewed_at="2026-10-02T12:00:00+00:00",
        )
        pr = await repo.get_pr(cid, "o/r", n)
        await db.execute("UPDATE prs SET diff_lines = $1 WHERE id = $2", (lines, pr["id"]))
        await repo.insert_analysis(
            pr_id=pr["id"],
            chatbot_id=cid,
            bot_suggestions=[FINDING],
            human_actions=[],
            matching_results=[],
            total_bot_comments=1,
            matched_bot_comments=0,
            precision=1.0,
            recall=None,
            f_beta=None,
            model_name="m",
        )
    await db.execute("CREATE TABLE ignored_tools (github_username TEXT)")
    await db.execute("INSERT INTO ignored_tools VALUES ($1)", ("hidden[bot]",))
    await db.close()

    s = await run(None, url)
    assert sorted(s) == ["qodo-code-review[bot]"]
    assert s["qodo-code-review[bot]"]["analyses"] == 2  # the merged account counts, the 5000-line PR does not
    assert (await run("qodo-free-for-open-source-projects[bot]", url))["qodo-code-review[bot]"]["analyses"] == 2
    assert sorted(await run(None, url, include_ignored=True, diff_lines_max=None)) == [
        "hidden[bot]",
        "qodo-code-review[bot]",
    ]


def test_until_includes_the_whole_end_date_as_the_api_does() -> None:
    from prompt_eval.coverage_only import _until_inclusive

    assert _until_inclusive("2026-11-06") == "2026-11-07T00:00:00+00:00"
    assert _until_inclusive(None) is None


async def test_ignored_tools_are_checked_against_the_merged_username(tmp_path: Path) -> None:
    from db.connection import DBAdapter
    from db.repository import PRRepository
    from db.schema import create_tables

    url = f"sqlite:///{tmp_path / 'x.db'}"
    db = DBAdapter(url)
    await db.connect()
    await create_tables(db)
    repo = PRRepository(db)
    cid = await repo.upsert_chatbot("qodo-free-for-open-source-projects[bot]")
    await repo.insert_pr(
        chatbot_id=cid,
        repo_name="o/r",
        pr_number=1,
        pr_url="https://github.com/o/r/pull/1",
        pr_merged=True,
        bot_reviewed_at="2026-10-02T12:00:00+00:00",
    )
    pr = await repo.get_pr(cid, "o/r", 1)
    await repo.insert_analysis(
        pr_id=pr["id"],
        chatbot_id=cid,
        bot_suggestions=[FINDING],
        human_actions=[],
        matching_results=[],
        total_bot_comments=1,
        matched_bot_comments=0,
        precision=1.0,
        recall=None,
        f_beta=None,
        model_name="m",
    )
    await db.execute("CREATE TABLE ignored_tools (github_username TEXT)")
    await db.execute("INSERT INTO ignored_tools VALUES ($1)", ("qodo-code-review[bot]",))
    await db.close()

    # db.rs hides a bot by its effective (merged) name, so the secondary account is hidden with it
    assert await run(None, url) == {}
    assert sorted(await run(None, url, include_ignored=True)) == ["qodo-code-review[bot]"]

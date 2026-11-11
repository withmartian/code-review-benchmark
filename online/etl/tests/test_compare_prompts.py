"""Tests for prompt_eval/compare_prompts.py (no network, no DB)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm.schemas import BotSuggestionsResponse
from llm.schemas import MatchingResponse
from prompt_eval.compare_prompts import experiment_fingerprint
from prompt_eval.compare_prompts import is_selected
from prompt_eval.compare_prompts import leaderboard
from prompt_eval.compare_prompts import metrics
from prompt_eval.compare_prompts import open_output
from prompt_eval.compare_prompts import paired_ci
from prompt_eval.compare_prompts import read_pr_list
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


def test_metrics_keep_the_leaderboard_arithmetic_and_count_invalid_id_pairs() -> None:
    # analyze_single_pr checks each endpoint of a matched pair on its own; the script keeps that
    # so its numbers match the leaderboard, and counts the pairs that name an unknown id.
    suggestions = [{"issue_id": "S1"}, {"issue_id": "S2"}]
    actions = [{"action_id": "A1"}, {"action_id": "A2"}]

    unknown_suggestion = metrics(
        suggestions, actions, [{"bot_issue_id": "S9", "human_action_id": "A1", "matched": True}]
    )
    assert unknown_suggestion["matched_suggestions"] == 0
    assert unknown_suggestion["precision"] == 0.0
    assert unknown_suggestion["recall"] == 0.5  # A1 is still credited, as in analyze_single_pr
    assert unknown_suggestion["invalid_id_matches"] == 1

    unknown_action = metrics(suggestions, actions, [{"bot_issue_id": "S1", "human_action_id": "A9", "matched": True}])
    assert unknown_action["matched_suggestions"] == 1  # S1 is still credited, as in analyze_single_pr
    assert unknown_action["precision"] == 0.5
    assert unknown_action["recall"] == 0.0
    assert unknown_action["invalid_id_matches"] == 1

    valid = metrics(
        suggestions,
        actions,
        [
            {"bot_issue_id": "S1", "human_action_id": "A1", "matched": True},
            {"bot_issue_id": "S9", "human_action_id": "A9", "matched": False},
        ],
    )
    assert valid["invalid_id_matches"] == 0  # unmatched pairs are not counted
    assert (valid["precision"], valid["recall"]) == (0.5, 0.5)


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
    assert "### reviewer[bot]" in table
    assert (
        "| candidate draw 1 | 20 | 4.00 | 3.00 | 75.0 | 50.0 | 20 | +25.0 [+25.0, +25.0] | +0.0 [+0.0, +0.0] | 0 |"
        in table
    )


def test_pr_list_with_a_bot_column_selects_only_the_listed_pairs(tmp_path: Path) -> None:
    csv_path = tmp_path / "prs.csv"
    csv_path.write_text("pr_url,bot\nhttps://github.com/o/r/pull/1,greptile-apps[bot]\n")
    urls, pairs = read_pr_list(str(csv_path))

    # one PR with stored analyses for two bots: only the listed pair is selected
    assert is_selected("greptile-apps[bot]", "https://github.com/o/r/pull/1", urls, pairs)
    assert not is_selected("reviewer[bot]", "https://github.com/o/r/pull/1", urls, pairs)
    assert not is_selected("greptile-apps[bot]", "https://github.com/o/r/pull/2", urls, pairs)


def test_pr_list_without_a_bot_column_selects_every_bot_for_listed_urls(tmp_path: Path) -> None:
    csv_path = tmp_path / "prs.csv"
    csv_path.write_text("pr_url\nhttps://github.com/o/r/pull/1\n")
    urls, pairs = read_pr_list(str(csv_path))

    assert pairs is None
    assert is_selected("reviewer[bot]", "https://github.com/o/r/pull/1", urls, pairs)
    assert not is_selected("reviewer[bot]", "https://github.com/o/r/pull/2", urls, pairs)
    assert is_selected("reviewer[bot]", "https://github.com/o/r/pull/2", None, None)


def _units() -> list[dict]:
    fields = step1_fields(_pr_row(), "reviewer[bot]")
    actions = [{"action_id": "A1", "description": "guard null", "category": "bug", "action_type": "fix"}]
    return [{"bot": "reviewer[bot]", "pr_url": "https://github.com/o/r/pull/1", "fields": fields, "actions": actions}]


def test_fingerprint_changes_with_prompts_model_and_inputs() -> None:
    templates = {"baseline": "A {bot_comments}", "candidate": "B {bot_comments}"}
    model = {"base_url": "", "model_name": "m1"}
    fp = experiment_fingerprint(templates, model, _units())

    assert fp == experiment_fingerprint(dict(templates), dict(model), _units())
    assert fp != experiment_fingerprint({**templates, "candidate": "C {bot_comments}"}, model, _units())
    assert fp != experiment_fingerprint(templates, {**model, "model_name": "m2"}, _units())
    changed = _units()
    changed[0]["actions"][0]["description"] = "something else"
    assert fp != experiment_fingerprint(templates, model, changed)


def test_open_output_resumes_a_compatible_file_and_refuses_a_stale_one(tmp_path: Path) -> None:
    out = tmp_path / "compare.jsonl"
    assert open_output(out, "fp-1") == set()
    with out.open("a") as f:
        f.write(json.dumps({"arm": "baseline draw 1", "bot": "reviewer[bot]", "pr_url": "u1"}) + "\n")

    assert open_output(out, "fp-1") == {("baseline draw 1", "reviewer[bot]", "u1")}
    with pytest.raises(SystemExit, match="pass a new --out path"):
        open_output(out, "fp-2")
    assert report([json.loads(out.read_text().splitlines()[0])]) == ""  # the header is not a row


class _FlakyLLM(_StubLLM):
    """Fails every step 1 call whose prompt starts with FAIL."""

    async def structured_completion(self, prompt: str, response_model):
        if prompt.startswith("FAIL"):
            raise RuntimeError("provider error")
        return await super().structured_completion(prompt, response_model)


async def test_a_failed_job_is_reported_and_not_scored_while_the_others_complete(tmp_path: Path) -> None:
    from prompt_eval.compare_prompts import run_jobs

    out = tmp_path / "compare.jsonl"
    open_output(out, "fp")
    templates = {"baseline": "OK {bot_comments}", "candidate": "FAIL {bot_comments}"}

    failures = await run_jobs(_FlakyLLM(), _units(), templates, 2, set(), out, 4)

    rows = [json.loads(x) for x in out.read_text().splitlines()[1:]]
    assert sorted(r["arm"] for r in rows) == ["baseline draw 1", "baseline draw 2"]
    assert all("error" not in r for r in rows)
    assert sorted(f["arm"] for f in failures) == ["candidate draw 1", "candidate draw 2"]
    # a rerun with the same output retries only the failed jobs
    assert open_output(out, "fp") == {
        ("baseline draw 1", "reviewer[bot]", "https://github.com/o/r/pull/1"),
        ("baseline draw 2", "reviewer[bot]", "https://github.com/o/r/pull/1"),
    }


def test_report_counts_prs_per_arm_and_pairs_deltas_over_prs_scored_in_both() -> None:
    def row(arm: str, i: int, p: float) -> dict:
        return {
            "arm": arm,
            "bot": "b[bot]",
            "pr_url": f"https://github.com/o/r/pull/{i}",
            "total_suggestions": 2,
            "matched_suggestions": int(2 * p),
            "precision": p,
            "recall": 0.5,
        }

    rows = [row("baseline draw 1", i, 0.5) for i in range(10)] + [row("candidate draw 1", i, 1.0) for i in range(6)]
    table = report(rows)

    assert "| baseline draw 1 | 10 |" in table
    assert "| candidate draw 1 | 6 | 2.00 | 2.00 | 100.0 | 50.0 | 6 | +50.0 [+50.0, +50.0] |" in table


def test_every_bot_in_the_packaged_pr_list_resolves_to_a_tracked_bot() -> None:
    from config import DEFAULT_CHATBOT_USERNAMES
    from pipeline.actors import normalize_github_actor

    urls, pairs = read_pr_list(str(Path(__file__).parents[1] / "prompt_eval" / "extraction_scope_prs.csv"))
    tracked = {normalize_github_actor(b) for b in DEFAULT_CHATBOT_USERNAMES}

    assert pairs
    assert {bot for bot, _ in pairs} <= tracked
    # Copilot is listed under its reviewer account but stored as "Copilot": both resolve to one name
    assert is_selected("Copilot", next(u for b, u in pairs if b == "copilot"), urls, pairs)


def test_packaged_results_cover_exactly_the_packaged_pr_list_and_hold_only_metrics() -> None:
    import csv
    import json
    import re

    here = Path(__file__).parents[1] / "prompt_eval"
    with (here / "extraction_scope_prs.csv").open() as f:
        listed = {(r["bot"], r["pr_url"]) for r in csv.DictReader(f)}
    rows = [json.loads(line) for line in (here / "extraction_scope_results.jsonl").open()]
    pr_url = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9]\d*$")
    metrics = {"total_suggestions", "matched_suggestions", "total_actions", "matched_actions", "precision", "recall"}
    arms = {
        "current",
        "current, 2nd draw",
        "earlier revision cf4460fb",
        "earlier revision cf4460fb, 2nd draw",
        "this PR at head, one draw",
    }

    assert all(pr_url.match(r["pr_url"]) for r in rows)
    assert all(set(r) <= {"table", "bot", "pr_url", "arm"} | metrics for r in rows)
    # metrics are numbers only: no extracted text
    assert all(
        r[k] is None or (isinstance(r[k], (int, float)) and not isinstance(r[k], bool))
        for r in rows
        for k in metrics & set(r)
    )
    per_bot = [r for r in rows if r["table"] == "per-bot"]
    assert {(r["bot"], r["pr_url"]) for r in per_bot} == listed
    # every listed PR is scored in all five arms, once each
    assert len(per_bot) == len(arms) * len(listed)
    assert {r["arm"] for r in per_bot} == arms


def test_deltas_never_fall_back_to_another_arm_when_baseline_draw_1_has_no_rows() -> None:
    def row(arm: str, i: int, p: float) -> dict:
        return {
            "arm": arm,
            "bot": "b[bot]",
            "pr_url": f"https://github.com/o/r/pull/{i}",
            "total_suggestions": 4,
            "matched_suggestions": int(4 * p),
            "precision": p,
            "recall": 0.5,
        }

    # every baseline draw 1 job failed; draw 2 and the candidate succeeded
    rows = [row("baseline draw 2", i, 0.5) for i in range(10)] + [row("candidate draw 1", i, 0.75) for i in range(10)]
    table = report(rows)

    assert "No PR has a scored baseline draw 1 row for this bot" in table
    assert "+25.0" not in table
    unavailable = "unavailable: no PR scored in both baseline draw 1 and this arm"
    assert f"| candidate draw 1 | 10 | 4.00 | 3.00 | 75.0 | 50.0 | 0 | {unavailable} | {unavailable} | 0 |" in table
    assert f"| baseline draw 2 | 10 | 4.00 | 2.00 | 50.0 | 50.0 | 0 | {unavailable} | {unavailable} | 0 |" in table


async def test_analyses_with_no_stored_actions_are_kept_and_scored_without_recall(tmp_path: Path, capsys) -> None:
    import argparse

    from config import DBConfig
    from db.connection import DBAdapter
    from db.repository import PRRepository
    from db.schema import create_tables
    from prompt_eval.compare_prompts import _run

    db = DBAdapter(f"sqlite:///{tmp_path / 'x.db'}")
    await db.connect()
    await create_tables(db)
    repo = PRRepository(db)
    cid = await repo.upsert_chatbot("reviewer[bot]")
    row = _pr_row()
    for n, actions in ((1, [{"action_id": "A1", "description": "fix"}]), (2, [])):
        await repo.insert_pr(
            chatbot_id=cid, repo_name="org/repo", pr_number=n, pr_url=f"https://github.com/org/repo/pull/{n}"
        )
        pr = await repo.get_pr(cid, "org/repo", n)
        await repo.update_commits(pr["id"], json.loads(row["commits"]))
        await repo.mark_assembled(pr["id"], json.loads(row["assembled"]))
        await repo.insert_analysis(
            pr_id=pr["id"],
            chatbot_id=cid,
            bot_suggestions=[],
            human_actions=actions,
            matching_results=[],
            total_bot_comments=0,
            matched_bot_comments=0,
            precision=None,
            recall=None,
            f_beta=None,
            model_name="m",
        )
    args = argparse.Namespace(pr_list=None, bots=None, limit_per_bot=None, draws=2, dry_run=True)

    assert await _run(args, DBConfig(), repo) == 0
    await db.close()

    assert "2 analyzed PRs (1 with no stored human actions, so no recall)" in capsys.readouterr().out


async def test_score_arm_with_no_actions_defines_precision_but_not_recall() -> None:
    llm = _StubLLM()
    res = await score_arm(llm, "T {bot_comments}", step1_fields(_pr_row(), "reviewer[bot]"), [])

    assert len(llm.prompts) == 1  # no matching call without actions
    assert res["total_suggestions"] == 2
    assert res["precision"] == 0.0
    assert res["recall"] is None


def test_cli_requires_a_pr_list_or_an_explicit_all_analyzed(capsys) -> None:
    import pytest

    from prompt_eval.compare_prompts import main

    with pytest.raises(SystemExit) as exc:
        main(["--dry-run"])
    assert exc.value.code == 2
    assert "--pr-list, or pass --all-analyzed" in capsys.readouterr().err

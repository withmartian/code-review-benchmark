"""Tests for `analyze --reanalyze`: selecting already-analyzed PRs in a window, counting them with
--dry-run, and the CLI guards. No LLM calls."""

from __future__ import annotations

import argparse
import json
from typing import ClassVar

import pytest
import pytest_asyncio

from config import DBConfig
from db.connection import DBAdapter
from db.repository import PRRepository
from db.schema import create_tables
from main import build_parser
from main import cmd_analyze
from pipeline.analyze import analyze_prs


@pytest_asyncio.fixture
async def db():
    adapter = DBAdapter("sqlite:///:memory:")
    await adapter.connect()
    await create_tables(adapter)
    yield adapter
    await adapter.close()


async def _seed(repo: PRRepository) -> int:
    """Four merged PRs: 100/101/102 analyzed on 4/17, 4/18, 4/19; 103 assembled, not analyzed."""
    cid = await repo.upsert_chatbot("reanalyze[bot]")
    stamps = {
        100: "2026-04-17T10:00:00+00:00",
        101: "2026-04-18T12:00:00+00:00",
        102: "2026-04-19T08:00:00+00:00",
        103: "2026-04-18T15:00:00+00:00",
    }
    for n, ts in stamps.items():
        await repo.insert_pr(
            chatbot_id=cid,
            repo_name="org/repo",
            pr_number=n,
            pr_url=f"https://x/{n}",
            pr_merged=True,
            bot_reviewed_at=ts,
        )
        pr = await repo.get_pr(cid, "org/repo", n)
        await repo.mark_assembled(pr["id"], {"pr_merged": True})
        if n != 103:
            await repo.insert_analysis(
                pr_id=pr["id"],
                chatbot_id=cid,
                bot_suggestions=[],
                human_actions=[],
                matching_results=[],
                total_bot_comments=0,
                matched_bot_comments=0,
                precision=None,
                recall=None,
                f_beta=None,
                model_name="m",
            )
    return cid


@pytest.mark.asyncio
async def test_get_analyzed_prs_selects_analyzed_prs_in_the_window_only(db: DBAdapter) -> None:
    repo = PRRepository(db)
    cid = await _seed(repo)

    rows = await repo.get_analyzed_prs(
        cid, limit=10, since="2026-04-18T00:00:00+00:00", until="2026-04-19T00:00:00+00:00"
    )
    assert {r["pr_number"] for r in rows} == {101}  # 103 is in the window but was never analyzed

    rows = await repo.get_analyzed_prs(cid, limit=10, since="2026-04-17T00:00:00+00:00")
    assert {r["pr_number"] for r in rows} == {100, 101, 102}


@pytest.mark.asyncio
async def test_dry_run_counts_without_calling_the_llm(db: DBAdapter) -> None:
    repo = PRRepository(db)
    cid = await _seed(repo)
    cfg = DBConfig()
    window = {"since": "2026-04-17T00:00:00+00:00", "until": "2026-04-19T00:00:00+00:00"}

    assert await analyze_prs(cfg, db, cid, "reanalyze[bot]", limit=10, reanalyze=True, dry_run=True, **window) == 2
    assert await analyze_prs(cfg, db, cid, "reanalyze[bot]", limit=10, dry_run=True, **window) == 1


def _args(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(["analyze", *argv])


def test_cli_accepts_reanalyze_and_dry_run() -> None:
    args = _args("--all", "--reanalyze", "--since", "2026-10-01", "--until", "2026-11-01", "--dry-run")

    assert args.reanalyze is True
    assert args.dry_run is True


@pytest.mark.asyncio
async def test_cli_refuses_reanalyze_without_since(tmp_path, caplog) -> None:
    db_url = f"sqlite:///{tmp_path / 'x.db'}"
    await cmd_analyze(_args("--all", "--reanalyze", "--dry-run", "--database-url", db_url))

    assert "--reanalyze requires --since" in caplog.text


# -- re-analysis end to end with a stub LLM ---------------------------------------------------

STORED_ACTIONS = [
    {
        "action_id": "A1",
        "description": "guard null value",
        "category": "bug",
        "file_path": "src/app.py",
        "commit_sha": None,
        "action_type": "fix",
    }
]


async def _seed_one(repo: PRRepository, model_name: str) -> tuple[int, int]:
    """One analyzed PR with reviewable events and commits, its analysis made with `model_name`."""
    cid = await repo.upsert_chatbot("reviewer[bot]")
    ts = "2026-04-18T12:00:00+00:00"
    await repo.insert_pr(
        chatbot_id=cid, repo_name="org/repo", pr_number=7, pr_url="https://x/7", pr_merged=True, bot_reviewed_at=ts
    )
    pr = await repo.get_pr(cid, "org/repo", 7)
    await repo.update_commits(pr["id"], [{"sha": "a" * 40, "message": "m", "author": "dev", "date": ts}])
    events = [
        {
            "actor": "reviewer[bot]",
            "event_type": "review_comment",
            "timestamp": ts,
            "data": {"path": "src/app.py", "line": 3, "body": "This can throw when value is null."},
        }
    ]
    await repo.mark_assembled(pr["id"], {"events": events, "pr_title": "t", "pr_author": "dev"})
    await repo.insert_analysis(
        pr_id=pr["id"],
        chatbot_id=cid,
        bot_suggestions=[],
        human_actions=STORED_ACTIONS,
        matching_results=[],
        total_bot_comments=0,
        matched_bot_comments=0,
        precision=None,
        recall=None,
        f_beta=None,
        model_name=model_name,
    )
    await repo.insert_labels(pr["id"], cid, {"language": "python"}, "m")
    return cid, pr["id"]


class _StubLLM:
    calls: ClassVar[list[str]] = []
    fail = False

    def __init__(self, **_: object) -> None:
        pass

    async def structured_completion(self, prompt: str, response_model, temperature: float = 1.0):  # noqa: ARG002
        from llm.schemas import BotSuggestionsResponse
        from llm.schemas import MatchingResponse

        _StubLLM.calls.append(response_model.__name__)
        if _StubLLM.fail:
            import httpx
            from openai import BadRequestError

            req = httpx.Request("POST", "https://llm.invalid/v1/chat/completions")
            raise BadRequestError("bad request", response=httpx.Response(400, request=req), body=None)
        if response_model is BotSuggestionsResponse:
            return BotSuggestionsResponse.model_validate(
                {
                    "suggestions": [
                        {
                            "issue_id": "S1",
                            "description": "null deref",
                            "category": "bug",
                            "file_path": "src/app.py",
                            "line_number": 3,
                            "severity": "high",
                        }
                    ]
                }
            )
        assert response_model is MatchingResponse, response_model
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

    async def close(self) -> None:
        pass


async def _analysis(db: DBAdapter, pr_id: int) -> list[dict]:
    return await db.fetchall("SELECT * FROM llm_analyses WHERE pr_id = $1", (pr_id,))


@pytest.mark.asyncio
async def test_reanalysis_reuses_stored_actions_stamps_upserts_and_clears_labels(db: DBAdapter, monkeypatch) -> None:
    import pipeline.analyze as analyze_mod

    monkeypatch.setattr(analyze_mod, "LLMClient", _StubLLM)
    _StubLLM.calls, _StubLLM.fail = [], False
    repo = PRRepository(db)
    cid, pr_id = await _seed_one(repo, "old-model")
    cfg = DBConfig()
    cfg.martian_model_name = "judge-model"
    window = {"since": "2026-04-18T00:00:00+00:00", "until": "2026-04-19T00:00:00+00:00"}

    assert await analyze_mod.analyze_prs(cfg, db, cid, "reviewer[bot]", limit=10, reanalyze=True, **window) == 1

    assert _StubLLM.calls == ["BotSuggestionsResponse", "MatchingResponse"]  # step 2 not redrawn
    rows = await _analysis(db, pr_id)
    assert len(rows) == 1  # upserted, not duplicated
    assert rows[0]["model_name"] == analyze_mod.stamped_model_name("judge-model")
    assert rows[0]["precision"] == 1.0 and rows[0]["recall"] == 1.0
    assert json.loads(rows[0]["human_actions"]) == STORED_ACTIONS
    assert await db.fetchall("SELECT * FROM pr_labels WHERE pr_id = $1", (pr_id,)) == []
    # stamped now, so a rerun selects nothing
    assert (
        await analyze_mod.analyze_prs(cfg, db, cid, "reviewer[bot]", limit=10, reanalyze=True, dry_run=True, **window)
        == 0
    )


@pytest.mark.asyncio
async def test_a_failed_reanalysis_keeps_the_old_row_and_is_retried(db: DBAdapter, monkeypatch) -> None:
    import pipeline.analyze as analyze_mod

    monkeypatch.setattr(analyze_mod, "LLMClient", _StubLLM)
    _StubLLM.calls, _StubLLM.fail = [], True
    repo = PRRepository(db)
    cid, pr_id = await _seed_one(repo, "old-model")
    cfg = DBConfig()
    cfg.martian_model_name = "judge-model"
    window = {"since": "2026-04-18T00:00:00+00:00"}

    assert await analyze_mod.analyze_prs(cfg, db, cid, "reviewer[bot]", limit=10, reanalyze=True, **window) == 0

    rows = await _analysis(db, pr_id)
    assert [r["model_name"] for r in rows] == ["old-model"]
    assert (await repo.get_pr_by_id(pr_id))["status"] == "analyzed"  # not marked as an error
    assert len(await db.fetchall("SELECT * FROM pr_labels WHERE pr_id = $1", (pr_id,))) == 1
    assert (
        await analyze_mod.analyze_prs(cfg, db, cid, "reviewer[bot]", limit=10, reanalyze=True, dry_run=True, **window)
        == 1
    )


@pytest.mark.asyncio
async def test_reanalysis_warns_when_the_selection_reaches_the_limit(db: DBAdapter, caplog) -> None:
    repo = PRRepository(db)
    cid = await _seed(repo)
    window = {"since": "2026-04-17T00:00:00+00:00"}

    await analyze_prs(DBConfig(), db, cid, "reanalyze[bot]", limit=2, reanalyze=True, dry_run=True, **window)

    assert "reached --limit 2" in caplog.text

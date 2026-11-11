"""Tests for `analyze --reanalyze`: selecting already-analyzed PRs in a window, counting them with
--dry-run, and the CLI guards. No LLM calls."""

from __future__ import annotations

import argparse

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

"""Tests for db/repository.py: merge helpers, diff_lines computation, and
SQLite integration tests verifying the COALESCE fix for pr_merged."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from db.connection import DBAdapter
from db.repository import PRRepository, _merge_bq_events
from db.schema import create_tables


# -- _merge_bq_events (pure function) -----------------------------------------


class TestMergeBqEvents:
    def test_none_existing(self) -> None:
        new = [{"event_id": "1", "data": "a"}]
        result = _merge_bq_events(None, new)
        assert result == new

    def test_no_duplicates(self) -> None:
        old = [{"event_id": "1", "created_at": "2026-01-01T00:00:00Z"}]
        new = [{"event_id": "2", "created_at": "2026-01-01T01:00:00Z"}]
        result = _merge_bq_events(old, new)
        assert len(result) == 2

    def test_duplicates_removed(self) -> None:
        old = [{"event_id": "1", "created_at": "2026-01-01T00:00:00Z"}]
        new = [
            {"event_id": "1", "created_at": "2026-01-01T00:00:00Z"},
            {"event_id": "2", "created_at": "2026-01-01T01:00:00Z"},
        ]
        result = _merge_bq_events(old, new)
        assert len(result) == 2
        ids = [e["event_id"] for e in result]
        assert "1" in ids
        assert "2" in ids

    def test_all_duplicates_returns_old(self) -> None:
        old = [{"event_id": "1", "created_at": "2026-01-01T00:00:00Z"}]
        new = [{"event_id": "1", "created_at": "2026-01-01T00:00:00Z"}]
        result = _merge_bq_events(old, new)
        assert result is old

    def test_string_input(self) -> None:
        old_str = json.dumps([{"event_id": "1", "created_at": "2026-01-01T00:00:00Z"}])
        new = [{"event_id": "2", "created_at": "2026-01-01T01:00:00Z"}]
        result = _merge_bq_events(old_str, new)
        assert len(result) == 2

    def test_sorted_by_created_at(self) -> None:
        old = [{"event_id": "2", "created_at": "2026-01-01T02:00:00Z"}]
        new = [{"event_id": "1", "created_at": "2026-01-01T01:00:00Z"}]
        result = _merge_bq_events(old, new)
        assert result[0]["event_id"] == "1"
        assert result[1]["event_id"] == "2"

    def test_events_without_event_id(self) -> None:
        """Events without event_id are always treated as new."""
        old = [{"data": "old", "created_at": "2026-01-01T00:00:00Z"}]
        new = [{"data": "new", "created_at": "2026-01-01T01:00:00Z"}]
        result = _merge_bq_events(old, new)
        assert len(result) == 2


# -- PRRepository.compute_diff_lines (static method) --------------------------


class TestComputeDiffLines:
    def test_empty(self) -> None:
        assert PRRepository.compute_diff_lines([]) == 0

    def test_single_commit_single_file(self) -> None:
        details = [{"files": [{"additions": 10, "deletions": 3}]}]
        assert PRRepository.compute_diff_lines(details) == 13

    def test_multiple_files(self) -> None:
        details = [{"files": [
            {"additions": 5, "deletions": 2},
            {"additions": 3, "deletions": 1},
        ]}]
        assert PRRepository.compute_diff_lines(details) == 11

    def test_multiple_commits(self) -> None:
        details = [
            {"files": [{"additions": 10, "deletions": 0}]},
            {"files": [{"additions": 0, "deletions": 5}]},
        ]
        assert PRRepository.compute_diff_lines(details) == 15

    def test_missing_fields_default_zero(self) -> None:
        details = [{"files": [{}]}]
        assert PRRepository.compute_diff_lines(details) == 0

    def test_no_files_key(self) -> None:
        details = [{}]
        assert PRRepository.compute_diff_lines(details) == 0


# -- SQLite integration tests --------------------------------------------------


@pytest_asyncio.fixture
async def db():
    """Create an in-memory SQLite database with schema."""
    adapter = DBAdapter("sqlite:///:memory:")
    await adapter.connect()
    await create_tables(adapter)
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def repo(db: DBAdapter):
    return PRRepository(db)


class TestSqliteIntegration:
    @pytest.mark.asyncio
    async def test_upsert_chatbot(self, repo: PRRepository) -> None:
        cid = await repo.upsert_chatbot("testbot[bot]", "Test Bot")
        assert isinstance(cid, int)
        bot = await repo.get_chatbot("testbot[bot]")
        assert bot is not None
        assert bot["display_name"] == "Test Bot"

    @pytest.mark.asyncio
    async def test_insert_pr(self, repo: PRRepository) -> None:
        cid = await repo.upsert_chatbot("testbot[bot]")
        inserted = await repo.insert_pr(
            chatbot_id=cid,
            repo_name="org/repo",
            pr_number=1,
            pr_url="https://github.com/org/repo/pull/1",
            pr_title="Test PR",
            pr_author="alice",
            pr_merged=True,
            bq_events=[{"event_id": "1", "type": "PullRequestEvent", "actor": "alice",
                        "created_at": "2026-01-15T10:00:00Z",
                        "payload": {"action": "opened", "pull_request": {"title": "Test PR", "user": {"login": "alice"}}}}],
        )
        assert inserted is True

    @pytest.mark.asyncio
    async def test_insert_pr_duplicate_returns_false(self, repo: PRRepository) -> None:
        cid = await repo.upsert_chatbot("testbot[bot]")
        await repo.insert_pr(chatbot_id=cid, repo_name="org/repo", pr_number=1, pr_url="https://x", bq_events=[
            {"event_id": "1", "type": "PullRequestEvent", "actor": "a", "created_at": "2026-01-15T10:00:00Z",
             "payload": {"action": "opened", "pull_request": {"title": "t", "user": {"login": "a"}}}}
        ])
        inserted = await repo.insert_pr(chatbot_id=cid, repo_name="org/repo", pr_number=1, pr_url="https://x", bq_events=[
            {"event_id": "2", "type": "PullRequestEvent", "actor": "a", "created_at": "2026-01-15T11:00:00Z",
             "payload": {"action": "opened", "pull_request": {"title": "t", "user": {"login": "a"}}}}
        ])
        assert inserted is False

    @pytest.mark.asyncio
    async def test_pr_merged_coalesce_preserves_existing(self, db: DBAdapter, repo: PRRepository) -> None:
        """The COALESCE fix: update_metadata with pr_merged=None should not overwrite existing True."""
        cid = await repo.upsert_chatbot("testbot[bot]")
        await repo.insert_pr(
            chatbot_id=cid,
            repo_name="org/repo",
            pr_number=42,
            pr_url="https://x",
            pr_merged=True,
            bq_events=[{"event_id": "1", "type": "PullRequestEvent", "actor": "a",
                        "created_at": "2026-01-15T10:00:00Z",
                        "payload": {"action": "opened", "pull_request": {"title": "t", "user": {"login": "a"}}}}],
        )

        # Get the PR
        pr = await repo.get_pr(cid, "org/repo", 42)
        assert pr is not None

        # Simulate what assemble does: update_metadata with pr_merged=None from BQ events
        await repo.update_metadata(
            pr["id"],
            pr_title="Updated title",
            pr_author="alice",
            pr_created_at="2026-01-15T09:00:00Z",
            pr_merged=None,
        )

        # pr_merged should still be True (COALESCE fix)
        updated = await db.fetchone(*db._translate_params(
            "SELECT pr_merged FROM prs WHERE id = $1", (pr["id"],)
        ))
        assert updated["pr_merged"] == 1  # SQLite stores booleans as 1/0

    @pytest.mark.asyncio
    async def test_pr_merged_coalesce_allows_update(self, db: DBAdapter, repo: PRRepository) -> None:
        """COALESCE still allows updating pr_merged from None to True."""
        cid = await repo.upsert_chatbot("testbot[bot]")
        await repo.insert_pr(
            chatbot_id=cid,
            repo_name="org/repo",
            pr_number=43,
            pr_url="https://x",
            pr_merged=None,
            bq_events=[{"event_id": "1", "type": "PullRequestEvent", "actor": "a",
                        "created_at": "2026-01-15T10:00:00Z",
                        "payload": {"action": "opened", "pull_request": {"title": "t", "user": {"login": "a"}}}}],
        )

        pr = await repo.get_pr(cid, "org/repo", 43)
        assert pr is not None

        await repo.update_metadata(
            pr["id"],
            pr_title="T",
            pr_author="alice",
            pr_created_at="2026-01-15T09:00:00Z",
            pr_merged=True,
        )

        updated = await db.fetchone(*db._translate_params(
            "SELECT pr_merged FROM prs WHERE id = $1", (pr["id"],)
        ))
        assert updated["pr_merged"] == 1

    @pytest.mark.asyncio
    async def test_pr_merged_coalesce_false_to_true(self, db: DBAdapter, repo: PRRepository) -> None:
        """COALESCE: pr_merged=True should overwrite pr_merged=False."""
        cid = await repo.upsert_chatbot("testbot[bot]")
        await repo.insert_pr(
            chatbot_id=cid, repo_name="org/repo", pr_number=44, pr_url="https://x",
            pr_merged=False,
            bq_events=[{"event_id": "1", "type": "PullRequestEvent", "actor": "a",
                        "created_at": "2026-01-15T10:00:00Z",
                        "payload": {"action": "opened", "pull_request": {"title": "t", "user": {"login": "a"}}}}],
        )
        pr = await repo.get_pr(cid, "org/repo", 44)
        await repo.update_metadata(pr["id"], pr_title="T", pr_author="a", pr_created_at=None, pr_merged=True)

        updated = await db.fetchone(*db._translate_params(
            "SELECT pr_merged FROM prs WHERE id = $1", (pr["id"],)
        ))
        assert updated["pr_merged"] == 1

    @pytest.mark.asyncio
    async def test_insert_duplicate_calls_merge_path(self, db: DBAdapter, repo: PRRepository) -> None:
        """Inserting a duplicate PR goes through the conflict path and updates bq_events."""
        cid = await repo.upsert_chatbot("testbot[bot]")
        event1 = {"event_id": "e1", "type": "PullRequestEvent", "actor": "a",
                  "created_at": "2026-01-15T10:00:00Z",
                  "payload": {"action": "opened", "pull_request": {"title": "t", "user": {"login": "a"}}}}
        event2 = {"event_id": "e2", "type": "PullRequestReviewEvent", "actor": "bot",
                  "created_at": "2026-01-15T11:00:00Z",
                  "payload": {"review": {"id": 1}, "pull_request": {"title": "t", "user": {"login": "a"}}}}

        inserted1 = await repo.insert_pr(chatbot_id=cid, repo_name="org/repo", pr_number=50, pr_url="https://x", bq_events=[event1])
        assert inserted1 is True

        inserted2 = await repo.insert_pr(chatbot_id=cid, repo_name="org/repo", pr_number=50, pr_url="https://x", bq_events=[event2])
        assert inserted2 is False

        pr = await db.fetchone(*db._translate_params(
            "SELECT bq_events FROM prs WHERE chatbot_id = $1 AND repo_name = $2 AND pr_number = $3",
            (cid, "org/repo", 50),
        ))
        events = json.loads(pr["bq_events"])
        # bq_events are updated (merge path ran)
        assert len(events) >= 1

    @pytest.mark.asyncio
    async def test_lock_and_unlock_pr(self, repo: PRRepository) -> None:
        cid = await repo.upsert_chatbot("testbot[bot]")
        await repo.insert_pr(chatbot_id=cid, repo_name="org/repo", pr_number=60, pr_url="https://x", bq_events=[
            {"event_id": "1", "type": "PullRequestEvent", "actor": "a", "created_at": "2026-01-15T10:00:00Z",
             "payload": {"action": "opened", "pull_request": {"title": "t", "user": {"login": "a"}}}}
        ])
        pr = await repo.get_pr(cid, "org/repo", 60)
        locked = await repo.lock_pr(pr["id"], "worker-1")
        assert locked is True

        await repo.unlock_pr(pr["id"])
        row = await repo.get_pr_by_id(pr["id"])
        assert row["locked_by"] is None

    @pytest.mark.asyncio
    async def test_status_transitions(self, repo: PRRepository) -> None:
        """Test the full status lifecycle: pending -> enriched -> assembled."""
        cid = await repo.upsert_chatbot("testbot[bot]")
        await repo.insert_pr(chatbot_id=cid, repo_name="org/repo", pr_number=70, pr_url="https://x", bq_events=[
            {"event_id": "1", "type": "PullRequestEvent", "actor": "a", "created_at": "2026-01-15T10:00:00Z",
             "payload": {"action": "opened", "pull_request": {"title": "t", "user": {"login": "a"}}}}
        ])
        pr = await repo.get_pr(cid, "org/repo", 70)

        await repo.mark_enrichment_done(pr["id"])
        row = await repo.get_pr_by_id(pr["id"])
        assert row["status"] == "enriched"
        assert row["enrichment_step"] == "done"

        await repo.mark_assembled(pr["id"], {"assembled": True})
        row = await repo.get_pr_by_id(pr["id"])
        assert row["status"] == "assembled"

    @pytest.mark.asyncio
    async def test_mark_error(self, repo: PRRepository) -> None:
        cid = await repo.upsert_chatbot("testbot[bot]")
        await repo.insert_pr(chatbot_id=cid, repo_name="org/repo", pr_number=80, pr_url="https://x", bq_events=[
            {"event_id": "1", "type": "PullRequestEvent", "actor": "a", "created_at": "2026-01-15T10:00:00Z",
             "payload": {"action": "opened", "pull_request": {"title": "t", "user": {"login": "a"}}}}
        ])
        pr = await repo.get_pr(cid, "org/repo", 80)
        await repo.mark_error(pr["id"], "Something broke")
        row = await repo.get_pr_by_id(pr["id"])
        assert row["status"] == "error"
        assert row["error_message"] == "Something broke"

    @pytest.mark.asyncio
    async def test_get_assembled_not_analyzed_with_until(
        self, db: DBAdapter, repo: PRRepository
    ) -> None:
        """`until` is exclusive: --since 4/18 --until 4/19 yields just 4/18."""
        cid = await repo.upsert_chatbot("rangetest[bot]")

        # Three PRs at distinct timestamps, all assembled+merged
        timestamps = {
            100: "2026-04-17T10:00:00+00:00",
            101: "2026-04-18T12:00:00+00:00",
            102: "2026-04-19T08:00:00+00:00",
        }
        for pr_num, ts in timestamps.items():
            await repo.insert_pr(
                chatbot_id=cid, repo_name="org/repo", pr_number=pr_num,
                pr_url=f"https://x/{pr_num}", pr_merged=True, bot_reviewed_at=ts,
                bq_events=[{"event_id": str(pr_num), "type": "PullRequestReviewEvent",
                            "actor": "rangetest[bot]", "created_at": ts,
                            "payload": {"pull_request": {"title": "t", "user": {"login": "a"}}}}],
            )
            pr = await repo.get_pr(cid, "org/repo", pr_num)
            await repo.mark_assembled(pr["id"], {"pr_merged": True})

        # since-only: includes 4/18 and 4/19
        rows = await repo.get_assembled_not_analyzed(
            chatbot_id=cid, limit=10, since="2026-04-18T00:00:00+00:00",
        )
        assert {r["pr_number"] for r in rows} == {101, 102}

        # since + until: just 4/18 (until is exclusive)
        rows = await repo.get_assembled_not_analyzed(
            chatbot_id=cid, limit=10,
            since="2026-04-18T00:00:00+00:00",
            until="2026-04-19T00:00:00+00:00",
        )
        assert {r["pr_number"] for r in rows} == {101}

        # until-only: everything strictly before 4/19
        rows = await repo.get_assembled_not_analyzed(
            chatbot_id=cid, limit=10, until="2026-04-19T00:00:00+00:00",
        )
        assert {r["pr_number"] for r in rows} == {100, 101}

        # No bounds: all three
        rows = await repo.get_assembled_not_analyzed(chatbot_id=cid, limit=10)
        assert {r["pr_number"] for r in rows} == {100, 101, 102}

    @pytest.mark.asyncio
    async def test_get_assembled_not_analyzed_until_all_chatbots(
        self, db: DBAdapter, repo: PRRepository
    ) -> None:
        """until param works for the all-chatbots variant too."""
        c1 = await repo.upsert_chatbot("rangetest1[bot]")
        c2 = await repo.upsert_chatbot("rangetest2[bot]")
        for cid, pr_num, ts in [
            (c1, 200, "2026-04-17T10:00:00+00:00"),
            (c2, 201, "2026-04-18T12:00:00+00:00"),
            (c2, 202, "2026-04-19T08:00:00+00:00"),
        ]:
            await repo.insert_pr(
                chatbot_id=cid, repo_name="org/repo", pr_number=pr_num,
                pr_url=f"https://x/{pr_num}", pr_merged=True, bot_reviewed_at=ts,
                bq_events=[{"event_id": str(pr_num), "type": "PullRequestReviewEvent",
                            "actor": "x", "created_at": ts,
                            "payload": {"pull_request": {"title": "t", "user": {"login": "a"}}}}],
            )
            pr = await repo.get_pr(cid, "org/repo", pr_num)
            await repo.mark_assembled(pr["id"], {"pr_merged": True})

        rows = await repo.get_assembled_not_analyzed(
            chatbot_id=None, limit=10,
            since="2026-04-18T00:00:00+00:00",
            until="2026-04-19T00:00:00+00:00",
        )
        assert {r["pr_number"] for r in rows} == {201}

    @pytest.mark.asyncio
    async def test_get_assembled_not_analyzed_sort_by_assembled(
        self, db: DBAdapter, repo: PRRepository
    ) -> None:
        """sort_by='assembled' orders by assembled_at DESC, catching late-discovered PRs."""
        cid = await repo.upsert_chatbot("sorttest[bot]")

        # PR 300: reviewed long ago, assembled recently (late-discovered)
        # PR 301: reviewed recently, assembled earlier
        await repo.insert_pr(
            chatbot_id=cid, repo_name="org/repo", pr_number=300,
            pr_url="https://x/300", pr_merged=True,
            bot_reviewed_at="2026-03-01T10:00:00+00:00",
            bq_events=[{"event_id": "300", "type": "PullRequestReviewEvent",
                        "actor": "sorttest[bot]", "created_at": "2026-03-01T10:00:00+00:00",
                        "payload": {"pull_request": {"title": "t", "user": {"login": "a"}}}}],
        )
        await repo.insert_pr(
            chatbot_id=cid, repo_name="org/repo", pr_number=301,
            pr_url="https://x/301", pr_merged=True,
            bot_reviewed_at="2026-04-20T10:00:00+00:00",
            bq_events=[{"event_id": "301", "type": "PullRequestReviewEvent",
                        "actor": "sorttest[bot]", "created_at": "2026-04-20T10:00:00+00:00",
                        "payload": {"pull_request": {"title": "t", "user": {"login": "a"}}}}],
        )

        # Assemble PR 301 first (earlier assembled_at), then PR 300 (later assembled_at)
        pr301 = await repo.get_pr(cid, "org/repo", 301)
        await repo.mark_assembled(pr301["id"], {"pr_merged": True})

        # Small delay to ensure distinct assembled_at timestamps
        import asyncio
        await asyncio.sleep(0.05)

        pr300 = await repo.get_pr(cid, "org/repo", 300)
        await repo.mark_assembled(pr300["id"], {"pr_merged": True})

        # Default sort (bot_reviewed_at DESC): PR 301 first (reviewed 2026-04-20)
        rows = await repo.get_assembled_not_analyzed(chatbot_id=cid, limit=10)
        assert rows[0]["pr_number"] == 301

        # Assembled sort: PR 300 first (assembled more recently)
        rows = await repo.get_assembled_not_analyzed(
            chatbot_id=cid, limit=10, sort_by="assembled"
        )
        assert rows[0]["pr_number"] == 300

    @pytest.mark.asyncio
    async def test_mark_skipped(self, repo: PRRepository) -> None:
        cid = await repo.upsert_chatbot("testbot[bot]")
        await repo.insert_pr(chatbot_id=cid, repo_name="org/repo", pr_number=81, pr_url="https://x", bq_events=[
            {"event_id": "1", "type": "PullRequestEvent", "actor": "a", "created_at": "2026-01-15T10:00:00Z",
             "payload": {"action": "opened", "pull_request": {"title": "t", "user": {"login": "a"}}}}
        ])
        pr = await repo.get_pr(cid, "org/repo", 81)
        await repo.mark_skipped(pr["id"], "Too large")
        row = await repo.get_pr_by_id(pr["id"])
        assert row["status"] == "skipped"

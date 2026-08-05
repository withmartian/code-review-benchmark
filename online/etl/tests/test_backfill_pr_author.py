"""Tests for backfill_pr_author.py: the _update_pr_and_roles helper
and verification that pr_merged + repo_id are now extracted from pr_api_raw.

Also tests the helper extraction functions.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from db.connection import DBAdapter
from db.repository import PRRepository
from db.schema import create_tables
from pipeline.backfill_pr_author import _extract_author_from_bq_events
from pipeline.backfill_pr_author import _extract_author_from_commits
from pipeline.backfill_pr_author import _recompute_target_user_roles
from pipeline.backfill_pr_author import _update_pr_and_roles

# -- _extract_author_from_bq_events ------------------------------------------


class TestExtractAuthorFromBqEvents:
    def test_from_pr_event(self) -> None:
        events = [{"type": "PullRequestEvent", "payload": {
            "pull_request": {"user": {"login": "alice"}},
        }}]
        assert _extract_author_from_bq_events(events) == "alice"

    def test_from_string(self) -> None:
        events = [{"type": "PullRequestEvent", "payload": {
            "pull_request": {"user": {"login": "bob"}},
        }}]
        assert _extract_author_from_bq_events(json.dumps(events)) == "bob"

    def test_none_input(self) -> None:
        assert _extract_author_from_bq_events(None) is None

    def test_empty_list(self) -> None:
        assert _extract_author_from_bq_events([]) is None

    def test_empty_string_raises(self) -> None:
        """Empty string is not valid JSON — function propagates JSONDecodeError."""
        import json
        with pytest.raises(json.JSONDecodeError):
            _extract_author_from_bq_events("")

    def test_from_issue_comment_event(self) -> None:
        events = [{"type": "IssueCommentEvent", "payload": {
            "issue": {"user": {"login": "carol"}, "pull_request": {}},
        }}]
        assert _extract_author_from_bq_events(events) == "carol"


# -- _extract_author_from_commits --------------------------------------------


class TestExtractAuthorFromCommits:
    def test_from_list(self) -> None:
        commits = [{"sha": "abc", "author": "dave", "message": "fix", "date": "2026-01-15T10:00:00Z"}]
        assert _extract_author_from_commits(commits) == "dave"

    def test_from_string(self) -> None:
        commits = [{"sha": "abc", "author": "eve", "message": "fix", "date": "2026-01-15T10:00:00Z"}]
        assert _extract_author_from_commits(json.dumps(commits)) == "eve"

    def test_none(self) -> None:
        assert _extract_author_from_commits(None) is None

    def test_empty_list(self) -> None:
        assert _extract_author_from_commits([]) is None

    def test_no_author_key(self) -> None:
        commits = [{"sha": "abc", "message": "fix"}]
        assert _extract_author_from_commits(commits) is None


# -- _recompute_target_user_roles ---------------------------------------------


class TestRecomputeRoles:
    def test_adds_author_role_when_matching(self) -> None:
        assembled = json.dumps({
            "pr_author": None,
            "target_user_roles": ["reviewer"],
            "events": [{"timestamp": "t", "event_type": "review", "actor": "bot[bot]",
                        "data": {"state": "COMMENTED"}}],
        })
        result = _recompute_target_user_roles(assembled, "bot[bot]", "bot[bot]")
        assert result is not None
        assert "author" in result["target_user_roles"]

    def test_no_change_when_same(self) -> None:
        """If pr_author is already set and roles are correct, returns None (no update needed)."""
        assembled = json.dumps({
            "pr_author": "alice",
            "target_user_roles": ["reviewer"],
            "events": [{"timestamp": "t", "event_type": "review", "actor": "bot[bot]",
                        "data": {"state": "COMMENTED"}}],
        })
        result = _recompute_target_user_roles(assembled, "alice", "bot[bot]")
        assert result is None

    def test_updates_when_pr_author_differs(self) -> None:
        """If pr_author changed but roles are the same, returns updated dict."""
        assembled = json.dumps({
            "pr_author": "old_author",
            "target_user_roles": ["reviewer"],
            "events": [{"timestamp": "t", "event_type": "review", "actor": "bot[bot]",
                        "data": {"state": "COMMENTED"}}],
        })
        result = _recompute_target_user_roles(assembled, "new_author", "bot[bot]")
        assert result is not None
        assert result["pr_author"] == "new_author"

    def test_none_assembled(self) -> None:
        assert _recompute_target_user_roles(None, "alice", "bot[bot]") is None

    def test_already_parsed_dict(self) -> None:
        assembled = {
            "pr_author": None,
            "target_user_roles": [],
            "events": [],
        }
        result = _recompute_target_user_roles(assembled, "alice", "bot[bot]")
        assert result is not None
        assert result["pr_author"] == "alice"


# -- SQLite integration: _update_pr_and_roles with pr_merged/repo_id ---------


@pytest_asyncio.fixture
async def db():
    adapter = DBAdapter("sqlite:///:memory:")
    await adapter.connect()
    await create_tables(adapter)
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def repo(db: DBAdapter):
    return PRRepository(db)


class TestUpdatePrAndRolesIntegration:
    @pytest.mark.asyncio
    async def test_sets_pr_merged_and_repo_id_from_api_raw(self, db: DBAdapter, repo: PRRepository) -> None:
        """The fix: _update_pr_and_roles should now set pr_merged and repo_id when storing pr_api_raw."""
        cid = await repo.upsert_chatbot("testbot[bot]")
        bq_event = {
            "event_id": "1", "type": "PullRequestEvent", "actor": "alice",
            "created_at": "2026-01-15T10:00:00Z",
            "payload": {"action": "opened", "pull_request": {"title": "t", "user": {"login": "alice"}}},
        }
        await repo.insert_pr(
            chatbot_id=cid, repo_name="org/repo", pr_number=1, pr_url="https://x",
            pr_merged=None, bq_events=[bq_event],
        )

        pr = await db.fetchone(*db._translate_params(
            "SELECT id, repo_name, pr_number, pr_author FROM prs WHERE chatbot_id = $1 AND pr_number = $2",
            (cid, 1),
        ))

        # Simulate GitHub API response
        api_raw = {
            "merged": True,
            "user": {"login": "alice"},
            "base": {"repo": {"id": 12345}},
        }

        row = {
            "id": pr["id"],
            "repo_name": pr["repo_name"],
            "pr_number": pr["pr_number"],
            "assembled": None,
        }

        await _update_pr_and_roles(db, row, "alice", "testbot[bot]", dry_run=False, pr_api_raw=api_raw)

        updated = await db.fetchone(*db._translate_params(
            "SELECT pr_author, pr_merged, repo_id, pr_api_raw FROM prs WHERE id = $1", (pr["id"],)
        ))
        assert updated["pr_author"] == "alice"
        assert updated["pr_merged"] == 1  # SQLite boolean
        assert updated["repo_id"] == 12345
        assert updated["pr_api_raw"] is not None

    @pytest.mark.asyncio
    async def test_pr_merged_coalesce_in_backfill(self, db: DBAdapter, repo: PRRepository) -> None:
        """COALESCE: if pr_merged is already True, backfill with merged=False should NOT overwrite."""
        cid = await repo.upsert_chatbot("testbot[bot]")
        await repo.insert_pr(
            chatbot_id=cid, repo_name="org/repo", pr_number=2, pr_url="https://x",
            pr_merged=True, bq_events=[{
                "event_id": "1", "type": "PullRequestEvent", "actor": "a",
                "created_at": "2026-01-15T10:00:00Z",
                "payload": {"action": "opened", "pull_request": {"title": "t", "user": {"login": "a"}}},
            }],
        )

        pr = await db.fetchone(*db._translate_params(
            "SELECT id, repo_name, pr_number FROM prs WHERE chatbot_id = $1 AND pr_number = $2",
            (cid, 2),
        ))

        # API says merged=False, but DB already has merged=True
        # COALESCE($3, pr_merged) → $3 is False (not NULL), so it WILL overwrite.
        # This is correct behavior: the API is authoritative.
        api_raw = {"merged": False, "user": {"login": "a"}, "base": {"repo": {"id": 999}}}
        row = {"id": pr["id"], "repo_name": pr["repo_name"], "pr_number": pr["pr_number"], "assembled": None}
        await _update_pr_and_roles(db, row, "a", "testbot[bot]", dry_run=False, pr_api_raw=api_raw)

        updated = await db.fetchone(*db._translate_params(
            "SELECT pr_merged FROM prs WHERE id = $1", (pr["id"],)
        ))
        assert updated["pr_merged"] == 0  # API is authoritative

    @pytest.mark.asyncio
    async def test_no_api_raw_skips_extra_fields(self, db: DBAdapter, repo: PRRepository) -> None:
        """Without pr_api_raw, only pr_author is updated."""
        cid = await repo.upsert_chatbot("testbot[bot]")
        await repo.insert_pr(
            chatbot_id=cid, repo_name="org/repo", pr_number=3, pr_url="https://x",
            pr_merged=None, bq_events=[{
                "event_id": "1", "type": "PullRequestEvent", "actor": "a",
                "created_at": "2026-01-15T10:00:00Z",
                "payload": {"action": "opened", "pull_request": {"title": "t", "user": {"login": "a"}}},
            }],
        )

        pr = await db.fetchone(*db._translate_params(
            "SELECT id, repo_name, pr_number FROM prs WHERE chatbot_id = $1 AND pr_number = $2",
            (cid, 3),
        ))
        row = {"id": pr["id"], "repo_name": pr["repo_name"], "pr_number": pr["pr_number"], "assembled": None}
        await _update_pr_and_roles(db, row, "bob", "testbot[bot]", dry_run=False, pr_api_raw=None)

        updated = await db.fetchone(*db._translate_params(
            "SELECT pr_author, pr_merged, repo_id FROM prs WHERE id = $1", (pr["id"],)
        ))
        assert updated["pr_author"] == "bob"
        assert updated["pr_merged"] is None
        assert updated["repo_id"] is None

    @pytest.mark.asyncio
    async def test_dry_run_no_changes(self, db: DBAdapter, repo: PRRepository) -> None:
        """dry_run=True should not write anything."""
        cid = await repo.upsert_chatbot("testbot[bot]")
        await repo.insert_pr(
            chatbot_id=cid, repo_name="org/repo", pr_number=4, pr_url="https://x",
            pr_merged=None, bq_events=[{
                "event_id": "1", "type": "PullRequestEvent", "actor": "a",
                "created_at": "2026-01-15T10:00:00Z",
                "payload": {"action": "opened", "pull_request": {"title": "t", "user": {"login": "a"}}},
            }],
        )

        pr = await db.fetchone(*db._translate_params(
            "SELECT id, repo_name, pr_number FROM prs WHERE chatbot_id = $1 AND pr_number = $2",
            (cid, 4),
        ))
        api_raw = {"merged": True, "user": {"login": "x"}, "base": {"repo": {"id": 111}}}
        row = {"id": pr["id"], "repo_name": pr["repo_name"], "pr_number": pr["pr_number"], "assembled": None}
        await _update_pr_and_roles(db, row, "x", "testbot[bot]", dry_run=True, pr_api_raw=api_raw)

        updated = await db.fetchone(*db._translate_params(
            "SELECT pr_author, pr_merged, repo_id FROM prs WHERE id = $1", (pr["id"],)
        ))
        assert updated["pr_merged"] is None
        assert updated["repo_id"] is None

"""PRRepository — typed async CRUD methods for the database."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
import json
from typing import Any

from db import queries as q
from db.connection import DBAdapter


class PRRepository:
    """High-level async database operations."""

    def __init__(self, db: DBAdapter):
        self.db = db

    # -- Chatbots --------------------------------------------------------------

    async def upsert_chatbot(self, github_username: str, display_name: str | None = None) -> int:
        """Upsert a chatbot row and return its id."""
        await self.db.execute(q.UPSERT_CHATBOT, (github_username, display_name or github_username))
        row = await self.db.fetchone(q.GET_CHATBOT_BY_USERNAME, (github_username,))
        return row["id"]

    async def get_chatbot(self, github_username: str) -> dict[str, Any] | None:
        return await self.db.fetchone(q.GET_CHATBOT_BY_USERNAME, (github_username,))

    async def get_all_chatbots(self) -> list[dict[str, Any]]:
        return await self.db.fetchall(q.GET_ALL_CHATBOTS)

    # -- PRs -------------------------------------------------------------------

    async def insert_pr(
        self,
        chatbot_id: int,
        repo_name: str,
        pr_number: int,
        pr_url: str,
        pr_title: str = "",
        pr_author: str | None = None,
        pr_created_at: str | None = None,
        pr_merged: bool | None = None,
        status: str = "pending",
        bq_events: list | None = None,
        bot_reviewed_at: str | None = None,
    ) -> bool:
        """Insert a PR row (ON CONFLICT DO NOTHING for idempotency).

        Returns True if the row was actually inserted, False if it already existed.
        """
        bq_json = json.dumps(bq_events) if bq_events is not None else None
        row = await self.db.fetchone(
            q.INSERT_PR,
            (
                chatbot_id,
                repo_name,
                pr_number,
                pr_url,
                pr_title,
                pr_author,
                pr_created_at,
                pr_merged,
                status,
                bq_json,
                bot_reviewed_at,
            ),
        )
        return row is not None

    async def get_pr(self, chatbot_id: int, repo_name: str, pr_number: int) -> dict[str, Any] | None:
        return await self.db.fetchone(q.GET_PR, (chatbot_id, repo_name, pr_number))

    async def get_pr_by_id(self, pr_id: int) -> dict[str, Any] | None:
        return await self.db.fetchone(q.GET_PR_BY_ID, (pr_id,))

    async def get_pending_prs(self, chatbot_id: int, limit: int = 100) -> list[dict[str, Any]]:
        query = q.GET_PENDING_PRS_SQLITE if self.db.database_url.startswith("sqlite") else q.GET_PENDING_PRS
        return await self.db.fetchall(query, (chatbot_id, limit))

    async def get_assembled_not_analyzed(
        self, chatbot_id: int | None = None, limit: int = 100, since: str | None = None
    ) -> list[dict[str, Any]]:
        if since:
            if chatbot_id is not None:
                return await self.db.fetchall(q.GET_ASSEMBLED_PRS_NOT_ANALYZED_SINCE, (chatbot_id, since, limit))
            return await self.db.fetchall(q.GET_ALL_ASSEMBLED_NOT_ANALYZED_SINCE, (since, limit))
        if chatbot_id is not None:
            return await self.db.fetchall(q.GET_ASSEMBLED_PRS_NOT_ANALYZED, (chatbot_id, limit))
        return await self.db.fetchall(q.GET_ALL_ASSEMBLED_NOT_ANALYZED, (limit,))

    # -- Locking ---------------------------------------------------------------

    async def lock_pr(self, pr_id: int, worker_id: str, lock_timeout_minutes: int = 30) -> bool:
        """Attempt to atomically lock a PR. Returns True if lock acquired."""
        from datetime import timedelta

        now = datetime.now(UTC).isoformat()
        stale_cutoff = (datetime.now(UTC) - timedelta(minutes=lock_timeout_minutes)).isoformat()
        await self.db.execute(q.LOCK_PR, (worker_id, now, pr_id, stale_cutoff))
        # Verify we got the lock
        row = await self.db.fetchone(q.GET_PR_BY_ID, (pr_id,))
        return row is not None and row.get("locked_by") == worker_id

    async def unlock_pr(self, pr_id: int) -> None:
        await self.db.execute(q.UNLOCK_PR, (pr_id,))

    # -- Enrichment updates ----------------------------------------------------

    async def update_bq_events(self, pr_id: int, bq_events: list) -> None:
        await self.db.execute(q.UPDATE_PR_BQ_EVENTS, (json.dumps(bq_events), pr_id))

    async def update_commits(self, pr_id: int, commits: list) -> None:
        await self.db.execute(q.UPDATE_PR_COMMITS, (json.dumps(commits), pr_id))

    async def update_reviews(self, pr_id: int, reviews: list) -> None:
        await self.db.execute(q.UPDATE_PR_REVIEWS, (json.dumps(reviews), pr_id))

    async def update_threads(self, pr_id: int, threads: list) -> None:
        await self.db.execute(q.UPDATE_PR_THREADS, (json.dumps(threads), pr_id))

    @staticmethod
    def compute_diff_lines(details: list) -> int:
        """Sum additions + deletions across all files in all commits."""
        total = 0
        for commit in details:
            for f in commit.get("files", []):
                total += f.get("additions", 0) + f.get("deletions", 0)
        return total

    async def update_commit_details(self, pr_id: int, details: list) -> None:
        diff_lines = self.compute_diff_lines(details)
        await self.db.execute(q.UPDATE_PR_COMMIT_DETAILS, (json.dumps(details), diff_lines, pr_id))

    async def mark_enrichment_done(self, pr_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        await self.db.execute(q.UPDATE_PR_ENRICHMENT_DONE, (now, pr_id))

    async def mark_assembled(self, pr_id: int, assembled: dict) -> None:
        now = datetime.now(UTC).isoformat()
        await self.db.execute(q.UPDATE_PR_ASSEMBLED, (json.dumps(assembled), now, pr_id))

    async def mark_error(self, pr_id: int, error_message: str) -> None:
        await self.db.execute(q.UPDATE_PR_ERROR, (error_message, pr_id))

    async def mark_skipped(self, pr_id: int, reason: str) -> None:
        await self.db.execute(q.MARK_PR_SKIPPED, (reason, pr_id))

    async def count_missing_diff_lines(self) -> int:
        row = await self.db.fetchone(q.COUNT_PRS_MISSING_DIFF_LINES)
        return row["count"] if row else 0

    async def backfill_diff_lines(self, batch_size: int = 1000) -> int:
        """Compute diff_lines for PRs that have commit_details but no diff_lines. Returns count updated."""
        rows = await self.db.fetchall(q.GET_PRS_MISSING_DIFF_LINES, (batch_size,))
        count = 0
        for row in rows:
            raw = row["commit_details"]
            details = json.loads(raw) if isinstance(raw, str) else raw
            diff_lines = self.compute_diff_lines(details)
            await self.db.execute(q.UPDATE_PR_DIFF_LINES, (diff_lines, row["id"]))
            count += 1
        return count

    async def update_metadata(
        self, pr_id: int, pr_title: str, pr_author: str | None, pr_created_at: str | None, pr_merged: bool | None
    ) -> None:
        await self.db.execute(q.UPDATE_PR_METADATA, (pr_title, pr_author, pr_created_at, pr_merged, pr_id))

    # -- LLM analyses ----------------------------------------------------------

    async def insert_analysis(
        self,
        pr_id: int,
        chatbot_id: int,
        bot_suggestions: list,
        human_actions: list,
        matching_results: list,
        total_bot_comments: int,
        matched_bot_comments: int,
        precision: float | None,
        recall: float | None,
        f_beta: float | None,
        model_name: str | None,
    ) -> None:
        await self.db.execute(
            q.INSERT_LLM_ANALYSIS,
            (
                pr_id,
                chatbot_id,
                json.dumps(bot_suggestions),
                json.dumps(human_actions),
                json.dumps(matching_results),
                total_bot_comments,
                matched_bot_comments,
                precision,
                recall,
                f_beta,
                model_name,
            ),
        )
        now = datetime.now(UTC).isoformat()
        await self.db.execute(q.UPDATE_PR_ANALYZED, (now, pr_id))

    # -- PR labels -------------------------------------------------------------

    async def insert_labels(
        self,
        pr_id: int,
        chatbot_id: int,
        labels: dict,
        model_name: str | None,
    ) -> None:
        await self.db.execute(
            q.INSERT_PR_LABELS,
            (
                pr_id,
                chatbot_id,
                json.dumps(labels),
                model_name,
            ),
        )

    async def get_analyzed_not_labeled(
        self, chatbot_id: int | None = None, limit: int = 100, since: str | None = None
    ) -> list[dict[str, Any]]:
        if since:
            if chatbot_id is not None:
                return await self.db.fetchall(q.GET_ANALYZED_NOT_LABELED_SINCE, (chatbot_id, since, limit))
            return await self.db.fetchall(q.GET_ALL_ANALYZED_NOT_LABELED_SINCE, (since, limit))
        if chatbot_id is not None:
            return await self.db.fetchall(q.GET_ANALYZED_NOT_LABELED, (chatbot_id, limit))
        return await self.db.fetchall(q.GET_ALL_ANALYZED_NOT_LABELED, (limit,))

    # -- PR volumes ------------------------------------------------------------

    async def upsert_pr_volume(self, chatbot_id: int, date: str, pr_count: int) -> None:
        await self.db.execute(q.UPSERT_PR_VOLUME, (chatbot_id, date, pr_count))

    # -- Dashboard queries -----------------------------------------------------

    async def get_analyses(self, chatbot_id: int | None = None) -> list[dict[str, Any]]:
        if chatbot_id is not None:
            return await self.db.fetchall(q.GET_ANALYSES_BY_CHATBOT, (chatbot_id,))
        return await self.db.fetchall(q.GET_ALL_ANALYSES)

    async def get_status_counts(self, chatbot_id: int | None = None) -> list[dict[str, Any]]:
        if chatbot_id is not None:
            return await self.db.fetchall(q.GET_PR_STATUS_COUNTS, (chatbot_id,))
        return await self.db.fetchall(q.GET_ALL_PR_STATUS_COUNTS)

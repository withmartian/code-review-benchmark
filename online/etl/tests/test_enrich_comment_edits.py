"""Tests for reading the edit history of a PR's bot comments with the threads request (GraphQL mocked)."""

from __future__ import annotations

from pipeline.enrich import COMMENT_EDITS_PAGE_QUERY
from pipeline.enrich import ENRICHMENT_STEPS
from pipeline.enrich import REVIEW_THREADS_QUERY
from pipeline.enrich import _fetch_review_threads
from pipeline.enrich import parse_comment_edits


def _edits(*revisions: tuple[str, str | None], next_cursor: str | None = None) -> dict:
    return {
        "nodes": [{"editedAt": t, "diff": body} for t, body in revisions],
        "pageInfo": {"hasNextPage": next_cursor is not None, "endCursor": next_cursor},
    }


def _comment(node_id: str, db_id: int, login: str, typename: str, edits: dict) -> dict:
    return {
        "id": node_id,
        "databaseId": db_id,
        "author": {"__typename": typename, "login": login},
        "body": "current",
        "createdAt": "2026-07-01T09:00:00Z",
        "updatedAt": "2026-07-01T12:00:00Z",
        "userContentEdits": edits,
    }


def _no_threads() -> dict:
    return {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}


class FakeGraphQL:
    """Serves canned pages of the threads query and of the long-history query, and records each query."""

    def __init__(self, comment_pages: list[dict], edit_pages: dict[tuple[str, str], dict]) -> None:
        self.comment_pages = comment_pages
        self.edit_pages = edit_pages
        self.calls: list[tuple[str, dict]] = []

    async def graphql(self, query: str, variables: dict) -> dict | None:
        self.calls.append((query, variables))
        if query is REVIEW_THREADS_QUERY:
            pr: dict = {}
            if variables["withThreads"]:
                pr["reviewThreads"] = _no_threads()
            if variables["withComments"]:
                cursor = variables["commentCursor"]
                pr["comments"] = self.comment_pages[0 if cursor is None else int(cursor)]
            return {"repository": {"pullRequest": pr}}
        if query is COMMENT_EDITS_PAGE_QUERY:
            return {"node": {"userContentEdits": self.edit_pages[(variables["id"], variables["cursor"])]}}
        raise AssertionError(query)


def test_the_edit_history_comes_with_the_threads_step() -> None:
    assert ENRICHMENT_STEPS == ["bq_events", "commits", "reviews", "threads", "details", "done"]
    assert "userContentEdits(first: 100)" in REVIEW_THREADS_QUERY


def test_parse_keeps_edited_bot_comments_with_revisions_oldest_first() -> None:
    nodes = [
        _comment(
            "n1", 1, "coderabbitai", "Bot", _edits(("2026-07-01T12:00:00Z", "v2"), ("2026-07-01T10:00:00Z", "v1"))
        ),
        _comment("n2", 2, "dev", "User", _edits(("2026-07-01T11:00:00Z", "fixed typo"))),
        _comment("n3", 3, "greptile-apps", "Bot", _edits()),
        _comment("n4", 4, "codecov", "Bot", _edits(("2026-07-01T11:00:00Z", None))),
    ]

    assert parse_comment_edits(nodes) == [
        {
            "id": 1,
            "author": "coderabbitai[bot]",
            "revisions": [
                {"edited_at": "2026-07-01T10:00:00Z", "body": "v1"},
                {"edited_at": "2026-07-01T12:00:00Z", "body": "v2"},
            ],
        },
    ]


async def test_one_request_returns_the_comments_with_their_edit_history() -> None:
    bot = _comment(
        "n1", 1, "coderabbitai", "Bot", _edits(("2026-07-01T12:00:00Z", "v2"), ("2026-07-01T10:00:00Z", "v1"))
    )
    gh = FakeGraphQL([{"nodes": [bot], "pageInfo": {"hasNextPage": False, "endCursor": None}}], {})

    _, comments, edits = await _fetch_review_threads(gh, "owner", "repo", 7)

    assert len(gh.calls) == 1
    assert [c["body"] for c in comments] == ["current"]
    assert [[r["body"] for r in c["revisions"]] for c in edits] == [["v1", "v2"]]


async def test_pages_through_comments_and_long_histories_of_bot_comments_only() -> None:
    bot = _comment("n1", 1, "coderabbitai", "Bot", _edits(("2026-07-01T12:00:00Z", "v3"), next_cursor="e1"))
    person = _comment("n2", 2, "dev", "User", _edits(("2026-07-01T11:00:00Z", "edit"), next_cursor="p1"))
    other_bot = _comment("n3", 3, "codex", "Bot", _edits(("2026-07-01T13:00:00Z", "limit")))
    pages = [
        {"nodes": [bot, person], "pageInfo": {"hasNextPage": True, "endCursor": "1"}},
        {"nodes": [other_bot], "pageInfo": {"hasNextPage": False, "endCursor": None}},
    ]
    older = {
        ("n1", "e1"): _edits(("2026-07-01T11:00:00Z", "v2"), next_cursor="e2"),
        ("n1", "e2"): _edits(("2026-07-01T10:00:00Z", "v1")),
    }
    gh = FakeGraphQL(pages, older)

    _, comments, edits = await _fetch_review_threads(gh, "owner", "repo", 7)

    assert [c["id"] for c in comments] == [1, 2, 3]
    assert [c["id"] for c in edits] == [1, 3]
    assert [r["body"] for r in edits[0]["revisions"]] == ["v1", "v2", "v3"]
    # Two pages of the threads request, then two more for the long history; never for a person's comment.
    assert [q is REVIEW_THREADS_QUERY for q, _ in gh.calls] == [True, True, False, False]
    assert [v.get("withThreads") for _, v in gh.calls[:2]] == [True, False]


async def test_a_failed_long_history_read_keeps_the_first_page() -> None:
    bot = _comment("n1", 1, "coderabbitai", "Bot", _edits(("2026-07-01T12:00:00Z", "v2"), next_cursor="e1"))
    gh = FakeGraphQL([{"nodes": [bot], "pageInfo": {"hasNextPage": False, "endCursor": None}}], {})

    async def fail_on_history(query: str, variables: dict) -> dict | None:
        if query is COMMENT_EDITS_PAGE_QUERY:
            return None
        return await FakeGraphQL.graphql(gh, query, variables)

    gh.graphql = fail_on_history  # type: ignore[method-assign]

    _, _, edits = await _fetch_review_threads(gh, "owner", "repo", 7)

    assert [[r["body"] for r in c["revisions"]] for c in edits] == [["v2"]]

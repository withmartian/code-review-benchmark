"""Tests for reading a PR's review threads and conversation comments in one GraphQL read (GraphQL mocked)."""

from __future__ import annotations

import pytest

from pipeline.enrich import ENRICHMENT_STEPS
from pipeline.enrich import REVIEW_THREADS_QUERY
from pipeline.enrich import IncompleteReadError
from pipeline.enrich import _fetch_review_threads
from pipeline.enrich import parse_issue_comments


def _page(nodes: list[dict], next_cursor: str | None = None) -> dict:
    return {"nodes": nodes, "pageInfo": {"hasNextPage": next_cursor is not None, "endCursor": next_cursor}}


def _thread(thread_id: str) -> dict:
    return {
        "id": thread_id,
        "isResolved": False,
        "resolvedBy": None,
        "comments": {
            "nodes": [
                {
                    "databaseId": 100,
                    "body": "finding",
                    "path": "a.py",
                    "line": 3,
                    "originalLine": 3,
                    "diffHunk": "@@",
                    "author": {"login": "coderabbitai"},
                    "createdAt": "2026-07-01T10:00:00Z",
                    "reactionGroups": [{"content": "THUMBS_UP", "reactors": {"totalCount": 2}}],
                }
            ]
        },
    }


def _comment(db_id: int, login: str | None, typename: str, body: str) -> dict:
    return {
        "databaseId": db_id,
        "author": {"__typename": typename, "login": login} if login else None,
        "body": body,
        "createdAt": "2026-07-01T09:00:00Z",
        "updatedAt": "2026-07-01T12:00:00Z",
    }


class FakeGraphQL:
    """Serves canned pages of the threads query, keyed by cursor, and records each request."""

    def __init__(self, thread_pages: dict, comment_pages: dict, fail: bool = False) -> None:
        self.thread_pages = thread_pages
        self.comment_pages = comment_pages
        self.fail = fail
        self.calls: list[dict] = []

    async def graphql(self, query: str, variables: dict) -> dict | None:
        assert query is REVIEW_THREADS_QUERY
        self.calls.append(variables)
        if self.fail:
            return None  # the read failed (deleted repository, 404)
        pr: dict = {}
        if variables["withThreads"]:
            pr["reviewThreads"] = self.thread_pages[variables["threadCursor"]]
        if variables["withComments"]:
            pr["comments"] = self.comment_pages[variables["commentCursor"]]
        return {"repository": {"pullRequest": pr}}


def test_the_comments_come_with_the_threads_step() -> None:
    assert ENRICHMENT_STEPS == ["bq_events", "commits", "reviews", "threads", "details", "done"]
    assert "comments(first: 100, after: $commentCursor)" in REVIEW_THREADS_QUERY


def test_parse_gives_the_rest_shape_with_bot_logins_suffixed() -> None:
    nodes = [
        _comment(1, "coderabbitai", "Bot", "Review limit reached"),
        _comment(2, "dev", "User", "thanks"),
        _comment(3, None, "User", "ghost"),
    ]

    assert parse_issue_comments(nodes) == [
        {
            "id": 1,
            "author": "coderabbitai[bot]",
            "body": "Review limit reached",
            "created_at": "2026-07-01T09:00:00Z",
            "updated_at": "2026-07-01T12:00:00Z",
        },
        {
            "id": 2,
            "author": "dev",
            "body": "thanks",
            "created_at": "2026-07-01T09:00:00Z",
            "updated_at": "2026-07-01T12:00:00Z",
        },
        {
            "id": 3,
            "author": None,
            "body": "ghost",
            "created_at": "2026-07-01T09:00:00Z",
            "updated_at": "2026-07-01T12:00:00Z",
        },
    ]


async def test_one_request_returns_threads_and_comments() -> None:
    gh = FakeGraphQL({None: _page([_thread("T1")])}, {None: _page([_comment(1, "coderabbitai", "Bot", "summary")])})

    threads, comments, _ = await _fetch_review_threads(gh, "owner", "repo", 7)

    assert [t["id"] for t in threads] == ["T1"]
    assert threads[0]["comments"][0]["reactions"] == {"THUMBS_UP": 2}
    assert [(c["id"], c["author"]) for c in comments] == [(1, "coderabbitai[bot]")]
    assert gh.calls == [
        {
            "owner": "owner",
            "repo": "repo",
            "prNumber": 7,
            "threadCursor": None,
            "commentCursor": None,
            "withThreads": True,
            "withComments": True,
        }
    ]


async def test_each_connection_pages_on_its_own_until_it_ends() -> None:
    threads = {None: _page([_thread("T1")], "t1"), "t1": _page([_thread("T2")])}
    comments = {
        None: _page([_comment(1, "coderabbitai", "Bot", "a")], "c1"),
        "c1": _page([_comment(2, "dev", "User", "b")], "c2"),
        "c2": _page([_comment(3, "coderabbitai", "Bot", "c")]),
    }
    gh = FakeGraphQL(threads, comments)

    got_threads, got_comments, _ = await _fetch_review_threads(gh, "owner", "repo", 7)

    assert [t["id"] for t in got_threads] == ["T1", "T2"]
    assert [c["id"] for c in got_comments] == [1, 2, 3]
    # Threads end after the second request; the third asks for comments only.
    assert [(v["withThreads"], v["threadCursor"], v["withComments"], v["commentCursor"]) for v in gh.calls] == [
        (True, None, True, None),
        (True, "t1", True, "c1"),
        (False, None, True, "c2"),
    ]


async def test_returns_nothing_when_the_pull_request_cannot_be_read() -> None:
    gh = FakeGraphQL({}, {}, fail=True)

    assert await _fetch_review_threads(gh, "owner", "repo", 7) == ([], [], [])
    assert len(gh.calls) == 1


async def test_a_failed_later_page_of_threads_raises_instead_of_keeping_the_first_page() -> None:
    threads = {None: _page([_thread("T1")], "t1")}
    gh = FakeGraphQL(threads, {None: _page([_comment(1, "coderabbitai", "Bot", "summary")])})
    read = gh.graphql

    async def fail_second_request(query: str, variables: dict) -> dict | None:
        return None if gh.calls else await read(query, variables)

    gh.graphql = fail_second_request  # type: ignore[method-assign]

    with pytest.raises(IncompleteReadError, match="reviewThreads could not be read after 1 threads"):
        await _fetch_review_threads(gh, "owner", "repo", 7)


async def test_a_failed_later_page_of_comments_raises_instead_of_keeping_the_first_page() -> None:
    # The second page answers with errors and no comments connection: the comments it held, and any
    # review recorded in them, would be missing.
    comments = {None: _page([_comment(1, "coderabbitai", "Bot", "summary")], "c1")}
    gh = FakeGraphQL({None: _page([_thread("T1")])}, comments)
    read = gh.graphql

    async def drop_comments_on_second_request(query: str, variables: dict) -> dict | None:
        if not gh.calls:
            return await read(query, variables)
        gh.calls.append(variables)
        return {"repository": {"pullRequest": {"comments": None}}}

    gh.graphql = drop_comments_on_second_request  # type: ignore[method-assign]

    with pytest.raises(IncompleteReadError, match="comments could not be read after 1 threads and 1 comments"):
        await _fetch_review_threads(gh, "owner", "repo", 7)
    assert [(v["withThreads"], v["withComments"]) for v in gh.calls] == [(True, True), (False, True)]


class FakeRepo:
    def __init__(self) -> None:
        self.writes: list[tuple] = []

    async def update_threads(
        self, pr_id: int, threads: list, issue_comments: list | None = None, comment_edits: list | None = None
    ) -> None:
        self.writes.append(("threads", pr_id, threads, issue_comments, comment_edits))

    async def update_commit_details(self, pr_id: int, details: list) -> None:
        self.writes.append(("details", pr_id, details))

    async def mark_enrichment_done(self, pr_id: int) -> None:
        self.writes.append(("done", pr_id))


async def test_the_threads_step_stores_the_comments_with_the_threads() -> None:
    from pipeline.enrich import enrich_single_pr

    gh = FakeGraphQL({None: _page([_thread("T1")])}, {None: _page([_comment(1, "coderabbitai", "Bot", "summary")])})
    repo = FakeRepo()
    pr_row = {"id": 5, "repo_name": "owner/repo", "pr_number": 7, "enrichment_step": "reviews", "commits": "[]"}

    await enrich_single_pr(gh, repo, pr_row)

    assert len(gh.calls) == 1
    kind, pr_id, threads, comments, edits = repo.writes[0]
    assert (kind, pr_id, [t["id"] for t in threads]) == ("threads", 5, ["T1"])
    assert [c["author"] for c in comments] == ["coderabbitai[bot]"]
    assert edits == []
    assert [w[0] for w in repo.writes] == ["threads", "details", "done"]

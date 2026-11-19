"""Tests for reading the edit history of a PR's bot comments with the threads request (GraphQL mocked)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline import enrich
from pipeline.enrich import COMMENT_EDITS_PAGE_QUERY
from pipeline.enrich import ENRICHMENT_STEPS
from pipeline.enrich import REVIEW_THREADS_QUERY
from pipeline.enrich import IncompleteReadError
from pipeline.enrich import _fetch_review_threads
from pipeline.enrich import enrich_single_pr
from pipeline.enrich import parse_comment_edits

CR = "coderabbitai[bot]"


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


def test_parse_keeps_edited_comments_of_the_scored_bot_with_revisions_oldest_first() -> None:
    nodes = [
        _comment(
            "n1", 1, "coderabbitai", "Bot", _edits(("2026-07-01T12:00:00Z", "v2"), ("2026-07-01T10:00:00Z", "v1"))
        ),
        _comment("n2", 2, "dev", "User", _edits(("2026-07-01T11:00:00Z", "fixed typo"))),
        _comment("n3", 3, "coderabbitai", "Bot", _edits()),
        _comment("n4", 4, "coderabbitai", "Bot", _edits(("2026-07-01T11:00:00Z", None))),
    ]

    assert parse_comment_edits(nodes, bot=CR) == [
        {
            "id": 1,
            "author": "coderabbitai[bot]",
            "revisions": [
                {"edited_at": "2026-07-01T10:00:00Z", "body": "v1"},
                {"edited_at": "2026-07-01T12:00:00Z", "body": "v2"},
            ],
        },
        # An edit GitHub keeps no text for is not a revision, but it is recorded as missing: a review
        # recorded only in it cannot be read.
        {"id": 4, "author": "coderabbitai[bot]", "revisions": [], "unavailable_edits": ["2026-07-01T11:00:00Z"]},
    ]


def test_parse_stores_the_edits_of_the_scored_bot_only() -> None:
    # CI and coverage bots rewrite their comments on every push too; the analysis reads only the
    # scored bot's record and notices, so the other bots' histories are not stored, missing edits
    # included.
    nodes = [
        _comment("n1", 1, "coderabbitai", "Bot", _edits(("2026-07-01T12:00:00Z", "v1"))),
        _comment("n2", 2, "codecov", "Bot", _edits(("2026-07-01T11:00:00Z", "coverage 80%"))),
        _comment("n3", 3, "greptile-apps", "Bot", _edits(("2026-07-01T11:30:00Z", None))),
        _comment("n4", 4, "github-actions", "Bot", _edits(("2026-07-01T11:40:00Z", "build ok"))),
    ]

    assert [c["id"] for c in parse_comment_edits(nodes, bot=CR)] == [1]
    assert [c["id"] for c in parse_comment_edits(nodes, bot="greptile-apps[bot]")] == [3]
    assert [c["id"] for c in parse_comment_edits(nodes, bot="codecov[bot]")] == [2]
    # A person who shares a bot's name is not the bot.
    person = _comment("n5", 5, "coderabbitai", "User", _edits(("2026-07-01T11:50:00Z", "edit")))
    assert parse_comment_edits([person], bot=CR) == []


async def test_one_request_returns_the_comments_with_their_edit_history() -> None:
    bot = _comment(
        "n1", 1, "coderabbitai", "Bot", _edits(("2026-07-01T12:00:00Z", "v2"), ("2026-07-01T10:00:00Z", "v1"))
    )
    gh = FakeGraphQL([{"nodes": [bot], "pageInfo": {"hasNextPage": False, "endCursor": None}}], {})

    _, comments, edits = await _fetch_review_threads(gh, "owner", "repo", 7, bot=CR)

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

    _, comments, edits = await _fetch_review_threads(gh, "owner", "repo", 7, bot=CR)

    assert [c["id"] for c in comments] == [1, 2, 3]
    # The other bot's edited comment is a conversation comment, but its history is not stored.
    assert [c["id"] for c in edits] == [1]
    assert [r["body"] for r in edits[0]["revisions"]] == ["v1", "v2", "v3"]
    # Two pages of the threads request, then two more for the long history; never for a person's comment.
    assert [q is REVIEW_THREADS_QUERY for q, _ in gh.calls] == [True, True, False, False]
    assert [v.get("withThreads") for _, v in gh.calls[:2]] == [True, False]


def _failing_history_gh() -> FakeGraphQL:
    """A bot comment whose second page of edits cannot be read."""
    bot = _comment("n1", 1, "coderabbitai", "Bot", _edits(("2026-07-01T12:00:00Z", "v2"), next_cursor="e1"))
    gh = FakeGraphQL([{"nodes": [bot], "pageInfo": {"hasNextPage": False, "endCursor": None}}], {})

    async def fail_on_history(query: str, variables: dict) -> dict | None:
        if query is COMMENT_EDITS_PAGE_QUERY:
            return None
        return await FakeGraphQL.graphql(gh, query, variables)

    gh.graphql = fail_on_history  # type: ignore[method-assign]
    return gh


async def test_a_failed_long_history_read_raises_instead_of_keeping_the_first_page() -> None:
    # A review recorded only in the unread revisions would read as a push the bot never reviewed.
    with pytest.raises(IncompleteReadError, match="comment 1 stopped after 1 edits"):
        await _fetch_review_threads(_failing_history_gh(), "owner", "repo", 7, bot=CR)


async def test_a_later_page_of_edits_with_null_nodes_raises() -> None:
    # The second page answers with its connection but no nodes (a partial answer): read as empty, the
    # history would end at the first page.
    bot = _comment("n1", 1, "coderabbitai", "Bot", _edits(("2026-07-01T12:00:00Z", "newest"), next_cursor="e1"))
    page = {"nodes": [bot], "pageInfo": {"hasNextPage": False, "endCursor": None}}
    gh = FakeGraphQL([page], {("n1", "e1"): {"nodes": None, "pageInfo": {"hasNextPage": False, "endCursor": None}}})

    with pytest.raises(IncompleteReadError, match="comment 1 stopped after 1 edits"):
        await _fetch_review_threads(gh, "owner", "repo", 7, bot=CR)


@pytest.mark.parametrize("missing", ["null", "absent", "no nodes"])
async def test_a_bot_comment_whose_first_page_of_edits_is_missing_raises(missing: str) -> None:
    # A partial answer (errors beside data) can leave a comment's userContentEdits null or out. Read as
    # no edits, it would be stored as a complete history that holds no earlier review.
    bot = _comment("IC_1", 1, "coderabbitai", "Bot", _edits())
    if missing == "null":
        bot["userContentEdits"] = None
    elif missing == "absent":
        del bot["userContentEdits"]
    else:
        bot["userContentEdits"] = {"nodes": None, "pageInfo": {"hasNextPage": False, "endCursor": None}}
    person = _comment("IC_2", 2, "dev", "User", None)  # a person's history is not read, so not checked
    page = {"nodes": [bot, person], "pageInfo": {"hasNextPage": False, "endCursor": None}}

    with pytest.raises(IncompleteReadError, match="edit history of comment 1 could not be read"):
        await _fetch_review_threads(FakeGraphQL([page], {}), "owner", "repo", 7, bot=CR)


async def test_another_bot_s_history_is_neither_paged_nor_checked() -> None:
    # Only the scored bot's history is stored, so only its pages are read and only its missing pages
    # stop the step. Another bot's long history (a next page that would fail) and its missing first
    # page are left alone.
    long_other = _comment("n2", 2, "codecov", "Bot", _edits(("2026-07-01T12:00:00Z", "cov"), next_cursor="x1"))
    missing_other = _comment("n3", 3, "github-actions", "Bot", None)
    scored = _comment("n1", 1, "coderabbitai", "Bot", _edits(("2026-07-01T12:00:00Z", "v2"), next_cursor="e1"))
    page = {"nodes": [long_other, missing_other, scored], "pageInfo": {"hasNextPage": False, "endCursor": None}}
    unreadable = {"nodes": None, "pageInfo": {"hasNextPage": False, "endCursor": None}}
    pages = {("n1", "e1"): _edits(("2026-07-01T10:00:00Z", "v1")), ("n2", "x1"): unreadable}
    gh = FakeGraphQL([page], pages)

    _, comments, edits = await _fetch_review_threads(gh, "owner", "repo", 7, bot=CR)

    assert [c["id"] for c in comments] == [2, 3, 1]
    assert [(c["id"], [r["body"] for r in c["revisions"]]) for c in edits] == [(1, ["v1", "v2"])]
    # One threads request and one page of the scored bot's history; none for codecov's.
    assert [v.get("id") for q, v in gh.calls if q is COMMENT_EDITS_PAGE_QUERY] == ["n1"]

    # Scored for codecov, the same answer pages codecov's history, and its unreadable page stops the step.
    with pytest.raises(IncompleteReadError, match="comment 2 stopped after 1 edits"):
        await _fetch_review_threads(FakeGraphQL([page], pages), "owner", "repo", 7, bot="codecov[bot]")
    # Scored for github-actions, its missing first page stops the step.
    with pytest.raises(IncompleteReadError, match="edit history of comment 3 could not be read"):
        await _fetch_review_threads(FakeGraphQL([page], pages), "owner", "repo", 7, bot="github-actions[bot]")


class FakeRepo:
    def __init__(self) -> None:
        self.writes: list[tuple] = []

    async def update_threads(self, pr_id: int, *args: object) -> None:
        self.writes.append(("threads", pr_id, *args))

    async def update_commit_details(self, pr_id: int, *_args: object) -> None:
        self.writes.append(("details", pr_id))

    async def mark_enrichment_done(self, pr_id: int) -> None:
        self.writes.append(("done", pr_id))

    async def lock_pr(self, pr_id: int, *_args: object) -> bool:
        self.writes.append(("lock", pr_id))
        return True

    async def unlock_pr(self, pr_id: int) -> None:
        self.writes.append(("unlock", pr_id))

    async def mark_error(self, pr_id: int, *_args: object) -> None:
        self.writes.append(("error", pr_id))


PR_ROW = {"id": 5, "repo_name": "owner/repo", "pr_number": 7, "enrichment_step": "reviews", "commits": "[]"}


async def test_an_incomplete_history_is_never_stored_so_it_excludes_nothing(monkeypatch) -> None:
    # No partial history reaches the analysis: after THREADS_READ_ATTEMPTS failed reads the step is
    # stored with the comments and edits as not read, which excludes nothing.
    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(enrich.asyncio, "sleep", no_sleep)
    repo = FakeRepo()

    await enrich_single_pr(_failing_history_gh(), repo, dict(PR_ROW), chatbot_username=CR)

    threads_writes = [w for w in repo.writes if w[0] == "threads"]
    assert len(threads_writes) == 1
    assert threads_writes[0][3] is None and threads_writes[0][4] is None
    with pytest.raises(IncompleteReadError):
        await _fetch_review_threads(_failing_history_gh(), "owner", "repo", 7, bot=CR)


async def test_the_loop_releases_a_pr_with_an_incomplete_read_for_the_next_pass(monkeypatch) -> None:
    repo = FakeRepo()
    passes = [[dict(PR_ROW)], []]

    async def get_pending_prs(*_args: object, **_kwargs: object) -> list[dict]:
        return passes.pop(0)

    repo.get_pending_prs = get_pending_prs  # type: ignore[attr-defined]
    scored: list[object] = []

    async def get_all_chatbots() -> list[dict]:
        return [{"id": 1, "github_username": CR}, {"id": 2, "github_username": "greptile-apps[bot]"}]

    repo.get_all_chatbots = get_all_chatbots  # type: ignore[attr-defined]

    async def incomplete(*_args: object, **kwargs: object) -> None:
        scored.append(kwargs.get("chatbot_username"))
        raise IncompleteReadError("edit history of comment 1 stopped after 100 edits")

    monkeypatch.setattr(enrich, "PRRepository", lambda _db: repo)
    monkeypatch.setattr(enrich, "enrich_single_pr", incomplete)
    cfg = SimpleNamespace(github_tokens=["t"], github_token="t", worker_id="w", lock_timeout_minutes=30)

    assert await enrich.enrich_loop(cfg, None, chatbot_id=1, one_shot=True) == 0
    # Unlocked, not marked as error (an errored PR is never read again) and not marked done.
    assert repo.writes == [("lock", 5), ("unlock", 5)]
    # The PR is enriched for the bot it is scored for, read by its id when no name is given.
    assert scored == [CR]

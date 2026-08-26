"""Tests for Search API discovery functions."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import httpx
from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st
import pytest

from pipeline.discover import _fetch_window
from pipeline.discover import _format_dt
from pipeline.discover import _parse_search_item
from pipeline.discover import _sample_prs
from pipeline.discover import _SearchTokenPool

# -- _parse_search_item -------------------------------------------------------


class TestParseSearchItem:
    def test_basic_item(self) -> None:
        item = {
            "repository_url": "https://api.github.com/repos/owner/repo",
            "number": 42,
            "html_url": "https://github.com/owner/repo/pull/42",
            "title": "Fix the thing",
            "user": {"login": "alice"},
            "created_at": "2026-08-06T12:00:00Z",
            "pull_request": {"merged_at": "2026-08-06T15:00:00Z"},
        }
        pr = _parse_search_item(item)
        assert pr["repo_name"] == "owner/repo"
        assert pr["pr_number"] == 42
        assert pr["pr_url"] == "https://github.com/owner/repo/pull/42"
        assert pr["pr_title"] == "Fix the thing"
        assert pr["pr_author"] == "alice"
        assert pr["pr_created_at"] == "2026-08-06T12:00:00Z"
        assert pr["pr_merged"] is True
        assert pr["bot_reviewed_at"] == "2026-08-06T15:00:00Z"

    def test_missing_user(self) -> None:
        item = {
            "repository_url": "https://api.github.com/repos/org/project",
            "number": 1,
            "html_url": "https://github.com/org/project/pull/1",
            "title": "",
            "user": None,
            "created_at": "2026-01-01T00:00:00Z",
        }
        pr = _parse_search_item(item)
        assert pr["pr_author"] is None
        assert pr["repo_name"] == "org/project"

    def test_nested_repo_name(self) -> None:
        item = {
            "repository_url": "https://api.github.com/repos/deep/nested-repo",
            "number": 99,
            "title": "Nested",
            "user": {"login": "bob"},
            "created_at": "2026-06-01T00:00:00Z",
        }
        pr = _parse_search_item(item)
        assert pr["repo_name"] == "deep/nested-repo"

    def test_fallback_pr_url(self) -> None:
        item = {
            "repository_url": "https://api.github.com/repos/a/b",
            "number": 5,
            "title": "Test",
            "user": {"login": "c"},
            "created_at": "2026-01-01T00:00:00Z",
        }
        pr = _parse_search_item(item)
        assert pr["pr_url"] == "https://github.com/a/b/pull/5"


# -- _sample_prs ---------------------------------------------------------------


class TestSamplePrs:
    def _make_prs(self, repos_and_counts: list[tuple[str, int]]) -> list[dict]:
        prs = []
        for repo, count in repos_and_counts:
            for i in range(count):
                prs.append({
                    "repo_name": repo,
                    "pr_number": i + 1,
                    "pr_url": f"https://github.com/{repo}/pull/{i + 1}",
                    "pr_title": f"PR {i + 1}",
                    "pr_author": "author",
                    "pr_created_at": "2026-01-01",
                    "pr_merged": True,
                })
        return prs

    def test_repo_cap_applied(self) -> None:
        prs = self._make_prs([("org/repo-a", 20)])
        sampled = _sample_prs(prs, max_prs_per_day=500, max_per_repo=10)
        assert len(sampled) == 10
        assert all(p["repo_name"] == "org/repo-a" for p in sampled)

    def test_sample_down_to_max(self) -> None:
        prs = self._make_prs([("org/a", 5), ("org/b", 5), ("org/c", 5)])
        sampled = _sample_prs(prs, max_prs_per_day=8, max_per_repo=10)
        assert len(sampled) == 8

    def test_no_sampling_when_under_limit(self) -> None:
        prs = self._make_prs([("org/a", 3)])
        sampled = _sample_prs(prs, max_prs_per_day=500, max_per_repo=10)
        assert len(sampled) == 3

    def test_repo_cap_then_sample(self) -> None:
        prs = self._make_prs([("org/a", 50), ("org/b", 50)])
        sampled = _sample_prs(prs, max_prs_per_day=5, max_per_repo=10)
        assert len(sampled) == 5
        repos = {p["repo_name"] for p in sampled}
        assert len(repos) <= 2

    @given(
        n_repos=st.integers(min_value=1, max_value=10),
        prs_per_repo=st.integers(min_value=1, max_value=30),
        max_per_day=st.integers(min_value=1, max_value=200),
        max_per_repo=st.integers(min_value=1, max_value=20),
    )
    @settings(max_examples=100)
    def test_sample_never_exceeds_limits(
        self, n_repos: int, prs_per_repo: int, max_per_day: int, max_per_repo: int,
    ) -> None:
        repos = [(f"org/repo-{i}", prs_per_repo) for i in range(n_repos)]
        prs = self._make_prs(repos)
        sampled = _sample_prs(prs, max_prs_per_day=max_per_day, max_per_repo=max_per_repo)
        assert len(sampled) <= max_per_day
        repo_counts: dict[str, int] = {}
        for p in sampled:
            repo_counts[p["repo_name"]] = repo_counts.get(p["repo_name"], 0) + 1
        for count in repo_counts.values():
            assert count <= max_per_repo


# -- _format_dt ----------------------------------------------------------------


def test_format_dt() -> None:
    dt = datetime(2026, 8, 6, 14, 30, 0, tzinfo=UTC)
    assert _format_dt(dt) == "2026-08-06T14:30:00"


# -- _fetch_window (mocked HTTP) -----------------------------------------------


@pytest.mark.asyncio
async def test_fetch_window_simple(monkeypatch: pytest.MonkeyPatch) -> None:
    """Window with count <= 1000 fetches all results without bisecting."""
    monkeypatch.setattr("pipeline.discover.SEARCH_API_SLEEP", 0)

    items = [
        {"repository_url": "https://api.github.com/repos/a/b", "number": i,
         "title": f"PR {i}", "user": {"login": "u"}, "created_at": "2026-08-06T00:00:00Z"}
        for i in range(3)
    ]

    call_count = 0

    async def mock_get(_url: str, params: dict | None = None, **_kwargs) -> MagicMock:  # type: ignore[type-arg]
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        if params and params.get("per_page") == "1":
            resp.json.return_value = {"total_count": 3, "items": []}
        else:
            resp.json.return_value = {"total_count": 3, "items": items}
        return resp

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = mock_get

    start = datetime(2026, 8, 6, tzinfo=UTC)
    end = datetime(2026, 8, 7, tzinfo=UTC)
    result = await _fetch_window(client, "testbot[bot]", start, end)

    assert len(result) == 3
    assert call_count == 2  # 1 count + 1 page


@pytest.mark.asyncio
async def test_fetch_window_bisects_when_count_exceeds_max_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """Window with count > max_items triggers bisection for unbiased sampling."""
    monkeypatch.setattr("pipeline.discover.SEARCH_API_SLEEP", 0)

    call_queries: list[str] = []

    async def mock_get(_url: str, params: dict | None = None, **_kwargs) -> MagicMock:  # type: ignore[type-arg]
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()

        query = (params or {}).get("q", "")
        call_queries.append(query)

        if params and params.get("per_page") == "1":
            # Full day returns >max_items, half-days return <=max_items
            if "T12:00:00" in query or "T00:00:00..2026-08-06T12:00:00" in query:
                resp.json.return_value = {"total_count": 100, "items": []}
            else:
                resp.json.return_value = {"total_count": 500, "items": []}
        else:
            items = [
                {"repository_url": "https://api.github.com/repos/a/b", "number": i,
                 "title": f"PR {i}", "user": {"login": "u"}, "created_at": "2026-08-06T00:00:00Z"}
                for i in range(5)
            ]
            resp.json.return_value = {"total_count": 100, "items": items}
        return resp

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = mock_get

    start = datetime(2026, 8, 6, tzinfo=UTC)
    end = datetime(2026, 8, 7, tzinfo=UTC)
    # count (500) > max_items (200) triggers bisection
    result = await _fetch_window(client, "testbot[bot]", start, end, max_items=200)

    # Should have bisected: 1 count for full day, then 2 counts + 2 fetches for halves
    assert len(result) == 10  # 5 from each half
    assert any("T12:00:00" in q for q in call_queries)


@pytest.mark.asyncio
async def test_fetch_window_skips_bisection_when_count_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Window with count <= max_items paginates directly without bisection."""
    monkeypatch.setattr("pipeline.discover.SEARCH_API_SLEEP", 0)

    call_queries: list[str] = []

    async def mock_get(_url: str, params: dict | None = None, **_kwargs) -> MagicMock:  # type: ignore[type-arg]
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()

        query = (params or {}).get("q", "")
        call_queries.append(query)

        if params and params.get("per_page") == "1":
            resp.json.return_value = {"total_count": 150, "items": []}
        else:
            items = [
                {"repository_url": "https://api.github.com/repos/a/b", "number": i,
                 "title": f"PR {i}", "user": {"login": "u"}, "created_at": "2026-08-06T00:00:00Z"}
                for i in range(100)
            ]
            resp.json.return_value = {"total_count": 150, "items": items}
        return resp

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = mock_get

    start = datetime(2026, 8, 6, tzinfo=UTC)
    end = datetime(2026, 8, 7, tzinfo=UTC)
    # count (150) <= max_items (200) — should NOT bisect
    result = await _fetch_window(client, "testbot[bot]", start, end, max_items=200)

    # Mock returns 100 per page x 2 pages; real API would return 50 on page 2
    assert len(result) == 200
    # No bisection: no T12:00:00 midpoint queries
    assert not any("T12:00:00" in q for q in call_queries)
    # 1 count query + 2 page fetches (ceil(150/100) pages)
    assert len(call_queries) == 3


@pytest.mark.asyncio
async def test_fetch_window_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Window with 0 results returns empty list."""
    monkeypatch.setattr("pipeline.discover.SEARCH_API_SLEEP", 0)

    async def mock_get(_url: str, _params: dict | None = None, **_kwargs) -> MagicMock:  # type: ignore[type-arg]
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"total_count": 0, "items": []}
        return resp

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = mock_get

    start = datetime(2026, 8, 6, tzinfo=UTC)
    end = datetime(2026, 8, 7, tzinfo=UTC)
    result = await _fetch_window(client, "testbot[bot]", start, end)
    assert result == []


# -- _SearchTokenPool ----------------------------------------------------------


class TestSearchTokenPool:
    def test_round_robin_rotation(self) -> None:
        pool = _SearchTokenPool(["token-a", "token-b", "token-c"], timeout=5.0)
        clients = [pool.next() for _ in range(6)]
        # Should cycle: a, b, c, a, b, c
        assert clients[0] is clients[3]
        assert clients[1] is clients[4]
        assert clients[2] is clients[5]
        assert clients[0] is not clients[1]
        assert clients[1] is not clients[2]

    def test_single_token_returns_same_client(self) -> None:
        pool = _SearchTokenPool(["only-token"])
        assert pool.size == 1
        c1 = pool.next()
        c2 = pool.next()
        assert c1 is c2

    def test_size(self) -> None:
        assert _SearchTokenPool(["a", "b"]).size == 2
        assert _SearchTokenPool(["a"]).size == 1


@pytest.mark.asyncio
async def test_fetch_window_rotates_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple API calls within _fetch_window use different clients from the pool."""
    monkeypatch.setattr("pipeline.discover.SEARCH_API_SLEEP", 0)

    clients_used: list[int] = []

    items = [
        {"repository_url": "https://api.github.com/repos/a/b", "number": i,
         "title": f"PR {i}", "user": {"login": "u"}, "created_at": "2026-08-06T00:00:00Z"}
        for i in range(100)
    ]

    async def mock_get(_url: str, params: dict | None = None, **_kwargs) -> MagicMock:  # type: ignore[type-arg]
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        if params and params.get("per_page") == "1":
            resp.json.return_value = {"total_count": 300, "items": []}
        else:
            resp.json.return_value = {"total_count": 300, "items": items}
        return resp

    # Create a pool with 3 real-ish clients, but mock their .get
    pool = _SearchTokenPool(["t1", "t2", "t3"], timeout=5.0)
    for i, c in enumerate(pool._clients):

        # Track which client index was used
        def make_tracked_get(idx: int):
            async def tracked_get(*args, **kwargs):  # type: ignore[no-untyped-def]
                clients_used.append(idx)
                return await mock_get(*args, **kwargs)
            return tracked_get

        c.get = make_tracked_get(i)  # type: ignore[assignment]

    start = datetime(2026, 8, 6, tzinfo=UTC)
    end = datetime(2026, 8, 7, tzinfo=UTC)
    result = await _fetch_window(pool, "testbot[bot]", start, end, max_items=300)

    assert len(result) == 300
    # Should have used multiple different clients
    assert len(set(clients_used)) > 1, f"Only used client(s): {set(clients_used)}"
    await pool.close()

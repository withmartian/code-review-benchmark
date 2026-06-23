"""Tests for volumes.py utility functions: date/suffix conversion and Search API volumes."""

from __future__ import annotations

from datetime import date
from datetime import timedelta
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import httpx
from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st
import pytest

from pipeline.volumes import SearchError
from pipeline.volumes import _date_range
from pipeline.volumes import _date_to_suffix
from pipeline.volumes import _search_api_count
from pipeline.volumes import _suffix_to_date
from pipeline.volumes import _weekly_chunks

# -- _date_to_suffix ----------------------------------------------------------


@pytest.mark.parametrize("date_str,expected", [
    ("2026-01-15", "260115"),
    ("2025-12-31", "251231"),
    ("2020-01-01", "200101"),
    ("2030-06-09", "300609"),
])
def test_date_to_suffix(date_str: str, expected: str) -> None:
    assert _date_to_suffix(date_str) == expected


# -- _suffix_to_date ----------------------------------------------------------


@pytest.mark.parametrize("suffix,expected", [
    ("260115", "2026-01-15"),
    ("251231", "2025-12-31"),
    ("200101", "2020-01-01"),
    ("300609", "2030-06-09"),
])
def test_suffix_to_date(suffix: str, expected: str) -> None:
    assert _suffix_to_date(suffix) == expected


# -- Roundtrip -----------------------------------------------------------------


@given(
    year=st.integers(min_value=2000, max_value=2099),
    month=st.integers(min_value=1, max_value=12),
    day=st.integers(min_value=1, max_value=28),
)
@settings(max_examples=200)
def test_roundtrip(year: int, month: int, day: int) -> None:
    """date -> suffix -> date should be identity."""
    date_str = f"{year:04d}-{month:02d}-{day:02d}"
    suffix = _date_to_suffix(date_str)
    assert _suffix_to_date(suffix) == date_str


@given(
    year=st.integers(min_value=2000, max_value=2099),
    month=st.integers(min_value=1, max_value=12),
    day=st.integers(min_value=1, max_value=28),
)
@settings(max_examples=200)
def test_suffix_always_6_digits(year: int, month: int, day: int) -> None:
    date_str = f"{year:04d}-{month:02d}-{day:02d}"
    suffix = _date_to_suffix(date_str)
    assert len(suffix) == 6
    assert suffix.isdigit()


# -- _date_range ---------------------------------------------------------------


@pytest.mark.parametrize("start,end,expected", [
    ("2026-06-15", "2026-06-15", ["2026-06-15"]),
    ("2026-06-15", "2026-06-17", ["2026-06-15", "2026-06-16", "2026-06-17"]),
    ("2026-12-30", "2027-01-02", ["2026-12-30", "2026-12-31", "2027-01-01", "2027-01-02"]),
])
def test_date_range(start: str, end: str, expected: list[str]) -> None:
    assert _date_range(start, end) == expected


def test_date_range_empty_when_start_after_end() -> None:
    assert _date_range("2026-06-17", "2026-06-15") == []


@given(
    year=st.integers(min_value=2020, max_value=2030),
    month=st.integers(min_value=1, max_value=12),
    day=st.integers(min_value=1, max_value=28),
    span=st.integers(min_value=0, max_value=60),
)
@settings(max_examples=100)
def test_date_range_length(year: int, month: int, day: int, span: int) -> None:
    start = date(year, month, day)
    end = start + timedelta(days=span)
    result = _date_range(start.isoformat(), end.isoformat())
    assert len(result) == span + 1


# -- _search_api_count ---------------------------------------------------------


# -- _weekly_chunks ------------------------------------------------------------


@pytest.mark.parametrize("start,end,expected", [
    ("2026-06-01", "2026-06-07", [("2026-06-01", "2026-06-07")]),
    ("2026-06-01", "2026-06-14", [("2026-06-01", "2026-06-07"), ("2026-06-08", "2026-06-14")]),
    ("2026-06-01", "2026-06-10", [("2026-06-01", "2026-06-07"), ("2026-06-08", "2026-06-10")]),
    ("2026-06-15", "2026-06-15", [("2026-06-15", "2026-06-15")]),
])
def test_weekly_chunks(start: str, end: str, expected: list[tuple[str, str]]) -> None:
    assert _weekly_chunks(start, end) == expected


def test_weekly_chunks_cover_full_range() -> None:
    """Every day in the range should appear in exactly one chunk."""
    chunks = _weekly_chunks("2026-01-01", "2026-03-15")
    all_days: list[str] = []
    for chunk_start, chunk_end in chunks:
        all_days.extend(_date_range(chunk_start, chunk_end))
    expected = _date_range("2026-01-01", "2026-03-15")
    assert all_days == expected


# -- _search_api_count ---------------------------------------------------------


@pytest.mark.asyncio
async def test_search_api_count_single_day() -> None:
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = {"total_count": 1515}

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = mock_response

    result = await _search_api_count(mock_client, "greptile-apps[bot]", "2026-06-15")
    assert result == 1515

    call_args = mock_client.get.call_args
    assert call_args[1]["params"]["q"] == "type:pr reviewed-by:greptile-apps[bot] created:2026-06-15..2026-06-15"


@pytest.mark.asyncio
async def test_search_api_count_date_range() -> None:
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = {"total_count": 5000}

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = mock_response

    result = await _search_api_count(mock_client, "coderabbitai[bot]", "2026-06-01", "2026-06-07")
    assert result == 5000

    call_args = mock_client.get.call_args
    assert call_args[1]["params"]["q"] == "type:pr reviewed-by:coderabbitai[bot] created:2026-06-01..2026-06-07"


@pytest.mark.asyncio
async def test_search_api_count_422_returns_unsearchable() -> None:
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 422

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = mock_response

    result = await _search_api_count(mock_client, "Copilot", "2026-06-15")
    assert result is SearchError.UNSEARCHABLE


@pytest.mark.asyncio
async def test_search_api_count_retries_on_rate_limit() -> None:
    rate_limited = MagicMock(spec=httpx.Response)
    rate_limited.status_code = 403
    rate_limited.headers = {"Retry-After": "1"}

    success = MagicMock(spec=httpx.Response)
    success.status_code = 200
    success.json.return_value = {"total_count": 42}

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.side_effect = [rate_limited, success]

    result = await _search_api_count(mock_client, "test[bot]", "2026-06-15")
    assert result == 42
    assert mock_client.get.call_count == 2

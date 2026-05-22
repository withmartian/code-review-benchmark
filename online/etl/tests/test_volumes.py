"""Tests for volumes.py utility functions: date/suffix conversion."""

from __future__ import annotations

from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st
import pytest

from pipeline.volumes import _date_to_suffix
from pipeline.volumes import _suffix_to_date

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

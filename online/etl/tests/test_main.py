"""Tests for CLI helpers in main.py."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

from main import _parse_time_bound


class TestParseTimeBound:
    def test_none_passes_through(self) -> None:
        assert _parse_time_bound(None) is None

    def test_empty_string_passes_through(self) -> None:
        assert _parse_time_bound("") is None

    def test_relative_form(self) -> None:
        result = _parse_time_bound("7d")
        assert result is not None
        # Should round-trip through fromisoformat and land roughly 7 days back
        parsed = datetime.fromisoformat(result)
        delta = datetime.now(UTC) - parsed
        # Allow up to a few seconds of drift between call sites
        assert timedelta(days=7) - timedelta(seconds=5) <= delta <= timedelta(days=7) + timedelta(seconds=5)

    def test_bare_date_is_normalized_to_midnight_utc(self) -> None:
        # The bug fix: asyncpg rejects bare-date strings when binding to
        # a timestamptz column, so we expand them here.
        result = _parse_time_bound("2026-04-18")
        assert result == "2026-04-18T00:00:00+00:00"
        # And the result must be parseable as a tz-aware datetime
        dt = datetime.fromisoformat(result)
        assert dt.tzinfo is not None
        assert dt.year == 2026 and dt.month == 4 and dt.day == 18

    def test_full_iso_timestamp_passes_through(self) -> None:
        # Already a full ISO timestamp — leave it alone, _coerce_args will
        # convert it to a datetime when binding.
        value = "2026-04-18T12:34:56+00:00"
        assert _parse_time_bound(value) == value

    def test_iso_with_space_separator_passes_through(self) -> None:
        value = "2026-04-18 12:34:56+00:00"
        assert _parse_time_bound(value) == value

"""Tests for llm/schemas.py: Pydantic model validation and serialization."""

from __future__ import annotations

from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st
from pydantic import ValidationError
import pytest

from llm.schemas import BotSuggestion
from llm.schemas import BotSuggestionsResponse
from llm.schemas import HumanAction
from llm.schemas import HumanActionsResponse
from llm.schemas import MatchingResponse
from llm.schemas import MatchResult
from llm.schemas import PRLabels
from llm.schemas import PRLabelsResponse

# -- BotSuggestion -----------------------------------------------------------


class TestBotSuggestion:
    def test_valid_minimal(self) -> None:
        s = BotSuggestion(issue_id="S1", description="Fix bug", category="bug")
        assert s.issue_id == "S1"
        assert s.severity == "medium"
        assert s.file_path is None
        assert s.line_number is None

    def test_valid_full(self) -> None:
        s = BotSuggestion(
            issue_id="S1", description="Fix bug", category="bug",
            file_path="main.py", line_number=42, severity="critical",
        )
        assert s.file_path == "main.py"
        assert s.line_number == 42
        assert s.severity == "critical"

    def test_missing_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            BotSuggestion(issue_id="S1")  # type: ignore[call-arg]

    def test_serialization(self) -> None:
        s = BotSuggestion(issue_id="S1", description="d", category="bug")
        d = s.model_dump()
        assert d["issue_id"] == "S1"
        assert "severity" in d


class TestBotSuggestionsResponse:
    def test_empty(self) -> None:
        r = BotSuggestionsResponse(suggestions=[])
        assert r.suggestions == []

    def test_multiple(self) -> None:
        r = BotSuggestionsResponse(suggestions=[
            BotSuggestion(issue_id="S1", description="a", category="bug"),
            BotSuggestion(issue_id="S2", description="b", category="style"),
        ])
        assert len(r.suggestions) == 2


# -- HumanAction -------------------------------------------------------------


class TestHumanAction:
    def test_valid(self) -> None:
        a = HumanAction(
            action_id="A1", description="Fixed null check", category="bug",
            action_type="fix",
        )
        assert a.action_id == "A1"
        assert a.file_path is None
        assert a.commit_sha is None

    def test_full(self) -> None:
        a = HumanAction(
            action_id="A1", description="d", category="refactor",
            file_path="x.py", commit_sha="abc123", action_type="improvement",
        )
        assert a.file_path == "x.py"
        assert a.commit_sha == "abc123"


class TestHumanActionsResponse:
    def test_empty(self) -> None:
        r = HumanActionsResponse(actions=[])
        assert r.actions == []


# -- MatchResult --------------------------------------------------------------


class TestMatchResult:
    def test_matched(self) -> None:
        m = MatchResult(
            bot_issue_id="S1", human_action_id="A1",
            matched=True, confidence=0.95, reasoning="Same file and issue",
        )
        assert m.matched is True
        assert m.confidence == 0.95

    def test_unmatched(self) -> None:
        m = MatchResult(
            bot_issue_id="S2", human_action_id=None,
            matched=False, confidence=0.1, reasoning="No related change",
        )
        assert m.matched is False
        assert m.human_action_id is None


class TestMatchingResponse:
    def test_round_trip(self) -> None:
        r = MatchingResponse(matches=[
            MatchResult(bot_issue_id="S1", matched=True, confidence=0.9, reasoning="ok"),
        ])
        d = r.model_dump()
        r2 = MatchingResponse.model_validate(d)
        assert r2.matches[0].bot_issue_id == "S1"


# -- PRLabels -----------------------------------------------------------------


class TestPRLabels:
    def test_valid(self) -> None:
        labels = PRLabels(
            language="Python",
            languages=["Python", "JavaScript"],
            domain="backend",
            pr_type="bugfix",
            issue_types=["bug", "performance"],
            severity="high",
            test_changes=True,
        )
        assert labels.language == "Python"
        assert labels.framework is None

    def test_with_framework(self) -> None:
        labels = PRLabels(
            language="TypeScript",
            languages=["TypeScript"],
            domain="frontend",
            pr_type="feature",
            issue_types=["style"],
            severity="low",
            framework="React",
            test_changes=False,
        )
        assert labels.framework == "React"

    def test_serialization_keys(self) -> None:
        labels = PRLabels(
            language="Go", languages=["Go"], domain="backend",
            pr_type="refactor", issue_types=[], severity="medium",
            test_changes=False,
        )
        d = labels.model_dump()
        expected_keys = {"language", "languages", "domain", "pr_type", "issue_types", "severity", "framework", "test_changes"}
        assert set(d.keys()) == expected_keys


class TestPRLabelsResponse:
    def test_wrapper(self) -> None:
        r = PRLabelsResponse(labels=PRLabels(
            language="Rust", languages=["Rust"], domain="backend",
            pr_type="feature", issue_types=[], severity="medium",
            test_changes=True,
        ))
        assert r.labels.language == "Rust"


# -- Property-based: BotSuggestion roundtrip ----------------------------------


@given(
    issue_id=st.text(min_size=1, max_size=10),
    description=st.text(min_size=1, max_size=100),
    category=st.sampled_from(["bug", "style", "performance", "security", "refactor", "documentation", "other"]),
    severity=st.sampled_from(["low", "medium", "high", "critical"]),
)
@settings(max_examples=100)
def test_bot_suggestion_roundtrip(issue_id: str, description: str, category: str, severity: str) -> None:
    s = BotSuggestion(issue_id=issue_id, description=description, category=category, severity=severity)
    d = s.model_dump()
    s2 = BotSuggestion.model_validate(d)
    assert s2.issue_id == issue_id
    assert s2.category == category


@given(
    confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    matched=st.booleans(),
)
@settings(max_examples=100)
def test_match_result_confidence_range(confidence: float, matched: bool) -> None:
    m = MatchResult(
        bot_issue_id="S1", matched=matched,
        confidence=confidence, reasoning="test",
    )
    assert 0.0 <= m.confidence <= 1.0

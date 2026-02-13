"""Pydantic models for structured LLM output."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BotSuggestion(BaseModel):
    issue_id: str = Field(description="Unique identifier for this suggestion (e.g. 'S1', 'S2')")
    description: str = Field(description="What the bot suggested or flagged")
    category: str = Field(description="Category: 'bug', 'style', 'performance', 'security', 'refactor', 'documentation', 'other'")
    file_path: str | None = Field(default=None, description="File path the suggestion relates to")
    line_number: int | None = Field(default=None, description="Line number if applicable")
    severity: str = Field(default="medium", description="Severity: 'low', 'medium', 'high', 'critical'")


class BotSuggestionsResponse(BaseModel):
    suggestions: list[BotSuggestion] = Field(description="All actionable suggestions made by the bot")


class HumanAction(BaseModel):
    action_id: str = Field(description="Unique identifier for this action (e.g. 'A1', 'A2')")
    description: str = Field(description="The specific code issue that was fixed (what was wrong)")
    category: str = Field(description="Issue category: 'bug', 'style', 'performance', 'security', 'refactor', 'documentation', 'other'")
    file_path: str | None = Field(default=None, description="File path where the fix was applied")
    commit_sha: str | None = Field(default=None, description="Commit SHA that fixed this issue")
    action_type: str = Field(description="Why it was changed: 'fix', 'improvement', 'cleanup', 'new_feature', 'other'")


class HumanActionsResponse(BaseModel):
    actions: list[HumanAction] = Field(description="Concrete code issues fixed in post-review commits")


class MatchResult(BaseModel):
    bot_issue_id: str = Field(description="ID of the bot suggestion")
    human_action_id: str | None = Field(default=None, description="ID of the matching human action, if any")
    matched: bool = Field(description="Whether this bot suggestion was addressed by a human action")
    confidence: float = Field(description="Confidence score 0.0-1.0")
    reasoning: str = Field(description="Brief explanation of why this is/isn't a match")


class MatchingResponse(BaseModel):
    matches: list[MatchResult] = Field(description="Match results for each bot suggestion")


class PRLabels(BaseModel):
    language: str = Field(description="Primary programming language in the PR")
    languages: list[str] = Field(description="All programming languages present in the PR")
    domain: str = Field(description="Domain: 'frontend', 'backend', 'infra', 'fullstack', 'docs', 'other'")
    pr_type: str = Field(description="PR type: 'feature', 'bugfix', 'refactor', 'chore', 'docs', 'test', 'other'")
    issue_types: list[str] = Field(description="Issue types found: 'bug', 'style', 'performance', 'security', 'refactor', 'docs', 'other'")
    severity: str = Field(description="Overall severity: 'low', 'medium', 'high', 'critical'")
    framework: str | None = Field(default=None, description="Framework detected (e.g. React, Django) or null")
    test_changes: bool = Field(description="Whether the PR includes test file changes")


class PRLabelsResponse(BaseModel):
    labels: PRLabels

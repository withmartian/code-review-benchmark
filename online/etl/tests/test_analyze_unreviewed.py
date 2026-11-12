"""Tests for leaving out of recall the fixes of code the bot said it did not review."""

from __future__ import annotations

import json

from llm.schemas import BotSuggestionsResponse
from llm.schemas import HumanActionsResponse
from llm.schemas import MatchingResponse
from pipeline.analyze import _with_current_issue_comments
from pipeline.analyze import analyze_single_pr

BOT = "coderabbitai[bot]"

COMMITS = [
    {"sha": "a" * 40, "message": "Add parser", "date": "2026-07-01T10:00:00Z", "author": "dev"},
    {"sha": "b" * 40, "message": "Fix parser null check", "date": "2026-07-01T12:00:00Z", "author": "dev"},
    {"sha": "c" * 40, "message": "Add exporter", "date": "2026-07-01T14:00:00Z", "author": "dev"},
    {"sha": "d" * 40, "message": "Fix exporter path", "date": "2026-07-01T15:00:00Z", "author": "dev"},
]

PATCHES = {
    "a": ("parser.py", "added", "@@ -0,0 +1,2 @@\n+def parse(text):\n+    return text.split(',')"),
    "b": (
        "parser.py",
        "modified",
        "@@ -1,2 +1,4 @@\n def parse(text):\n+    if text is None:\n+        return []\n     return text.split(',')",
    ),
    "c": ("exporter.py", "added", "@@ -0,0 +1,2 @@\n+def export(rows):\n+    return write('/tmp/out.csv', rows)"),
    "d": (
        "exporter.py",
        "modified",
        "@@ -1,2 +1,2 @@\n def export(rows):\n-    return write('/tmp/out.csv', rows)\n+    return write(OUT_PATH, rows)",
    ),
}

ACTIONS = [
    {"action_id": "A1", "file_path": "parser.py", "commit_sha": "b" * 12, "description": "Null check missing"},
    {"action_id": "A2", "file_path": "exporter.py", "commit_sha": "d" * 12, "description": "Wrong export path"},
]


def _details(patches: dict[str, tuple[str, str, str]]) -> list[dict]:
    return [
        {"sha": k * 40, "files": [{"filename": path, "status": status, "patch": patch}]}
        for k, (path, status, patch) in patches.items()
    ]


def _notice(kind: str, text: str) -> str:
    return (
        f"<!-- This is an auto-generated comment: {kind} by coderabbit.ai -->\n{text}\n"
        f"<!-- end of auto-generated comment: {kind} by coderabbit.ai -->"
    )


RATE_LIMITED = _notice(
    "rate limited",
    "> [!WARNING]\n> ## Review limit reached\n>\n> You've used all free OSS reviews for now.\n>\n"
    f"> Reviewing files that changed from the base of the PR and between {'b' * 40} and {'d' * 40}.",
)
INCREMENTAL_OFF = _notice(
    "skip review", "> [!IMPORTANT]\n> ## Review skipped\n>\n> Auto incremental reviews are disabled on this repository."
)


def test_current_issue_comments_replace_timeline_bodies() -> None:
    events = [
        {
            "actor": BOT,
            "event_type": "issue_comment",
            "timestamp": "2026-07-01T10:01:00Z",
            "data": {"body": "Reviewing"},
        },
        {"actor": BOT, "event_type": "review", "timestamp": "2026-07-01T10:05:00Z", "data": {"body": "Finding."}},
    ]
    current = [
        {
            "id": 1,
            "author": BOT,
            "body": "Review limit reached",
            "created_at": "2026-07-01T10:01:00Z",
            "updated_at": "2026-07-01T14:01:00Z",
        }
    ]

    merged = _with_current_issue_comments(events, current)

    assert [e["event_type"] for e in merged] == ["issue_comment", "review"]
    assert merged[0]["data"]["body"] == "Review limit reached"
    assert merged[0]["data"]["updated_at"] == "2026-07-01T14:01:00Z"
    assert _with_current_issue_comments(events, []) is events


class FakeLLM:
    def __init__(self, matched_action: str = "A1", actions: list[dict] | None = None) -> None:
        self.matched_action = matched_action
        self.actions = actions or ACTIONS
        self.prompts: list[str] = []

    async def structured_completion(self, prompt: str, schema: type) -> object:
        self.prompts.append(prompt)
        if schema is BotSuggestionsResponse:
            return schema(suggestions=[{"issue_id": "S1", "description": "Null check missing", "category": "bug"}])
        if schema is HumanActionsResponse:
            return schema(actions=[{**a, "category": "bug", "action_type": "fix"} for a in self.actions])
        if schema is MatchingResponse:
            return schema(
                matches=[
                    {
                        "bot_issue_id": "S1",
                        "human_action_id": self.matched_action,
                        "matched": True,
                        "confidence": 0.9,
                        "reasoning": "r",
                    }
                ]
            )
        raise AssertionError(schema)


class FakeRepo:
    def __init__(self) -> None:
        self.stored: dict = {}

    async def insert_analysis(self, **kwargs: object) -> None:
        self.stored = kwargs


def _review(sha: str, ts: str, body: str) -> dict:
    return {
        "actor": BOT,
        "event_type": "review",
        "timestamp": ts,
        "data": {"state": "commented", "body": body, "commit_id": sha},
    }


def _pr_row(notice: str = RATE_LIMITED, patches: dict | None = None) -> dict:
    # The bot reviewed a (first review) and b, then was rate limited on c and d.
    events = [
        {
            "actor": BOT,
            "event_type": "issue_comment",
            "timestamp": "2026-07-01T10:01:00Z",
            "data": {"body": "Summary of the change."},
        },
        _review("a" * 40, "2026-07-01T11:00:00Z", "Null check missing."),
        _review("b" * 40, "2026-07-01T12:30:00Z", "**Actionable comments posted: 0**"),
    ]
    issue_comments = [
        {
            "id": 1,
            "author": BOT,
            "body": f"<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n{notice}\nWalkthrough",
            "created_at": "2026-07-01T10:01:00Z",
            "updated_at": "2026-07-01T15:01:00Z",
        }
    ]
    return {
        "id": 1,
        "chatbot_id": 1,
        "repo_name": "owner/repo",
        "pr_number": 7,
        "assembled": json.dumps({"events": events, "pr_title": "Parser", "pr_author": "dev"}),
        "commits": json.dumps(COMMITS),
        "commit_details": json.dumps(_details(patches or PATCHES)),
        "reviews": json.dumps([{"author": BOT, "commit_id": "a" * 40, "body": "Null check missing."}]),
        "issue_comments": json.dumps(issue_comments),
    }


async def test_a_fix_of_code_written_after_the_rate_limit_is_left_out_of_recall() -> None:
    llm = FakeLLM()
    repo = FakeRepo()

    assert await analyze_single_pr(llm, repo, _pr_row(), BOT, "model")

    assert len(llm.prompts) == 3
    assert repo.stored["recall"] == 1.0
    assert repo.stored["precision"] == 1.0
    actions = {a["action_id"]: a for a in repo.stored["human_actions"]}
    assert "excluded_from_recall" not in actions["A1"]
    assert actions["A2"]["excluded_from_recall"].startswith(f"limit notice; last reviewed commit {'b' * 12}; ")
    assert actions["A2"]["unreviewed_commits"] == ["c" * 40, "d" * 40]


async def test_a_fix_of_reviewed_code_in_an_unreviewed_commit_still_counts() -> None:
    # d fixes the parser line written in a, which the bot reviewed.
    patches = {
        **PATCHES,
        "d": (
            "parser.py",
            "modified",
            "@@ -1,4 +1,4 @@\n def parse(text):\n     if text is None:\n         return []\n"
            "-    return text.split(',')\n+    return text.split(';')",
        ),
    }
    llm = FakeLLM(actions=[ACTIONS[0], {**ACTIONS[1], "file_path": "parser.py"}])
    repo = FakeRepo()

    assert await analyze_single_pr(llm, repo, _pr_row(patches=patches), BOT, "model")

    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])


async def test_a_skip_by_configuration_excludes_nothing() -> None:
    repo = FakeRepo()

    assert await analyze_single_pr(FakeLLM(), repo, _pr_row(notice=INCREMENTAL_OFF), BOT, "model")

    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])


async def test_an_action_the_bot_matched_always_counts() -> None:
    # The bot matched the exporter fix; the unmatched parser fix is in a commit it reviewed.
    repo = FakeRepo()

    assert await analyze_single_pr(FakeLLM(matched_action="A2"), repo, _pr_row(), BOT, "model")

    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])


async def test_without_fetched_issue_comments_nothing_is_excluded() -> None:
    row = _pr_row()
    row["issue_comments"] = None
    repo = FakeRepo()

    assert await analyze_single_pr(FakeLLM(), repo, row, BOT, "model")

    assert repo.stored["recall"] == 0.5

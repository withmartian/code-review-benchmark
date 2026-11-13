"""Tests for leaving out of recall the fixes of code no review of the bot read."""

from __future__ import annotations

import json

import pytest

from llm.schemas import BotSuggestionsResponse
from llm.schemas import HumanActionsResponse
from llm.schemas import MatchingResponse
from pipeline.analyze import _with_comment_revisions
from pipeline.analyze import _with_current_issue_comments
from pipeline.analyze import analyze_single_pr
from pipeline.review_coverage import find_review_records

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
FEWER_STARS = _notice(
    "skip review",
    "> [!IMPORTANT]\n> - [ ] Trigger review\n>\n"
    "> This repository does not receive automatic reviews because it has fewer than 10 stars.",
)


def _recent_review(start: str, head: str) -> str:
    """The summary comment's record of a review that posted nothing."""
    return (
        "<!-- recent_review_start -->\n\nNo actionable comments were generated in the recent review. 🎉\n\n"
        "<details>\n<summary>📥 Commits</summary>\n\nReviewing files that changed from the base of the PR and "
        f"between {start} and {head}.\n\n</details>\n\n<!-- recent_review_end -->"
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
    assert actions["A2"]["excluded_from_recall"].startswith(f"last review before the fix read {'b' * 12}; all 1 lines")
    assert actions["A2"]["excluded_from_recall"].endswith("; the bot posted limit notices")
    assert actions["A2"]["unreviewed_commits"] == ["c" * 40]


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


@pytest.mark.parametrize("skip", [INCREMENTAL_OFF, FEWER_STARS])
async def test_pushes_skipped_by_configuration_or_policy_are_covered_without_a_notice(skip: str) -> None:
    # Incremental reviews off, or the free plan's star rule: the bot reviewed a and b only.
    repo = FakeRepo()

    assert await analyze_single_pr(FakeLLM(), repo, _pr_row(notice=skip), BOT, "model")

    assert repo.stored["recall"] == 1.0
    actions = {a["action_id"]: a for a in repo.stored["human_actions"]}
    assert actions["A2"]["excluded_from_recall"] == (
        f"last review before the fix read {'b' * 12}; all 1 lines the fix changed in exporter.py and the 1 "
        f"unchanged code lines next to its additions were written in commits no review of the bot read before "
        f"the fix ({'c' * 12})"
    )


async def test_an_action_the_bot_matched_always_counts() -> None:
    # The bot matched the exporter fix; the unmatched parser fix is in a commit it reviewed.
    repo = FakeRepo()

    assert await analyze_single_pr(FakeLLM(matched_action="A2"), repo, _pr_row(), BOT, "model")

    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])


async def test_a_review_that_posted_nothing_counts_through_the_summary_record() -> None:
    # The bot reviewed c without posting anything; its summary comment records the range b..c.
    row = _pr_row(notice=_recent_review("b" * 40, "c" * 40))
    repo = FakeRepo()

    assert await analyze_single_pr(FakeLLM(), repo, row, BOT, "model")

    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])

    # Comments never read (NULL): the record may hold reviews the posted ones do not show, so for a
    # bot that keeps its record in comments no fix is left out.
    row["issue_comments"] = None
    assert await analyze_single_pr(FakeLLM(), repo, row, BOT, "model")
    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])


async def test_comments_never_read_keep_every_fix_only_for_a_bot_with_a_record_in_comments() -> None:
    # The rate-limit case leaves the exporter fix out once the comments were read, even when none is
    # the bot's; with them never read it is kept for CodeRabbit.
    row = _pr_row()
    row["issue_comments"] = json.dumps([])
    repo = FakeRepo()
    assert await analyze_single_pr(FakeLLM(), repo, row, BOT, "model")
    assert repo.stored["recall"] == 1.0

    row["issue_comments"] = None
    assert await analyze_single_pr(FakeLLM(), repo, row, BOT, "model")
    assert repo.stored["recall"] == 0.5

    # A bot without a record (its reviews are its posted reviews) is read the same either way.
    cubic = "cubic-dev-ai[bot]"
    other = json.loads(json.dumps(row).replace(BOT, cubic))
    assert await analyze_single_pr(FakeLLM(), repo, other, cubic, "model")
    assert repo.stored["recall"] == 1.0


SUMMARY = "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\nWalkthrough"


def _gap_row(edits: list[dict] | None) -> dict:
    # The bot reviewed a and b, was rate limited on c, then reviewed d after the limit reset and
    # rewrote its summary comment: the limit notice is only in the comment's edit history.
    row = _pr_row()
    assembled = json.loads(row["assembled"])
    assembled["events"].append(_review("d" * 40, "2026-07-01T15:30:00Z", "**Actionable comments posted: 0**"))
    row["assembled"] = json.dumps(assembled)
    current = json.loads(row["issue_comments"])
    current[0]["body"] = SUMMARY
    row["issue_comments"] = json.dumps(current)
    row["issue_comment_edits"] = None if edits is None else json.dumps(edits)
    return row


GAP_NOTICE = _notice(
    "rate limited",
    "> [!WARNING]\n> ## Review limit reached\n>\n"
    f"> Reviewing files that changed from the base of the PR and between {'b' * 40} and {'c' * 40}.",
)
EDITS = [
    {
        "id": 1,
        "author": BOT,
        "revisions": [
            {"edited_at": "2026-07-01T10:01:00Z", "body": SUMMARY},
            {"edited_at": "2026-07-01T14:01:00Z", "body": f"{SUMMARY}\n{GAP_NOTICE}"},
            {"edited_at": "2026-07-01T15:31:00Z", "body": SUMMARY},
        ],
    }
]


def test_comment_revisions_are_added_at_their_edit_time() -> None:
    events = [{"actor": BOT, "event_type": "review", "timestamp": "2026-07-01T12:00:00Z", "data": {"body": "x"}}]
    current = [{"id": 1, "author": BOT, "body": SUMMARY}]

    merged = _with_comment_revisions(events, EDITS, current)

    assert [(e["timestamp"], e["event_type"]) for e in merged] == [
        ("2026-07-01T10:01:00Z", "issue_comment"),
        ("2026-07-01T12:00:00Z", "review"),
        ("2026-07-01T14:01:00Z", "issue_comment"),
    ]
    assert merged[2]["data"]["revision_of"] == 1
    # Without the current body, the newest revision is added as well.
    assert len(_with_comment_revisions(events, EDITS, [])) == 4
    assert _with_comment_revisions(events, [], current) is events


def test_a_timeline_comment_the_api_no_longer_returns_keeps_its_record() -> None:
    # Greptile's summary recorded a review of c; the comment was deleted before the comments were read,
    # so the API list holds only a person's comment. Its record still shows c reviewed.
    greptile = "greptile-apps[bot]"
    summary = "Last reviewed commit: [Exporter](https://github.com/owner/repo/commit/" + "c" * 40 + ")"
    events = [
        _review("a" * 40, "2026-07-01T11:00:00Z", "Null check missing."),
        {
            "actor": greptile,
            "event_type": "issue_comment",
            "timestamp": "2026-07-01T13:00:00Z",
            "data": {"comment_id": 9, "body": summary},
        },
    ]
    events[0]["actor"] = greptile
    current = [{"id": 5, "author": "dev", "body": "thanks", "created_at": "2026-07-01T14:00:00Z"}]
    commits = [{"sha": s * 40} for s in "abcd"]

    merged = _with_current_issue_comments(events, current)

    assert [r.head for r in find_review_records(merged, commits, greptile)] == [0, 2]
    kept = next(e for e in merged if e["data"].get("comment_id") == 9)
    assert kept["data"]["revision_of"] == 9  # the body first posted, not known to be the last
    # A timeline comment the API list holds is replaced by its current body, as before.
    current.append({"id": 9, "author": greptile, "body": "rewritten", "created_at": "2026-07-01T13:00:00Z"})
    assert [e["data"]["body"] for e in _with_current_issue_comments(events, current)][1:] == ["rewritten", "thanks"]


def _revision_bodies(events: list[dict]) -> list[str]:
    return [e["data"]["body"] for e in events if e["data"].get("revision_of") is not None]


def test_the_current_body_is_left_out_by_its_text_when_two_edits_share_a_time() -> None:
    # The record and the current body were saved in the same second; sorted by time, the record can
    # come last. Only the revision that is the current body is left out.
    record = f"{SUMMARY}\n{_recent_review('b' * 40, 'c' * 40)}"
    edits = [{"id": 1, "author": BOT, "revisions": [
        {"edited_at": "2026-07-01T14:01:00Z", "body": SUMMARY},
        {"edited_at": "2026-07-01T14:01:00Z", "body": record},
    ]}]  # fmt: skip
    current = [{"id": 1, "author": BOT, "body": SUMMARY}]

    assert _revision_bodies(_with_comment_revisions([], edits, current)) == [record]


def test_every_revision_is_kept_when_the_current_body_is_not_among_them() -> None:
    # GitHub kept no text for the edit that wrote the current body (a null diff, filtered out), so the
    # newest revision left is an earlier state holding the record: it is kept.
    record = f"{SUMMARY}\n{_recent_review('b' * 40, 'c' * 40)}"
    edits = [{"id": 1, "author": BOT, "revisions": [
        {"edited_at": "2026-07-01T10:01:00Z", "body": SUMMARY},
        {"edited_at": "2026-07-01T14:01:00Z", "body": record},
    ]}]  # fmt: skip
    current = [{"id": 1, "author": BOT, "body": f"{SUMMARY}\nrewritten"}]

    assert _revision_bodies(_with_comment_revisions([], edits, current)) == [SUMMARY, record]


@pytest.mark.parametrize("edits", [EDITS, None, []])
async def test_a_fix_reviewed_only_in_its_fixed_state_is_left_out_with_or_without_the_notice(
    edits: list[dict] | None,
) -> None:
    # d fixes the export path written in c; the review of d saw only the fixed line. The rule needs
    # no notice: the limit notice in the edit history only adds its reason to the label.
    repo = FakeRepo()

    assert await analyze_single_pr(FakeLLM(), repo, _gap_row(edits), BOT, "model")

    assert repo.stored["recall"] == 1.0
    actions = {a["action_id"]: a for a in repo.stored["human_actions"]}
    assert actions["A2"]["excluded_from_recall"].startswith(f"last review before the fix read {'b' * 12}; ")
    assert actions["A2"]["excluded_from_recall"].endswith("; the bot posted limit notices") == (edits == EDITS)
    assert actions["A2"]["unreviewed_commits"] == ["c" * 40]


async def test_a_reviewed_commit_amended_by_a_force_push_keeps_every_fix() -> None:
    # The bot reviewed b, which adds a guard to the parser; the author then amended b into b2 and
    # force-pushed, so b is no longer in the commit list and its review cannot be placed. d fixes the
    # guard's log line, which the bot read in b: without the guard b2 reads as never reviewed and the
    # fix would be left out.
    guard = "+    if text is None:\n+        log('empty input')\n+        return []\n"
    patches = {
        **PATCHES,
        "b": ("parser.py", "modified", "@@ -1,2 +1,5 @@\n def parse(text):\n" + guard + "     return text.split(',')"),
        "d": (
            "parser.py",
            "modified",
            "@@ -2,3 +2,3 @@\n     if text is None:\n-        log('empty input')\n+        log.warning('empty input')\n"
            "         return []",
        ),
    }
    row = _pr_row(patches=patches)
    llm = FakeLLM(actions=[ACTIONS[0], {**ACTIONS[1], "file_path": "parser.py"}])

    repo = FakeRepo()
    assert await analyze_single_pr(llm, repo, dict(row), BOT, "model")
    assert repo.stored["recall"] == 0.5  # b in place: its review read the line, so the fix counts

    amended = "9" * 40
    row["commits"] = row["commits"].replace("b" * 40, amended)
    row["commit_details"] = row["commit_details"].replace("b" * 40, amended)
    assert await analyze_single_pr(llm, repo, row, BOT, "model")

    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])


async def test_a_review_recorded_only_in_the_edit_history_keeps_the_fix() -> None:
    # The summary comment once recorded a review of c that posted nothing; the current body no longer does.
    edits = [{"id": 1, "author": BOT, "revisions": [
        {"edited_at": "2026-07-01T14:01:00Z", "body": f"{SUMMARY}\n{_recent_review('b' * 40, 'c' * 40)}"},
        {"edited_at": "2026-07-01T15:31:00Z", "body": SUMMARY},
    ]}]  # fmt: skip
    repo = FakeRepo()

    assert await analyze_single_pr(FakeLLM(), repo, _gap_row(edits), BOT, "model")

    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])

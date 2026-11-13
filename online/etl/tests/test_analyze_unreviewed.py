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


def _commits_read(start: str, head: str) -> str:
    """The "📥 Commits" block of a CodeRabbit review body: the range the review read."""
    return (
        "<details>\n<summary>📥 Commits</summary>\n\nReviewing files that changed from the base of the PR and "
        f"between {start} and {head}.\n\n</details>"
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
    assert _with_current_issue_comments(events, None) is events  # never read
    # An empty read holds none of them: the summary first posted is kept as an earlier state.
    gone = _with_current_issue_comments(events, [])
    assert gone[0]["data"]["body"] == "Reviewing"
    assert gone[0]["data"]["revision_of"] == ""


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
        _review("a" * 40, "2026-07-01T11:00:00Z", f"Null check missing.\n{_commits_read('f' * 40, 'a' * 40)}"),
        _review(
            "b" * 40, "2026-07-01T12:30:00Z", f"**Actionable comments posted: 0**\n{_commits_read('a' * 40, 'b' * 40)}"
        ),
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
async def test_pushes_skipped_by_configuration_or_policy_are_covered(skip: str) -> None:
    # Incremental reviews off, or the free plan's star rule: the bot reviewed a and b only.
    repo = FakeRepo()

    assert await analyze_single_pr(FakeLLM(), repo, _pr_row(notice=skip), BOT, "model")

    assert repo.stored["recall"] == 1.0
    actions = {a["action_id"]: a for a in repo.stored["human_actions"]}
    assert actions["A2"]["excluded_from_recall"] == (
        f"last review before the fix read {'b' * 12}; all 1 lines the fix changed in exporter.py were written in "
        f"commits no review of the bot read before the fix ({'c' * 12}); the bot posted skip notices"
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


async def test_comments_never_read_keep_every_fix() -> None:
    # The rate-limit case leaves the exporter fix out once the comments were read; with them never
    # read it is kept for CodeRabbit.
    row = _pr_row()
    repo = FakeRepo()
    assert await analyze_single_pr(FakeLLM(), repo, dict(row), BOT, "model")
    assert repo.stored["recall"] == 1.0

    # A read that returned none of the bot's comments, empty or with a person's comment only: the
    # summary the timeline holds as first posted is gone, its later edits were never read, so the
    # fix is kept, as with the comments never read.
    person = {"id": 5, "author": "dev", "body": "thanks", "created_at": "2026-07-01T16:00:00Z"}
    for read in ([], [person]):
        row["issue_comments"] = json.dumps(read)
        assert await analyze_single_pr(FakeLLM(), repo, dict(row), BOT, "model")
        assert repo.stored["recall"] == 0.5, read
        assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])

    row["issue_comments"] = None
    assert await analyze_single_pr(FakeLLM(), repo, row, BOT, "model")
    assert repo.stored["recall"] == 0.5

    # A bot in none of the tables (cubic, Gemini) may post a later review as a conversation comment,
    # which is unplaceable: with its comments read and none there, the exporter fix is left out; with
    # them never read, it is kept. The fixture's conversation comments are CodeRabbit's, so they are
    # dropped for this bot.
    cubic = "cubic-dev-ai[bot]"
    other = json.loads(json.dumps(row).replace(BOT, cubic))
    assembled = json.loads(other["assembled"])
    assembled["events"] = [e for e in assembled["events"] if e["event_type"] != "issue_comment"]
    other["assembled"] = json.dumps(assembled)
    other["issue_comments"] = json.dumps([])
    assert await analyze_single_pr(FakeLLM(), repo, dict(other), cubic, "model")
    assert repo.stored["recall"] == 1.0
    other["issue_comments"] = None
    assert await analyze_single_pr(FakeLLM(), repo, other, cubic, "model")
    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])


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


async def test_a_record_bot_whose_record_is_missing_keeps_every_fix() -> None:
    # Greptile reviewed a and b, and writes its record in a summary comment. The comments were read but
    # hold no summary (deleted, say): a review it ran without posting cannot be seen, so no fix is left
    # out. With the summary present, the exporter fix of c is left out as before.
    greptile = "greptile-apps[bot]"
    row = json.loads(json.dumps(_pr_row()).replace(BOT, greptile))
    row["issue_comments"] = json.dumps(
        [{"id": 5, "author": "dev", "body": "thanks", "created_at": "2026-07-01T16:00:00Z"}]
    )
    repo = FakeRepo()

    assert await analyze_single_pr(FakeLLM(), repo, dict(row), greptile, "model")
    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])

    summary = f"Last reviewed commit: [Parser](https://github.com/owner/repo/commit/{'b' * 40})"
    # The summary the timeline holds as first posted (created 10:01), edited since.
    comments = [{"id": 6, "author": greptile, "body": summary, "created_at": "2026-07-01T10:01:00Z"}]
    row["issue_comments"] = json.dumps(comments)
    assert await analyze_single_pr(FakeLLM(), repo, row, greptile, "model")
    assert repo.stored["recall"] == 1.0


async def test_a_record_in_review_bodies_does_not_stand_in_for_the_summary_record() -> None:
    # CodeRabbit's review bodies record its reviews of a and b, but a review that posted nothing (of c,
    # say) is recorded only in the summary comment, which is gone: the comments read hold only a
    # person's. The body records do not show that no such review ran, so no fix is left out.
    row = _pr_row()
    row["issue_comments"] = json.dumps(
        [{"id": 5, "author": "dev", "body": "thanks", "created_at": "2026-07-01T16:00:00Z"}]
    )
    repo = FakeRepo()
    assert await analyze_single_pr(FakeLLM(), repo, dict(row), BOT, "model")
    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])

    # A summary that carries no range is CodeRabbit's normal state after a review that posted: with
    # the review bodies' records, the exporter fix of c is left out.
    row["issue_comments"] = json.dumps(
        [{"id": 1, "author": BOT, "body": SUMMARY, "created_at": "2026-07-01T10:01:00Z"}]
    )
    assert await analyze_single_pr(FakeLLM(), repo, dict(row), BOT, "model")
    assert repo.stored["recall"] == 1.0

    # With no record anywhere, neither in the summary nor in a review body, its reviews cannot be told
    # apart from silent ones, so nothing is left out.
    bare = dict(row)
    assembled = json.loads(row["assembled"])
    for e in assembled["events"]:
        if e["event_type"] == "review":
            e["data"]["body"] = e["data"]["body"].split("\n")[0]
    bare["assembled"] = json.dumps(assembled)
    assert await analyze_single_pr(FakeLLM(), repo, bare, BOT, "model")
    assert repo.stored["recall"] == 0.5

    # With the summary read, recording the review of b as the last, the exporter fix of c is left out.
    summary = (
        f"<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n{_recent_review('a' * 40, 'b' * 40)}"
    )
    row["issue_comments"] = json.dumps(
        [{"id": 1, "author": BOT, "body": summary, "created_at": "2026-07-01T10:01:00Z"}]
    )
    assert await analyze_single_pr(FakeLLM(), repo, row, BOT, "model")
    assert repo.stored["recall"] == 1.0


async def test_a_record_bot_with_an_unavailable_revision_keeps_every_fix() -> None:
    # Greptile's summary records its reviews of a and d, but one revision of it has no text left on
    # GitHub: a review of b recorded only there cannot be read, so no fix is left out.
    greptile = "greptile-apps[bot]"
    row = json.loads(json.dumps(_pr_row()).replace(BOT, greptile))
    summary = f"Last reviewed commit: [Parser](https://github.com/owner/repo/commit/{'b' * 40})"
    row["issue_comments"] = json.dumps(
        [{"id": 6, "author": greptile, "body": summary, "created_at": "2026-07-01T10:01:00Z"}]
    )
    repo = FakeRepo()
    assert await analyze_single_pr(FakeLLM(), repo, dict(row), greptile, "model")
    assert repo.stored["recall"] == 1.0  # the exporter fix of c is left out

    edits = [{"id": 6, "author": greptile, "revisions": [], "unavailable_edits": ["2026-07-01T13:00:00Z"]}]
    row["issue_comment_edits"] = json.dumps(edits)
    assert await analyze_single_pr(FakeLLM(), repo, dict(row), greptile, "model")
    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])

    # Another account's unavailable edit says nothing about the bot's record.
    row["issue_comment_edits"] = json.dumps([{**edits[0], "author": "other-bot[bot]"}])
    assert await analyze_single_pr(FakeLLM(), repo, row, greptile, "model")
    assert repo.stored["recall"] == 1.0


async def test_a_gh_archive_review_event_takes_its_commit_from_the_api_review() -> None:
    # cubic keeps no record: its reviews are placed by their commits. Its review events came from GH
    # Archive, which carries no commit; the API reviews with the same ids hold them. cubic reviewed b,
    # which wrote the guard d fixes, so the fix counts.
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
    cubic = "cubic-dev-ai[bot]"
    row = json.loads(json.dumps(_pr_row(patches=patches)).replace(BOT, cubic))
    assembled = json.loads(row["assembled"])
    # The fixture's conversation comments are CodeRabbit's; for cubic they would read as later
    # unplaceable reviews, so they are dropped and only the review events place its reviews.
    assembled["events"] = [e for e in assembled["events"] if e["event_type"] != "issue_comment"]
    row["issue_comments"] = json.dumps([])
    for n, e in enumerate(e for e in assembled["events"] if e["event_type"] == "review"):
        e["data"]["review_id"] = n + 1
        del e["data"]["commit_id"]
    row["assembled"] = json.dumps(assembled)
    row["reviews"] = json.dumps(
        [{"id": 1, "author": cubic, "commit_id": "a" * 40}, {"id": 2, "author": cubic, "commit_id": "b" * 40}]
    )
    llm = FakeLLM(actions=[ACTIONS[0], {**ACTIONS[1], "file_path": "parser.py"}])
    repo = FakeRepo()

    assert await analyze_single_pr(llm, repo, dict(row), cubic, "model")
    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])

    # Placed, the reviews of a and b leave the exporter c wrote unreviewed, so its fix is left out.
    # Read without their commits they would all count as unplaceable, and nothing would be.
    plain = json.loads(json.dumps(_pr_row()).replace(BOT, cubic))
    plain["assembled"] = row["assembled"]
    plain["reviews"] = row["reviews"]
    plain["issue_comments"] = row["issue_comments"]
    assert await analyze_single_pr(FakeLLM(), repo, plain, cubic, "model")
    assert repo.stored["recall"] == 1.0

    # A GH Archive review with no API review to fill it in cannot be placed: like a review whose
    # commit GitHub nulled, it leaves nothing out on the PR.
    row["reviews"] = json.dumps([{"id": 1, "author": cubic, "commit_id": "a" * 40}])
    assert await analyze_single_pr(llm, repo, row, cubic, "model")
    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])


async def test_a_deleted_record_comment_the_timeline_still_holds_keeps_every_fix() -> None:
    # Greptile's summary, as first posted, named a; the summary was deleted since, so the comments read
    # hold only a person's. The timeline copy still shows the review of a, but the edits that would
    # record later reviews (a silent one of c, say) were never read: no fix is left out.
    greptile = "greptile-apps[bot]"
    row = json.loads(json.dumps(_pr_row()).replace(BOT, greptile))
    assembled = json.loads(row["assembled"])
    # The summary is comment 9 here; the fixture's id-less copy of it is dropped.
    assembled["events"] = [e for e in assembled["events"] if e["event_type"] != "issue_comment"]
    first = f"Last reviewed commit: [Parser](https://github.com/owner/repo/commit/{'a' * 40})"
    assembled["events"].append(
        {
            "actor": greptile,
            "event_type": "issue_comment",
            "timestamp": "2026-07-01T11:01:00Z",
            "data": {"comment_id": 9, "body": first},
        }
    )
    row["assembled"] = json.dumps(assembled)
    row["issue_comments"] = json.dumps(
        [{"id": 5, "author": "dev", "body": "thanks", "created_at": "2026-07-01T16:00:00Z"}]
    )
    repo = FakeRepo()

    assert await analyze_single_pr(FakeLLM(), repo, dict(row), greptile, "model")
    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])

    # With the summary still there (and recording b), the exporter fix of c is left out.
    later = f"Last reviewed commit: [Parser](https://github.com/owner/repo/commit/{'b' * 40})"
    row["issue_comments"] = json.dumps(
        [{"id": 9, "author": greptile, "body": later, "created_at": "2026-07-01T11:01:00Z"}]
    )
    assert await analyze_single_pr(FakeLLM(), repo, row, greptile, "model")
    assert repo.stored["recall"] == 1.0


@pytest.mark.parametrize(
    "bot", ["claude[bot]", "qodo-code-review[bot]", "cubic-dev-ai[bot]", "gemini-code-assist[bot]"]
)
async def test_a_review_posted_as_a_conversation_comment_keeps_every_fix(bot: str) -> None:
    # Claude posts a re-review as a new conversation comment, Qodo by editing its persistent one; such
    # a review names no commit. After c, the bot reviewed again that way: the exporter fix of c may be
    # of code it read, so nothing is left out. A bot in none of the tables (cubic, Gemini) is treated
    # the same, since its conversation comment with text is a review too.
    row = json.loads(json.dumps(_pr_row()).replace(BOT, bot))
    assembled = json.loads(row["assembled"])
    assembled["events"] = [e for e in assembled["events"] if e["event_type"] != "issue_comment"]
    row["assembled"] = json.dumps(assembled)
    row["issue_comments"] = json.dumps([])
    repo = FakeRepo()
    assert await analyze_single_pr(FakeLLM(), repo, dict(row), bot, "model")
    assert repo.stored["recall"] == 1.0  # only the review events: the fix is left out

    review = {"id": 7, "author": bot, "body": "## Review\nI read exporter.py.", "created_at": "2026-07-01T14:30:00Z"}
    if bot.startswith("qodo"):  # an edit of its persistent comment, posted before the first review
        row["issue_comments"] = json.dumps([{**review, "created_at": "2026-07-01T09:00:00Z"}])
        row["issue_comment_edits"] = json.dumps(
            [
                {
                    "id": 7,
                    "author": bot,
                    "revisions": [{"edited_at": "2026-07-01T14:30:00Z", "body": "Code Review by Qodo"}],
                }
            ]
        )
    else:
        row["issue_comments"] = json.dumps([review])
    assert await analyze_single_pr(FakeLLM(), repo, row, bot, "model")
    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])


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


async def test_an_inline_comment_read_from_the_threads_is_placed_by_its_commit() -> None:
    # The Search API path reads inline comments only from the GraphQL threads. Read with its commit,
    # the comment places its review on a and the exporter fix is left out. A commit the answer gave
    # as null (gone after a force-push), or a thread stored before the query asked for the commit,
    # leaves a review that cannot be placed, so every fix is kept. A reply is not a review.
    def with_inline(extra: dict) -> dict:
        row = _pr_row()
        assembled = json.loads(row["assembled"])
        inline = {"comment_id": 9, "body": "Null check missing.", "path": "parser.py", "source": "api", **extra}
        assembled["events"].append(
            {"actor": BOT, "event_type": "review_comment", "timestamp": "2026-07-01T11:00:00Z", "data": inline}
        )
        row["assembled"] = json.dumps(assembled)
        return row

    for extra, recall in (
        ({"original_commit_id": "a" * 40, "in_reply_to_id": None}, 1.0),
        ({}, 0.5),
        ({"original_commit_id": None, "in_reply_to_id": 3}, 1.0),
        ({"original_commit_id": None, "in_reply_to_id": None}, 0.5),
    ):
        repo = FakeRepo()
        assert await analyze_single_pr(FakeLLM(), repo, with_inline(extra), BOT, "model")
        assert repo.stored["recall"] == recall, extra


def _notice_only_row(bot: str = BOT) -> dict:
    # GH Archive admits a PR where the bot posted only a notice: rate limited from the first push
    # (the range starts at the PR's base), then a reply in a human's thread, posted as an empty review.
    row = _pr_row(notice=RATE_LIMITED.replace("b" * 40, "f" * 40))
    assembled = json.loads(row["assembled"])
    reply = {"state": "commented", "body": "", "commit_id": "b" * 40}
    assembled["events"] = [
        assembled["events"][0],
        {"actor": BOT, "event_type": "review", "timestamp": "2026-07-01T13:00:00Z", "data": reply},
    ]
    row["assembled"] = json.dumps(assembled)
    row["reviews"] = json.dumps([])
    return json.loads(json.dumps(row).replace(BOT, bot))


async def test_a_pr_the_bot_never_reviewed_leaves_every_fix_out() -> None:
    repo = FakeRepo()

    assert await analyze_single_pr(FakeLLM(matched_action="none"), repo, _notice_only_row(), BOT, "model")

    # No fix matched and no code of the PR was reviewed: no fix counts, so the PR has no recall.
    assert repo.stored["recall"] is None
    assert [a["excluded_from_recall"] for a in repo.stored["human_actions"]] == [
        "the bot completed no review on the PR; it posted limit notices"
    ] * 2

    # A suggestion of the bot matched a fix (raised in a chat reply, say): a matched fix always
    # counts, so the PR is not left out whole.
    assert await analyze_single_pr(FakeLLM(), repo, _notice_only_row(), BOT, "model")
    assert repo.stored["recall"] is not None
    assert not any(
        (a.get("excluded_from_recall") or "").startswith("the bot completed no review")
        for a in repo.stored["human_actions"]
    )


async def test_a_pr_with_a_review_or_a_record_of_one_is_not_left_out_whole() -> None:
    # The same PR with one completed review of a. Its summary holds no record of it, so the comment
    # record guard keeps every fix; the PR is not left out whole.
    row = _notice_only_row()
    assembled = json.loads(row["assembled"])
    assembled["events"].append(_review("a" * 40, "2026-07-01T11:00:00Z", "Null check missing."))
    row["assembled"] = json.dumps(assembled)
    repo = FakeRepo()
    assert await analyze_single_pr(FakeLLM(), repo, row, BOT, "model")
    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])

    # A record of a review of a that posted nothing is a review too: only the exporter fix, of code
    # written after it, is left out.
    row = _notice_only_row()
    row["issue_comments"] = json.dumps(
        [{**json.loads(row["issue_comments"])[0], "body": f"{SUMMARY}\n{_recent_review('f' * 40, 'a' * 40)}"}]
    )
    assert await analyze_single_pr(FakeLLM(), repo, row, BOT, "model")
    assert repo.stored["recall"] == 1.0
    assert [a.get("excluded_from_recall", "")[:4] for a in repo.stored["human_actions"]] == ["", "last"]

    # Claude posts its review as a conversation comment: that is a completed review.
    claude = "claude[bot]"
    row = _notice_only_row(claude)
    row["issue_comments"] = json.dumps(
        [{"id": 5, "author": claude, "body": "## Review\nparser.py: null check.", "created_at": "2026-07-01T10:30:00Z"}]
    )
    assert await analyze_single_pr(FakeLLM(), repo, row, claude, "model")
    assert repo.stored["recall"] is not None


async def test_a_fix_in_a_file_the_reviews_left_out_by_path_filters_is_left_out() -> None:
    # d fixes the parser, which the review of d (the fix's own range) lists as ignored due to path
    # filters, and no review lists as read: the filter applied before the fix too.
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
    row = _pr_row(patches=patches)
    filters = (
        "<details>\n<summary>⛔ Files ignored due to path filters (1)</summary>\n\n"
        "* `parser.py` is excluded by `!**/parser.py`\n\n</details>"
    )
    assembled = json.loads(row["assembled"])
    assembled["events"].append(_review("d" * 40, "2026-07-01T15:30:00Z", f"No findings.\n{filters}"))
    row["assembled"] = json.dumps(assembled)
    repo = FakeRepo()

    assert await analyze_single_pr(llm, repo, row, BOT, "model")

    actions = {a["action_id"]: a for a in repo.stored["human_actions"]}
    assert "excluded_from_recall" not in actions["A1"]  # matched
    assert actions["A2"]["excluded_from_recall"] == (
        "every review of the bot that lists parser.py lists it as ignored due to path filters"
    )
    assert repo.stored["recall"] == 1.0

    # A review that lists the file as read means it was read: the fix counts.
    read = "<details>\n<summary>📒 Files selected for processing (1)</summary>\n\n* `parser.py`\n\n</details>"
    assembled["events"][2]["data"]["body"] += f"\n{read}"
    row["assembled"] = json.dumps(assembled)
    assert await analyze_single_pr(llm, repo, row, BOT, "model")
    assert repo.stored["recall"] == 0.5


async def test_a_qodo_re_review_shown_only_by_the_current_body_keeps_every_fix() -> None:
    # Qodo re-reviewed after c by editing its persistent comment. The latest revision is exactly the
    # fetched current body, so it is left out of the revisions as its duplicate; the edit shows only
    # in the comment's updated_at, and the review after c cannot be placed: nothing is left out.
    qodo = "qodo-code-review[bot]"
    row = json.loads(json.dumps(_pr_row()).replace(BOT, qodo))
    body = "Code Review by Qodo\n\nexporter.py: path."
    comment = {"id": 7, "author": qodo, "body": body, "created_at": "2026-07-01T10:01:00Z"}
    row["issue_comments"] = json.dumps([{**comment, "updated_at": "2026-07-01T10:01:00Z"}])
    repo = FakeRepo()
    assert await analyze_single_pr(FakeLLM(), repo, dict(row), qodo, "model")
    assert repo.stored["recall"] == 1.0  # never edited: the exporter fix is left out

    row["issue_comments"] = json.dumps([{**comment, "updated_at": "2026-07-01T14:30:00Z"}])
    edits = [{"id": 7, "author": qodo, "revisions": [{"edited_at": "2026-07-01T14:30:00Z", "body": body}]}]
    row["issue_comment_edits"] = json.dumps(edits)
    assert await analyze_single_pr(FakeLLM(), repo, row, qodo, "model")
    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])


async def test_no_review_needs_the_whole_conversation_history() -> None:
    # Qodo's comment reads "reviews paused" now, but an earlier revision of it has no text left on
    # GitHub and may hold a review: the PR is not taken as never reviewed, and no fix is left out.
    qodo = "qodo-code-review[bot]"
    row = _notice_only_row(qodo)
    pause = "### Qodo reviews are paused for this user.\n\nTroubleshooting steps vary by plan."
    row["issue_comments"] = json.dumps(
        [{"id": 7, "author": qodo, "body": pause, "created_at": "2026-07-01T10:01:00Z", "updated_at": None}]
    )
    repo = FakeRepo()
    assert await analyze_single_pr(FakeLLM(matched_action="none"), repo, dict(row), qodo, "model")
    assert repo.stored["recall"] is None  # every revision read: no review, every fix left out

    edits = [{"id": 7, "author": qodo, "revisions": [], "unavailable_edits": ["2026-07-01T13:00:00Z"]}]
    row["issue_comment_edits"] = json.dumps(edits)
    assert await analyze_single_pr(FakeLLM(), repo, dict(row), qodo, "model")
    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])

    # The same with the comments never read. A bot without a record or comment reviews is then not
    # taken as never reviewed either: its fixes are judged line by line as before.
    row.pop("issue_comment_edits")
    row["issue_comments"] = None
    assert await analyze_single_pr(FakeLLM(), repo, dict(row), qodo, "model")
    assert repo.stored["recall"] == 0.5
    cubic = "cubic-dev-ai[bot]"
    other = json.loads(json.dumps(row).replace(qodo, cubic))
    assert await analyze_single_pr(FakeLLM(), repo, other, cubic, "model")
    assert repo.stored["recall"] is not None
    assert not any(
        a.get("excluded_from_recall", "").startswith("the bot completed no review")
        for a in repo.stored["human_actions"]
    )


@pytest.mark.parametrize("bot", ["greptile-apps[bot]", "chatgpt-codex-connector[bot]"])
async def test_a_review_shown_only_by_a_summary_without_a_record_keeps_every_fix(bot: str) -> None:
    # Greptile (or Codex) reviewed and found nothing: its summary comment is all it posted, and that
    # summary carries no record of the commit it read. It is a review, not no review, and its record
    # is missing, so every fix is kept, the matched one included.
    row = json.loads(json.dumps(_pr_row()).replace(BOT, bot))
    assembled = json.loads(row["assembled"])
    assembled["events"] = [e for e in assembled["events"] if e["event_type"] != "review"]
    row["assembled"] = json.dumps(assembled)
    row["reviews"] = json.dumps([])
    summary = "<h3>Greptile Summary</h3>\n\nThis PR adds a parser and an exporter.\n\nConfidence Score: 5/5"
    row["issue_comments"] = json.dumps(
        [{"id": 6, "author": bot, "body": summary, "created_at": "2026-07-01T10:01:00Z", "updated_at": None}]
    )
    repo = FakeRepo()

    assert await analyze_single_pr(FakeLLM(), repo, row, bot, "model")

    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])


def _coderabbit_only_comment_row(body: str) -> dict:
    # GH Archive admits a PR whose only CodeRabbit post is its summary comment: no review, no inline comment.
    row = _pr_row()
    assembled = json.loads(row["assembled"])
    assembled["events"] = [e for e in assembled["events"] if e["event_type"] != "review"]
    row["assembled"] = json.dumps(assembled)
    row["reviews"] = json.dumps([])
    row["issue_comments"] = json.dumps(
        [{"id": 1, "author": BOT, "body": body, "created_at": "2026-07-01T10:01:00Z", "updated_at": None}]
    )
    return row


IN_PROGRESS = _notice(
    "review in progress",
    "> [!NOTE]\n> Currently processing new changes in this PR. This may take a few minutes, please wait...\n>\n"
    f"> {_commits_read('f' * 40, 'a' * 40)}",
)


@pytest.mark.parametrize("notice", [IN_PROGRESS, RATE_LIMITED.replace("b" * 40, "f" * 40), FEWER_STARS])
async def test_a_coderabbit_pr_whose_summary_is_only_a_placeholder_or_a_notice_was_never_reviewed(notice: str) -> None:
    # The in-progress placeholder (with the range under way), a limit or a skip block is not a review:
    # every fix is left out and the PR has no recall.
    repo = FakeRepo()

    assert await analyze_single_pr(
        FakeLLM(matched_action="none"), repo, _coderabbit_only_comment_row(f"{SUMMARY}\n{notice}"), BOT, "model"
    )

    assert repo.stored["recall"] is None
    assert all(
        a["excluded_from_recall"].startswith("the bot completed no review") for a in repo.stored["human_actions"]
    )


async def test_a_coderabbit_summary_with_a_walkthrough_but_no_range_keeps_every_fix() -> None:
    # The summary carries the review's walkthrough and changes table but no range: a review ran, its
    # record is missing, so every fix is kept.
    walkthrough = (
        "<!-- walkthrough_start -->\n\n<details>\n<summary><strong>📝 Walkthrough</strong></summary>\n<dl>\n<dd>\n\n"
        "Adds a parser and an exporter.\n\n**Changes: Parser**\n\n|Layer / File(s)|Summary|\n|:---|:---|\n"
        "|**Parser** <br> `parser.py`|Splits on commas.|\n\n</dd>\n</dl>\n</details>\n\n<!-- walkthrough_end -->"
    )
    repo = FakeRepo()

    assert await analyze_single_pr(
        FakeLLM(), repo, _coderabbit_only_comment_row(f"{SUMMARY}\n{walkthrough}"), BOT, "model"
    )

    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])


async def test_a_deleted_summary_is_not_stood_in_for_by_another_comment_of_the_bot() -> None:
    # The summary as first posted (the timeline's copy, no id) is gone from the comments read, which
    # hold only a chat reply of the bot: the summary's later edits were never read, so every fix is
    # kept, as when the read holds only a person's comment.
    row = _pr_row()
    reply = {"id": 2, "author": BOT, "body": "You're right, thanks.", "created_at": "2026-07-01T16:00:00Z"}
    row["issue_comments"] = json.dumps([reply])
    repo = FakeRepo()

    assert await analyze_single_pr(FakeLLM(), repo, row, BOT, "model")

    assert repo.stored["recall"] == 0.5
    assert all("excluded_from_recall" not in a for a in repo.stored["human_actions"])

    # The same comment read back, edited since (same creation time), still holds it.
    edited = {"id": 1, "author": BOT, "body": f"{SUMMARY}\n{RATE_LIMITED}", "created_at": "2026-07-01T10:01:00Z"}
    row["issue_comments"] = json.dumps([edited, reply])
    assert await analyze_single_pr(FakeLLM(), repo, row, BOT, "model")
    assert repo.stored["recall"] == 1.0


@pytest.mark.parametrize("bot", ["cubic-dev-ai[bot]", "gemini-code-assist[bot]"])
async def test_a_bot_outside_the_tables_that_reviews_in_a_conversation_comment_keeps_its_fixes(bot: str) -> None:
    # A bot no table describes posted its review as a conversation comment and nothing else: that is
    # a review, not a PR it never reviewed, so its fixes are not all left out.
    row = json.loads(json.dumps(_pr_row()).replace(BOT, bot))
    assembled = json.loads(row["assembled"])
    assembled["events"] = [e for e in assembled["events"] if e["event_type"] != "review"]
    row["assembled"] = json.dumps(assembled)
    row["reviews"] = json.dumps([])
    body = "## Review\n\nI reviewed the PR. `parser.py`: the delimiter is wrong on line 2."
    row["issue_comments"] = json.dumps(
        [{"id": 1, "author": bot, "body": body, "created_at": "2026-07-01T10:01:00Z", "updated_at": None}]
    )
    repo = FakeRepo()

    assert await analyze_single_pr(FakeLLM(), repo, row, bot, "model")

    assert repo.stored["recall"] is not None
    assert not any(
        a.get("excluded_from_recall", "").startswith("the bot completed no review")
        for a in repo.stored["human_actions"]
    )

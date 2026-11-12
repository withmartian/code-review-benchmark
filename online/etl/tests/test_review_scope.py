"""Tests for cutting the answer key at the bot's last reviewed commit."""

from __future__ import annotations

import json

from llm.schemas import BotSuggestionsResponse
from llm.schemas import HumanActionsResponse
from llm.schemas import MatchingResponse
from pipeline.analyze import analyze_single_pr
from pipeline.review_scope import find_last_reviewed_commit
from pipeline.review_scope import split_actions_at_last_reviewed

BOT = "coderabbitai[bot]"
A = "a" * 40
B = "b" * 40


def _block(kind: str, inner: str) -> str:
    return (
        f"<!-- This is an auto-generated comment: {kind} by coderabbit.ai -->\n{inner}\n"
        f"<!-- end of auto-generated comment: {kind} by coderabbit.ai -->"
    )


def _comment(body: str, author: str = BOT) -> dict:
    return {"id": 1, "author": author, "body": body}


RATE_LIMITED = _block(
    "rate limited",
    f"> ## Review limit reached\n> Reviewing files that changed from the base of the PR and between {A} and {B}.",
)
PAUSED = _block(
    "review paused",
    f"> ## Reviews paused\n> - Last reviewed commit: {A}\n> - Latest commit, not reviewed: {B}",
)
IN_PROGRESS = _block(
    "review in progress",
    f"> Currently processing new changes.\n> Reviewing files that changed from the base of the PR and between {A} and {B}.",
)
SKIPPED_NO_CHANGES = _block(
    "skip review",
    "> Review was skipped as selected files did not have any reviewable changes.\n"
    f"> Reviewing files that changed from the base of the PR and between {A} and {B}.",
)
SKIPPED_INCREMENTAL = _block(
    "skip review", "> ## Review skipped\n> Auto incremental reviews are disabled on this repository."
)
SKIPPED_STARS = _block(
    "skip review",
    "> ## Review skipped\n> This repository does not receive automatic reviews because it has fewer than 10 stars.",
)
SKIPPED_LABEL = _block("skip review", "> ## Review skipped\n> Ignore keyword(s) in the title.")

C1, C2, C3 = (f"{i:040x}" for i in (1, 2, 3))
PR_COMMITS = [{"sha": C1}, {"sha": C2}, {"sha": C3}]


def _review(sha: str, body: str, author: str = BOT) -> dict:
    return {"author": author, "commit_id": sha, "body": body}


# -- Stop notices ---------------------------------------------------------------


def test_rate_limited_notice_gives_start_of_range() -> None:
    assert find_last_reviewed_commit([_comment(RATE_LIMITED)], BOT) == (A, "rate limited")


def test_paused_notice_gives_last_reviewed_commit() -> None:
    assert find_last_reviewed_commit([_comment(PAUSED)], BOT) == (A, "review paused")


def test_in_progress_notice_gives_start_of_range() -> None:
    assert find_last_reviewed_commit([_comment(IN_PROGRESS)], "coderabbitai") == (A, "review in progress")


def test_skipped_no_reviewable_changes_is_not_a_stop() -> None:
    assert find_last_reviewed_commit([_comment(SKIPPED_NO_CHANGES)], BOT) is None


def test_completed_review_range_outside_a_notice_is_not_a_stop() -> None:
    body = f"Reviewing files that changed from the base of the PR and between {A} and {B}."
    assert find_last_reviewed_commit([_comment(body)], BOT) is None


def test_bot_without_entry_has_no_cut() -> None:
    assert find_last_reviewed_commit([_comment(RATE_LIMITED, author="other-bot[bot]")], "other-bot[bot]") is None


def test_notice_quoted_by_a_human_is_ignored() -> None:
    assert find_last_reviewed_commit([_comment(RATE_LIMITED, author="alice")], BOT) is None


def test_skip_without_commit_uses_newest_non_empty_review_record() -> None:
    # Records out of order; the empty-body record on C3 is a thread reply, the human's is not the bot's.
    reviews = [
        _review(C2, "Actionable comments posted: 1"),
        _review(C1, "**Actionable comments posted: 2**"),
        _review(C3, ""),
        _review(C3, "LGTM", author="alice"),
    ]
    for notice, kind in ((SKIPPED_INCREMENTAL, "incremental reviews disabled"), (SKIPPED_STARS, "fewer than 10 stars")):
        assert find_last_reviewed_commit([_comment(notice)], BOT, reviews, PR_COMMITS, C1) == (
            C2,
            f"skip review: {kind}",
        )


def test_skip_without_commit_falls_back_to_hash_x() -> None:
    reviews = [_review(C3, "   ")]
    assert find_last_reviewed_commit([_comment(SKIPPED_STARS)], BOT, reviews, PR_COMMITS, C1) == (
        C1,
        "skip review: fewer than 10 stars",
    )


def test_other_skip_reasons_do_not_cut() -> None:
    reviews = [_review(C2, "Actionable comments posted: 1")]
    for notice in (SKIPPED_LABEL, SKIPPED_NO_CHANGES):
        assert find_last_reviewed_commit([_comment(notice)], BOT, reviews, PR_COMMITS, C1) is None


def test_last_notice_wins() -> None:
    later = PAUSED.replace(A, "c" * 40)
    assert find_last_reviewed_commit([_comment(RATE_LIMITED + "\n" + later)], BOT) == ("c" * 40, "review paused")


# -- Line provenance --------------------------------------------------------------

TEN = "".join(f"+l{i}\n" for i in range(1, 11))


def _commits(*file_lists: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    commits = [{"sha": f"{i + 1:040x}"} for i in range(len(file_lists))]
    details = {c["sha"]: {"sha": c["sha"], "files": files} for c, files in zip(commits, file_lists, strict=True)}
    return commits, details


def _f(patch: str, status: str = "modified", name: str = "f.py") -> dict:
    return {"filename": name, "status": status, "patch": patch}


def _action(commit_index: int, path: str | None = "f.py") -> dict:
    return {"action_id": "A1", "file_path": path, "commit_sha": f"{commit_index:040x}"}


CREATE = _f("@@ -0,0 +1,10 @@\n" + TEN, status="added")


def _split(commits: list[dict], details: dict, action: dict, last_reviewed: int = 1) -> tuple[list, list]:
    return split_actions_at_last_reviewed([action], commits, details, f"{last_reviewed:040x}")


def test_fix_of_line_added_after_last_reviewed_is_dropped() -> None:
    commits, details = _commits(
        [CREATE],
        [_f("@@ -8,2 +8,3 @@\n l8\n+new\n l9")],
        [_f("@@ -8,3 +8,2 @@\n l8\n-new\n l9")],
    )
    kept, dropped = _split(commits, details, _action(3))
    assert kept == []
    assert dropped[0]["drop_reason"] == "fixed_lines_added_after_last_reviewed_commit"
    assert dropped[0]["last_reviewed_commit"] == commits[0]["sha"]


def test_fix_of_line_that_existed_at_last_reviewed_is_kept() -> None:
    # c2 inserts a line near the top, shifting l9 from line 9 to line 10 before the fix removes it.
    commits, details = _commits(
        [CREATE],
        [_f("@@ -1,2 +1,3 @@\n l1\n+new\n l2")],
        [_f("@@ -9,3 +9,2 @@\n l8\n-l9\n l10")],
    )
    kept, dropped = _split(commits, details, _action(3))
    assert (len(kept), dropped) == (1, [])


def test_deletion_only_hunk_shifts_lines_back() -> None:
    commits, details = _commits(
        [CREATE],
        [_f("@@ -3,2 +2,0 @@\n-l3\n-l4")],
        [_f("@@ -2,3 +2,2 @@\n l2\n-l5\n l6")],
    )
    assert _split(commits, details, _action(3))[1] == []


def test_mixed_fix_is_kept() -> None:
    commits, details = _commits(
        [CREATE],
        [_f("@@ -8,2 +8,3 @@\n l8\n+new\n l9")],
        [_f("@@ -7,4 +7,2 @@\n l7\n-l8\n-new\n l9")],
    )
    assert _split(commits, details, _action(3))[1] == []


def test_file_created_after_last_reviewed_is_dropped() -> None:
    commits, details = _commits(
        [CREATE],
        [_f("@@ -0,0 +1,2 @@\n+x\n+y", status="added", name="g.py")],
        [_f("@@ -1,2 +1,2 @@\n-x\n+z\n y", name="g.py")],
    )
    dropped = _split(commits, details, _action(3, path="g.py"))[1]
    assert dropped[0]["drop_reason"] == "file_created_after_last_reviewed_commit"


def test_fix_at_or_before_last_reviewed_is_kept() -> None:
    commits, details = _commits([CREATE], [_f("@@ -8,3 +8,2 @@\n l8\n-l9\n l10")])
    assert _split(commits, details, _action(2), last_reviewed=2)[1] == []


def test_action_without_file_or_commit_is_kept() -> None:
    commits, details = _commits([CREATE], [_f("@@ -8,2 +8,3 @@\n l8\n+new\n l9")], [])
    assert _split(commits, details, _action(3, path=None))[1] == []
    assert _split(commits, details, {"action_id": "A1", "file_path": "f.py", "commit_sha": None})[1] == []


def test_pure_addition_fix_is_kept() -> None:
    commits, details = _commits(
        [CREATE],
        [_f("@@ -8,2 +8,3 @@\n l8\n+new\n l9")],
        [_f("@@ -9,2 +9,3 @@\n new\n+guard\n l9")],
    )
    assert _split(commits, details, _action(3))[1] == []


def test_untraceable_intermediate_commit_is_kept() -> None:
    # GitHub omits the patch of large files; the line cannot be traced, so the action stays.
    commits, details = _commits(
        [CREATE],
        [{"filename": "f.py", "status": "modified", "patch": "", "additions": 900, "deletions": 0}],
        [_f("@@ -8,3 +8,2 @@\n l8\n-new\n l9")],
    )
    assert _split(commits, details, _action(3))[1] == []


def test_last_reviewed_commit_not_in_pr_drops_nothing() -> None:
    commits, details = _commits(
        [CREATE], [_f("@@ -8,2 +8,3 @@\n l8\n+new\n l9")], [_f("@@ -8,3 +8,2 @@\n l8\n-new\n l9")]
    )
    kept, dropped = split_actions_at_last_reviewed([_action(3)], commits, details, "f" * 40)
    assert (len(kept), dropped) == (1, [])


# -- analyze_single_pr ------------------------------------------------------------


class _FakeLLM:
    def __init__(self, responses: dict) -> None:
        self.responses = responses

    async def structured_completion(self, _prompt: str, model_cls: type) -> object:
        return model_cls.model_validate(self.responses[model_cls])


class _FakeRepo:
    def __init__(self) -> None:
        self.inserted: dict | None = None

    async def insert_analysis(self, **kwargs: object) -> None:
        self.inserted = kwargs


def _pr_row(issue_comments: list[dict]) -> dict:
    commits, details = _commits(
        [CREATE],
        [_f("@@ -8,2 +8,3 @@\n l8\n+new\n l9")],
        [_f("@@ -8,3 +8,2 @@\n l8\n-new\n l9")],
        [_f("@@ -1,3 +1,2 @@\n l1\n-l2\n l3")],
    )
    for c in commits:
        c["date"] = "2026-11-01T00:00:00Z"
    return {
        "id": 7,
        "chatbot_id": 1,
        "repo_name": "o/r",
        "pr_number": 5,
        "assembled": json.dumps(
            {"events": [{"event_type": "review", "actor": BOT, "timestamp": "t", "data": {"body": "x"}}]}
        ),
        "commits": json.dumps(commits),
        "commit_details": json.dumps(list(details.values())),
        "reviews": json.dumps([{"author": BOT, "commit_id": commits[0]["sha"]}]),
        "issue_comments": json.dumps(issue_comments),
    }


_RESPONSES = {
    BotSuggestionsResponse: {
        "suggestions": [
            {"issue_id": "S1", "description": "d", "category": "bug"},
            {"issue_id": "S2", "description": "d", "category": "bug"},
        ]
    },
    HumanActionsResponse: {
        "actions": [
            {
                "action_id": "A1",
                "description": "d",
                "category": "bug",
                "file_path": "f.py",
                "commit_sha": f"{3:040x}",
                "action_type": "fix",
            },
            {
                "action_id": "A2",
                "description": "d",
                "category": "bug",
                "file_path": "f.py",
                "commit_sha": f"{4:040x}",
                "action_type": "fix",
            },
        ]
    },
    MatchingResponse: {
        "matches": [
            {"bot_issue_id": "S1", "human_action_id": "A2", "matched": True, "confidence": 1, "reasoning": "r"},
            {"bot_issue_id": "S2", "human_action_id": None, "matched": False, "confidence": 1, "reasoning": "r"},
        ]
    },
}


async def test_analyze_drops_unreviewed_fix_from_recall_only() -> None:
    repo = _FakeRepo()
    notice = PAUSED.replace(A, f"{1:040x}")
    ok = await analyze_single_pr(_FakeLLM(_RESPONSES), repo, _pr_row([_comment(notice)]), BOT, "m")
    assert ok
    r = repo.inserted
    assert [a["action_id"] for a in r["human_actions"]] == ["A2"]
    assert [a["action_id"] for a in r["dropped_human_actions"]] == ["A1"]
    assert (r["precision"], r["recall"]) == (0.5, 1.0)


async def test_analyze_skip_notice_cuts_at_newest_reviewed_commit() -> None:
    repo = _FakeRepo()
    row = _pr_row([_comment(SKIPPED_INCREMENTAL)])
    commits = json.loads(row["commits"])
    row["reviews"] = json.dumps(
        [_review(commits[0]["sha"], "Actionable comments posted: 1"), _review(commits[3]["sha"], "")]
    )
    ok = await analyze_single_pr(_FakeLLM(_RESPONSES), repo, row, BOT, "m")
    assert ok
    r = repo.inserted
    assert [a["action_id"] for a in r["dropped_human_actions"]] == ["A1"]
    assert r["dropped_human_actions"][0]["last_reviewed_commit"] == commits[0]["sha"]
    assert (r["precision"], r["recall"]) == (0.5, 1.0)


async def test_analyze_without_notice_is_unchanged() -> None:
    repo = _FakeRepo()
    ok = await analyze_single_pr(_FakeLLM(_RESPONSES), repo, _pr_row([]), BOT, "m")
    assert ok
    r = repo.inserted
    assert [a["action_id"] for a in r["human_actions"]] == ["A1", "A2"]
    assert r["dropped_human_actions"] is None
    assert (r["precision"], r["recall"]) == (0.5, 0.5)

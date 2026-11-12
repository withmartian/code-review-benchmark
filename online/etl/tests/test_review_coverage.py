"""Tests for reading a bot's own not-reviewed notices and the line history of a fix."""

from __future__ import annotations

from pipeline.review_coverage import find_notices
from pipeline.review_coverage import find_review_cutoff
from pipeline.review_coverage import unreviewed_code_origin

CR = "coderabbitai[bot]"
CODEX = "chatgpt-codex-connector[bot]"
A, B, C, D = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
COMMITS = [{"sha": s} for s in (A, B, C, D)]


def _block(kind: str, text: str) -> str:
    return (
        f"<!-- This is an auto-generated comment: {kind} by coderabbit.ai -->\n{text}\n"
        f"<!-- end of auto-generated comment: {kind} by coderabbit.ai -->"
    )


def rate_limited(reviewed: str, head: str) -> str:
    return _block(
        "rate limited",
        "> [!WARNING]\n> ## Review limit reached\n>\n> You've used all free OSS reviews for now.\n>\n"
        "> Reviewing files that changed from the base of the PR and between "
        f"{reviewed} and {head}.",
    )


def paused(reviewed: str | None = None, head: str | None = None) -> str:
    lines = ["> [!NOTE]", "> ## Reviews paused", ">", "> CodeRabbit has automatically paused this review."]
    if reviewed:
        lines.append(f"> - Last reviewed commit: {reviewed}")
    if head:
        lines.append(f"> - Latest commit, not reviewed: {head}")
    return _block("review paused", "\n".join(lines))


def summary(*blocks: str) -> str:
    return "\n".join(["<!-- This is an auto-generated comment: summarize by coderabbit.ai -->", *blocks, "Walkthrough"])


def issue_comment(body: str, ts: str = "2026-07-01T09:00:00Z", actor: str = CR, updated: str | None = None) -> dict:
    return {
        "actor": actor,
        "event_type": "issue_comment",
        "timestamp": ts,
        "data": {"body": body, "updated_at": updated},
    }


def review(
    sha: str, body: str = "**Actionable comments posted: 1**", ts: str = "2026-07-01T10:00:00Z", actor: str = CR
) -> dict:
    return {"actor": actor, "event_type": "review", "timestamp": ts, "data": {"body": body, "commit_id": sha}}


def test_rate_limit_notice_names_the_range_it_did_not_review() -> None:
    [n] = find_notices([issue_comment(summary(rate_limited(B, D)))], CR)

    assert (n.reason, n.in_place, n.reviewed_sha, n.unreviewed_sha) == ("limit", True, B, D)


def test_paused_and_failure_notices() -> None:
    failure = _block("failure", "> [!CAUTION]\n> ## Review failed\n>\n> The pull request is closed.")
    notices = find_notices([issue_comment(summary(paused(B, D))), issue_comment(summary(failure))], CR)

    assert [(n.reason, n.reviewed_sha, n.unreviewed_sha) for n in notices] == [("pause", B, D), ("failure", None, None)]


def test_skips_by_design_or_configuration_are_not_notices() -> None:
    skips = [
        "> ## Review skipped\n>\n> Auto incremental reviews are disabled on this repository.",
        "> This repository does not receive automatic reviews because it has fewer than 10 stars.",
        "> ## Review skipped\n>\n> Review was skipped as selected files did not have any reviewable changes.",
    ]
    events = [issue_comment(summary(_block("skip review", s))) for s in skips]
    events.append(issue_comment(summary(_block("review in progress", "> Currently processing new changes"))))

    assert find_notices(events, CR) == []


def test_a_skip_that_reports_a_failed_incremental_review_is_a_failure() -> None:
    text = "> ## Review skipped\n>\n> We couldn't safely recover the incremental review. No full review was started."

    assert [n.reason for n in find_notices([issue_comment(summary(_block("skip review", text)))], CR)] == ["failure"]


def test_a_command_reply_refused_by_the_limit_is_a_timed_notice() -> None:
    body = "<details>\n<summary>Action not completed</summary>\n\nReview rate limited.\n</details>"

    [n] = find_notices([issue_comment(body)], CR)

    assert (n.reason, n.in_place) == ("limit", False)


def test_notices_belong_to_the_bot_they_are_written_for() -> None:
    codex = "You have reached your Codex usage limits for code reviews."

    assert find_notices([issue_comment(summary(rate_limited(B, D)), actor="someone")], CR) == []
    assert find_notices([issue_comment(codex, actor=CODEX)], CR) == []
    assert [n.reason for n in find_notices([issue_comment(codex, actor=CODEX)], CODEX)] == ["limit"]


def test_cutoff_is_the_last_reviewed_commit_the_notice_names() -> None:
    events = [issue_comment(summary(rate_limited(B, D))), review(A), review(B, ts="2026-07-01T11:00:00Z")]

    cut = find_review_cutoff(events, COMMITS, COMMITS[1:], CR)

    assert cut is not None
    assert (cut.last_reviewed, cut.unreviewed, cut.reasons) == (B, (C, D), ("limit",))
    assert (cut.notice_reviewed, cut.reviews_reviewed) == (B, B)


def test_a_review_without_a_review_event_still_counts_when_the_notice_names_it() -> None:
    # The bot reviewed B without posting a review (nothing to comment), and says so in the notice.
    cut = find_review_cutoff([issue_comment(summary(rate_limited(B, D))), review(A)], COMMITS, COMMITS[1:], CR)

    assert cut is not None
    assert (cut.last_reviewed, cut.notice_reviewed, cut.reviews_reviewed) == (B, B, A)


def test_a_later_completed_review_makes_the_notice_stale() -> None:
    events = [issue_comment(summary(rate_limited(A, B))), review(A), review(C, ts="2026-07-01T12:00:00Z")]

    cut = find_review_cutoff(events, COMMITS, COMMITS[1:], CR)

    assert cut is None


def test_thread_replies_do_not_count_as_reviews() -> None:
    reply_review = review(D, body="", ts="2026-07-01T13:00:00Z")
    reply = {
        "actor": CR,
        "event_type": "review_comment",
        "timestamp": "2026-07-01T13:00:00Z",
        "data": {"original_commit_id": D, "in_reply_to_id": 9, "body": "Thanks for the fix."},
    }
    events = [issue_comment(summary(rate_limited(B, D))), review(A), reply_review, reply]

    cut = find_review_cutoff(events, COMMITS, COMMITS[1:], CR)

    assert cut is not None and cut.last_reviewed == B


def test_a_new_inline_comment_counts_as_a_review() -> None:
    inline = {
        "actor": CR,
        "event_type": "review_comment",
        "timestamp": "2026-07-01T13:00:00Z",
        "data": {"original_commit_id": C, "body": "Finding."},
    }
    events = [issue_comment(summary(paused(B))), review(A), inline]

    cut = find_review_cutoff(events, COMMITS, COMMITS[1:], CR)

    assert cut is not None and (cut.last_reviewed, cut.unreviewed) == (C, (D,))


def test_a_notice_head_missing_from_the_commit_list_still_stands() -> None:
    cut = find_review_cutoff([issue_comment(summary(rate_limited(B, "e" * 40))), review(A)], COMMITS, COMMITS[1:], CR)

    assert cut is not None and cut.unreviewed == (C, D)


def test_a_timed_notice_counts_only_after_the_last_completed_review() -> None:
    limit = "You have reached your Codex usage limits for code reviews."
    before = [review(A, actor=CODEX), issue_comment(limit, ts="2026-07-01T10:30:00Z", actor=CODEX),
              review(B, ts="2026-07-01T11:00:00Z", actor=CODEX)]  # fmt: skip
    after = [*before, issue_comment(limit, ts="2026-07-01T12:00:00Z", actor=CODEX)]

    assert find_review_cutoff(before, COMMITS, COMMITS[1:], CODEX) is None
    cut = find_review_cutoff(after, COMMITS, COMMITS[1:], CODEX)
    assert cut is not None and (cut.last_reviewed, cut.unreviewed) == (B, (C, D))


def test_no_cutoff_without_a_notice_or_post_review_commits() -> None:
    assert find_review_cutoff([review(A), review(B)], COMMITS, COMMITS[1:], CR) is None
    assert find_review_cutoff([issue_comment(summary(rate_limited(A, D)))], COMMITS, [], CR) is None


# -- line history ----------------------------------------------------------------


def _details(*patches: tuple[str, str, str, str]) -> dict[str, dict]:
    out: dict[str, dict] = {s: {"sha": s, "files": []} for s in (A, B, C, D)}
    for sha, path, status, patch in patches:
        out[sha]["files"].append({"filename": path, "status": status, "patch": patch})
    return out


PARSER_A = "@@ -0,0 +1,2 @@\n+def parse(text):\n+    return text.split(',')"
EXPORTER_C = "@@ -0,0 +1,3 @@\n+def export(rows):\n+    target = '/tmp/out.csv'\n+    return write(target, rows)"


def _action(sha: str, path: str) -> dict:
    return {"action_id": "A1", "commit_sha": sha[:12], "file_path": path, "description": "bug"}


def test_a_fix_of_lines_written_after_the_cutoff_is_unreviewed() -> None:
    fix = "@@ -1,3 +1,3 @@\n def export(rows):\n-    target = '/tmp/out.csv'\n+    target = OUT_PATH\n     return write(target, rows)"
    details = _details(
        (A, "parser.py", "added", PARSER_A),
        (C, "exporter.py", "added", EXPORTER_C),
        (D, "exporter.py", "modified", fix),
    )

    origin, reason = unreviewed_code_origin(_action(D, "exporter.py"), COMMITS, details, B)

    assert origin == "unreviewed"
    assert "all 1 lines" in reason


def test_a_fix_of_reviewed_lines_is_reviewed() -> None:
    fix = "@@ -1,2 +1,2 @@\n def parse(text):\n-    return text.split(',')\n+    return text.split(';')"
    details = _details((A, "parser.py", "added", PARSER_A), (D, "parser.py", "modified", fix))

    assert unreviewed_code_origin(_action(D, "parser.py"), COMMITS, details, B)[0] == "reviewed"


def test_a_fix_over_both_reviewed_and_new_lines_is_mixed() -> None:
    later = "@@ -1,2 +1,3 @@\n def parse(text):\n+    text = text.strip()\n     return text.split(',')"
    fix = "@@ -1,3 +1,2 @@\n def parse(text):\n-    text = text.strip()\n-    return text.split(',')\n+    return (text or '').strip().split(',')"
    details = _details(
        (A, "parser.py", "added", PARSER_A), (C, "parser.py", "modified", later), (D, "parser.py", "modified", fix)
    )

    assert unreviewed_code_origin(_action(D, "parser.py"), COMMITS, details, B)[0] == "mixed"


def test_a_line_lightly_edited_after_the_cutoff_keeps_its_reviewed_origin() -> None:
    # C appends to a reviewed line; the defect in the reviewed part was there before the cutoff.
    edit = "@@ -1,2 +1,2 @@\n def parse(text):\n-    return text.split(',')\n+    return text.split(',')[:100]"
    fix = "@@ -1,2 +1,2 @@\n def parse(text):\n-    return text.split(',')[:100]\n+    return text.split(';')[:100]"
    details = _details(
        (A, "parser.py", "added", PARSER_A), (C, "parser.py", "modified", edit), (D, "parser.py", "modified", fix)
    )

    assert unreviewed_code_origin(_action(D, "parser.py"), COMMITS, details, B)[0] == "reviewed"


def test_a_fix_that_only_adds_lines_is_placed_by_the_lines_beside_it() -> None:
    fix = "@@ -1,3 +1,5 @@\n def export(rows):\n     target = '/tmp/out.csv'\n+    if not rows:\n+        return None\n     return write(target, rows)"
    details = _details((C, "exporter.py", "added", EXPORTER_C), (D, "exporter.py", "modified", fix))

    assert unreviewed_code_origin(_action(D, "exporter.py"), COMMITS, details, B)[0] == "unreviewed"


def test_unknown_when_the_history_cannot_tell() -> None:
    new_file = _details((D, "test_exporter.py", "added", "@@ -0,0 +1 @@\n+def test_export(): ..."))
    assert unreviewed_code_origin(_action(D, "test_exporter.py"), COMMITS, new_file, B)[0] == "unknown"

    no_patch = _details(
        (C, "exporter.py", "modified", ""), (D, "exporter.py", "modified", "@@ -1 +1 @@\n-old_line()\n+new_line()")
    )
    no_patch[C]["files"][0]["additions"] = 3
    assert unreviewed_code_origin(_action(D, "exporter.py"), COMMITS, no_patch, B)[0] == "unknown"

    missing = _details((D, "exporter.py", "modified", "@@ -1 +1 @@\n-old_line()\n+new_line()"))
    del missing[C]
    assert unreviewed_code_origin(_action(D, "exporter.py"), COMMITS, missing, B)[0] == "unknown"

    assert unreviewed_code_origin(_action(B, "exporter.py"), COMMITS, missing, B)[0] == "unknown"
    assert unreviewed_code_origin({**_action(D, "x.py"), "file_path": None}, COMMITS, missing, B)[0] == "unknown"

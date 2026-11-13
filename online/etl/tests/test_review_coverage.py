"""Tests for reading what a bot reviewed (its record, reviews and notices) and the line history of a fix."""

from __future__ import annotations

from pipeline.review_coverage import NO_COMMIT
from pipeline.review_coverage import ReviewRecord
from pipeline.review_coverage import _is_source
from pipeline.review_coverage import comment_record_missing
from pipeline.review_coverage import completed_no_review
from pipeline.review_coverage import find_notices
from pipeline.review_coverage import find_review_records
from pipeline.review_coverage import fix_code_origin
from pipeline.review_coverage import later_comment_review
from pipeline.review_coverage import notice_reasons
from pipeline.review_coverage import path_filtered
from pipeline.review_coverage import record_in_issue_comments
from pipeline.review_coverage import reviewed_before
from pipeline.review_coverage import rewritten_review_commits

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


def test_skips_and_the_in_progress_placeholder_are_notices_that_name_no_commit() -> None:
    skips = [
        "> ## Review skipped\n>\n> Auto incremental reviews are disabled on this repository.",
        "> This repository does not receive automatic reviews because it has fewer than 10 stars.",
        "> ## Review skipped\n>\n> Review was skipped as selected files did not have any reviewable changes.",
        "> ## Draft PR not reviewed\n>\n> Draft PRs are not automatically reviewed by default.",
    ]
    events = [issue_comment(summary(_block("skip review", s))) for s in skips]
    progress = f"> Currently processing new changes in this PR.\n>\n> {review_range(A, B)}"
    events.append(issue_comment(summary(_block("review in progress", progress))))

    notices = find_notices(events, CR)
    assert [n.reason for n in notices] == ["skip"] * 4 + ["in progress"]
    assert all(n.reviewed_sha is None for n in notices)
    # A review under way is not a push left unreviewed: it is never named in a label.
    assert notice_reasons(events, CR) == ["skip"]


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


# -- earlier revisions and gaps ------------------------------------------------------


def revision(body: str, ts: str, actor: str = CR) -> dict:
    """An earlier revision of an edited comment, as the analysis adds it from the edit history."""
    return {"actor": actor, "event_type": "issue_comment", "timestamp": ts, "data": {"body": body, "revision_of": 1}}


def test_an_earlier_revision_is_a_notice_posted_at_its_edit_time() -> None:
    [n] = find_notices([revision(summary(rate_limited(A, C)), ts="2026-07-01T10:30:00Z")], CR)

    assert (n.reason, n.in_place, n.timestamp, n.reviewed_sha, n.unreviewed_sha) == (
        "limit",
        False,
        "2026-07-01T10:30:00Z",
        A,
        C,
    )


# -- the bot's own record of what it reviewed ------------------------------------


def review_range(start: str, head: str) -> str:
    return (
        "<details>\n<summary>📥 Commits</summary>\n\n"
        f"Reviewing files that changed from the base of the PR and between {start} and {head}.\n\n</details>"
    )


def recent_review(
    start: str, head: str, found: str = "No actionable comments were generated in the recent review. 🎉"
) -> str:
    return f"<!-- recent_review_start -->\n\n{found}\n\n{review_range(start, head)}\n\n<!-- recent_review_end -->"


BASE = "f" * 40  # the PR's base commit: not one of its commits


def _spans(records: list[ReviewRecord]) -> list[tuple[int, int, str]]:
    return [(r.start, r.head, r.source) for r in records]


def test_a_review_body_records_the_range_it_read() -> None:
    events = [review(A, body=f"**Actionable comments posted: 1**\n{review_range(BASE, A)}"),
              review(C, body=f"**Actionable comments posted: 2**\n{review_range(A, C)}")]  # fmt: skip

    assert _spans(find_review_records(events, COMMITS, CR)) == [(-1, 0, "record"), (0, 2, "record")]


def test_the_start_of_a_range_is_a_reviewed_commit() -> None:
    # The review of C read the range B..C, so the bot had reviewed B, though B's own record (say, a
    # summary revision GitHub no longer keeps) was not read. The notice parser reads the same sentence
    # with B as the last reviewed commit.
    events = [review(A, body=f"**Actionable comments posted: 1**\n{review_range(BASE, A)}"),
              review(C, body=f"**Actionable comments posted: 2**\n{review_range(B, C)}")]  # fmt: skip

    records = find_review_records(events, COMMITS, CR)

    assert _spans(records) == [(-1, 0, "record"), (-1, 1, "record-start"), (1, 2, "record")]
    assert reviewed_before(1, 3, records)
    # Where a record already has the start as its head, nothing is added.
    events.insert(1, review(B, body=f"**Actionable comments posted: 0**\n{review_range(A, B)}"))
    assert [r.source for r in find_review_records(events, COMMITS, CR)] == ["record"] * 3


def test_a_review_that_found_nothing_is_recorded_in_the_summary() -> None:
    # The review of C posted nothing; only the summary comment (a later revision) records it.
    events = [review(A, body=review_range(BASE, A)),
              revision(summary(recent_review(A, C)), ts="2026-07-01T11:00:00Z")]  # fmt: skip

    records = find_review_records(events, COMMITS, CR)

    assert _spans(records) == [(-1, 0, "record"), (0, 2, "record")]
    assert reviewed_before(2, 3, records)
    assert not reviewed_before(3, 3, records)


def test_ranges_in_unfinished_or_refused_blocks_are_not_reviews() -> None:
    in_progress = _block("review in progress", f"> Currently processing new changes.\n> {review_range(A, D)}")
    stars = _block(
        "skip review",
        "> - [ ] Trigger review\n>\n> This repository does not receive automatic reviews because it has fewer than "
        f"10 stars.\n> {review_range(A, D)}",
    )
    events = [review(A, body=review_range(BASE, A)), issue_comment(summary(in_progress, stars, rate_limited(A, D)))]

    assert _spans(find_review_records(events, COMMITS, CR)) == [(-1, 0, "record")]


def test_a_review_of_the_fixed_commit_or_later_did_not_read_the_code_the_fix_removed() -> None:
    # A limit was rewritten away: the bot reviewed A, skipped B and C, then reviewed A..D at once.
    events = [review(A, body=review_range(BASE, A)),
              revision(summary(rate_limited(A, C)), ts="2026-07-01T10:30:00Z"),
              review(D, body=review_range(A, D), ts="2026-07-01T12:00:00Z")]  # fmt: skip

    records = find_review_records(events, COMMITS, CR)

    assert _spans(records) == [(-1, 0, "record"), (0, 3, "record")]
    assert not reviewed_before(2, 3, records)  # code written in C, for a fix in D
    assert reviewed_before(2, 4, records)  # the same code, for a fix after D


def test_a_completed_review_without_a_record_reads_the_whole_pull_request() -> None:
    inline = {"actor": CR, "event_type": "review_comment", "timestamp": "2026-07-01T13:00:00Z",
              "data": {"original_commit_id": C, "body": "Finding."}}  # fmt: skip
    reply = {"actor": CR, "event_type": "review_comment", "timestamp": "2026-07-01T13:00:00Z",
             "data": {"original_commit_id": D, "in_reply_to_id": 9, "body": "Thanks."}}  # fmt: skip
    events = [review(A), inline, reply, review(D, body="")]

    assert _spans(find_review_records(events, COMMITS, CR)) == [(-1, 0, "review"), (-1, 2, "review")]


def test_a_submitted_review_with_an_empty_body_is_a_completed_review() -> None:
    # cubic reviewed A, B added code, cubic approved C with no body and no inline comment, D fixes B's
    # line: the approval read B, so the fix is of reviewed code.
    cubic = "cubic-dev-ai[bot]"

    def submitted(sha: str, state: str, body: str = "", actor: str = cubic) -> dict:
        return {
            "actor": actor,
            "event_type": "review",
            "timestamp": "",
            "data": {"state": state, "body": body, "commit_id": sha},
        }

    events = [submitted(A, "COMMENTED", "Found 1 issue."), submitted(C, "APPROVED")]
    records = find_review_records(events, COMMITS, cubic)
    assert _spans(records) == [(-1, 0, "review"), (-1, 2, "review")]

    exporter_b = "@@ -0,0 +1,2 @@\n+def export(rows):\n+    return write('/tmp/out.csv', rows)"
    fix_d = "@@ -1,2 +1,2 @@\n def export(rows):\n-    return write('/tmp/out.csv', rows)\n+    return write(OUT, rows)"
    details = _details(
        (A, "a.py", "added", "@@ -0,0 +1 @@\n+x = 1"), (B, "e.py", "added", exporter_b), (D, "e.py", "modified", fix_d)
    )
    assert fix_code_origin(_action(D, "e.py"), COMMITS, details, records, 0)[0] == "reviewed"
    # Without the approval the fix is of code no review read.
    assert fix_code_origin(_action(D, "e.py"), COMMITS, details, records[:1], 0)[0] == "unreviewed"

    # A notice posted as a review is still not one, whatever its state; a replaced commit an empty
    # approval names is still a rewritten review.
    limit = "You have reached your Codex usage limits for code reviews."
    assert _spans(find_review_records([submitted(B, "COMMENTED", limit, CODEX)], COMMITS, CODEX)) == []
    cr_limit = submitted(B, "COMMENTED", summary(rate_limited(A, B)), CR)
    assert [r.source for r in find_review_records([cr_limit], COMMITS, CR)] == ["notice"]
    assert rewritten_review_commits([submitted("e" * 40, "APPROVED")], COMMITS, cubic) == ["e" * 40]
    # A dismissed review was an approval or a change request: it read its commit too.
    assert rewritten_review_commits([submitted("e" * 40, "DISMISSED")], COMMITS, cubic) == ["e" * 40]
    dismissed = find_review_records([events[0], submitted(C, "DISMISSED")], COMMITS, cubic)
    assert fix_code_origin(_action(D, "e.py"), COMMITS, details, dismissed, 0)[0] == "reviewed"
    # GitHub nulls the commit of a review once the reviewed commit is gone: the review cannot be placed.
    for gone in (None, ""):
        assert rewritten_review_commits([submitted(gone, "COMMENTED", "All issues addressed.")], COMMITS, cubic) == [
            NO_COMMIT
        ]
    assert rewritten_review_commits([submitted(None, "COMMENTED")], COMMITS, cubic) == []  # a thread reply
    # An empty commented review is a reply posted in a review thread, not a review of its commit.
    assert _spans(find_review_records([submitted(C, "COMMENTED")], COMMITS, cubic)) == []
    assert _spans(find_review_records([submitted(C, "CHANGES_REQUESTED")], COMMITS, cubic)) == [(-1, 2, "review")]


def test_a_notice_s_last_reviewed_commit_counts_only_where_no_record_shows_one() -> None:
    no_record = [review(A), issue_comment(summary(rate_limited(B, D)))]
    with_record = [review(A, body=review_range(BASE, A)), review(B, body=review_range(A, B)),
                   issue_comment(summary(rate_limited(B, D)))]  # fmt: skip

    assert _spans(find_review_records(no_record, COMMITS, CR)) == [(-1, 0, "review"), (-1, 1, "notice")]
    assert _spans(find_review_records(with_record, COMMITS, CR)) == [(-1, 0, "record"), (0, 1, "record")]


def test_a_record_naming_a_rebased_commit_is_dropped() -> None:
    gone = "e" * 40
    events = [review(A, body=review_range(BASE, A)), review(gone, body=review_range(A, gone))]

    assert _spans(find_review_records(events, COMMITS, CR)) == [(-1, 0, "record")]
    # The dropped review would make the rewritten commit read as never reviewed: the PR is marked as
    # naming a replaced commit, and the analysis then leaves no fix out (test_analyze_unreviewed).
    assert rewritten_review_commits(events, COMMITS, CR) == [gone]
    assert rewritten_review_commits(events[:1], COMMITS, CR) == []


def test_every_kind_of_named_commit_is_checked_against_the_commit_list() -> None:
    gone = "e" * 40
    inline = {"actor": CR, "event_type": "review_comment", "timestamp": "", "data": {"original_commit_id": gone}}
    reply = {**inline, "data": {"original_commit_id": "9" * 40, "in_reply_to_id": 1}}
    greptile = "greptile-apps[bot]"
    last_reviewed = issue_comment(f"Last reviewed commit: [x](https://github.com/o/r/commit/{gone})", actor=greptile)

    assert rewritten_review_commits([inline, reply], COMMITS, CR) == [gone]
    # An inline comment whose commit the answer gave as null cannot be placed; a thread reply with
    # none is not a review. Nor can one read without its commit (a review thread stored before the
    # threads query asked for it): its review ran at a commit that cannot be told.
    null_commit = {**inline, "data": {"body": "Finding.", "original_commit_id": None}}
    assert rewritten_review_commits([null_commit], COMMITS, CR) == [NO_COMMIT]
    unasked = {**inline, "data": {"body": "Finding.", "source": "api"}}
    assert rewritten_review_commits([unasked], COMMITS, CR) == [NO_COMMIT]
    assert rewritten_review_commits([{**reply, "data": {"in_reply_to_id": 1}}], COMMITS, CR) == []
    assert rewritten_review_commits([issue_comment(summary(paused(gone, D)))], COMMITS, CR) == [gone]
    # The start of a range is the PR's base on a first review, never one of its commits: not checked.
    assert rewritten_review_commits([issue_comment(summary(rate_limited(gone, D)))], COMMITS, CR) == []
    assert rewritten_review_commits([review(A, body=review_range(gone, A))], COMMITS, CR) == []
    # A review whose body is a notice, posted on a commit since replaced, did not review it.
    assert rewritten_review_commits([review(gone, body=paused())], COMMITS, CR) == []
    assert rewritten_review_commits([last_reviewed], COMMITS, greptile) == [gone]
    assert rewritten_review_commits([last_reviewed], COMMITS, CR) == []


def test_other_bots_records() -> None:
    greptile = "greptile-apps[bot]"
    summary_g = (
        f'<sub>Reviews (2) · Last reviewed commit: ["fix: the parser..."](https://github.com/o/r/commit/{C})</sub>'
    )
    state = f'<!-- codex-review-state:v1 {{"code_review":{{"commit_sha":"{B}","has_any_findings":false}}}} -->'

    assert _spans(find_review_records([issue_comment(summary_g, actor=greptile)], COMMITS, greptile)) == [
        (-1, 2, "record")
    ]
    assert _spans(find_review_records([issue_comment(state, actor=CODEX)], COMMITS, CODEX)) == [(-1, 1, "record")]
    assert find_review_records([issue_comment(summary_g, actor=greptile)], COMMITS, CR) == []


def test_which_bots_keep_a_record_in_conversation_comments() -> None:
    for bot in (CR, "coderabbitai", "greptile-apps[bot]", CODEX):
        assert record_in_issue_comments(bot)
    for bot in ("cubic-dev-ai[bot]", "claude[bot]", "Copilot", "qodo-code-review[bot]"):
        assert not record_in_issue_comments(bot)


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


def _origin(action: dict, details: dict[str, dict]) -> tuple[str, str]:
    # The analysis split at A; a review read B; nothing after B was reviewed.
    origin, reason, _ = fix_code_origin(action, COMMITS, details, [ReviewRecord(-1, 1, "record")], 0)
    return origin, reason


def test_a_fix_of_lines_written_after_the_last_review_is_unreviewed() -> None:
    fix = "@@ -1,3 +1,3 @@\n def export(rows):\n-    target = '/tmp/out.csv'\n+    target = OUT_PATH\n     return write(target, rows)"
    details = _details(
        (A, "parser.py", "added", PARSER_A),
        (C, "exporter.py", "added", EXPORTER_C),
        (D, "exporter.py", "modified", fix),
    )

    origin, reason = _origin(_action(D, "exporter.py"), details)

    assert origin == "unreviewed"
    assert "all 1 lines" in reason


def test_a_fix_of_reviewed_lines_is_reviewed() -> None:
    fix = "@@ -1,2 +1,2 @@\n def parse(text):\n-    return text.split(',')\n+    return text.split(';')"
    details = _details((A, "parser.py", "added", PARSER_A), (D, "parser.py", "modified", fix))

    assert _origin(_action(D, "parser.py"), details)[0] == "reviewed"


def test_a_fix_over_both_reviewed_and_new_lines_is_mixed() -> None:
    later = "@@ -1,2 +1,3 @@\n def parse(text):\n+    text = text.strip()\n     return text.split(',')"
    fix = "@@ -1,3 +1,2 @@\n def parse(text):\n-    text = text.strip()\n-    return text.split(',')\n+    return (text or '').strip().split(',')"
    details = _details(
        (A, "parser.py", "added", PARSER_A), (C, "parser.py", "modified", later), (D, "parser.py", "modified", fix)
    )

    assert _origin(_action(D, "parser.py"), details)[0] == "mixed"


def test_a_line_lightly_edited_after_the_last_review_keeps_its_reviewed_origin() -> None:
    # C appends to a reviewed line; the defect in the reviewed part was there before the last review.
    edit = "@@ -1,2 +1,2 @@\n def parse(text):\n-    return text.split(',')\n+    return text.split(',')[:100]"
    fix = "@@ -1,2 +1,2 @@\n def parse(text):\n-    return text.split(',')[:100]\n+    return text.split(';')[:100]"
    details = _details(
        (A, "parser.py", "added", PARSER_A), (C, "parser.py", "modified", edit), (D, "parser.py", "modified", fix)
    )

    assert _origin(_action(D, "parser.py"), details)[0] == "reviewed"


def test_a_line_written_unreviewed_then_edited_in_a_reviewed_commit_is_reviewed() -> None:
    # A was reviewed; B wrote the line without a review; C lightly edited it and a review read B..C,
    # so that review's diff showed the faulty delimiter; D fixes it.
    parser_b = "@@ -1 +1,2 @@\n def parse(text):\n+    return text.split(',')"
    edit_c = "@@ -1,2 +1,2 @@\n def parse(text):\n-    return text.split(',')\n+    return text.split(',')[:100]"
    fix_d = "@@ -1,2 +1,2 @@\n def parse(text):\n-    return text.split(',')[:100]\n+    return text.split(';')[:100]"
    details = _details(
        (A, "parser.py", "added", "@@ -0,0 +1 @@\n+def parse(text):"),
        (B, "parser.py", "modified", parser_b),
        (C, "parser.py", "modified", edit_c),
        (D, "parser.py", "modified", fix_d),
    )
    action = _action(D, "parser.py")
    reviewed_c = [ReviewRecord(-1, 0, "record"), ReviewRecord(1, 2, "record")]

    assert fix_code_origin(action, COMMITS, details, reviewed_c, 0)[0] == "reviewed"
    # Without the review of C, neither commit that wrote the line was read: the fix is unreviewed.
    origin, _, written = fix_code_origin(action, COMMITS, details, [ReviewRecord(-1, 0, "record")], 0)
    assert (origin, written) == ("unreviewed", (B, C))


def test_a_fix_that_only_adds_lines_is_kept_even_beside_new_lines() -> None:
    # The lines beside the additions were written after the last review, but the fix changes no earlier
    # line, so the diff cannot say where the code it fixes was written: it is kept.
    fix = "@@ -1,3 +1,5 @@\n def export(rows):\n     target = '/tmp/out.csv'\n+    if not rows:\n+        return None\n     return write(target, rows)"
    details = _details((C, "exporter.py", "added", EXPORTER_C), (D, "exporter.py", "modified", fix))

    origin, reason = _origin(_action(D, "exporter.py"), details)

    assert origin == "unknown"
    assert "only adds lines" in reason


def test_an_add_only_fix_after_a_refactor_of_reviewed_code_is_kept() -> None:
    # A reviews a read loop; C moves it into a new helper (new wrapper lines, the loop lines unchanged
    # in text); D adds a check after the loop. The defect (no short-read check) was in reviewed code.
    loop_a = "@@ -0,0 +1,3 @@\n+def download(r):\n+    while chunk := r.read1(1024):\n+        yield chunk"
    refactor_c = (
        "@@ -1,3 +1,6 @@\n-def download(r):\n+def _read_body(r):\n+    # Read the body in chunks.\n"
        "     while chunk := r.read1(1024):\n         yield chunk\n+def download(r):\n+    return _read_body(r)"
    )
    fix_d = (
        "@@ -1,4 +1,6 @@\n def _read_body(r):\n     # Read the body in chunks.\n"
        "     while chunk := r.read1(1024):\n         yield chunk\n+    if r.length:\n+        raise IOError('short read')"
    )
    details = _details(
        (A, "dl.py", "added", loop_a), (C, "dl.py", "modified", refactor_c), (D, "dl.py", "modified", fix_d)
    )

    assert _origin(_action(D, "dl.py"), details)[0] == "unknown"


APP_A = "@@ -0,0 +1,2 @@\n+def parse(text):\n+    return text.split(',')"
APP_C = (
    "@@ -1,2 +1,5 @@\n def parse(text):\n     return text.split(',')\n"
    "+def export(rows):\n+    target = '/tmp/out.csv'\n+    return write(target, rows)"
)
EXPORT_FIX_D = (
    "\n def export(rows):\n-    target = '/tmp/out.csv'\n+    target = OUT_PATH\n     return write(target, rows)"
)


def test_a_fix_that_also_adds_lines_to_reviewed_code_in_the_same_commit_is_kept() -> None:
    # A reviewed parser; C adds an exporter without a review; D adds a null guard to the parser (an
    # add-only hunk) and replaces the export path (a line C wrote). Both actions on app.py in D get the
    # same answer, so the parser guard must not be taken for a fix of the unreviewed exporter.
    fix_d = (
        "@@ -1,2 +1,4 @@\n def parse(text):\n+    if text is None:\n+        return []\n     return text.split(',')\n"
        "@@ -3,3 +5,3 @@" + EXPORT_FIX_D
    )
    details = _details(
        (A, "app.py", "added", APP_A), (C, "app.py", "modified", APP_C), (D, "app.py", "modified", fix_d)
    )

    origin, reason = _origin(_action(D, "app.py"), details)

    assert origin == "unknown"
    assert "adds lines among code a review read (2 of the 2 lines" in reason


def test_an_addition_in_the_same_hunk_as_an_unreviewed_change_is_judged_on_its_own() -> None:
    # The parser guard and the export path change of the test above, merged into one hunk: the guard is
    # its own change block, among reviewed parser lines, so the fix is still kept.
    fix_d = (
        "@@ -1,5 +1,7 @@\n def parse(text):\n+    if text is None:\n+        return []\n     return text.split(',')"
        "\n def export(rows):\n-    target = '/tmp/out.csv'\n+    target = OUT_PATH\n     return write(target, rows)"
    )
    details = _details(
        (A, "app.py", "added", APP_A), (C, "app.py", "modified", APP_C), (D, "app.py", "modified", fix_d)
    )

    origin, reason = _origin(_action(D, "app.py"), details)

    assert origin == "unknown"
    assert "adds lines among code a review read (2 of the 3 lines" in reason

    # A block of the same hunk that adds a line after the unreviewed exporter is in unreviewed code.
    fix_d = (
        "@@ -3,3 +3,4 @@\n def export(rows):\n-    target = '/tmp/out.csv'\n+    target = OUT_PATH\n"
        "     return write(target, rows)\n+export_all = export"
    )
    details = _details(
        (A, "app.py", "added", APP_A), (C, "app.py", "modified", APP_C), (D, "app.py", "modified", fix_d)
    )

    assert _origin(_action(D, "app.py"), details)[0] == "unreviewed"


def test_a_guard_that_replaces_an_unreviewed_line_is_left_out_by_design() -> None:
    # A reviewed the split; C inserted a debug print before it without a review; D replaces the print
    # with a null guard. The guard also protects the reviewed split below it, but the line the fix
    # changed, the print, was never reviewed: the fix is of unreviewed code and is left out.
    print_c = "@@ -1,2 +1,3 @@\n def parse(text):\n+    print(text)\n     return text.split(',')"
    fix_d = (
        "@@ -1,3 +1,4 @@\n def parse(text):\n-    print(text)\n+    if text is None:\n+        return []\n"
        "     return text.split(',')"
    )
    details = _details(
        (A, "parser.py", "added", PARSER_A), (C, "parser.py", "modified", print_c), (D, "parser.py", "modified", fix_d)
    )

    origin, reason = _origin(_action(D, "parser.py"), details)

    assert origin == "unreviewed"
    assert reason.startswith("all 1 lines the fix changed in parser.py were written in commits no review")

    # The same guard inserted without replacing the print changes no line, so that fix is kept.
    insert_d = (
        "@@ -1,3 +1,5 @@\n def parse(text):\n     print(text)\n+    if text is None:\n+        return []\n"
        "     return text.split(',')"
    )
    details = _details(
        (A, "parser.py", "added", PARSER_A),
        (C, "parser.py", "modified", print_c),
        (D, "parser.py", "modified", insert_d),
    )
    assert _origin(_action(D, "parser.py"), details)[0] == "unknown"


def test_a_fix_that_also_adds_lines_to_the_unreviewed_code_is_unreviewed() -> None:
    # The same commit adds a check inside the exporter C wrote: every hunk is in code no review read.
    fix_d = (
        "@@ -3,2 +3,2 @@\n def export(rows):\n-    target = '/tmp/out.csv'\n+    target = OUT_PATH\n"
        "@@ -5 +5,3 @@\n     return write(target, rows)\n+def export_all(rows):\n+    return [export(r) for r in rows]"
    )
    details = _details(
        (A, "app.py", "added", APP_A), (C, "app.py", "modified", APP_C), (D, "app.py", "modified", fix_d)
    )

    origin, reason = _origin(_action(D, "app.py"), details)

    assert origin == "unreviewed"
    assert "and the 1 lines around its other additions" in reason


def test_a_hunk_that_adds_lines_with_no_code_around_them_is_unknown() -> None:
    fix_d = "@@ -3,3 +3,3 @@" + EXPORT_FIX_D + "\n@@ -5,0 +6 @@\n+# done"
    details = _details(
        (A, "app.py", "added", APP_A), (C, "app.py", "modified", APP_C), (D, "app.py", "modified", fix_d)
    )

    assert _origin(_action(D, "app.py"), details) == (
        "unknown",
        "the fix also changes lines in app.py with no code line around them",
    )


def test_reviewed_code_an_unreviewed_commit_moved_or_re_wrapped_keeps_its_fix() -> None:
    # A reviewed g; C, unreviewed, moves g to the top of the file in two hunks; D fixes g.
    m_a = "@@ -0,0 +1,14 @@\n+def f(a):\n+    return a + 1\n" + "".join(f"+x{i} = {i}\n" for i in range(10))
    m_a += "+def g(b):\n+    return b * 2"
    move_c = "@@ -0,0 +1,2 @@\n+def g(b):\n+    return b * 2\n@@ -13,2 +14,0 @@\n-def g(b):\n-    return b * 2"
    fix_d = "@@ -1,2 +1,2 @@\n def g(b):\n-    return b * 2\n+    return b * 3"
    details = _details((A, "m.py", "added", m_a), (C, "m.py", "modified", move_c), (D, "m.py", "modified", fix_d))
    origin, reason = _origin(_action(D, "m.py"), details)
    assert origin == "unknown"
    assert reason.startswith(f"commit {'c' * 12} moved or re-wrapped a line")

    # C extracts a reviewed helper of app.py into a new file, helpers.py; D fixes it there.
    helper = "+def helper(x):\n+    y = x * 2\n+    return y"
    details = _details(
        (A, "app.py", "added", "@@ -0,0 +1,3 @@\n" + helper),
        (C, "app.py", "modified", "@@ -1,3 +0,0 @@\n" + helper.replace("+", "-")),
        (C, "helpers.py", "added", "@@ -0,0 +1,3 @@\n" + helper),
        (
            D,
            "helpers.py",
            "modified",
            "@@ -1,3 +1,3 @@\n def helper(x):\n-    y = x * 2\n+    y = x * 3\n     return y",
        ),
    )
    assert _origin(_action(D, "helpers.py"), details)[0] == "unknown"

    # C re-wraps a reviewed call over three lines (a formatter); D changes one argument.
    r_a = "@@ -0,0 +1,2 @@\n+def run():\n+    result = compute(alpha, beta, gamma, delta)"
    wrap_c = (
        "@@ -1,2 +1,4 @@\n def run():\n-    result = compute(alpha, beta, gamma, delta)\n"
        "+    result = compute(\n+        alpha, beta, gamma, delta\n+    )"
    )
    fix_d = "@@ -2,3 +2,3 @@\n     result = compute(\n-        alpha, beta, gamma, delta\n+        alpha, beta, gamma, eps\n     )"
    details = _details((A, "r.py", "added", r_a), (C, "r.py", "modified", wrap_c), (D, "r.py", "modified", fix_d))
    assert _origin(_action(D, "r.py"), details)[0] == "unknown"

    # Without the removal, the same lines are new code C wrote: a copy, not a move.
    copy_c = "@@ -14,0 +15,2 @@\n+def g(b):\n+    return b * 2"
    fix_copy = "@@ -15,2 +15,2 @@\n def g(b):\n-    return b * 2\n+    return b * 3"
    details = _details((A, "m.py", "added", m_a), (C, "m.py", "modified", copy_c), (D, "m.py", "modified", fix_copy))
    assert _origin(_action(D, "m.py"), details)[0] == "unreviewed"


def test_an_exact_move_is_found_before_a_light_edit_is_followed() -> None:
    # A reviewed old.py's call; B, unreviewed, wrote new.py with strict=True; C deletes old.py and puts
    # the reviewed line, verbatim, in place of B's. The line is a light edit of B's line, but it is the
    # reviewed code moved, so the fix of it is kept.
    reviewed = "    result = process(payload, strict=False)"
    details = _details(
        (A, "old.py", "added", f"@@ -0,0 +1,2 @@\n+def run(payload):\n+{reviewed}"),
        (B, "new.py", "added", "@@ -0,0 +1,2 @@\n+def run(payload):\n+    result = process(payload, strict=True)"),
        (C, "old.py", "removed", f"@@ -1,2 +0,0 @@\n-def run(payload):\n-{reviewed}"),
        (
            C,
            "new.py",
            "modified",
            f"@@ -1,2 +1,2 @@\n def run(payload):\n-    result = process(payload, strict=True)\n+{reviewed}",
        ),
        (D, "new.py", "modified", f"@@ -1,2 +1,2 @@\n def run(payload):\n-{reviewed}\n+{reviewed[:-1]}, retries=3)"),
    )
    a_reviewed = [ReviewRecord(-1, 0, "record")]

    origin, reason, _ = fix_code_origin(_action(D, "new.py"), COMMITS, details, a_reviewed, 0)

    assert origin == "unknown"
    assert reason.startswith(f"commit {'c' * 12} moved or re-wrapped a line")

    # Without old.py's removal, C's line is a light edit of B's: both unreviewed, the fix is left out.
    del details[C]["files"][0]
    assert fix_code_origin(_action(D, "new.py"), COMMITS, details, a_reviewed, 0)[0] == "unreviewed"


def test_a_reviewed_line_removed_then_restored_keeps_its_fix() -> None:
    # A wrote the reviewed split; B removed it and C reverted B, both without a review; D fixes the line.
    # C's line is the reviewed code put back, not code C wrote.
    remove_b = "@@ -1,2 +1 @@\n def parse(text):\n-    return text.split(',')"
    revert_c = "@@ -1 +1,2 @@\n def parse(text):\n+    return text.split(',')"
    fix_d = "@@ -1,2 +1,2 @@\n def parse(text):\n-    return text.split(',')\n+    return text.split(';')"
    details = _details(
        (A, "parser.py", "added", PARSER_A),
        (B, "parser.py", "modified", remove_b),
        (C, "parser.py", "modified", revert_c),
        (D, "parser.py", "modified", fix_d),
    )

    # The analysis split at A, and only A was reviewed.
    origin, reason, _ = fix_code_origin(_action(D, "parser.py"), COMMITS, details, [ReviewRecord(-1, 0, "record")], 0)

    assert origin == "unknown"
    assert reason.startswith(f"commit {'c' * 12} put back in parser.py a line the fix reads that commit {'b' * 12}")


def test_a_removed_punctuation_line_is_judged_by_the_code_around_it() -> None:
    # A reviewed a loop with a stray empty statement; C, unreviewed, appended a log call. D deletes the
    # `;` (the fix of the reviewed loop) and also rewrites C's call: the `;` block changes reviewed code.
    loop_a = "@@ -0,0 +1,3 @@\n+while (next())\n+  ;\n+process();"
    log_c = "@@ -3 +3,2 @@\n process();\n+log('done');"
    fix_d = "@@ -1,3 +1,2 @@\n while (next())\n-  ;\n process();\n@@ -4 +3 @@\n-log('done');\n+log('finished');"
    details = _details((A, "w.js", "added", loop_a), (C, "w.js", "modified", log_c), (D, "w.js", "modified", fix_d))

    origin, reason = _origin(_action(D, "w.js"), details)

    assert origin == "unknown"
    assert "(2 of the 2 lines" in reason

    # Without the `;` deletion the fix changes only C's line and is left out.
    only_log = "@@ -4 +4 @@\n-log('done');\n+log('finished');"
    details = _details((A, "w.js", "added", loop_a), (C, "w.js", "modified", log_c), (D, "w.js", "modified", only_log))
    assert _origin(_action(D, "w.js"), details)[0] == "unreviewed"


def test_a_reviewed_line_restored_in_place_of_a_similar_one_keeps_its_fix() -> None:
    # A reviewed the call; B replaced it with `pass`; C added the strict=True variant; D puts A's exact
    # line back in place of C's, a light edit of it. E then fixes the line: it is the reviewed call
    # restored, not D's or C's code.
    e = "e" * 40
    commits = [{"sha": s} for s in (A, B, C, D, e)]
    details = {s: {"sha": s, "files": []} for s in (A, B, C, D, e)}
    for sha, patch in (
        (A, "@@ -0,0 +1,2 @@\n+def run(text):\n+    return parse(text, strict=False)"),
        (B, "@@ -1,2 +1,2 @@\n def run(text):\n-    return parse(text, strict=False)\n+    pass"),
        (C, "@@ -1,2 +1,3 @@\n def run(text):\n     pass\n+    return parse(text, strict=True)"),
        (D, "@@ -2,2 +2,2 @@\n     pass\n-    return parse(text, strict=True)\n+    return parse(text, strict=False)"),
        (
            e,
            "@@ -2,2 +2,2 @@\n     pass\n-    return parse(text, strict=False)\n+    return parse(text, strict=False, limit=9)",
        ),
    ):
        details[sha]["files"].append({"filename": "r.py", "status": "modified", "patch": patch})

    origin, reason, _ = fix_code_origin(_action(e, "r.py"), commits, details, [ReviewRecord(-1, 0, "record")], 0)

    assert origin == "unknown"
    assert reason.startswith(f"commit {'d' * 12} put back in r.py a line the fix reads that commit {'b' * 12}")


def test_an_unreadable_removal_in_another_file_keeps_the_fix() -> None:
    # C extracts a reviewed helper of app.py into helpers.py, but its patch for app.py is missing (a
    # large diff) or does not add up: the removal cannot be read, so the move cannot be ruled out.
    helper = "+def helper(x):\n+    y = x * 2\n+    return y"
    fix_d = "@@ -1,3 +1,3 @@\n def helper(x):\n-    y = x * 2\n+    y = x * 3\n     return y"

    def details_with(app_c: dict) -> dict[str, dict]:
        details = _details(
            (A, "app.py", "added", "@@ -0,0 +1,3 @@\n" + helper),
            (C, "helpers.py", "added", "@@ -0,0 +1,3 @@\n" + helper),
            (D, "helpers.py", "modified", fix_d),
        )
        details[C]["files"].append({"filename": "app.py", "status": "modified", **app_c})
        return details

    for app_c in ({"patch": "", "deletions": 3}, {"patch": "@@ -1,3 +0,0 @@\n-def helper(x):"}):
        origin, reason = _origin(_action(D, "helpers.py"), details_with(app_c))
        assert origin == "unknown", app_c
        assert reason.startswith(f"commit {'c' * 12} removed lines whose patch is not readable")

    # A file with no patch that removed nothing (an added binary, say) leaves the history complete.
    assert _origin(_action(D, "helpers.py"), details_with({"patch": "", "additions": 1, "deletions": 0}))[0] == (
        "unreviewed"
    )


def test_each_side_of_an_insertion_is_judged_on_its_own() -> None:
    # A reviewed load(); C, unreviewed, appended save(). D inserts a guard right after load()'s three
    # closing braces and edits save(). The side before the guard holds only braces, which close
    # reviewed code, so the guard's place is not known to be unreviewed code: the fix is kept.
    load_a = "@@ -0,0 +1,7 @@\n+function load() {\n+  if (x) {\n+    for (;;) {\n+      read();\n+    }\n+  }\n+}"
    save_c = "@@ -7 +7,4 @@\n }\n+function save() {\n+  write();\n+}"
    fix_d = (
        "@@ -5,6 +5,7 @@\n     }\n   }\n }\n+if (!ready) throw new Error('not ready');\n function save() {\n"
        "-  write();\n+  write(data);\n }"
    )
    details = _details((A, "s.js", "added", load_a), (C, "s.js", "modified", save_c), (D, "s.js", "modified", fix_d))

    assert _origin(_action(D, "s.js"), details) == (
        "unknown",
        "the fix also changes lines in s.js with no code line around them",
    )

    # When the hunk reaches load()'s own code, that nearest code line stands in for the braces.
    fix_d = (
        "@@ -4,7 +4,8 @@\n       read();\n     }\n   }\n }\n+if (!ready) throw new Error('not ready');\n"
        " function save() {\n-  write();\n+  write(data);\n }"
    )
    details = _details((A, "s.js", "added", load_a), (C, "s.js", "modified", save_c), (D, "s.js", "modified", fix_d))
    origin, reason = _origin(_action(D, "s.js"), details)
    assert origin == "unknown"
    assert "adds lines among code a review read (1 of the 2 lines" in reason


def test_a_re_wrap_that_adds_a_trailing_comma_is_a_re_wrap() -> None:
    # A reviewed one call; C, a formatter (Black, Prettier), wraps it with one argument per line and a
    # trailing comma; D changes an argument. The line is the reviewed call re-wrapped.
    call_a = "@@ -0,0 +1,2 @@\n+def run():\n+    result = compute(alpha, beta)"
    wrap_c = (
        "@@ -1,2 +1,5 @@\n def run():\n-    result = compute(alpha, beta)\n"
        "+    result = compute(\n+        alpha,\n+        beta,\n+    )"
    )
    fix_d = "@@ -3,3 +3,3 @@\n         alpha,\n-        beta,\n+        gamma,\n     )"
    details = _details((A, "w.py", "added", call_a), (C, "w.py", "modified", wrap_c), (D, "w.py", "modified", fix_d))

    origin, reason = _origin(_action(D, "w.py"), details)

    assert origin == "unknown"
    assert reason.startswith(f"commit {'c' * 12} moved or re-wrapped a line")


def test_a_rename_onto_a_reused_path_is_not_traced_across() -> None:
    # A, reviewed, wrote old.py; B, unreviewed, had a new.py of its own; C renames old.py to new.py
    # and edits its first line. D fixes the second line, A's reviewed `return 1`. Traced by name past
    # the rename, the line would be taken for B's new.py line at the same place.
    details = _details(
        (A, "old.py", "added", "@@ -0,0 +1,2 @@\n+def f():\n+    return 1"),
        (B, "new.py", "added", "@@ -0,0 +1,2 @@\n+def g():\n+    return 1"),
        (C, "new.py", "renamed", "@@ -1,2 +1,2 @@\n-def f():\n+def f(x):\n     return 1"),
        (D, "new.py", "modified", "@@ -1,2 +1,2 @@\n def f(x):\n-    return 1\n+    return x"),
    )
    a_reviewed = [ReviewRecord(-1, 0, "record")]

    origin, reason, _ = fix_code_origin(_action(D, "new.py"), COMMITS, details, a_reviewed, 0)

    assert origin == "unknown"
    assert reason.startswith(f"commit {'c' * 12} renamed new.py")

    # A fixing commit that renames the file reads its old lines under the old name.
    details[D]["files"][0]["status"] = "renamed"
    assert fix_code_origin(_action(D, "new.py"), COMMITS, details, a_reviewed, 0) == (
        "unknown",
        "the fixing commit renames the file, and the history is read by file name",
        (),
    )


def test_a_copy_whose_original_a_later_commit_removes_is_a_move() -> None:
    # A reviewed a helper in a.py; B, unreviewed, copies it into b.py; C, or the fix D itself, then
    # deletes the original. B's lines are the reviewed helper moved in two steps.
    helper = '+def quote(s):\n+    s = s.replace("\'", "\'\'")\n+    return "\'" + s + "\'"'
    remove = {"filename": "a.py", "status": "removed", "patch": "@@ -1,3 +0,0 @@\n" + helper.replace("+", "-")}
    fix = '@@ -1,3 +1,3 @@\n def quote(s):\n-    s = s.replace("\'", "\'\'")\n+    s = s.replace("\'", "\'\\\\\'\'")\n     return "\'" + s + "\'"'
    a_reviewed = [ReviewRecord(-1, 0, "record")]
    for deleting in (C, D):
        details = _details(
            (A, "a.py", "added", "@@ -0,0 +1,3 @@\n" + helper),
            (B, "b.py", "added", "@@ -0,0 +1,3 @@\n" + helper),
            (D, "b.py", "modified", fix),
        )
        details[deleting]["files"].append(remove)

        origin, reason, _ = fix_code_origin(_action(D, "b.py"), COMMITS, details, a_reviewed, 0)

        assert origin == "unknown", deleting[:1]
        assert reason.startswith(f"commit {'b' * 12} added in b.py a copy of code commit {deleting[:12]} then removed")

    # Without the deletion B's lines are a copy, new code; the fix's own removal of the line it
    # changes is not taken for a deletion of the original.
    details = _details(
        (A, "a.py", "added", "@@ -0,0 +1,3 @@\n" + helper),
        (B, "b.py", "added", "@@ -0,0 +1,3 @@\n" + helper),
        (D, "b.py", "modified", fix),
    )
    assert fix_code_origin(_action(D, "b.py"), COMMITS, details, a_reviewed, 0)[0] == "unreviewed"

    # A later deletion whose patch cannot be read could be of the original: the fix is kept.
    details[C]["files"].append({"filename": "a.py", "status": "removed", "patch": "", "deletions": 3})
    origin, reason, _ = fix_code_origin(_action(D, "b.py"), COMMITS, details, a_reviewed, 0)
    assert (origin, reason[:48]) == ("unknown", f"commit {'c' * 12} removed lines whose patch is")


def test_a_patch_cut_short_between_hunks_is_not_read_as_whole() -> None:
    # GitHub truncates a large patch between whole hunks: the hunks parse, but they hold fewer lines
    # than the entry's additions and deletions say.
    fix = "@@ -1,3 +1,3 @@\n def export(rows):\n-    target = '/tmp/out.csv'\n+    target = OUT_PATH\n     return write(target, rows)"
    details = _details((C, "exporter.py", "added", EXPORTER_C), (D, "exporter.py", "modified", fix))
    assert _origin(_action(D, "exporter.py"), details)[0] == "unreviewed"

    details[D]["files"][0].update(additions=5, deletions=4)
    assert _origin(_action(D, "exporter.py"), details) == (
        "unknown",
        "the fixing commit has no readable patch for the file (missing, malformed or cut short)",
    )

    details = _details((C, "exporter.py", "added", EXPORTER_C), (D, "exporter.py", "modified", fix))
    details[C]["files"][0].update(additions=40, deletions=0)
    assert _origin(_action(D, "exporter.py"), details)[1] == (
        f"the patch of commit {'c' * 12} for the file does not add up or is cut short"
    )

    # A removal patch cut short in another file leaves the commit's removed lines incomplete.
    helper = "+def helper(x):\n+    y = x * 2\n+    return y"
    details = _details(
        (A, "app.py", "added", "@@ -0,0 +1,3 @@\n" + helper),
        (C, "helpers.py", "added", "@@ -0,0 +1,3 @@\n" + helper),
        (
            D,
            "helpers.py",
            "modified",
            "@@ -1,3 +1,3 @@\n def helper(x):\n-    y = x * 2\n+    y = x * 3\n     return y",
        ),
    )
    other = "@@ -9,2 +9 @@\n def other():\n-    pass"
    details[C]["files"].append(
        {"filename": "app.py", "status": "modified", "patch": other, "additions": 0, "deletions": 4}
    )
    origin, reason = _origin(_action(D, "helpers.py"), details)
    assert origin == "unknown"
    assert reason.startswith(f"commit {'c' * 12} removed lines whose patch is not readable")


def test_a_punctuation_line_removed_with_code_is_judged_by_where_it_is() -> None:
    # A reviewed the loop and its stray `;`; C, unreviewed, inserted a log call after it. D deletes both
    # in one block: the log is C's, but the `;` sits in the reviewed loop, so the fix is kept.
    loop_a = "@@ -0,0 +1,3 @@\n+while (next())\n+  ;\n+process();"
    log_c = "@@ -2,2 +2,3 @@\n   ;\n+log('done');\n process();"
    fix_d = "@@ -1,4 +1,2 @@\n while (next())\n-  ;\n-log('done');\n process();"
    details = _details((A, "w.js", "added", loop_a), (C, "w.js", "modified", log_c), (D, "w.js", "modified", fix_d))

    origin, reason = _origin(_action(D, "w.js"), details)

    assert origin == "mixed"
    assert reason.startswith("1 of the 2 lines the fix changed in w.js were in code no review read")

    # Deleting only C's log is a fix of unreviewed code.
    only_log = "@@ -2,3 +2,2 @@\n   ;\n-log('done');\n process();"
    details = _details((A, "w.js", "added", loop_a), (C, "w.js", "modified", log_c), (D, "w.js", "modified", only_log))
    assert _origin(_action(D, "w.js"), details)[0] == "unreviewed"


def test_every_file_of_the_fixing_commit_is_judged() -> None:
    # D fixes the exporter C wrote in app.py, and in the same commit guards the loader A reviewed in
    # loader.py, or changes one of its lines: the commit changes reviewed code, so the fix is kept.
    loader_a = "@@ -0,0 +1,2 @@\n+def load(p):\n+    return open(p).read()"
    app_fix = "@@ -1,3 +1,3 @@\n def export(rows):\n-    target = '/tmp/out.csv'\n+    target = OUT_PATH\n     return write(target, rows)"
    guard = "@@ -1,2 +1,4 @@\n def load(p):\n+    if not p:\n+        return ''\n     return open(p).read()"
    change = "@@ -1,2 +1,2 @@\n def load(p):\n-    return open(p).read()\n+    return open(p, encoding='utf-8').read()"
    for loader_d, reason in ((guard, "adds lines among code a review read"), (change, "were in code a review")):
        details = _details(
            (A, "loader.py", "added", loader_a),
            (C, "app.py", "added", EXPORTER_C),
            (D, "app.py", "modified", app_fix),
            (D, "loader.py", "modified", loader_d),
        )
        origin, why = _origin(_action(D, "app.py"), details)
        assert origin == "unknown"
        assert why.startswith("the fixing commit also changes loader.py, not wholly in unreviewed code")
        assert reason in why

    # A new file in the same commit changes no reviewed code: the fix is still left out.
    details = _details(
        (A, "loader.py", "added", loader_a),
        (C, "app.py", "added", EXPORTER_C),
        (D, "app.py", "modified", app_fix),
        (D, "test_app.py", "added", "@@ -0,0 +1 @@\n+def test_export(): ..."),
    )
    assert _origin(_action(D, "app.py"), details)[0] == "unreviewed"

    # A side edit of a reviewed test, doc or config file in the same commit is not a fix of reviewed
    # code: only other source files count.
    for side in (
        "tests/test_loader.py",
        "docs/loader.md",
        "README.md",
        ".github/workflows/ci.yml",
        "config.json",
        "lint.jsonc",
    ):
        details = _details(
            (A, side, "added", loader_a),
            (C, "app.py", "added", EXPORTER_C),
            (D, "app.py", "modified", app_fix),
            (D, side, "modified", change),
        )
        assert _origin(_action(D, "app.py"), details)[0] == "unreviewed", side


def test_unknown_when_the_history_cannot_tell() -> None:
    new_file = _details((D, "test_exporter.py", "added", "@@ -0,0 +1 @@\n+def test_export(): ..."))
    assert _origin(_action(D, "test_exporter.py"), new_file)[0] == "unknown"

    no_patch = _details(
        (C, "exporter.py", "modified", ""), (D, "exporter.py", "modified", "@@ -1 +1 @@\n-old_line()\n+new_line()")
    )
    no_patch[C]["files"][0]["additions"] = 3
    assert _origin(_action(D, "exporter.py"), no_patch)[0] == "unknown"

    missing = _details((D, "exporter.py", "modified", "@@ -1 +1 @@\n-old_line()\n+new_line()"))
    del missing[C]
    assert _origin(_action(D, "exporter.py"), missing)[0] == "unknown"

    assert _origin(_action(A, "exporter.py"), missing)[0] == "unknown"
    assert _origin({**_action(D, "x.py"), "file_path": None}, missing)[0] == "unknown"


F_A = "@@ -0,0 +1,2 @@\n+def f(a):\n+    return a + 1"
G_C = "@@ -1,2 +1,4 @@\n def f(a):\n     return a + 1\n+def g(a):\n+    return a + 1"


def test_a_copy_of_a_reviewed_line_added_later_is_not_the_reviewed_line() -> None:
    # C copies a reviewed line into a new function; D fixes the reviewed occurrence, then the copy.
    fix_f = "@@ -1,3 +1,3 @@\n def f(a):\n-    return a + 1\n+    return a - 1\n def g(a):"
    fix_g = "@@ -2,3 +2,3 @@\n     return a + 1\n def g(a):\n-    return a + 1\n+    return a - 1"

    for fix, origin in ((fix_f, "reviewed"), (fix_g, "unreviewed")):
        details = _details((A, "p.py", "added", F_A), (C, "p.py", "modified", G_C), (D, "p.py", "modified", fix))
        assert _origin(_action(D, "p.py"), details)[0] == origin


def test_short_lines_are_traced_too() -> None:
    # `x = 0` has no three-letter word; it was reviewed, and the line after it was written in C.
    run_a = "@@ -0,0 +1,2 @@\n+def run(items):\n+    x = 0"
    run_c = "@@ -1,2 +1,3 @@\n def run(items):\n     x = 0\n+    total = compute(items)"
    fix = (
        "@@ -1,3 +1,2 @@\n def run(items):\n-    x = 0\n-    total = compute(items)\n+    total = compute(items or [])"
    )
    details = _details((A, "run.py", "added", run_a), (C, "run.py", "modified", run_c), (D, "run.py", "modified", fix))

    origin, reason = _origin(_action(D, "run.py"), details)

    assert origin == "mixed"
    assert reason.startswith("1 of the 2 lines")


def test_a_line_not_where_the_history_puts_it_is_unknown() -> None:
    fix = "@@ -1,3 +1,3 @@\n def f(a):\n-    return a * 2\n+    return a - 1\n def g(a):"
    garbled = "@@ -1,2 +1,3 @@\n def f(a):\n-    return a\n+    return a"
    for c_patch, fix_patch in ((G_C, fix), (garbled, fix)):
        details = _details(
            (A, "p.py", "added", F_A), (C, "p.py", "modified", c_patch), (D, "p.py", "modified", fix_patch)
        )
        assert _origin(_action(D, "p.py"), details)[0] == "unknown"


def test_a_removed_punctuation_line_is_traced_by_position_but_never_paired() -> None:
    # C rewrote the body and kept the reviewed closing brace (unchanged in its diff). D removes C's line
    # and that brace: the brace is a line a review read, so the fix is not wholly of unreviewed code.
    body_a = "@@ -0,0 +1,3 @@\n+def check(v) {\n+  return v > 0\n+}"
    body_c = "@@ -1,3 +1,3 @@\n def check(v) {\n-  return v > 0\n+  return v >= 0 && v < limit\n }"
    fix = "@@ -1,3 +1,2 @@\n def check(v) {\n-  return v >= 0 && v < limit\n-}\n+  return v >= 0 && v <= limit }"
    details = _details((A, "c.js", "added", body_a), (C, "c.js", "modified", body_c), (D, "c.js", "modified", fix))

    assert _origin(_action(D, "c.js"), details)[0] == "mixed"

    # When C's diff removed the reviewed brace and added one of its own, the added brace is C's: which
    # brace a diff pairs with which is arbitrary, so a punctuation line is never a light edit.
    body_c = "@@ -1,3 +1,3 @@\n def check(v) {\n-  return v > 0\n-}\n+  return v >= 0 && v < limit\n+}"
    details = _details((A, "c.js", "added", body_a), (C, "c.js", "modified", body_c), (D, "c.js", "modified", fix))
    assert _origin(_action(D, "c.js"), details)[0] == "unreviewed"


def test_a_bot_that_posted_only_notices_completed_no_review() -> None:
    limit = issue_comment(summary(rate_limited("f" * 40, D)))
    assert completed_no_review([limit], COMMITS, CR)
    # An empty commented review is a reply in a thread, a notice posted as a review is a notice.
    assert completed_no_review([limit, review(A, body="")], COMMITS, CR)
    assert completed_no_review([review(A, body=paused())], COMMITS, CR)
    assert not completed_no_review([limit, review(A, body="Finding.")], COMMITS, CR)
    approval = review(A, body="")
    approval["data"]["state"] = "APPROVED"
    assert not completed_no_review([approval], COMMITS, CR)
    inline = {"actor": CR, "event_type": "review_comment", "timestamp": "", "data": {"body": "Finding."}}
    assert not completed_no_review([limit, inline], COMMITS, CR)
    assert completed_no_review([limit, {**inline, "data": {"body": "Reply.", "in_reply_to_id": 1}}], COMMITS, CR)
    # A review of a commit since replaced, or one named only by its notice, is still a review.
    assert not completed_no_review([review("e" * 40, body="Finding.")], COMMITS, CR)
    assert not completed_no_review([issue_comment(summary(paused(B, D)))], COMMITS, CR)


def review_info(ignored: list[str] = (), selected: list[str] = ()) -> str:
    """CodeRabbit's "Review info" block, with its nested file lists."""
    parts = ["<details>\n<summary>Review info</summary>\n", review_range("f" * 40, A)]
    if ignored:
        items = "\n".join(f"* `{f}` is excluded by `!**/*.lock`" for f in ignored)
        parts.append(
            f"<details>\n<summary>⛔ Files ignored due to path filters ({len(ignored)})</summary>\n\n{items}\n\n</details>"
        )
    if selected:
        items = "\n".join(f"* `{f}`" for f in selected)
        parts.append(
            f"<details>\n<summary>📒 Files selected for processing ({len(selected)})</summary>\n\n{items}\n\n</details>"
        )
    return "\n\n".join(parts) + "\n\n</details>"


def test_a_file_every_review_left_out_by_path_filters() -> None:
    ignored_d = review(D, body=f"Finding.\n{review_info(ignored=['Cargo.lock'], selected=['src/main.rs'])}")
    assert path_filtered("Cargo.lock", [ignored_d], CR)
    assert path_filtered("./Cargo.lock", [ignored_d], CR)
    assert not path_filtered("src/main.rs", [ignored_d], CR)
    # A review that does not list the file says nothing; one that read it, at any time, means it was
    # read or the configuration changed.
    other_b = review(B, body=review_info(selected=["src/lib.rs"]))
    assert path_filtered("Cargo.lock", [other_b, ignored_d], CR)
    read_b = review(B, body=review_info(selected=["Cargo.lock"]))
    assert not path_filtered("Cargo.lock", [read_b, ignored_d], CR)
    assert not path_filtered("Cargo.lock", [ignored_d, review(D, body=review_info(selected=["Cargo.lock"]))], CR)
    # The list as the first nested block of "Review info", with no other list: not swallowed.
    only = review(A, body=f"<details>\n<summary>Review info</summary>\n\n{review_info(ignored=['go.sum'])}")
    assert path_filtered("go.sum", [only], CR)
    # Another bot's review in CodeRabbit's format is not read: no other bot writes such a list.
    assert not path_filtered("Cargo.lock", [{**ignored_d, "actor": "greptile-apps[bot]"}], "greptile-apps[bot]")


def test_the_codex_review_summary_records_a_completed_review() -> None:
    def table(status: str) -> str:
        return (
            "<!-- codex-pull-request-review-summary -->\n\n## Codex Review Summary\n\n"
            "| Review | Status | Commit | Review trigger |\n| --- | --- | --- | --- |\n"
            f"| 📝 **Code Review** | {status} | `{B[:7]}` | New commits |\n"
        )

    done = table('✅ **Completed** <relative-time datetime="2026-10-07T22:21:06Z">2026-10-07</relative-time>')
    assert _spans(find_review_records([issue_comment(done, actor=CODEX)], COMMITS, CODEX)) == [(-1, 1, "record")]
    assert not completed_no_review([issue_comment(done, actor=CODEX)], COMMITS, CODEX)
    running = table('🔄 **Running** since <relative-time datetime="2026-10-07T21:45:00Z">x</relative-time>')
    assert find_review_records([issue_comment(running, actor=CODEX)], COMMITS, CODEX) == []
    # A running row is no record, but a status comment that is not a notice may be the only trace of
    # a review: the PR is not taken as never reviewed.
    assert not completed_no_review([issue_comment(running, actor=CODEX)], COMMITS, CODEX)
    limit = "You have reached your Codex usage limits for code reviews."
    assert completed_no_review([issue_comment(limit, actor=CODEX)], COMMITS, CODEX)


def test_a_comment_edited_after_the_first_review_is_a_later_comment_review() -> None:
    qodo = "qodo-code-review[bot]"
    comment = issue_comment("Code Review by Qodo", ts="2026-07-01T09:00:00Z", actor=qodo)
    first = review(A, ts="2026-07-01T10:00:00Z", actor=qodo)
    assert not later_comment_review([comment, first], qodo)
    edited = issue_comment("Code Review by Qodo", ts="2026-07-01T09:00:00Z", actor=qodo, updated="2026-07-01T11:00:00Z")
    assert later_comment_review([edited, first], qodo)
    # The earliest event counts from its creation time: an edit of the only comment is after it.
    assert later_comment_review([edited], qodo)
    assert not later_comment_review([comment], qodo)


def test_a_summary_without_a_record_is_a_review_whose_record_is_missing() -> None:
    greptile = "greptile-apps[bot]"
    only = issue_comment("<h3>Greptile Summary</h3>\n\nAdds a parser.\n\nConfidence Score: 5/5", actor=greptile)
    assert not completed_no_review([only], COMMITS, greptile)
    assert comment_record_missing([only], COMMITS, greptile)
    # A comment that is only a notice is no review.
    limit = issue_comment(summary(rate_limited("f" * 40, D)))
    assert completed_no_review([limit], COMMITS, CR)
    assert not comment_record_missing([limit], COMMITS, CR)


def test_an_inline_comment_on_the_file_shows_it_was_reviewed() -> None:
    # A review at A with its record, an inline comment on parser.py at A, and the review of the fix
    # D listing parser.py as ignored due to path filters, with no earlier list of files read: the
    # inline comment shows the bot reviewed the file, so it is not path-filtered.
    reviewed_a = review(A, body=f"Finding.\n{review_range('f' * 40, A)}")
    inline = {
        "actor": CR,
        "event_type": "review_comment",
        "timestamp": "2026-07-01T10:00:00Z",
        "data": {"body": "Null check missing.", "path": "parser.py", "original_commit_id": A},
    }
    filtered_d = review(D, body=review_info(ignored=["parser.py"]))
    assert path_filtered("parser.py", [reviewed_a, filtered_d], CR)
    assert not path_filtered("parser.py", [reviewed_a, inline, filtered_d], CR)
    assert not path_filtered(
        "./parser.py", [reviewed_a, {**inline, "data": {**inline["data"], "path": "./parser.py"}}, filtered_d], CR
    )
    # A reply in a thread on the file, or an inline comment on another file, says nothing.
    reply = {**inline, "data": {**inline["data"], "in_reply_to_id": 3}}
    assert path_filtered("parser.py", [reviewed_a, reply, filtered_d], CR)
    other = {**inline, "data": {**inline["data"], "path": "exporter.py"}}
    assert path_filtered("parser.py", [reviewed_a, other, filtered_d], CR)


def test_which_files_of_a_fixing_commit_are_source() -> None:
    # Code under a doc/ or spec/ folder, or with "generated" in its name, is source.
    for path in (
        "src/go/doc/reader.go",
        "internal/spec/validate.go",
        "lib/docs/render.py",
        "src/generated_names.py",
        "app/is-generated-file.ts",
        "src/main.go",
    ):
        assert _is_source(path), path
    # Docs by extension, tests by name or test folder, generated files by their markers, CI and config.
    for path in (
        "docs/guide.md",
        "doc/api.rst",
        "notes.txt",
        "manual/index.adoc",
        "docs/screenshots/dashboard.png",
        "docs/evidence/run.log",
        "tests/test_parser.py",
        "src/__tests__/app.ts",
        "pkg/parser_test.go",
        "web/app.spec.ts",
        "spec/models/user_spec.rb",
        "constants.guides.generated.ts",
        "api/types_generated.go",
        "api/service.pb.go",
        "src/generated/client.ts",
        ".github/workflows/ci.yml",
        "config/app.toml",
        "go.sum",
    ):
        assert not _is_source(path), path

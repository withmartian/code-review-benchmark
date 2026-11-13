"""Tests for reading what a bot reviewed (its record, reviews and notices) and the line history of a fix."""

from __future__ import annotations

from pipeline.review_coverage import ReviewRecord
from pipeline.review_coverage import find_notices
from pipeline.review_coverage import find_review_records
from pipeline.review_coverage import fix_code_origin
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
    assert rewritten_review_commits([submitted("e" * 40, "DISMISSED")], COMMITS, cubic) == []
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


def test_punctuation_lines_do_not_decide_where_code_was_written() -> None:
    # C rewrote the body; the diff paired its closing brace with the reviewed one. D removes both.
    body_a = "@@ -0,0 +1,3 @@\n+def check(v) {\n+  return v > 0\n+}"
    body_c = "@@ -1,3 +1,3 @@\n def check(v) {\n-  return v > 0\n+  return v >= 0 && v < limit\n }"
    fix = "@@ -1,3 +1,2 @@\n def check(v) {\n-  return v >= 0 && v < limit\n-}\n+  return v >= 0 && v <= limit }"
    details = _details((A, "c.js", "added", body_a), (C, "c.js", "modified", body_c), (D, "c.js", "modified", fix))

    assert _origin(_action(D, "c.js"), details)[0] == "unreviewed"

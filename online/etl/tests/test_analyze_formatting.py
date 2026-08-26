"""Tests for bot comment formatting used by LLM extraction."""

from __future__ import annotations

import json

from pipeline.analyze import _clean_bot_comment_body
from pipeline.analyze import _format_bot_comments


def test_clean_bot_comment_body_removes_hidden_html_comments() -> None:
    body = """<!-- hidden classifier notes -->
Actual review finding.
"""

    assert _clean_bot_comment_body(body) == "Actual review finding."


def test_clean_bot_comment_body_preserves_visible_markdown_blocks() -> None:
    body = """Actual review finding.

<details>
<summary>Debug trace</summary>
Internal prompt text
</details>

<sub>generated metadata</sub>
"""

    assert _clean_bot_comment_body(body) == body.strip()


def test_clean_bot_comment_body_preserves_footer_text() -> None:
    body = """Potential null dereference here.

---
If you found this review helpful, react to this comment.
"""

    assert _clean_bot_comment_body(body) == body.strip()


def test_clean_bot_comment_body_preserves_configured_findings_after_separator() -> None:
    body = """Initial context.

---
P2: This option is configured incorrectly and can fail at runtime.
"""

    assert (
        _clean_bot_comment_body(body)
        == "Initial context.\n\n---\nP2: This option is configured incorrectly and can fail at runtime."
    )


def test_format_bot_comments_labels_and_numbers_comments() -> None:
    events = [
        {
            "actor": "reviewer[bot]",
            "event_type": "review_comment",
            "timestamp": "2026-07-01T12:00:00Z",
            "data": {
                "path": "src/app.py",
                "line": 42,
                "diff_hunk": "@@ -1 +1 @@\n-old\n+new",
                "body": "This can throw when value is null.",
            },
        },
        {
            "actor": "reviewer[bot]",
            "event_type": "review_comment",
            "timestamp": "2026-07-01T12:01:00Z",
            "data": {
                "in_reply_to_id": 123,
                "path": "src/app.py",
                "line": 42,
                "body": "Fixed now.",
            },
        },
        {
            "actor": "reviewer[bot]",
            "event_type": "review",
            "timestamp": "2026-07-01T12:02:00Z",
            "data": {"state": "commented", "body": "Summary finding."},
        },
    ]

    formatted = _format_bot_comments(events, "reviewer[bot]").review

    assert "COMMENT C1 [INLINE_REVIEW_COMMENT path=src/app.py:42 timestamp=2026-07-01T12:00:00Z]" in formatted
    assert "Code context:\n```diff\n@@ -1 +1 @@\n-old\n+new\n```" in formatted
    assert "COMMENT C2 [REVIEW_BODY state=commented timestamp=2026-07-01T12:02:00Z]" in formatted
    assert "Fixed now." not in formatted


def _macroscope_event(kind: str | None, body_text: str, ts: str, **extra: object) -> dict:
    """Build an issue_comment event, optionally stamped with a macroscope-meta marker.

    Mirrors how Macroscope emits comments: the provenance marker is a hidden HTML
    comment carrying a JSON payload, prepended to the visible body. `kind=None`
    produces an untagged comment; `extra` adds sibling payload fields (variant,
    config, check, ...) that the parser must ignore.
    """
    marker = "" if kind is None else f"<!-- macroscope-meta: {json.dumps({'kind': kind, **extra})} -->\n"
    return {
        "actor": "macroscopeapp[bot]",
        "event_type": "issue_comment",
        "timestamp": ts,
        "data": {"body": f"{marker}{body_text}"},
    }


def test_format_bot_comments_segments_non_code_review_kinds_out_of_review_text() -> None:
    """Requirement: a comment marked with a non-code_review kind must not feed the
    precision denominator.

    EXTRACT_BOT_SUGGESTIONS is built from the returned `review` text, so a check_run
    (or any non-review surface) comment must be absent from it and instead recorded
    in `custom_check`. Otherwise convention/style/policy check-run comments — rarely
    "fixed" by developers — would drag Macroscope's precision unfairly.
    """
    events = [
        _macroscope_event("code_review", "Possible null dereference here.", "2026-07-01T12:00:00Z"),
        _macroscope_event("check_run", "Naming convention: use snake_case.", "2026-07-01T12:01:00Z"),
    ]

    segments = _format_bot_comments(events, "macroscopeapp[bot]")

    assert "Possible null dereference here." in segments.review
    assert "Naming convention: use snake_case." not in segments.review
    assert [c["kind"] for c in segments.custom_check] == ["check_run"]


def test_format_bot_comments_keys_on_not_code_review_so_future_kinds_are_excluded() -> None:
    """Requirement: exclusion keys on `kind != code_review`, not an allowlist of
    known non-review kinds.

    A brand-new surface (here `pr_assistant`) that Martian has never heard of must
    be excluded automatically, without another benchmark change.
    """
    events = [
        _macroscope_event("pr_assistant", "Want me to summarize this PR?", "2026-07-01T12:00:00Z"),
        _macroscope_event("approvability", "This PR looks safe to merge.", "2026-07-01T12:01:00Z"),
    ]

    segments = _format_bot_comments(events, "macroscopeapp[bot]")

    assert segments.review == "(no bot comments)"
    assert sorted(c["kind"] for c in segments.custom_check) == ["approvability", "pr_assistant"]


def test_format_bot_comments_keeps_code_review_and_untagged_comments() -> None:
    """Requirement: real review (kind=code_review) and legacy untagged comments are
    unaffected — they still feed the precision denominator.

    Untagged comments (comments predating the marker, or from bots that never emit
    it) must keep the pre-change behavior of being scored.
    """
    events = [
        _macroscope_event("code_review", "Tagged review finding.", "2026-07-01T12:00:00Z"),
        _macroscope_event(None, "Untagged review finding.", "2026-07-01T12:01:00Z"),
    ]

    segments = _format_bot_comments(events, "macroscopeapp[bot]")

    assert "Tagged review finding." in segments.review
    assert "Untagged review finding." in segments.review
    assert segments.custom_check == []
    assert "COMMENT C1 " in segments.review
    assert "COMMENT C2 " in segments.review


def test_format_bot_comments_detects_marker_on_raw_body_before_html_cleaning() -> None:
    """Requirement (the raw-vs-cleaned gotcha): the marker must be detected on the
    RAW body, before `_clean_bot_comment_body` strips HTML comments.

    The marker lives inside an HTML comment, which cleaning removes. If detection
    ran on the cleaned body the kind would be invisible and the comment would leak
    back into the precision denominator. This asserts the marked comment is excluded
    even though the marker is exactly the kind of HTML comment cleaning strips.
    """
    stripped_marker = '<!-- macroscope-meta: {"kind":"check_run"} -->'
    assert _clean_bot_comment_body(stripped_marker) == ""  # cleaning erases the marker entirely

    events = [_macroscope_event("check_run", "Style nit.", "2026-07-01T12:00:00Z")]

    segments = _format_bot_comments(events, "macroscopeapp[bot]")

    assert segments.review == "(no bot comments)"
    assert [c["kind"] for c in segments.custom_check] == ["check_run"]


def test_format_bot_comments_ignores_extra_payload_fields() -> None:
    """Requirement: the JSON payload may carry sibling fields (variant, config,
    check, ...); the parser reads only `kind` and ignores the rest.

    Macroscope stamps additional metadata per surface — e.g. check_run comments
    carry `config` and `check`. Those must not affect the code_review-only rule.
    """
    events = [
        _macroscope_event("code_review", "Real finding.", "2026-07-01T12:00:00Z", variant="inline"),
        _macroscope_event("check_run", "Style nit.", "2026-07-01T12:01:00Z", config="lint", check="naming"),
    ]

    segments = _format_bot_comments(events, "macroscopeapp[bot]")

    assert "Real finding." in segments.review
    assert "Style nit." not in segments.review
    assert [c["kind"] for c in segments.custom_check] == ["check_run"]


def test_format_bot_comments_treats_malformed_payload_as_untagged() -> None:
    """Requirement: a marker whose payload is not valid JSON (or lacks a string
    `kind`) is treated as untagged — the comment stays scored, and parsing never
    throws.

    Robustness: a truncated or malformed marker must not silently exclude a real
    review comment, nor crash the pipeline. Untagged is the safe default.
    """
    events = [
        {  # malformed JSON payload
            "actor": "macroscopeapp[bot]",
            "event_type": "issue_comment",
            "timestamp": "2026-07-01T12:00:00Z",
            "data": {"body": '<!-- macroscope-meta: {"kind": check_run,,,} -->\nReview finding A.'},
        },
        {  # valid JSON, but no "kind" field
            "actor": "macroscopeapp[bot]",
            "event_type": "issue_comment",
            "timestamp": "2026-07-01T12:01:00Z",
            "data": {"body": '<!-- macroscope-meta: {"variant":"inline"} -->\nReview finding B.'},
        },
    ]

    segments = _format_bot_comments(events, "macroscopeapp[bot]")

    assert "Review finding A." in segments.review
    assert "Review finding B." in segments.review
    assert segments.custom_check == []

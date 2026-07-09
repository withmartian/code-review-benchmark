"""Tests for bot comment formatting used by LLM extraction."""

from __future__ import annotations

from pipeline.analyze import _clean_bot_comment_body
from pipeline.analyze import _format_bot_comments


def test_clean_bot_comment_body_removes_hidden_metadata() -> None:
    body = """<!-- hidden classifier notes -->
Actual review finding.

<details>
<summary>Debug trace</summary>
Internal prompt text
</details>

<sub>generated metadata</sub>
"""

    assert _clean_bot_comment_body(body) == "Actual review finding."


def test_clean_bot_comment_body_removes_promotional_footer() -> None:
    body = """Potential null dereference here.

---
If you found this review helpful, react to this comment.
"""

    assert _clean_bot_comment_body(body) == "Potential null dereference here."


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

    formatted = _format_bot_comments(events, "reviewer[bot]")

    assert "COMMENT C1 [INLINE_REVIEW_COMMENT path=src/app.py:42 timestamp=2026-07-01T12:00:00Z]" in formatted
    assert "Code context:\n```diff\n@@ -1 +1 @@\n-old\n+new\n```" in formatted
    assert "COMMENT C2 [REVIEW_BODY state=commented timestamp=2026-07-01T12:02:00Z]" in formatted
    assert "Fixed now." not in formatted

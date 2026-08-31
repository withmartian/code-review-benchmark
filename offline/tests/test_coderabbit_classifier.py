"""Tests for CodeRabbit comment-level classification."""

from __future__ import annotations

from code_review_benchmark.parsers.coderabbit import classify_comment


def test_inline_comment():
    comment = {"body": "Some issue here", "path": "src/main.py", "line": 42}
    assert classify_comment(comment) == "inline"


def test_inline_requires_both_path_and_line():
    comment = {"body": "Some issue", "path": "src/main.py", "line": None}
    assert classify_comment(comment) != "inline"

    comment2 = {"body": "Some issue", "path": None, "line": 42}
    assert classify_comment(comment2) != "inline"


def test_walkthrough_comment():
    comment = {
        "body": (
            "<!-- walkthrough_start -->\n\n<details>\n"
            "<summary>\U0001f4dd Walkthrough</summary>\n\n## Walkthrough\nSome changes..."
        ),
        "path": None,
        "line": None,
    }
    assert classify_comment(comment) == "walkthrough"


def test_status_comment():
    comment = {
        "body": "**Actionable comments posted: 4**\n\n<details>\n<summary>\U0001f916 Fix all issues",
        "path": None,
        "line": None,
    }
    assert classify_comment(comment) == "status"


def test_outside_diff_standalone():
    comment = {
        "body": (
            "> [!CAUTION]\n> Some comments are outside the diff\n\n"
            "<details>\n<summary>\u26a0\ufe0f Outside diff range comments (2)</summary>"
        ),
        "path": None,
        "line": None,
    }
    assert classify_comment(comment) == "outside_diff"


def test_pre_merge_checks():
    comment = {
        "body": "<!-- pre_merge_checks_walkthrough_start -->\n\n<details>\n<summary>\U0001f6a5 Pre-merge checks",
        "path": None,
        "line": None,
    }
    assert classify_comment(comment) == "pre_merge_checks"


def test_finishing_touches():
    comment = {
        "body": "<!-- finishing_touch_checkbox_start -->\n\n<details>\n<summary>\u2728 Finishing touches</summary>",
        "path": None,
        "line": None,
    }
    assert classify_comment(comment) == "finishing_touches"


def test_unknown_comment():
    comment = {
        "body": "This is just a regular comment with no markers.",
        "path": None,
        "line": None,
    }
    assert classify_comment(comment) == "unknown"


def test_composite_comment_walkthrough_wins():
    """When a comment contains walkthrough + pre_merge + finishing, walkthrough wins (checked first)."""
    comment = {
        "body": "<!-- walkthrough_start -->\nstuff\n<!-- pre_merge_checks_walkthrough_start -->\nmore stuff",
        "path": None,
        "line": None,
    }
    assert classify_comment(comment) == "walkthrough"


def test_inline_takes_priority_over_markers():
    """If path+line are set, it's inline even if body has walkthrough markers."""
    comment = {
        "body": "<!-- walkthrough_start -->\nSome text",
        "path": "src/main.py",
        "line": 10,
    }
    assert classify_comment(comment) == "inline"

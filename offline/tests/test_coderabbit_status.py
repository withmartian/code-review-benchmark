"""Tests for status comment section parsing."""

from __future__ import annotations

from code_review_benchmark.parsers.coderabbit import parse_status_comment


def test_actionable_summary_extracted():
    body = (
        "**Actionable comments posted: 2**\n\n"
        "<details>\n<summary>\U0001f916 Fix all issues with AI agents</summary>\n\n"
        "```\nIn `@src/main.py`:\n- Around line 42: Missing null check\n```\n\n"
        "</details>\n\n"
        "<details>\n<summary>\U0001f4dc Review details</summary>\nConfig stuff\n</details>"
    )
    sections = parse_status_comment(body)
    assert "actionable_summary" in sections
    assert len(sections["actionable_summary"]) == 1
    assert "Missing null check" in sections["actionable_summary"][0]["body"]


def test_nitpick_section_extracted():
    body = (
        "**Actionable comments posted: 0**\n\n"
        "<details>\n<summary>\U0001f9f9 Nitpick comments (2)</summary><blockquote>\n\n"
        "<details>\n<summary>src/util.py (1)</summary><blockquote>\n\n"
        "`10-15`: **Use List.of() instead of Arrays.asList()**\n\n"
        "Description of the nitpick.\n\n"
        "</blockquote></details>\n\n"
        "<details>\n<summary>src/main.py (1)</summary><blockquote>\n\n"
        "`30-35`: **Rename variable for clarity**\n\n"
        "Another nitpick.\n\n"
        "</blockquote></details>\n\n"
        "</blockquote></details>"
    )
    sections = parse_status_comment(body)
    assert "nitpick" in sections
    assert len(sections["nitpick"]) == 2
    assert sections["nitpick"][0]["path"] == "src/util.py"
    assert sections["nitpick"][1]["path"] == "src/main.py"


def test_outside_diff_section_extracted():
    body = (
        "**Actionable comments posted: 1**\n\n"
        "> [!CAUTION]\n"
        "> Some comments are outside the diff\n\n"
        "<details>\n<summary>\u26a0\ufe0f Outside diff range comments (1)</summary><blockquote>\n\n"
        "<details>\n<summary>src/init.py (1)</summary><blockquote>\n\n"
        "`59-70`: **Race condition in initialization.**\n\n"
        "Details here.\n\n"
        "</blockquote></details>\n\n"
        "</blockquote></details>\n\n"
        "<details>\n<summary>\U0001f4dc Review details</summary>\nStuff\n</details>"
    )
    sections = parse_status_comment(body)
    assert "outside_diff" in sections
    assert len(sections["outside_diff"]) == 1
    assert sections["outside_diff"][0]["path"] == "src/init.py"
    assert "Race condition" in sections["outside_diff"][0]["body"]


def test_review_details_extracted():
    body = (
        "**Actionable comments posted: 0**\n\n"
        "<details>\n<summary>\U0001f4dc Review details</summary>\n\n"
        "**Configuration used**: CHILL\n\n"
        "</details>"
    )
    sections = parse_status_comment(body)
    assert "review_details" in sections


def test_additional_comments_extracted():
    body = (
        "**Actionable comments posted: 0**\n\n"
        "<details>\n<summary>\U0001f509 Additional comments (3)</summary><blockquote>\n\n"
        "LGTM! Good stuff.\n\n"
        "</blockquote></details>"
    )
    sections = parse_status_comment(body)
    assert "additional_comments" in sections


def test_additional_context_extracted():
    body = (
        "**Actionable comments posted: 0**\n\n"
        "<details>\n<summary>\U0001f9f0 Additional context used</summary>\n\n"
        "Code graph analysis...\n\n"
        "</details>"
    )
    sections = parse_status_comment(body)
    assert "additional_context" in sections


def test_multiple_sections_in_one_status():
    body = (
        "**Actionable comments posted: 2**\n\n"
        "<details>\n<summary>\U0001f916 Fix all issues with AI agents</summary>\n\nFix A\n\n</details>\n\n"
        "<details>\n<summary>\U0001f9f9 Nitpick comments (1)</summary><blockquote>\n\n"
        "<details>\n<summary>a.py (1)</summary><blockquote>\n\n"
        "`1-5`: **Nit**\n\nDesc.\n\n"
        "</blockquote></details>\n\n"
        "</blockquote></details>\n\n"
        "<details>\n<summary>\U0001f4dc Review details</summary>\nConfig\n</details>\n\n"
        "<details>\n<summary>\U0001f509 Additional comments (1)</summary><blockquote>\nLGTM\n</blockquote></details>"
    )
    sections = parse_status_comment(body)
    assert "actionable_summary" in sections
    assert "nitpick" in sections
    assert "review_details" in sections
    assert "additional_comments" in sections


def test_outside_diff_strips_blockquote_prefixes():
    """Outside-diff content uses markdown blockquote > prefixes that must be stripped."""
    body = (
        "**Actionable comments posted: 1**\n\n"
        "> [!CAUTION]\n"
        "> Some comments are outside the diff\n>\n"
        "<details>\n<summary>\u26a0\ufe0f Outside diff range comments (1)</summary><blockquote>\n\n"
        "<details>\n<summary>src/init.py (1)</summary><blockquote>\n\n"
        "> \n"
        "> `59-70`: **Hidden attributes issue.**\n"
        "> \n"
        "> The filter is only applied inside the search branch.\n"
        "> \n"
        "</blockquote></details>\n\n"
        "</blockquote></details>"
    )
    sections = parse_status_comment(body)
    assert "outside_diff" in sections
    assert len(sections["outside_diff"]) == 1
    item = sections["outside_diff"][0]
    assert item["path"] == "src/init.py"
    assert item["line"] == 59
    # Body should not have > prefixes
    assert not item["body"].startswith(">")
    assert "Hidden attributes issue." in item["body"]


def test_nitpick_line_range_extracted():
    body = (
        "**Actionable comments posted: 0**\n\n"
        "<details>\n<summary>\U0001f9f9 Nitpick comments (1)</summary><blockquote>\n\n"
        "<details>\n<summary>src/util.py (1)</summary><blockquote>\n\n"
        "`304-326`: **Add CLIENTS branch**\n\nSome description.\n\n"
        "</blockquote></details>\n\n"
        "</blockquote></details>"
    )
    sections = parse_status_comment(body)
    assert sections["nitpick"][0]["line"] == 304
    assert "Add CLIENTS branch" in sections["nitpick"][0]["body"]

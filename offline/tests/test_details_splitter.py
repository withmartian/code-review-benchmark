"""Tests for DetailsBlockSplitter."""

from __future__ import annotations

from code_review_benchmark.parsers.coderabbit import split_details_blocks


def test_single_details_block():
    html = "<details><summary>Title</summary>\nContent\n</details>"
    blocks = split_details_blocks(html)
    assert len(blocks) == 1
    assert "Title" in blocks[0]
    assert "Content" in blocks[0]


def test_multiple_top_level_blocks():
    html = "<details><summary>First</summary>\nA\n</details>\n" "<details><summary>Second</summary>\nB\n</details>"
    blocks = split_details_blocks(html)
    assert len(blocks) == 2
    assert "First" in blocks[0]
    assert "Second" in blocks[1]


def test_nested_details_counted_as_one():
    html = (
        "<details><summary>Outer</summary>\n"
        "<details><summary>Inner</summary>\nNested\n</details>\n"
        "</details>"
    )
    blocks = split_details_blocks(html)
    assert len(blocks) == 1
    assert "Outer" in blocks[0]
    assert "Inner" in blocks[0]


def test_deeply_nested():
    html = (
        "<details><summary>L1</summary>\n"
        "<details><summary>L2</summary>\n"
        "<details><summary>L3</summary>\nDeep\n</details>\n"
        "</details>\n"
        "</details>"
    )
    blocks = split_details_blocks(html)
    assert len(blocks) == 1
    assert "Deep" in blocks[0]


def test_text_outside_details_ignored():
    html = "Some preamble\n<details><summary>A</summary>\nX\n</details>\nTrailing text"
    blocks = split_details_blocks(html)
    assert len(blocks) == 1
    assert "preamble" not in blocks[0]
    assert "Trailing" not in blocks[0]


def test_empty_input():
    assert split_details_blocks("") == []


def test_no_details_tags():
    assert split_details_blocks("Just plain text") == []


def test_extract_summary():
    html = "<details><summary>\U0001f9f9 Nitpick comments (4)</summary>\nStuff\n</details>"
    blocks = split_details_blocks(html)
    assert len(blocks) == 1
    assert "\U0001f9f9 Nitpick comments (4)" in blocks[0]

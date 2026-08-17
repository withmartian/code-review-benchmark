"""Tests for analysis.golden_version module."""

from __future__ import annotations

import json

from analysis.golden_version import golden_set_version

ENTRY = {
    "url": "https://github.com/org/repo/pull/1",
    "pr_title": "Fix the thing",
    "comments": [
        {"comment": "Off by one", "category": "bug", "severity": "High"},
        {"comment": "Races on close", "category": "concurrency", "severity": "Medium"},
    ],
}


def _write(golden_dir, name, entries, **dump_kwargs):
    path = golden_dir / name
    path.write_text(json.dumps(entries, **dump_kwargs))
    return path


def test_version_is_stable_across_calls(tmp_path):
    _write(tmp_path, "a.json", [ENTRY])

    first = golden_set_version(tmp_path)
    second = golden_set_version(tmp_path)

    assert first == second
    assert len(first["sha256"]) == 64
    assert first["short"] == first["sha256"][:12]


def test_reformatting_does_not_change_version(tmp_path):
    _write(tmp_path, "a.json", [ENTRY])
    compact = golden_set_version(tmp_path)

    _write(tmp_path, "a.json", [ENTRY], indent=4, sort_keys=True)
    reformatted = golden_set_version(tmp_path)

    assert compact["sha256"] == reformatted["sha256"]


def test_editing_a_comment_changes_version(tmp_path):
    _write(tmp_path, "a.json", [ENTRY])
    before = golden_set_version(tmp_path)

    edited = json.loads(json.dumps(ENTRY))
    edited["comments"][0]["category"] = "security"
    _write(tmp_path, "a.json", [edited])
    after = golden_set_version(tmp_path)

    assert before["sha256"] != after["sha256"]


def test_adding_a_comment_changes_version_and_counts(tmp_path):
    _write(tmp_path, "a.json", [ENTRY])
    before = golden_set_version(tmp_path)

    extended = json.loads(json.dumps(ENTRY))
    extended["comments"].append({"comment": "Leaks a handle", "category": "bug", "severity": "Low"})
    _write(tmp_path, "a.json", [extended])
    after = golden_set_version(tmp_path)

    assert before["sha256"] != after["sha256"]
    assert before["comments"] == 2
    assert after["comments"] == 3


def test_counts_cover_every_file(tmp_path):
    _write(tmp_path, "a.json", [ENTRY, ENTRY])
    _write(tmp_path, "b.json", [ENTRY])

    version = golden_set_version(tmp_path)

    assert version["files"] == 2
    assert version["prs"] == 3
    assert version["comments"] == 6


def test_returns_none_when_there_is_no_golden_set(tmp_path):
    assert golden_set_version(tmp_path / "missing") is None
    assert golden_set_version(tmp_path) is None

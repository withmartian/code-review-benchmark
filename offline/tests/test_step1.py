"""Tests for step1_download_prs module."""

from __future__ import annotations

import json

import pytest

from code_review_benchmark import step1_download_prs as step1


def test_load_golden_comments(tmp_path):
    content = [
        {
            "url": "https://github.com/example/repo/pull/1",
            "pr_title": "Fix bug",
            "original_url": "https://github.com/upstream/repo/pull/1",
            "comments": [{"comment": "Issue", "severity": "High"}],
            "az_comment": "note",
        }
    ]
    file_path = tmp_path / "example.json"
    file_path.write_text(json.dumps(content))

    golden = step1.load_golden_comments(str(tmp_path))
    assert golden == {
        "https://github.com/example/repo/pull/1": {
            "pr_title": "Fix bug",
            "original_url": "https://github.com/upstream/repo/pull/1",
            "comments": [{"comment": "Issue", "severity": "High"}],
            "az_comment": "note",
            "source_file": "example.json",
        }
    }


@pytest.mark.parametrize(
    "name,expected",
    [
        (
            "cal_dot_com__repo-name__tool-x__PR12__20240101",
            {
                "config_prefix": "cal_dot_com",
                "original_repo": "repo-name",
                "tool": "tool-x",
                "pr_number": 12,
                "date": "20240101",
            },
        ),
        ("invalid_repo_name", None),
    ],
)
def test_parse_repo_name(name, expected):
    assert step1.parse_repo_name(name) == expected


def test_find_golden_url():
    golden = {
        "https://github.com/example/repo/pull/5": {},
        "https://github.com/example/other/pull/7": {},
    }
    result = step1.find_golden_url(golden, "repo", 5)
    assert result == "https://github.com/example/repo/pull/5"


def test_fetch_review_comments(monkeypatch):
    responses = [
        [
            {"path": "file.py", "line": 10, "body": "inline", "created_at": "2024-01-01"}
        ],
        [{"body": "top-level", "submitted_at": "2024-01-01"}],
        [
            {"body": "issue", "created_at": "2024-01-01"},
            {"body": "/propel review", "created_at": "2024-01-01"},
        ],
    ]

    def fake_gh(_args):
        return responses.pop(0)

    monkeypatch.setattr(step1, "gh", fake_gh)

    comments = step1.fetch_review_comments("org", "repo", 1)
    assert comments == [
        {"path": "file.py", "line": 10, "body": "inline", "created_at": "2024-01-01"},
        {"path": None, "line": None, "body": "top-level", "created_at": "2024-01-01"},
        {"path": None, "line": None, "body": "issue", "created_at": "2024-01-01"},
    ]


def test_fetch_repo_data(monkeypatch):
    def stub_pr_metadata(_org, _repo, pr):
        return {"title": f"PR {pr}", "url": "https://github.com/org/repo/pull/1"}

    def stub_review_comments(_org, _repo, _pr):
        return [{"path": "file.py", "line": 5, "body": "content", "created_at": "now"}]

    monkeypatch.setattr(step1, "fetch_pr_metadata", stub_pr_metadata)
    monkeypatch.setattr(step1, "fetch_review_comments", stub_review_comments)

    result = step1.fetch_repo_data("org", "repo",)
    assert result == {
        "repo_name": "repo",
        "pr_meta": {"title": "PR 1", "url": "https://github.com/org/repo/pull/1"},
        "comments": [{"path": "file.py", "line": 5, "body": "content", "created_at": "now"}],
    }


def test_load_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=bar\n# comment\nBAZ='quoted'\n")
    monkeypatch.chdir(tmp_path)

    step1.load_dotenv(str(env_file))

    assert step1.os.environ["FOO"] == "bar"
    assert step1.os.environ["BAZ"] == "quoted"

"""Tests for step0_fork_prs module."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from code_review_benchmark import step0_fork_prs as step0


class DummyCompletedProcess:
    def __init__(self, args: tuple[str, ...]):
        self.args = args
        self.returncode = 0
        self.stdout = ""
        self.stderr = ""


def _make_forker() -> step0.GitHubPRForker:
    forker = step0.GitHubPRForker.__new__(step0.GitHubPRForker)
    forker.token = "token"
    forker.org = "my-org"
    return forker


def test_parse_pr_url_success():
    forker = _make_forker()
    owner, repo, number = step0.GitHubPRForker.parse_pr_url(
        forker, "https://github.com/example/repo/pull/42"
    )
    assert owner == "example"
    assert repo == "repo"
    assert number == 42


def test_parse_pr_url_invalid():
    forker = _make_forker()
    with pytest.raises(ValueError):
        step0.GitHubPRForker.parse_pr_url(forker, "https://github.com/example/repo/issues/42")


def test_generate_repo_name_with_prefix(monkeypatch):
    class DummyDateTime:
        @staticmethod
        def now():
            return SimpleNamespace(strftime=lambda _: "20240201")

    monkeypatch.setattr(step0, "datetime", DummyDateTime)
    forker = _make_forker()

    result = step0.GitHubPRForker.generate_repo_name(
        forker, "repo-name", 7, "My Tool", config_prefix="cal_dot_com"
    )
    assert result == "cal_dot_com__repo-name__my-tool__PR7__20240201"


def test_load_pr_urls_from_file(tmp_path):
    data = [
        {"url": "https://github.com/example/repo/pull/1"},
        {"pr_url": "https://github.com/example/repo/pull/2"},
        {"other": "ignored"},
    ]
    path = tmp_path / "prs.json"
    path.write_text(json.dumps(data))

    urls = step0._load_pr_urls_from_file(path)
    assert urls == [
        "https://github.com/example/repo/pull/1",
        "https://github.com/example/repo/pull/2",
    ]


def test_process_pr_happy_path(monkeypatch, tmp_path):
    class DummyDateTime:
        @staticmethod
        def now():
            return SimpleNamespace(strftime=lambda _: "20240201")

    monkeypatch.setattr(step0, "datetime", DummyDateTime)

    tmp_dir_path = tmp_path / "clone"
    tmp_dir_path.mkdir()

    class DummyTempDir:
        def __init__(self, path: str):
            self._path = path

        def __enter__(self):
            return self._path

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        step0.tempfile,
        "TemporaryDirectory",
        lambda: DummyTempDir(str(tmp_dir_path)),
    )

    commands = []

    def fake_run(cmd, **_kwargs):
        commands.append(tuple(cmd))
        return DummyCompletedProcess(tuple(cmd))

    monkeypatch.setattr(step0.subprocess, "run", fake_run)

    run_git_calls = []

    forker = _make_forker()

    def fake_run_git(tmpdir: str, *args: str):
        run_git_calls.append((tmpdir, args))
        return DummyCompletedProcess(args)

    forker.run_git = fake_run_git  # type: ignore[assignment]

    pr_details = {
        "title": "Add feature",
        "body": "Description",
        "base": {"ref": "main", "sha": "abc1234"},
    }

    def stub_get_pr_details(*_args, **_kwargs):
        return pr_details

    def stub_repo_exists(_name):
        return False

    created_repos = []

    def stub_create_repo(name):
        created_repos.append(name)

    disabled_actions = []

    def stub_disable_actions(name):
        disabled_actions.append(name)

    forker.get_pr_details = stub_get_pr_details  # type: ignore[assignment]
    forker.repo_exists = stub_repo_exists  # type: ignore[assignment]
    forker.create_repo = stub_create_repo  # type: ignore[assignment]
    forker.disable_actions = stub_disable_actions  # type: ignore[assignment]

    monkeypatch.setattr(step0.time, "sleep", lambda _: None)

    expected_pr_url = "https://github.com/my-org/repo/pr/1"

    def fake_create_pr(**kwargs):
        assert kwargs == {
            "repo": "cal_dot_com__repo__my-tool__PR123__20240201",
            "title": "Add feature",
            "body": "Description",
            "head": "pr-123",
            "base": "main",
        }
        return {"html_url": expected_pr_url}

    forker.create_pull_request = fake_create_pr  # type: ignore[assignment]
    forker.make_repo_public = lambda _name: None  # type: ignore[assignment]

    result = step0.GitHubPRForker.process_pr(
        forker,
        "https://github.com/owner/repo/pull/123",
        "My Tool",
        config_prefix="cal_dot_com",
    )

    assert result == {"new_pr_url": expected_pr_url}
    assert created_repos == ["cal_dot_com__repo__my-tool__PR123__20240201"]
    assert disabled_actions == ["cal_dot_com__repo__my-tool__PR123__20240201"]
    assert any("clone" in " ".join(cmd) for cmd in commands)
    assert run_git_calls

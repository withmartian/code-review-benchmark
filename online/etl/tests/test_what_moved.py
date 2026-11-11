"""prompt_eval/what_moved.py: per-bot dropouts, entries and top contributors between two arms."""

from __future__ import annotations

import pytest

from prompt_eval.what_moved import RESULTS
from prompt_eval.what_moved import bot_moves
from prompt_eval.what_moved import load
from prompt_eval.what_moved import render


def _row(url: str, p: float | None, r: float | None, matched: int = 0, total: int = 1) -> dict:
    return {"pr_url": url, "precision": p, "recall": r, "matched_suggestions": matched, "total_suggestions": total}


def _arms() -> dict:
    u = [f"https://github.com/o/r/pull/{i}" for i in range(1, 5)]
    base = {
        u[0]: _row(u[0], 0.0, 0.0),
        u[1]: _row(u[1], 0.5, 1.0),
        u[2]: _row(u[2], 1.0, 0.5),
        u[3]: _row(u[3], None, 0.0),
    }
    arm = {
        u[0]: _row(u[0], None, 0.0),
        u[1]: _row(u[1], 1.0, 1.0),
        u[2]: _row(u[2], 1.0, 0.5),
        u[3]: _row(u[3], 0.0, 0.0),
    }
    return {"base": base, "arm": arm}


def test_dropouts_entries_and_contributions() -> None:
    m = bot_moves(_arms(), "base", "arm")

    assert m["n"] == (3, 3)
    assert m["precision"] == (pytest.approx(0.5), pytest.approx(2 / 3))
    assert [d["pr_url"][-1] for d in m["dropouts"]] == ["1"]
    assert m["dropouts"][0]["shift"]["precision"] == pytest.approx(0.75 - 0.5)  # the base mean without it
    assert [e["pr_url"][-1] for e in m["entries"]] == ["4"]
    assert m["entries"][0]["shift"]["precision"] == pytest.approx(2 / 3 - 1.0)  # the arm mean with it
    # only PRs scored in both arms whose own value changed are ranked
    assert [(t["pr_url"][-1], round(t["contribution"], 4)) for t in m["top_precision"]] == [
        ("2", round(1 / 3 - 0.5 / 3, 4))
    ]
    assert m["top_recall"] == []


def test_render_names_each_pr_and_its_draws() -> None:
    text = render("b[bot]", _arms(), "base", "arm")

    assert text.startswith("**b[bot]**: precision 50.0 -> 66.7 (+16.7)")
    assert "o/r#1 drops out: precision 0.0 -> undefined" in text
    assert "o/r#4 enters: precision undefined -> 0.0" in text
    assert "o/r#2: precision 50.0 -> 100.0, contributes +16.7" in text
    assert "o/r#1 matched/extracted by arm: base: 0/1; arm: 0/1" in text


def test_the_figures_quoted_in_the_pr_description_come_from_the_results_file() -> None:
    data = load(RESULTS)
    head = "this PR at head, one draw"

    qodo = bot_moves(data["qodo-code-review[bot]"], "current", head)
    top_p = {t["pr_url"].split("github.com/")[1]: round(100 * t["contribution"], 1) for t in qodo["top_precision"]}
    top_r = {t["pr_url"].split("github.com/")[1]: round(100 * t["contribution"], 1) for t in qodo["top_recall"]}
    assert top_p == {"anolilab/neore/pull/451": 4.2, "TW199501/multica/pull/50": 1.7, "varveio/swath/pull/243": -1.4}
    assert top_r == {"varveio/swath/pull/243": -12.5, "anolilab/neore/pull/451": -1.4}

    codex = bot_moves(data["chatgpt-codex-connector[bot]"], "current", head)
    assert [d["pr_url"].split("github.com/")[1] for d in codex["dropouts"]] == ["harshanandak/forge/pull/98"]
    assert round(100 * codex["dropouts"][0]["shift"]["precision"], 1) == 5.1
    assert round(100 * codex["dropouts"][0]["shift"]["recall"], 1) == 2.4

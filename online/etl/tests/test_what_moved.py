"""prompt_eval/what_moved.py: an exact additive split of each bot's precision and recall change."""

from __future__ import annotations

import itertools

import pytest

from prompt_eval.what_moved import RESULTS
from prompt_eval.what_moved import bot_moves
from prompt_eval.what_moved import load
from prompt_eval.what_moved import render


def _row(url: str, p: float | None, r: float | None, matched: int = 0, total: int = 1) -> dict:
    return {"pr_url": url, "precision": p, "recall": r, "matched_suggestions": matched, "total_suggestions": total}


def _arms(base: list, arm: list) -> dict:
    urls = [f"https://github.com/o/r/pull/{i}" for i in range(1, len(base) + 1)]
    return {
        "base": {u: _row(u, p, r) for u, (p, r) in zip(urls, base, strict=True)},
        "arm": {u: _row(u, p, r) for u, (p, r) in zip(urls, arm, strict=True)},
    }


def test_a_leaving_pr_and_a_worsening_pr_add_up_to_the_total() -> None:
    # baseline [0, 0.9, 0.6] (mean 0.5), candidate [undefined, 0.8, 0.6] (mean 0.7): +0.2 in all
    m = bot_moves(_arms([(0.0, 0.0), (0.9, 1.0), (0.6, 0.5)], [(None, 0.0), (0.8, 1.0), (0.6, 0.5)]), "base", "arm")
    p = m["precision"]

    assert m["n"] == (3, 2, 2)
    assert p["total"] == pytest.approx(0.2)
    assert p["dropouts"] == pytest.approx(0.25)  # (mean over the retained PRs 0.75 - 0) / 3
    assert p["retained"] == pytest.approx(-0.05)  # (0.8 - 0.9) / 2
    assert p["dropouts"] + p["entries"] + p["retained"] == pytest.approx(p["total"])
    assert [(t["pr_url"][-1], round(t["contribution"], 4)) for t in m["top_precision"]] == [("2", -0.05)]


def test_an_entering_pr_is_counted_on_the_arm_side() -> None:
    m = bot_moves(_arms([(None, 0.0), (0.5, 1.0), (1.0, 0.5)], [(0.0, 0.0), (0.5, 1.0), (1.0, 0.5)]), "base", "arm")
    p = m["precision"]

    assert [e["pr_url"][-1] for e in m["entries"]] == ["1"]
    assert m["entries"][0]["effect"]["precision"] == pytest.approx((0.0 - 0.75) / 3)
    assert p["dropouts"] + p["entries"] + p["retained"] == pytest.approx(p["total"])


def test_render_names_the_split_and_each_pr() -> None:
    text = render(
        "b[bot]", _arms([(0.0, 0.0), (0.9, 1.0), (0.6, 0.5)], [(None, 0.0), (0.8, 1.0), (0.6, 0.5)]), "base", "arm"
    )

    assert text.startswith("**b[bot]**: PRs with both 3 -> 2 (2 in both arms)")
    assert "- precision 50.0 -> 70.0 (+20.0) = PRs leaving +25.0 + PRs entering +0.0 + PRs in both -5.0" in text
    assert "o/r#1 leaves: precision 0.0 -> undefined" in text
    assert "o/r#2: precision 90.0 -> 80.0, contributes -5.0" in text


def test_the_parts_sum_to_the_total_for_every_bot_and_arm_pair_in_the_results_file() -> None:
    data = load(RESULTS)
    for bot, by_arm in data.items():
        for base, arm in itertools.permutations(sorted(by_arm), 2):
            m = bot_moves(by_arm, base, arm, top=10_000)
            for key in ("precision", "recall"):
                k = m[key]
                assert k["dropouts"] + k["entries"] + k["retained"] == pytest.approx(k["total"], abs=1e-12), (
                    bot,
                    base,
                    arm,
                )
                assert sum(t["contribution"] for t in m[f"top_{key}"]) == pytest.approx(k["retained"], abs=1e-12)


def test_the_figures_quoted_in_the_pr_description_come_from_the_results_file() -> None:
    data = load(RESULTS)
    head = "this PR at head, one draw"

    def short(url: str) -> str:
        return url.split("github.com/")[1]

    qodo = bot_moves(data["qodo-code-review[bot]"], "current", head)
    assert round(100 * qodo["precision"]["total"], 1) == 10.0
    assert round(100 * qodo["precision"]["dropouts"], 1) == 9.7
    assert round(100 * qodo["precision"]["retained"], 1) == 0.3
    assert round(100 * qodo["recall"]["dropouts"], 1) == 6.9
    assert round(100 * qodo["recall"]["retained"], 1) == -14.3
    assert [short(d["pr_url"]) for d in qodo["dropouts"]] == ["Heretek-AI/claude-4-opencode/pull/4"]
    top_p = {short(t["pr_url"]): round(100 * t["contribution"], 1) for t in qodo["top_precision"]}
    top_r = {short(t["pr_url"]): round(100 * t["contribution"], 1) for t in qodo["top_recall"]}
    assert top_p == {"TW199501/multica/pull/50": 1.9, "varveio/swath/pull/243": -1.6}
    assert top_r == {"varveio/swath/pull/243": -14.3}

    codex = bot_moves(data["chatgpt-codex-connector[bot]"], "current", head)
    assert [short(d["pr_url"]) for d in codex["dropouts"]] == ["harshanandak/forge/pull/98"]
    assert round(100 * codex["precision"]["dropouts"], 1) == 5.1
    assert round(100 * codex["recall"]["dropouts"], 1) == 2.4
    assert [short(e["pr_url"]) for e in codex["entries"]] == ["LATTIX-IO/cryptopolicy/pull/7"]
    assert round(100 * codex["precision"]["entries"], 1) == -5.1
    assert round(100 * codex["precision"]["total"], 1) == 0.5

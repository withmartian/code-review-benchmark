"""Explain each bot's leaderboard precision and recall change between two arms of the results file.

Reads extraction_scope_results.jsonl (no LLM call, nothing written) and, per bot, compares two arms
on the leaderboard means (precision and recall averaged over the PRs that have both, as the API's
compute.rs does):

- the change of each mean split exactly into PRs leaving, PRs entering and PRs in both arms (see
  bot_moves), with each leaving or entering PR's part;
- the PRs whose own value changes and that move each mean most, among those scored in both arms,
  with their contribution (the change in their value over the number of PRs in both arms) and
  values;
- for every PR listed, its matched suggestions in each arm of the file, so a flip can be read
  against the other draws.

    uv run python -m prompt_eval.what_moved --base current --arm "this PR at head, one draw"
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path

RESULTS = Path(__file__).parent / "extraction_scope_results.jsonl"


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _both(rows: dict[str, dict]) -> dict[str, dict]:
    return {u: r for u, r in rows.items() if r["precision"] is not None and r["recall"] is not None}


def bot_moves(by_arm: dict[str, dict[str, dict]], base: str, arm: str, top: int = 3) -> dict:
    """The moves of one bot from `base` to `arm`, as an exact additive decomposition.

    With A and B the PRs that have both values in each arm and R = A & B the retained ones, each mean's
    change is split as (mean over R in the base - base mean), the cohort effect of the PRs that leave,
    computed on base values; plus the mean change over R; plus (arm mean - mean over R in the arm),
    the cohort effect of the PRs that enter. A leaving PR i contributes (base mean over R - a_i) / |A|,
    an entering PR (b_i - arm mean over R) / |B|, a retained PR (b_i - a_i) / |R|; the parts sum to the
    total. by_arm: {arm: {pr_url: row}}."""
    a, b = _both(by_arm[base]), _both(by_arm[arm])
    kept = sorted(set(a) & set(b))
    out: dict = {"n": (len(a), len(b), len(kept)), "dropouts": [], "entries": []}
    for url in sorted(set(a) - set(b)):
        out["dropouts"].append({"pr_url": url, "base": a[url], "arm": by_arm[arm].get(url), "effect": {}})
    for url in sorted(set(b) - set(a)):
        out["entries"].append({"pr_url": url, "base": by_arm[base].get(url), "arm": b[url], "effect": {}})
    for key in ("precision", "recall"):
        ma, mb = _mean([r[key] for r in a.values()]), _mean([r[key] for r in b.values()])
        # The split needs both means and at least one PR in both arms: without one there is no
        # reference mean for the leaving and entering cohorts, so the parts are unavailable (None),
        # never computed against an invented zero. The total is unavailable when either mean is.
        split = ma is not None and mb is not None and bool(kept)
        ka = _mean([a[u][key] for u in kept])
        kb = _mean([b[u][key] for u in kept])
        for d in out["dropouts"]:
            d["effect"][key] = (ka - d["base"][key]) / len(a) if split else None
        for e in out["entries"]:
            e["effect"][key] = (e["arm"][key] - kb) / len(b) if split else None
        retained = [((b[u][key] - a[u][key]) / len(kept), u) for u in kept if b[u][key] != a[u][key]]
        retained.sort(key=lambda c: (-abs(c[0]), c[1]))
        out[key] = {
            "base": ma,
            "arm": mb,
            "total": mb - ma if ma is not None and mb is not None else None,
            "dropouts": sum(d["effect"][key] for d in out["dropouts"]) if split else None,
            "entries": sum(e["effect"][key] for e in out["entries"]) if split else None,
            "retained": sum(c for c, _ in retained) if split else None,
        }
        out[f"top_{key}"] = [
            {"pr_url": u, "contribution": c, "base": a[u][key], "arm": b[u][key]} for c, u in retained[:top]
        ]
    return out


def _pr(url: str) -> str:
    parts = url.removeprefix("https://github.com/").split("/")
    return f"{parts[0]}/{parts[1]}#{parts[3]}" if len(parts) == 4 else url


def _v(x: float | None) -> str:
    return "undefined" if x is None else f"{100 * x:.1f}"


def _d(x: float | None) -> str:
    return "unavailable" if x is None else f"{100 * x:+.1f}"


def _matched(by_arm: dict[str, dict[str, dict]], url: str) -> str:
    cells = []
    for arm, rows in by_arm.items():
        r = rows.get(url)
        cells.append(
            f"{arm}: {'-' if r is None else r['matched_suggestions']}/{'-' if r is None else r['total_suggestions']}"
        )
    return "; ".join(cells)


def render(bot: str, by_arm: dict[str, dict[str, dict]], base: str, arm: str, top: int = 3) -> str:
    m = bot_moves(by_arm, base, arm, top)
    lines = [f"**{bot}**: PRs with both {m['n'][0]} -> {m['n'][1]} ({m['n'][2]} in both arms)"]
    for key in ("precision", "recall"):
        k = m[key]
        head = f"- {key} {_v(k['base'])} -> {_v(k['arm'])}"
        if k["total"] is None:
            lines.append(f"{head} (change unavailable: no PR with both values in one arm)")
        elif k["retained"] is None:
            lines.append(f"{head} ({_d(k['total'])}; not split: no PR has both values in both arms)")
        else:
            lines.append(
                f"{head} ({_d(k['total'])}) = PRs leaving {_d(k['dropouts'])} + PRs entering "
                f"{_d(k['entries'])} + PRs in both {_d(k['retained'])}"
            )
    for label, items in (("leaves", m["dropouts"]), ("enters", m["entries"])):
        for d in items:
            before = d["base"] or {"precision": None, "recall": None}
            after = d["arm"] or {"precision": None, "recall": None}
            lines.append(
                f"- {_pr(d['pr_url'])} {label}: precision {_v(before['precision'])} -> {_v(after['precision'])}, "
                f"recall {_v(before['recall'])} -> {_v(after['recall'])}; effect on precision "
                f"{_d(d['effect']['precision'])}, on recall {_d(d['effect']['recall'])}"
            )
    for key in ("precision", "recall"):
        for t in m[f"top_{key}"]:
            lines.append(
                f"- {_pr(t['pr_url'])}: {key} {_v(t['base'])} -> {_v(t['arm'])}, contributes {100 * t['contribution']:+.1f}"
            )
    listed = {d["pr_url"] for d in m["dropouts"] + m["entries"]} | {
        t["pr_url"] for k in ("precision", "recall") for t in m[f"top_{k}"]
    }
    for url in sorted(listed):
        lines.append(f"  - {_pr(url)} matched/extracted by arm: {_matched(by_arm, url)}")
    return "\n".join(lines)


def load(path: Path) -> dict[str, dict[str, dict[str, dict]]]:
    """{bot: {arm: {pr_url: row}}} from the per-bot table of the results file."""
    by: dict[str, dict[str, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))
    for line in path.open():
        r = json.loads(line)
        if r["table"] == "per-bot":
            by[r["bot"]][r["arm"]][r["pr_url"]] = r
    return by


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", default=str(RESULTS))
    ap.add_argument("--base", default="current")
    ap.add_argument("--arm", default="this PR at head, one draw")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--bot", help="one bot; default: every bot in the file")
    args = ap.parse_args(argv)
    data = load(Path(args.results))
    bots = [args.bot] if args.bot else sorted(data)
    print("\n\n".join(render(b, data[b], args.base, args.arm, args.top) for b in bots))


if __name__ == "__main__":
    main()

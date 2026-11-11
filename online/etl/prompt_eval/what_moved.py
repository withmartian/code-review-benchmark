"""Explain each bot's leaderboard precision and recall change between two arms of the results file.

Reads extraction_scope_results.jsonl (no LLM call, nothing written) and, per bot, compares two arms
on the leaderboard means (precision and recall averaged over the PRs that have both, as the API's
compute.rs does):

- dropouts and entries: PRs whose precision is defined in one arm and undefined in the other, with
  the shift their leaving or entering causes in that arm's means;
- the PRs whose own value changes and that move each mean most, among those scored in both arms,
  with their contribution (value in the second arm / its PR count minus value in the first / its
  PR count) and values;
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
    """The moves of one bot from `base` to `arm`. by_arm: {arm: {pr_url: row}}."""
    a, b = _both(by_arm[base]), _both(by_arm[arm])
    out: dict = {"n": (len(a), len(b))}
    for key in ("precision", "recall"):
        ma, mb = _mean([r[key] for r in a.values()]), _mean([r[key] for r in b.values()])
        out[key] = (ma, mb)
    dropouts, entries = [], []
    for url in sorted(set(by_arm[base]) | set(by_arm[arm])):
        ra, rb = by_arm[base].get(url), by_arm[arm].get(url)
        if ra is None or rb is None:
            continue
        if url in a and rb["precision"] is None:
            rest = [r for u, r in a.items() if u != url]
            shift = {k: _mean([r[k] for r in rest]) - out[k][0] for k in ("precision", "recall")}
            dropouts.append({"pr_url": url, "base": ra, "arm": rb, "shift": shift})
        if url in b and ra["precision"] is None:
            rest = [r for u, r in b.items() if u != url]
            shift = {k: out[k][1] - _mean([r[k] for r in rest]) for k in ("precision", "recall")}
            entries.append({"pr_url": url, "base": ra, "arm": rb, "shift": shift})
    out["dropouts"], out["entries"] = dropouts, entries
    common = sorted(set(a) & set(b))
    for key in ("precision", "recall"):
        # PRs whose own value changed; a changed PR count moves the others too, which the dropout and
        # entry shifts above account for
        contrib = [(b[u][key] / len(b) - a[u][key] / len(a), u) for u in common if b[u][key] != a[u][key]]
        contrib.sort(key=lambda c: (-abs(c[0]), c[1]))
        out[f"top_{key}"] = [
            {"pr_url": u, "contribution": c, "base": a[u][key], "arm": b[u][key]} for c, u in contrib[:top]
        ]
    return out


def _pr(url: str) -> str:
    parts = url.removeprefix("https://github.com/").split("/")
    return f"{parts[0]}/{parts[1]}#{parts[3]}" if len(parts) == 4 else url


def _v(x: float | None) -> str:
    return "undefined" if x is None else f"{100 * x:.1f}"


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
    (pa, pb), (ra, rb) = m["precision"], m["recall"]
    lines = [
        f"**{bot}**: precision {_v(pa)} -> {_v(pb)} ({100 * (pb - pa):+.1f}), recall {_v(ra)} -> {_v(rb)} "
        f"({100 * (rb - ra):+.1f}), PRs with both {m['n'][0]} -> {m['n'][1]}"
    ]
    for label, items in (("drops out", m["dropouts"]), ("enters", m["entries"])):
        for d in items:
            lines.append(
                f"- {_pr(d['pr_url'])} {label}: precision {_v(d['base']['precision'])} -> {_v(d['arm']['precision'])}, "
                f"recall {_v(d['base']['recall'])} -> {_v(d['arm']['recall'])}; shifts the mean precision "
                f"{100 * d['shift']['precision']:+.1f} and recall {100 * d['shift']['recall']:+.1f}"
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

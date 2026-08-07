#!/usr/bin/env python3
"""
Score tools under different profiles and F-beta values.

Reads evaluations.json and golden_comments/*.json, computes per-profile
metrics with matched-excluded handling, and prints a comparison table.

Usage:
    python -m analysis.score_profiles
    python -m analysis.score_profiles --beta 1.0
    python -m analysis.score_profiles --profile strict --beta 0.5
    python -m analysis.score_profiles --all-profiles
"""

import argparse
import json
from pathlib import Path

PROFILE_CATEGORIES: dict[str, frozenset[str]] = {
    "strict": frozenset({"bug", "security", "concurrency", "data", "api"}),
    "core": frozenset({"bug", "security", "concurrency", "data", "api", "perf", "test_gap", "doc_defect"}),
    "all": frozenset({
        "bug", "security", "concurrency", "data", "api",
        "perf", "test_gap", "doc_defect", "style", "speculative",
    }),
}

# Tools excluded from the dashboard (superseded versions, incomplete runs, etc.)
_HIDDEN_TOOLS: frozenset[str] = frozenset({
    "qodo", "greptile", "linearb", "bito", "sentry", "vercel", "kodus",
    "cubic-dev", "cubic-v3", "greptile-v4", "mergemonkey", "qodo-v22",
    "qodo-v2-2", "qodo-extended-summary", "qodo-extended", "entelligence",
    "mesa", "codeant", "propel", "propel-v2", "gemini", "gitar", "deepsource",
})
_HIDDEN_PREFIXES: tuple[str, ...] = ("mra-",)


def _is_hidden(tool: str) -> bool:
    return tool in _HIDDEN_TOOLS or any(tool.startswith(p) for p in _HIDDEN_PREFIXES)


def _load_golden_categories(results_dir: Path) -> dict[str, str]:
    """Build lookup from golden comment text to its category."""
    golden_dir = results_dir.parent / "golden_comments"
    if not golden_dir.exists():
        golden_dir = Path("golden_comments")

    lookup: dict[str, str] = {}
    if golden_dir.exists():
        for json_file in golden_dir.glob("*.json"):
            with open(json_file) as f:
                data = json.load(f)
            for entry in data if isinstance(data, list) else []:
                for gc in entry.get("comments", []):
                    comment = gc.get("comment", "")
                    category = gc.get("category", "")
                    if comment and category:
                        lookup[comment] = category

    if not lookup:
        benchmark_path = results_dir / "benchmark_data.json"
        if benchmark_path.exists():
            with open(benchmark_path) as f:
                bd = json.load(f)
            for _url, entry in bd.items():
                for gc in entry.get("golden_comments", []):
                    comment = gc.get("comment", "")
                    category = gc.get("category", "")
                    if comment and category:
                        lookup[comment] = category

    return lookup


def _get_category(entry: dict, golden_categories: dict[str, str]) -> str:
    return entry.get("category") or golden_categories.get(entry.get("golden_comment", ""), "")


def _fbeta(precision: float, recall: float, beta: float) -> float:
    beta2 = beta * beta
    denom = beta2 * precision + recall
    return (1 + beta2) * precision * recall / denom if denom > 0 else 0.0


def score_tools(
    evaluations: dict,
    golden_categories: dict[str, str],
    profile_name: str,
    beta: float,
) -> dict[str, dict]:
    """Compute per-tool aggregate metrics for a given profile and beta."""
    profile_cats = PROFILE_CATEGORIES[profile_name]
    tool_totals: dict[str, dict[str, int]] = {}

    for _pr_url, pr_evals in evaluations.items():
        for tool, tool_eval in pr_evals.items():
            if _is_hidden(tool) or tool_eval.get("skipped"):
                continue

            if tool not in tool_totals:
                tool_totals[tool] = {"tp": 0, "fp": 0, "fn": 0, "prs": 0}

            tps = tool_eval.get("true_positives", [])
            fns = tool_eval.get("false_negatives", [])
            fp = tool_eval.get("fp", 0)

            tp_in = sum(1 for t in tps if _get_category(t, golden_categories) in profile_cats)
            fn_in = sum(1 for f in fns if _get_category(f, golden_categories) in profile_cats)

            tool_totals[tool]["tp"] += tp_in
            tool_totals[tool]["fp"] += fp
            tool_totals[tool]["fn"] += fn_in
            tool_totals[tool]["prs"] += 1

    results: dict[str, dict] = {}
    for tool, t in tool_totals.items():
        precision = t["tp"] / (t["tp"] + t["fp"]) if (t["tp"] + t["fp"]) > 0 else 0
        recall = t["tp"] / (t["tp"] + t["fn"]) if (t["tp"] + t["fn"]) > 0 else 0
        f1 = _fbeta(precision, recall, 1.0)
        fb = _fbeta(precision, recall, beta)

        results[tool] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "fbeta": fb,
            "tp": t["tp"],
            "fp": t["fp"],
            "fn": t["fn"],
            "prs": t["prs"],
        }

    return results


def print_table(results: dict[str, dict], profile: str, beta: float) -> None:
    """Print a formatted table of tool scores."""
    beta_label = f"F{beta:g}"
    sorted_tools = sorted(results.keys(), key=lambda t: results[t]["fbeta"], reverse=True)

    print(f"\n{'=' * 75}")
    categories = ", ".join(sorted(PROFILE_CATEGORIES[profile]))
    print(f"  Profile: {profile.upper()}  |  {beta_label}  |  Categories: {categories}")
    print(f"{'=' * 75}")
    print(f"  {'Tool':<22s} {'Prec':>6s} {'Recall':>7s} {'F1':>6s} {beta_label:>6s}  {'TP':>4s} {'FP':>4s} {'FN':>4s}")
    print(f"  {'-' * 70}")

    for tool in sorted_tools:
        m = results[tool]
        print(
            f"  {tool:<22s} {m['precision']*100:>5.1f}% {m['recall']*100:>6.1f}% "
            f"{m['f1']*100:>5.1f}% {m['fbeta']*100:>5.1f}%  {m['tp']:>4d} {m['fp']:>4d} {m['fn']:>4d}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Score tools under different profiles and F-beta values")
    parser.add_argument("--results-dir", type=Path, default=Path("results"))
    parser.add_argument("--model", help="Model directory name (default: auto-detect)")
    parser.add_argument("--profile", choices=list(PROFILE_CATEGORIES.keys()), default="core")
    parser.add_argument("--beta", type=float, default=2.0, help="F-beta value (default: 2.0)")
    parser.add_argument("--all-profiles", action="store_true", help="Show all profiles side by side")
    args = parser.parse_args()

    # Find model directory
    if args.model:
        model_dir = args.results_dir / args.model
    else:
        candidates = [
            d for d in args.results_dir.iterdir()
            if d.is_dir() and (d / "evaluations.json").exists()
        ]
        if not candidates:
            print(f"No evaluations found in {args.results_dir}")
            return
        model_dir = max(candidates, key=lambda d: d.stat().st_mtime)

    eval_path = model_dir / "evaluations.json"
    if not eval_path.exists():
        print(f"Error: {eval_path} not found")
        return

    print(f"Model: {model_dir.name}")
    print(f"Evaluations: {eval_path}")

    with open(eval_path) as f:
        evaluations = json.load(f)

    golden_categories = _load_golden_categories(args.results_dir)
    print(f"Golden categories loaded: {len(golden_categories)}")

    if args.all_profiles:
        for profile in PROFILE_CATEGORIES:
            results = score_tools(evaluations, golden_categories, profile, args.beta)
            print_table(results, profile, args.beta)
    else:
        results = score_tools(evaluations, golden_categories, args.profile, args.beta)
        print_table(results, args.profile, args.beta)


if __name__ == "__main__":
    main()

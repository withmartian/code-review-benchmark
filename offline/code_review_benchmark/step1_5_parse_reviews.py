#!/usr/bin/env python3
"""Parse tool review comments into categorized sections.

Produces results/parsed_{tool}.json with filtered comments and rendered markdown.
Step2 auto-detects these files and uses them instead of raw comments.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from code_review_benchmark.parsers import get_parser
from code_review_benchmark.parsers.base import ParsedReview

RESULTS_DIR = Path("results")
BENCHMARK_DATA_FILE = RESULTS_DIR / "benchmark_data.json"


def resolve_sections(
    include: list[str] | None,
    exclude: list[str] | None,
    only: list[str] | None,
    defaults: dict[str, bool] | None = None,
) -> dict[str, bool]:
    """Resolve section filter config from CLI flags.

    *defaults* comes from the parser's ``default_sections()``; when ``None``
    the caller must supply it via get_parser(tool).default_sections().
    """
    if defaults is None:
        defaults = {}
    if only:
        return {name: (name in only) for name in defaults}
    config = dict(defaults)
    if include:
        for name in include:
            if name in config:
                config[name] = True
    if exclude:
        for name in exclude:
            if name in config:
                config[name] = False
    return config


SEVERITY_RANK = {"critical": 3, "major": 2, "minor": 1}


def build_review_output(
    tool: str,
    review_comments: list[dict],
    sections_config: dict[str, bool],
    min_severity: str | None = None,
) -> dict:
    """Parse and filter a single review's comments, returning the output structure."""
    parser = get_parser(tool)
    parsed: ParsedReview = parser.parse(review_comments)

    min_rank = SEVERITY_RANK.get(min_severity, 0) if min_severity else 0

    included_comments: list[dict] = []
    excluded_comments: list[dict] = []

    # Build a filtered ParsedReview for markdown rendering
    included_sections: dict = {}

    for section_name, comments in parsed.sections.items():
        section_included = sections_config.get(section_name, False)
        for comment in comments:
            d = comment.to_dict()
            # Filter by severity if min_severity is set
            comment_included = section_included
            if comment_included and min_rank > 0 and comment.severity:
                if SEVERITY_RANK.get(comment.severity, 0) < min_rank:
                    comment_included = False
            if comment_included:
                included_comments.append(d)
                included_sections.setdefault(section_name, []).append(comment)
            else:
                excluded_comments.append(d)

    # Render markdown from included sections only
    included_review = ParsedReview(sections=included_sections)
    rendered_markdown = included_review.to_markdown()

    return {
        "tool": tool,
        "review_comments": included_comments,
        "excluded_comments": excluded_comments,
        "rendered_markdown": rendered_markdown,
    }


def run_parser(tool: str, sections_config: dict[str, bool], min_severity: str | None = None) -> None:
    """Run the parser for a specific tool and write results."""
    if not BENCHMARK_DATA_FILE.exists():
        print(f"Error: {BENCHMARK_DATA_FILE} not found")
        return

    with open(BENCHMARK_DATA_FILE) as f:
        data = json.load(f)

    included = [k for k, v in sections_config.items() if v]
    excluded = [k for k, v in sections_config.items() if not v]

    output = {
        "config": {
            "tool": tool,
            "included_sections": included,
            "excluded_sections": excluded,
            "min_severity": min_severity,
        },
        "reviews": {},
    }

    review_count = 0
    for golden_url, entry in data.items():
        for review in entry.get("reviews", []):
            if review["tool"] != tool:
                continue
            comments = review.get("review_comments", [])
            review_output = build_review_output(tool, comments, sections_config, min_severity)
            output["reviews"][golden_url] = review_output
            review_count += 1

    output_file = RESULTS_DIR / f"parsed_{tool}.json"
    with open(output_file, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Parsed {review_count} {tool} reviews")
    print(f"Included sections: {included}")
    if min_severity:
        print(f"Min severity: {min_severity}")
    print(f"Output: {output_file}")


def _format_candidate_text(body: str, path: str | None, line: int | None, include_path: bool) -> str:
    """Format candidate text, optionally prefixing with file path."""
    if include_path and path:
        location = f"{path}:{line}" if line is not None else path
        return f"{location} — {body}"
    return body


def write_candidates_direct(
    tool: str,
    sections_config: dict[str, bool],
    min_severity: str | None = None,
    model_dir: str | None = None,
    include_path: bool = False,
) -> None:
    """Write parsed comments directly to candidates.json, bypassing step2 LLM extraction.

    Each parsed comment becomes a candidate entry with text, path, line, and source fields.
    """
    if not BENCHMARK_DATA_FILE.exists():
        print(f"Error: {BENCHMARK_DATA_FILE} not found")
        return

    with open(BENCHMARK_DATA_FILE) as f:
        data = json.load(f)

    # Determine output directory
    import os

    if model_dir:
        out_dir = RESULTS_DIR / model_dir
    else:
        model = os.environ.get("MARTIAN_MODEL", "direct")
        out_dir = RESULTS_DIR / model.replace("/", "_")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load existing candidates (preserve other tools)
    candidates_file = out_dir / "candidates.json"
    if candidates_file.exists():
        with open(candidates_file) as f:
            all_candidates = json.load(f)
    else:
        all_candidates = {}

    review_count = 0
    total_candidates = 0

    for golden_url, entry in data.items():
        for review in entry.get("reviews", []):
            if review["tool"] != tool:
                continue
            comments = review.get("review_comments", [])
            review_output = build_review_output(tool, comments, sections_config, min_severity)

            # Convert included comments to candidate format
            candidates = []
            for c in review_output["review_comments"]:
                text = _format_candidate_text(c["body"], c.get("path"), c.get("line"), include_path)
                candidates.append({
                    "text": text,
                    "path": c.get("path"),
                    "line": c.get("line"),
                    "source": "parsed",
                })

            if golden_url not in all_candidates:
                all_candidates[golden_url] = {}
            all_candidates[golden_url][tool] = candidates
            review_count += 1
            total_candidates += len(candidates)

    with open(candidates_file, "w") as f:
        json.dump(all_candidates, f, indent=2)

    included = [k for k, v in sections_config.items() if v]
    print(f"Direct pass-through: {review_count} {tool} reviews → {total_candidates} candidates")
    print(f"Included sections: {included}")
    if min_severity:
        print(f"Min severity: {min_severity}")
    print(f"Output: {candidates_file}")


def main():
    parser = argparse.ArgumentParser(description="Parse tool review comments into sections")
    parser.add_argument("--tool", default="coderabbit", help="Tool to parse (default: coderabbit)")
    parser.add_argument("--include", nargs="*", help="Additional sections to include")
    parser.add_argument("--exclude", nargs="*", help="Sections to exclude from defaults")
    parser.add_argument("--only", help="Comma-separated list of sections to include (exclusive)")
    parser.add_argument("--preview", action="store_true", help="Print rendered markdown to stdout")
    parser.add_argument("--min-severity", choices=["minor", "major", "critical"], help="Exclude inline comments below this severity")
    parser.add_argument("--write-candidates", action="store_true", help="Write parsed comments directly to candidates.json (bypass step2)")
    parser.add_argument("--model-dir", help="Model directory name for candidates.json output (used with --write-candidates)")
    parser.add_argument("--include-path", action="store_true", help="Prefix candidate text with file path (used with --write-candidates)")
    args = parser.parse_args()

    only = args.only.split(",") if args.only else None
    tool_parser = get_parser(args.tool)
    defaults = tool_parser.default_sections()
    sections_config = resolve_sections(args.include, args.exclude, only, defaults)

    if args.preview:
        if not BENCHMARK_DATA_FILE.exists():
            print(f"Error: {BENCHMARK_DATA_FILE} not found")
            return
        with open(BENCHMARK_DATA_FILE) as f:
            data = json.load(f)
        for golden_url, entry in data.items():
            for review in entry.get("reviews", []):
                if review["tool"] != args.tool:
                    continue
                result = build_review_output(args.tool, review.get("review_comments", []), sections_config, args.min_severity)
                print(f"\n{'=' * 60}")
                print(f"PR: {golden_url}")
                print(f"{'=' * 60}")
                print(result["rendered_markdown"])
        return

    if args.write_candidates:
        write_candidates_direct(args.tool, sections_config, args.min_severity, args.model_dir, args.include_path)
    else:
        run_parser(args.tool, sections_config, args.min_severity)


if __name__ == "__main__":
    main()

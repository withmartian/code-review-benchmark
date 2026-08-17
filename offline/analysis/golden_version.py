#!/usr/bin/env python3
"""Derive a content identifier for the golden comment set.

Every score is measured against the golden set, and the golden set changes.
Two scores are comparable only if both were measured against the same one, so
this derives an identifier from the golden files themselves rather than from a
number someone has to remember to bump.

The identifier is a SHA-256 over every golden_comments/*.json, in sorted
filename order. Each file contributes its name and its JSON re-serialized
canonically (sorted keys, compact separators), so reformatting a file leaves
the identifier alone, while editing a comment, category, severity, title or
URL changes it. Files are read as UTF-8 explicitly, so the identifier does
not depend on the platform's locale.

Usage:
    python -m analysis.golden_version
    python -m analysis.golden_version --results-dir results
"""

import argparse
import hashlib
import json
from pathlib import Path

SHORT_LENGTH = 12


def resolve_golden_dir(results_dir: Path) -> Path:
    """Locate golden_comments/ the same way the scorers do."""
    golden_dir = results_dir.parent / "golden_comments"
    if not golden_dir.exists():
        golden_dir = Path("golden_comments")
    return golden_dir


def golden_set_version(golden_dir: Path) -> dict | None:
    """Identify the golden set in golden_dir, or None if there isn't one.

    Returns the digest plus the counts a human can check at a glance: how many
    files, how many PRs, how many golden comments.
    """
    if not golden_dir.exists():
        return None

    paths = sorted(golden_dir.glob("*.json"))
    if not paths:
        return None

    digest = hashlib.sha256()
    prs = 0
    comments = 0

    for path in paths:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest.update(f"{path.name}\0{canonical}\0".encode())

        entries = data if isinstance(data, list) else []
        prs += len(entries)
        comments += sum(len(entry.get("comments", [])) for entry in entries)

    sha256 = digest.hexdigest()
    return {
        "sha256": sha256,
        "short": sha256[:SHORT_LENGTH],
        "files": len(paths),
        "prs": prs,
        "comments": comments,
    }


def main() -> None:
    """Print the identifier of the golden set that scoring would use."""
    parser = argparse.ArgumentParser(description="Print the content identifier of the golden comment set")
    parser.add_argument("--results-dir", type=Path, default=Path("results"))
    args = parser.parse_args()

    golden_dir = resolve_golden_dir(args.results_dir)
    version = golden_set_version(golden_dir)

    if version is None:
        print(f"No golden comment files found in {golden_dir}")
        return

    print(f"Golden set:  {golden_dir}")
    print(f"  version:   {version['short']}  ({version['sha256']})")
    print(f"  contents:  {version['files']} files, {version['prs']} PRs, {version['comments']} comments")


if __name__ == "__main__":
    main()

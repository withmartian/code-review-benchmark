"""CodeRabbit comment parser.

Parses CodeRabbit's structured markdown/HTML comments into categorized sections.
Uses marker-based classification for comment-level types and html-based parsing
for nested <details> blocks within status comments.
"""

from __future__ import annotations

import re

from code_review_benchmark.parsers.base import BaseParser
from code_review_benchmark.parsers.base import ParsedComment
from code_review_benchmark.parsers.base import ParsedReview

# ---------------------------------------------------------------------------
# Severity badge patterns
# ---------------------------------------------------------------------------

_SEVERITY_PATTERNS = {
    "critical": re.compile(r"_\U0001f534\s*Critical_", re.IGNORECASE),
    "major": re.compile(r"_\U0001f7e0\s*Major_", re.IGNORECASE),
    "minor": re.compile(r"_\U0001f7e1\s*Minor_", re.IGNORECASE),
}

_SEVERITY_BADGE_LINE = re.compile(
    r"^_\u26a0\ufe0f\s*Potential issue_(\s*\|\s*_[^\n_]*_)?\s*\n*",
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Noise patterns
# ---------------------------------------------------------------------------

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_NOISE_SUMMARIES = {
    "\U0001f9e9 Analysis chain",
    "\U0001f916 Prompt for AI Agents",
    "\U0001f9f0 Tools",         # Wrapper for linting tool output (RuboCop, Ruff, Biome, Brakeman)
    "\U0001fa9b",               # 🪛 Individual linting tool blocks (standalone)
}

# ---------------------------------------------------------------------------
# Status comment section mapping
# ---------------------------------------------------------------------------

_SECTION_SUMMARY_MAP = {
    "\U0001f916 Fix all issues": "actionable_summary",
    "\U0001f9f9 Nitpick comments": "nitpick",
    "\u26a0\ufe0f Outside diff range comments": "outside_diff",
    "\U0001f4dc Review details": "review_details",
    "\U0001f509 Additional comments": "additional_comments",
    "\U0001f9f0 Additional context used": "additional_context",
}

_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)
_LINE_RANGE_RE = re.compile(r"^`(\d+)(?:-\d+)?`:\s*", re.MULTILINE)
_FILE_SUMMARY_RE = re.compile(r"^(.*?)\s*\(\d+\)$")


# ---------------------------------------------------------------------------
# Comment-level classification
# ---------------------------------------------------------------------------


def classify_comment(comment: dict) -> str:
    """Classify a raw comment into a section type based on markers.

    Checks run in priority order: inline first, then HTML comment markers,
    then content-based detection.
    """
    path = comment.get("path")
    line = comment.get("line")
    body = comment.get("body", "")

    # Inline comments: path and line both set
    if path and line is not None:
        return "inline"

    # HTML comment markers (most reliable)
    if "<!-- walkthrough_start -->" in body:
        return "walkthrough"

    # Status comment: starts with actionable comments count
    if body.lstrip().startswith("**Actionable comments posted:"):
        return "status"

    # Standalone outside-diff (not inside a status comment)
    if "\u26a0\ufe0f Outside diff range comments" in body:
        return "outside_diff"

    if "<!-- pre_merge_checks_walkthrough_start -->" in body:
        return "pre_merge_checks"

    if "<!-- finishing_touch_checkbox_start -->" in body:
        return "finishing_touches"

    return "unknown"


# ---------------------------------------------------------------------------
# <details> block splitter
# ---------------------------------------------------------------------------

_DETAILS_TAG_RE = re.compile(r"<details(?:\s[^>]*)?>|</details>", re.IGNORECASE)


def split_details_blocks(html: str) -> list[str]:
    """Split HTML into top-level <details>...</details> blocks.

    Returns the full text of each top-level block including nested blocks.
    """
    blocks: list[str] = []
    depth = 0
    start = 0

    for match in _DETAILS_TAG_RE.finditer(html):
        tag_text = match.group()
        if tag_text.lower().startswith("<details"):
            if depth == 0:
                start = match.start()
            depth += 1
        elif tag_text.lower() == "</details>":
            if depth == 0:
                # Orphaned closing tag — skip to avoid going negative
                continue
            depth -= 1
            if depth == 0:
                blocks.append(html[start : match.end()])
    return blocks


# ---------------------------------------------------------------------------
# Severity parsing and inline noise removal
# ---------------------------------------------------------------------------


def parse_severity(body: str) -> str | None:
    """Extract severity level from inline comment badge patterns."""
    for severity, pattern in _SEVERITY_PATTERNS.items():
        if pattern.search(body):
            return severity
    return None


def clean_inline_body(body: str) -> str:
    """Remove noise from an inline comment body, preserving issue text and code suggestions."""
    # Remove severity badge line
    cleaned = _SEVERITY_BADGE_LINE.sub("", body)

    # Remove noisy <details> blocks using proper nesting-aware splitter
    # (handles nested blocks like 🧰 Tools containing 🪛 RuboCop/Ruff/Biome)
    blocks = split_details_blocks(cleaned)
    for block in reversed(blocks):
        summary_match = _SUMMARY_RE.search(block[:200])
        if summary_match:
            summary_text = summary_match.group(1).strip()
            if any(noise in summary_text for noise in _NOISE_SUMMARIES):
                cleaned = cleaned.replace(block, "")

    # Remove HTML comments (internal state, fingerprinting, etc.)
    cleaned = _HTML_COMMENT.sub("", cleaned)

    return cleaned.strip()


# ---------------------------------------------------------------------------
# Status comment section parser
# ---------------------------------------------------------------------------


def _classify_details_block(block: str) -> str | None:
    """Identify which section a top-level <details> block belongs to."""
    summary_match = _SUMMARY_RE.search(block[:300])
    if not summary_match:
        return None
    summary_text = summary_match.group(1).strip()
    for pattern, section_name in _SECTION_SUMMARY_MAP.items():
        if pattern in summary_text:
            return section_name
    return None


def _parse_file_grouped_section(block: str) -> list[dict]:
    """Parse a section that groups comments by file (nitpick, outside_diff).

    Returns list of dicts with body, path, line keys.
    """
    results: list[dict] = []
    # Get inner content after the outer summary
    outer_summary_end = block.find("</summary>")
    if outer_summary_end == -1:
        return results
    inner_content = block[outer_summary_end + len("</summary>") :]
    # Remove the closing </details> of the outer block
    last_close = inner_content.rfind("</details>")
    if last_close != -1:
        inner_content = inner_content[:last_close]

    file_blocks = split_details_blocks(inner_content)

    for file_block in file_blocks:
        # Extract file path from summary
        summary_match = _SUMMARY_RE.search(file_block[:300])
        if not summary_match:
            continue
        summary_text = summary_match.group(1).strip()
        # Remove count suffix: "path/to/file.py (2)" -> "path/to/file.py"
        file_match = _FILE_SUMMARY_RE.match(summary_text)
        file_path = file_match.group(1).strip() if file_match else summary_text

        # Extract content after summary, before closing tags
        content_start = file_block.find("</summary>")
        if content_start == -1:
            continue
        content = file_block[content_start + len("</summary>") :]
        # Strip blockquote tags
        content = re.sub(r"</?blockquote>", "", content)
        # Strip trailing </details>
        content_last_close = content.rfind("</details>")
        if content_last_close != -1:
            content = content[:content_last_close]
        # Strip markdown blockquote prefixes (> ) from each line
        content = re.sub(r"^>\s?", "", content, flags=re.MULTILINE)
        content = content.strip()

        if not content:
            continue

        # Split individual comments by --- delimiter
        items = re.split(r"\n---\n", content)
        for item in items:
            item = item.strip()
            if not item:
                continue
            # Extract line number from `NNN-NNN`: pattern
            line_match = _LINE_RANGE_RE.search(item)
            line_num = int(line_match.group(1)) if line_match else None

            results.append({
                "body": item,
                "path": file_path,
                "line": line_num,
            })

    return results


def parse_status_comment(body: str) -> dict[str, list[dict]]:
    """Parse a status comment into its sub-sections.

    Returns dict mapping section names to lists of comment dicts.
    """
    sections: dict[str, list[dict]] = {}

    # Split into top-level <details> blocks
    blocks = split_details_blocks(body)

    for block in blocks:
        section_name = _classify_details_block(block)
        if section_name is None:
            continue

        if section_name in ("nitpick", "outside_diff"):
            # These have file-grouped inner structure
            items = _parse_file_grouped_section(block)
            if items:
                sections.setdefault(section_name, []).extend(items)
        else:
            # Other sections: extract the content between summary and closing
            summary_end = block.find("</summary>")
            if summary_end == -1:
                continue
            content = block[summary_end + len("</summary>") :]
            last_close = content.rfind("</details>")
            if last_close != -1:
                content = content[:last_close]
            content = re.sub(r"</?blockquote>", "", content).strip()
            sections[section_name] = [{"body": content, "path": None, "line": None}]

    return sections


# ---------------------------------------------------------------------------
# Full parser
# ---------------------------------------------------------------------------


class CodeRabbitParser(BaseParser):
    """Parses CodeRabbit structured comments into categorized sections."""

    def default_sections(self) -> dict[str, bool]:
        return {
            "inline": True,
            "actionable_summary": False,
            "outside_diff": True,
            "nitpick": False,
            "walkthrough": False,
            "pre_merge_checks": False,
            "finishing_touches": False,
            "review_details": False,
            "additional_comments": False,
            "additional_context": False,
            "unknown": False,
        }

    def parse(self, review_comments: list[dict]) -> ParsedReview:
        sections: dict[str, list[ParsedComment]] = {}

        for comment in review_comments:
            comment_type = classify_comment(comment)
            body = comment.get("body", "")
            path = comment.get("path")
            line = comment.get("line")
            created_at = comment.get("created_at", "")

            if comment_type == "inline":
                severity = parse_severity(body)
                cleaned_body = clean_inline_body(body)
                parsed = ParsedComment(
                    body=cleaned_body,
                    path=path,
                    line=line,
                    created_at=created_at,
                    section="inline",
                    severity=severity,
                )
                sections.setdefault("inline", []).append(parsed)

            elif comment_type == "status":
                sub_sections = parse_status_comment(body)
                for section_name, items in sub_sections.items():
                    for item in items:
                        parsed = ParsedComment(
                            body=item["body"],
                            path=item.get("path"),
                            line=item.get("line"),
                            created_at=created_at,
                            section=section_name,
                            severity=None,
                        )
                        sections.setdefault(section_name, []).append(parsed)

            elif comment_type == "outside_diff":
                # Standalone outside-diff comment — parse like a status section
                sub_sections = parse_status_comment(body)
                if "outside_diff" in sub_sections:
                    for item in sub_sections["outside_diff"]:
                        parsed = ParsedComment(
                            body=item["body"],
                            path=item.get("path"),
                            line=item.get("line"),
                            created_at=created_at,
                            section="outside_diff",
                            severity=None,
                        )
                        sections.setdefault("outside_diff", []).append(parsed)
                else:
                    cleaned = _HTML_COMMENT.sub("", body).strip()
                    parsed = ParsedComment(
                        body=cleaned, path=None, line=None,
                        created_at=created_at, section="outside_diff", severity=None,
                    )
                    sections.setdefault("outside_diff", []).append(parsed)

            else:
                # walkthrough, pre_merge_checks, finishing_touches, unknown
                cleaned = _HTML_COMMENT.sub("", body).strip()
                parsed = ParsedComment(
                    body=cleaned, path=path, line=line,
                    created_at=created_at, section=comment_type, severity=None,
                )
                sections.setdefault(comment_type, []).append(parsed)

        return ParsedReview(sections=sections)

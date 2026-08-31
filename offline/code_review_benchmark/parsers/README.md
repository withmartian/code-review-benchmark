# Review comment parsers

Raw tool review comments mix useful findings with tool-specific noise such as analysis chains, AI-agent prompts, linter output, severity badges, and HTML comments. They can also embed structure, including status or summary comments with nested `<details>` sections. Parsers strip that noise and classify comments into named sections before LLM extraction so the benchmark judges clean, comparable input. Each tool can provide its own parser; tools without one use a passthrough parser.

## Where it sits in the pipeline

```text
Step 1                    Step 1.5                 Step 2              Step 2.5           Step 3
benchmark_data.json  -->  parsed_{tool}.json  -->  extract comments --> deduplicate  -->  judge
       |                                             ^
       +---- raw comments when parsed file/entry ----+
             is absent (auto-detected by step 2)
```

Step 1.5 is optional. When `results/parsed_{tool}.json` is absent, step 2 reads the raw `review_comments` from `results/benchmark_data.json`.

## Components

| File | Role |
|---|---|
| [`base.py`](base.py) | Defines `BaseParser`, `ParsedComment`, `ParsedReview`, and markdown rendering. |
| [`__init__.py`](__init__.py) | Owns the parser registry and the `get_parser(tool)` lookup. |
| [`default.py`](default.py) | Provides the `DefaultParser` passthrough for tools without a registered parser. |
| [`coderabbit.py`](coderabbit.py) | Implements CodeRabbit-specific classification, structured-section parsing, and noise removal. |
| [`../step1_5_parse_reviews.py`](../step1_5_parse_reviews.py) | Exposes filtering and preview controls and writes `results/parsed_{tool}.json`. |

## Core contracts

### Parser input and output

Every parser subclasses `BaseParser` and implements `BaseParser.parse(review_comments: list[dict]) -> ParsedReview`:

```python
def parse(self, review_comments: list[dict]) -> ParsedReview:
    ...
```

Each input dictionary has the shape used by `review_comments` entries in `benchmark_data.json`: `body`, `path`, `line`, and `created_at`.

A `ParsedComment` preserves those four values and adds two parser-owned fields:

| Field | Type | Meaning |
|---|---|---|
| `body` | `str` | Cleaned comment text. |
| `path` | `str \| None` | Referenced file, if any. |
| `line` | `int \| None` | Referenced line, if any. |
| `created_at` | `str` | Original creation timestamp. |
| `section` | `str` | Named category emitted by the parser. |
| `severity` | `str \| None` | Optional severity metadata. |

`ParsedReview.sections` is a dictionary from section name to a list of `ParsedComment` objects.

### Section defaults and filtering

Each parser also implements:

```python
def default_sections(self) -> dict[str, bool]:
    ...
```

Every section name that `parse()` can emit must be a key in this dictionary. `True` includes the section by default; `False` excludes it by default while still allowing a caller to opt in.

This declaration is required, not advisory. A section emitted by `parse()` but missing from `default_sections()` is silently excluded and cannot be enabled from the CLI: `resolve_sections()` only toggles declared keys, and `build_review_output()` evaluates undeclared sections with `sections_config.get(name, False)`.

Excluded comments are not deleted. Step 1.5 writes them to `excluded_comments` in `parsed_{tool}.json` for auditability.

`severity` is optional metadata. The CLI accepts `critical`, `major`, and `minor` for `--min-severity`; those are the only recognized ranks. Comments with `severity=None` are never severity-filtered. Custom parsers should use only the recognized values: any other nonempty value has rank 0 and is excluded when a minimum severity is active.

### Registration and fallback

The `PARSERS` dictionary in `parsers/__init__.py` is populated lazily by `_register_parsers()`. `get_parser(tool)` returns an instance of the registered parser or falls back to `DefaultParser`, which emits every input comment in an included `raw` section.

### Rendering and step 2 integration

`ParsedReview.to_markdown()` renders each comment as a section header followed by its body, with comments separated by `---`. A comment with all metadata uses this form:

```markdown
## {Section Label} — {path}:{line} [{severity}]

{body}
```

The path and line suffix is omitted when there is no complete location, and the severity suffix is omitted when `severity` is `None`. Section labels replace underscores with spaces and use title case.

This `rendered_markdown` is the text consumed by step 2's LLM extraction. If `results/parsed_{tool}.json` exists and its `reviews` map contains the PR, step 2 uses that entry's `rendered_markdown` even when it is empty. Empty markdown means the parser found nothing actionable, so the review is effectively skipped by step 2's minimum 20-character check. If the parsed file or PR entry is absent, `get_comment_text_for_review()` in [`../step2_extract_comments.py`](../step2_extract_comments.py) falls back to joining the raw comment bodies.

## Adding a parser for your tool

The following example adds an `acme` parser that recognizes inline findings and summary comments, extracts supported severity metadata, removes Acme's hidden analysis blocks, and excludes unclassified comments by default.

1. Create `code_review_benchmark/parsers/acme.py`:

```python
from __future__ import annotations

import re

from code_review_benchmark.parsers.base import BaseParser
from code_review_benchmark.parsers.base import ParsedComment
from code_review_benchmark.parsers.base import ParsedReview

_SEVERITY_RE = re.compile(r"^<!-- acme:severity=(critical|major|minor) -->\s*", re.IGNORECASE)
_ANALYSIS_RE = re.compile(
    r"<details>\s*<summary>Acme analysis</summary>.*?</details>",
    re.IGNORECASE | re.DOTALL,
)
_SUMMARY_RE = re.compile(r"^## Acme review summary\s*", re.IGNORECASE)


class AcmeParser(BaseParser):
    def default_sections(self) -> dict[str, bool]:
        return {"inline": True, "summary": True, "other": False}

    def parse(self, review_comments: list[dict]) -> ParsedReview:
        sections: dict[str, list[ParsedComment]] = {}
        for comment in review_comments:
            body = comment.get("body", "")
            severity_match = _SEVERITY_RE.match(body)
            severity = severity_match.group(1).lower() if severity_match else None
            body = _SEVERITY_RE.sub("", body, count=1)
            body = _ANALYSIS_RE.sub("", body).strip()

            if comment.get("path") and comment.get("line") is not None:
                section = "inline"
            elif _SUMMARY_RE.match(body):
                section = "summary"
                body = _SUMMARY_RE.sub("", body, count=1).strip()
            else:
                section = "other"

            parsed = ParsedComment(
                body=body,
                path=comment.get("path"),
                line=comment.get("line"),
                created_at=comment.get("created_at", ""),
                section=section,
                severity=severity,
            )
            sections.setdefault(section, []).append(parsed)
        return ParsedReview(sections=sections)
```

2. Import and register it in `_register_parsers()` in `parsers/__init__.py`:

```python
def _register_parsers() -> None:
    global _registered
    if _registered:
        return
    _registered = True
    try:
        from code_review_benchmark.parsers.acme import AcmeParser
        from code_review_benchmark.parsers.coderabbit import CodeRabbitParser

        PARSERS["acme"] = AcmeParser
        PARSERS["coderabbit"] = CodeRabbitParser
    except ImportError:
        pass
```

3. Parse Acme reviews from `results/benchmark_data.json`:

```bash
uv run python -m code_review_benchmark.step1_5_parse_reviews --tool acme
```

4. Inspect the rendered input before writing the output file:

```bash
uv run python -m code_review_benchmark.step1_5_parse_reviews --tool acme --preview
```

5. Add tests that mirror the focused parser coverage in `offline/tests/test_parsers_*.py` and the classification, cleanup, structured parsing, and integration coverage in `offline/tests/test_coderabbit_*.py`.

## The CodeRabbit parser as a reference implementation

`CodeRabbitParser` demonstrates a parser for a tool with several comment formats. Its classifier applies a deliberate priority: a comment with both `path` and `line` is inline, then known HTML markers identify structured comment types, and finally content markers identify status and standalone outside-diff comments.

For structured status comments, `split_details_blocks()` tracks nesting depth so top-level `<details>` blocks can contain nested file groups without being split incorrectly. Inline cleanup removes blocks whose summaries match `_NOISE_SUMMARIES`, then removes HTML comments and severity-badge lines. `_SEVERITY_PATTERNS` extracts `critical`, `major`, or `minor`; `_SECTION_SUMMARY_MAP` maps status-comment summaries to benchmark section names. The `nitpick` and `outside_diff` sections receive additional file-grouped parsing that recovers file paths and starting line numbers from their nested blocks.

## CLI reference

Run the CLI from `offline/` with `uv run python -m code_review_benchmark.step1_5_parse_reviews`.

| Flag | Behavior |
|---|---|
| `--tool TOOL` | Parser and review tool to process. Defaults to `coderabbit`; unknown names use `DefaultParser`. |
| `--include [SECTION ...]` | Include additional declared sections on top of the parser defaults. |
| `--exclude [SECTION ...]` | Exclude declared sections from the parser defaults. |
| `--only SECTION[,SECTION...]` | Include only the comma-separated declared sections. |
| `--preview` | Print each matching PR's rendered markdown without writing `parsed_{tool}.json`. |
| `--min-severity {minor,major,critical}` | Exclude comments with recognized severity below the selected rank. Comments without severity remain included. |
| `--write-candidates` | Write included parsed comments directly to `results/{model}/candidates.json`, bypassing step 2. |
| `--model-dir DIR` | Select the results subdirectory used with `--write-candidates`. |
| `--include-path` | Prefix candidate text with `path` or `path:line` when used with `--write-candidates`. |

Without `--preview` or `--write-candidates`, step 1.5 writes this shape to `results/parsed_{tool}.json`:

```json
{
  "config": {
    "tool": "acme",
    "included_sections": ["inline", "summary"],
    "excluded_sections": ["other"],
    "min_severity": null
  },
  "reviews": {
    "https://github.com/example/project/pull/123": {
      "tool": "acme",
      "review_comments": [
        {
          "body": "This can dereference None.",
          "path": "src/service.py",
          "line": 42,
          "created_at": "2026-01-01T00:00:00Z",
          "section": "inline",
          "severity": "major"
        }
      ],
      "excluded_comments": [],
      "rendered_markdown": "## Inline — src/service.py:42 [major]\n\nThis can dereference None."
    }
  }
}
```

"""Opt-in model check: run EXTRACT_BOT_SUGGESTIONS on each labelled fixture and check the labels.

Makes one paid LLM call per fixture, so it is skipped unless all of these are set:
  RUN_EXTRACTION_SCOPE_EVAL=1, MARTIAN_API_KEY, MARTIAN_MODEL_NAME (MARTIAN_BASE_URL optional)

    RUN_EXTRACTION_SCOPE_EVAL=1 uv run pytest tests/test_extraction_scope_live.py -v

A labelled finding passes when some extracted suggestion matches its ``pattern`` (and its file, when
the label names one). A labelled non-finding passes when no extracted suggestion matches its
``pattern``. Labels whose pattern is null are not checked here. One draw at temperature 1.0 can
flip a borderline item, so a single failure is a prompt to look, not proof of a regression.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

import pytest

from llm.client import LLMClient
from llm.prompts import EXTRACT_BOT_SUGGESTIONS
from llm.schemas import BotSuggestionsResponse
from pipeline.analyze import _format_bot_comments

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "extraction_scope").glob("*.json"))

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("RUN_EXTRACTION_SCOPE_EVAL") == "1"
        and os.environ.get("MARTIAN_API_KEY")
        and os.environ.get("MARTIAN_MODEL_NAME")
    ),
    reason="opt-in: set RUN_EXTRACTION_SCOPE_EVAL=1, MARTIAN_API_KEY and MARTIAN_MODEL_NAME",
)


def _same_file(a: str | None, b: str) -> bool:
    a = (a or "").removeprefix("./")
    return bool(a) and (a == b or a.endswith("/" + b) or b.endswith("/" + a))


def check_labels(fx: dict, suggestions: list[dict]) -> list[str]:
    """Return one message per label the suggestions violate."""
    problems = []
    for label in fx["findings"]:
        if not label.get("pattern"):
            continue
        hit = any(
            re.search(label["pattern"], s.get("description") or "")
            and (label.get("path") is None or _same_file(s.get("file_path"), label["path"]))
            for s in suggestions
        )
        if not hit:
            problems.append(f"finding not extracted ({label['kind']}): {label['quote']}")
    for label in fx["non_findings"]:
        if not label.get("pattern"):
            continue
        for s in suggestions:
            if re.search(label["pattern"], s.get("description") or ""):
                problems.append(f"non-finding extracted ({label['kind']}): {s.get('description')}")
                break
    return problems


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
async def test_model_keeps_labelled_findings_and_skips_labelled_non_findings(path: Path) -> None:
    fx = json.loads(path.read_text())
    llm = LLMClient(
        base_url=os.environ.get("MARTIAN_BASE_URL") or None,
        api_key=os.environ["MARTIAN_API_KEY"],
        model_name=os.environ["MARTIAN_MODEL_NAME"],
    )
    prompt = EXTRACT_BOT_SUGGESTIONS.format(
        bot_username=fx["bot_username"],
        pr_title="",
        pr_author="unknown",
        repo_name="/".join(fx["source"].split("/")[3:5]),
        bot_comments=_format_bot_comments(fx["events"], fx["bot_username"]),
        commits_under_review="(commit diffs not included in this fixture)",
    )
    try:
        resp = await llm.structured_completion(prompt, BotSuggestionsResponse)
    finally:
        await llm.close()
    suggestions = [s.model_dump() for s in resp.suggestions]

    problems = check_labels(fx, suggestions)
    assert problems == [], json.dumps({"problems": problems, "suggestions": suggestions}, indent=1)

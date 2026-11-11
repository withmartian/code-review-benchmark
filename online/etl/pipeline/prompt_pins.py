"""The inputs an EXTRACTION_PROMPT_VERSION stands for, as sha256 digests.

An analysis depends on the three step prompts, the code that splits the commits at the reviewed one
and renders each step's input, the actor matching that decides which comments are the bot's, the
metric arithmetic in analyze_single_pr, and the JSON schema of each step's structured response.
tests/test_prompts.py pins these digests per version, and prompt_eval/compare_prompts.py puts them
in its resume fingerprint. Whitespace and comments in the pinned code count, on purpose.
"""

from __future__ import annotations

import hashlib
import inspect
import json

ANALYZE_FUNCTIONS = (
    "_find_bot_review_commit",
    "_split_commits_at_hash",
    "_build_details_by_sha",
    "_format_commits_with_diffs",
    "_clean_bot_comment_body",
    "_format_bot_comments",
    "_format_post_review_activity",
    "_format_suggestions",
    "_format_actions",
    "analyze_single_pr",
)
RESPONSE_SCHEMAS = ("BotSuggestionsResponse", "HumanActionsResponse", "MatchingResponse")
PROMPTS = ("EXTRACT_BOT_SUGGESTIONS", "EXTRACT_HUMAN_ACTIONS", "JUDGE_MATCHING")


def pinned_inputs() -> dict[str, str]:
    from llm import prompts
    from llm import schemas
    from pipeline import actors
    from pipeline import analyze

    texts = {name: getattr(prompts, name) for name in PROMPTS}
    for fn in ANALYZE_FUNCTIONS:
        texts[f"pipeline.analyze.{fn}"] = inspect.getsource(getattr(analyze, fn))
    texts["pipeline.analyze._HTML_COMMENT_RE"] = analyze._HTML_COMMENT_RE.pattern
    texts["pipeline.actors"] = inspect.getsource(actors)
    for model in RESPONSE_SCHEMAS:
        texts[f"llm.schemas.{model}"] = json.dumps(getattr(schemas, model).model_json_schema(), sort_keys=True)
    return {name: hashlib.sha256(text.encode()).hexdigest() for name, text in texts.items()}

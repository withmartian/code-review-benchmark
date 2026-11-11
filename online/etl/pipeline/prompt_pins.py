"""The inputs an EXTRACTION_PROMPT_VERSION stands for, as sha256 digests.

An analysis depends on the three step prompts, the code that splits the commits at the reviewed one
and renders each step's input, the actor matching that decides which comments are the bot's, the
metric arithmetic in analyze_single_pr, the JSON schema of each step's structured response, and the
LLM call (LLMClient.structured_completion: message shape, temperature, response format).
tests/test_prompts.py pins these digests per version, and prompt_eval/compare_prompts.py puts them
in its resume fingerprint. Whitespace and comments in the pinned code count, on purpose.

Not pinned, on purpose: the model name and base URL (configuration, stored in model_name next to the
version), the openai SDK and pydantic versions (uv.lock), and the data an analysis reads (the PR's
events and commits).
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
    from llm import client
    from llm import prompts
    from llm import schemas
    from pipeline import actors
    from pipeline import analyze

    texts = {name: getattr(prompts, name) for name in PROMPTS}
    for fn in ANALYZE_FUNCTIONS:
        texts[f"pipeline.analyze.{fn}"] = inspect.getsource(getattr(analyze, fn))
    texts["pipeline.analyze._HTML_COMMENT_RE"] = analyze._HTML_COMMENT_RE.pattern
    texts["pipeline.actors"] = inspect.getsource(actors)
    texts["llm.client.LLMClient.structured_completion"] = inspect.getsource(client.LLMClient.structured_completion)
    for model in RESPONSE_SCHEMAS:
        texts[f"llm.schemas.{model}"] = json.dumps(getattr(schemas, model).model_json_schema(), sort_keys=True)
    return {name: hashlib.sha256(text.encode()).hexdigest() for name, text in texts.items()}


def changed_entries(base: dict[str, dict], head: dict[str, dict]) -> list[str]:
    """Versions whose pinned entry differs between the base branch and this branch.

    Editing the newest entry in place keeps the per-version test green without a version bump; this
    comparison against the merge-base catches it (tests/test_prompts.py runs it when CI names the
    base through PROMPT_PINS_BASE_REF)."""
    return sorted(v for v in base if head.get(v) != base[v])


NULL_SHA = "0" * 40


def pins_at_merge_base(base_ref: str, test_file: str) -> dict[str, dict]:
    """PINNED_INPUTS_BY_VERSION as it is in `test_file` at the merge-base of HEAD and `base_ref`.

    Returns {} only when the base predates the version stamp (its llm/prompts.py has no
    EXTRACTION_PROMPT_VERSION). Raises when git cannot resolve the refs, and when the base has a
    version but the test file or its table is missing there (a moved or deleted table would otherwise
    disable the check)."""
    import ast
    from pathlib import Path
    import subprocess

    here = Path(test_file).resolve().parent

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(here), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def show(rel: str) -> str | None:
        """File content at the merge-base, or None when the path does not exist there."""
        done = subprocess.run(["git", "-C", str(here), "show", f"{merge_base}:{rel}"], capture_output=True, text=True)
        if done.returncode == 0:
            return done.stdout
        if "does not exist" in done.stderr or "exists on disk, but not in" in done.stderr:
            return None
        raise RuntimeError(f"git show {merge_base}:{rel} failed: {done.stderr.strip()}")

    merge_base = git("merge-base", "HEAD", base_ref)
    top = Path(git("rev-parse", "--show-toplevel"))
    test_rel = Path(test_file).resolve().relative_to(top)
    prompts = show((test_rel.parent.parent / "llm" / "prompts.py").as_posix())
    if prompts is None or "EXTRACTION_PROMPT_VERSION =" not in prompts:
        return {}
    source = show(test_rel.as_posix())
    if source is None:
        raise RuntimeError(f"{test_rel} is missing at {merge_base}, which already has a prompt version")
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "PINNED_INPUTS_BY_VERSION":
            return ast.literal_eval(node.value)
    raise RuntimeError(f"PINNED_INPUTS_BY_VERSION is missing from {test_rel} at {merge_base}")

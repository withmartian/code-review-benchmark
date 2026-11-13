"""The inputs an EXTRACTION_PROMPT_VERSION stands for, as sha256 digests.

An analysis depends on the three step prompts, the code that splits the commits at the reviewed one
and renders each step's input, the actor matching that decides which comments are the bot's, the
metric arithmetic in analyze_single_pr, the JSON schema of each step's structured response, and the
LLM call (LLMClient.structured_completion: message shape, temperature, response format).
tests/test_prompts.py pins these digests per version. Whitespace and comments in the pinned code
count, on purpose.

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


def unreachable_push_base(base_ref: str, cwd: str) -> bool:
    """True when `base_ref` is a raw commit sha (a push's github.event.before) that is not in this
    clone, as after a force-push that dropped it. A named ref (a pull request's origin/<base>) is never
    reported unreachable here: when it does not resolve, the merge-base check fails loudly instead."""
    import re
    import subprocess

    if not re.fullmatch(r"[0-9a-f]{40}", base_ref):
        return False
    done = subprocess.run(["git", "-C", cwd, "cat-file", "-e", f"{base_ref}^{{commit}}"], capture_output=True)
    return done.returncode != 0


def pins_at_merge_base(base_ref: str, test_file: str) -> dict[str, dict]:
    """PINNED_INPUTS_BY_VERSION as it is in `test_file` at the merge-base of HEAD and `base_ref`.

    Returns {} only when the base predates the version stamp: its llm/prompts.py has no
    EXTRACTION_PROMPT_VERSION and the pinned test file does not exist there. Raises when git cannot
    resolve the refs, when the base has a version but the test file or its table is missing, and when
    the test file or table exists but prompts.py or its version does not (a moved file would otherwise
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
    prompts_rel = (test_rel.parent.parent / "llm" / "prompts.py").as_posix()
    prompts = show(prompts_rel)
    source = show(test_rel.as_posix())
    table = None
    for node in ast.parse(source).body if source is not None else []:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "PINNED_INPUTS_BY_VERSION":
            table = ast.literal_eval(node.value)
    has_version = prompts is not None and "EXTRACTION_PROMPT_VERSION =" in prompts
    if not has_version:
        if table is not None or (prompts is None and source is not None):
            # the pinned table (or its test file) exists but the version moved: the check would go blind
            raise RuntimeError(f"{prompts_rel} has no EXTRACTION_PROMPT_VERSION at {merge_base}, but {test_rel} exists")
        return {}  # the base predates the version stamp: nothing to compare
    if source is None:
        raise RuntimeError(f"{test_rel} is missing at {merge_base}, which already has a prompt version")
    if table is None:
        raise RuntimeError(f"PINNED_INPUTS_BY_VERSION is missing from {test_rel} at {merge_base}")
    return table

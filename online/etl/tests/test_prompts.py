"""Change detectors for llm/prompts.py: each test pins the wording of one extraction-scope rule.

They fail whenever that rule's text changes, on purpose: a reviewer then checks that the change is
intended and updates the pinned sentence. Whitespace is collapsed, so rewrapping alone does not fail.
Model behavior is measured separately (test_extraction_scope_live.py and prompt_eval/).
"""

from __future__ import annotations

from llm.prompts import EXTRACT_BOT_SUGGESTIONS


def _render() -> str:
    return EXTRACT_BOT_SUGGESTIONS.format(
        bot_username="reviewer[bot]",
        pr_title="Add retry",
        pr_author="dev",
        repo_name="org/repo",
        bot_comments="COMMENT C1 [ISSUE_COMMENT timestamp=2026-07-01T12:00:00Z]\nWalkthrough ...",
        commits_under_review="(no commits)",
    )


def flat(text: str) -> str:
    """Collapse every run of whitespace to one space."""
    return " ".join(text.split())


def _rules() -> str:
    """The rendered prompt up to the inputs, whitespace collapsed."""
    rendered = flat(_render())
    return rendered[: rendered.index("=== Bot Review Comments ===")]


def test_extract_bot_suggestions_renders_with_all_placeholders() -> None:
    rendered = _render()

    assert "reviewer[bot]" in rendered
    assert "org/repo" in rendered
    assert "{" not in rendered
    assert "}" not in rendered


def test_overviews_are_not_findings_but_problems_named_inside_them_are() -> None:
    rules = _rules()

    assert "walkthroughs, change summaries, per-file tables, release notes" in rules
    assert "A concrete problem named inside a summary or walkthrough is still a finding: extract it." in rules


def test_ratings_are_not_findings_but_problems_in_their_explanation_are() -> None:
    rules = _rules()

    assert "Effort, risk, priority, impact, or confidence ratings themselves." in rules
    assert (
        "When the text that explains a rating names a concrete problem in the changed code, "
        "that problem is a finding: extract it, even when no other comment states it." in rules
    )


def test_repository_policy_gates_are_not_findings_on_any_surface() -> None:
    rules = _rules()

    assert "Repository-policy gates:" in rules
    assert "coverage thresholds (for example docstring or test coverage percentages)" in rules
    assert "regardless of which bot posts them or where (summary, review body, or inline comment)" in rules
    assert "A metric below a threshold is not by itself a finding." in rules


def test_documentation_requests_about_specific_code_are_still_findings() -> None:
    rules = _rules()

    assert (
        'A request to add docstrings or tests to "the touched functions" so a coverage threshold is met '
        "is a gate result, wherever it appears." in rules
    )
    assert (
        "A comment on a specific location in the changed code (a named function or file and line) that "
        "asks for documentation of that code, for a reason other than meeting a threshold, is a "
        "documentation finding: extract it." in rules
    )
    # tie-break between the two sentences above, for a located request whose only reason is the gate
    assert (
        "When a request names specific code and its only stated reason is a coverage threshold (for example "
        '"add a docstring to parse() to meet the 80% docstring coverage rule"), it is a gate result; when it '
        "cites a documentation or style rule for that specific code, it is a finding." in rules
    )


def test_controls_generation_offers_and_run_metadata_are_not_findings() -> None:
    rules = _rules()

    assert "checkboxes, buttons, commands the reader can run" in rules
    assert "offers to generate code for the author, for example to write docstrings or tests" in rules
    assert "Review status and run metadata: counts of comments posted" in rules


def test_inputs_the_bot_lists_and_raw_tool_output_are_not_findings() -> None:
    rules = _rules()

    assert "Inputs the bot lists as having used for its review" in rules
    assert "Do not extract findings from these inputs, even when a listed tool result names a file and line" in rules
    assert (
        "Tool output quoted as supporting context inside a comment, such as a linter or static-analysis log "
        "next to the bot's own text, is not its own finding: only problems the bot states in its own words "
        "are findings." in rules
    )
    assert (
        "A tool result the bot posts as its own comment at a location in the changed code is a finding, and "
        "so is a linter or static-analysis result the bot writes up as a problem in its own comment text." in rules
    )


def test_findings_in_review_body_sections_or_collapsed_sections_are_still_findings() -> None:
    rules = _rules()

    assert "These rules depend on what a piece of text is, not on where it sits." in rules
    assert (
        "A concrete finding listed in a review-body section rather than as an inline comment, or inside a "
        "collapsed or nested section, is still a finding: extract it." in rules
    )


def _pinned_inputs() -> dict[str, str]:
    from pipeline.prompt_pins import pinned_inputs

    return pinned_inputs()


def _entry_digest(entry: dict) -> str:
    import hashlib
    import json

    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()


def _ledger_problems(ledger: dict[str, dict]) -> list[str]:
    """Each entry names the sha256 of the entry before it. This does not stop an edit to a past
    entry, but such an edit also has to rewrite every later link, so it is visible in the diff."""
    problems, previous = [], None
    for version, entry in ledger.items():
        if entry.get("previous") != previous:
            problems.append(f"{version}: 'previous' does not match the entry before it (a past entry changed)")
        previous = _entry_digest(entry)
    inputs = [tuple(sorted(e["inputs"].items())) for e in ledger.values()]
    if len(set(inputs)) != len(inputs):
        problems.append("two versions share the same inputs")
    return problems


# What each EXTRACTION_PROMPT_VERSION was released with, oldest first. Do not edit a past entry:
# when any pinned input changes, bump EXTRACTION_PROMPT_VERSION in llm/prompts.py and add a new entry
# whose "previous" is _entry_digest() of the entry before it, so stored analyses made with the two
# can be told apart. An edit to a past entry is visible in the diff (it breaks or rewrites the links).
PINNED_INPUTS_BY_VERSION = {
    "extract-v2": {
        "previous": None,
        "inputs": {
            "EXTRACT_BOT_SUGGESTIONS": "6d8d28f476c1b3c29d433193659d7950d012e0653eba75b7ed2032b9548bb069",
            "EXTRACT_HUMAN_ACTIONS": "a205a85d059f54091b2f16ba6ac768f2ec5dab85dac54ac84656ab54241f755f",
            "JUDGE_MATCHING": "b110fde45775b98940a77ddf699fcc312f32fa4eab0588b1f9da489879f376f8",
            "pipeline.analyze._find_bot_review_commit": "0624307b300584f18f066fd60a92b4fd8302fca5aeb7618b5d8e210612447767",
            "pipeline.analyze._split_commits_at_hash": "a35eec4a52654a2808becab0b0d3c4cf3000eb7156fecf35dfb02f656b2c5915",
            "pipeline.analyze._build_details_by_sha": "7ee4ea52b9b58f9aa97f4e4ba2344ff6528e4621d02b1c44612686cd317f1103",
            "pipeline.analyze._format_commits_with_diffs": "4c24815cc71086530723fa6eb6a4346896622098e5c08d0f19358646ae635f9b",
            "pipeline.analyze._clean_bot_comment_body": "4d586e1efbdb23031f39f2d91a06f4d211b427bd4701ecb49b9cade11a46df87",
            "pipeline.analyze._format_bot_comments": "246149b41308c2626d38ea943fd6caed474dd4ce1981ab0a1cc7e64eabc770a2",
            "pipeline.analyze._format_post_review_activity": "aed6a6644974acb60a5b40e05d6075a6847c3e26ad331d5f2b7787727332ca38",
            "pipeline.analyze._format_suggestions": "ddeb73e2e299f01837ab9fa5a445cb0c0284f9c464be3c7cc7b9ce35fc4d795f",
            "pipeline.analyze._format_actions": "29532725a032aed8001f305444060bad0bae12b4833fc144d8462443a2a6f798",
            "pipeline.analyze.analyze_single_pr": "8f0cfd7d38d9e8318ccb8c89c9034a8ddf72e5fb54e15a4f09a326ea94095a8c",
            "pipeline.analyze._HTML_COMMENT_RE": "39d3d18fd21298dd2ea96526389f29db50575fe2b20f99d3306ddf6b269e79fa",
            "pipeline.actors": "d2cf21925cedd789b7ec2cdb6b93a32b7b3062dec265de1cd8f9af96b874b3cd",
            "llm.client.LLMClient.structured_completion": "8e334213d7602d491ff14ac64efdc147555210d422edd38022d8a41bd8fecf2a",
            "llm.schemas.BotSuggestionsResponse": "5969e73467cf574786325ea59d8470b0d6e817c82b7d02ce206d8e612e48b110",
            "llm.schemas.HumanActionsResponse": "34ad210bbaa719035d8e38a22d73f22e316e3efcc59b53133e946f8a175d880c",
            "llm.schemas.MatchingResponse": "05113b67df3ffd830cf4fbaa3daa429749eba8820e30be6928a9b575a2016eea",
        },
    },
}


def test_prompt_version_is_pinned_to_every_prompt_formatter_and_schema() -> None:
    from llm.prompts import EXTRACTION_PROMPT_VERSION

    assert list(PINNED_INPUTS_BY_VERSION)[-1] == EXTRACTION_PROMPT_VERSION, "the current version is the last entry"
    pinned = PINNED_INPUTS_BY_VERSION[EXTRACTION_PROMPT_VERSION]["inputs"]
    current = _pinned_inputs()
    assert set(pinned) == set(current)
    changed = sorted(name for name in current if current[name] != pinned[name])
    assert not changed, f"{changed} changed: bump EXTRACTION_PROMPT_VERSION and append a new entry here"


def test_prompt_version_entries_are_linked_and_distinct() -> None:
    assert _ledger_problems(PINNED_INPUTS_BY_VERSION) == []


def test_editing_a_past_version_entry_is_detected() -> None:
    import copy

    v1 = {"previous": None, "inputs": {"EXTRACT_BOT_SUGGESTIONS": "a" * 64}}
    v2 = {"previous": _entry_digest(v1), "inputs": {"EXTRACT_BOT_SUGGESTIONS": "b" * 64}}
    ledger = {"v1": v1, "v2": v2}
    assert _ledger_problems(ledger) == []

    edited = copy.deepcopy(ledger)
    edited["v1"]["inputs"]["EXTRACT_BOT_SUGGESTIONS"] = "c" * 64
    assert _ledger_problems(edited) == ["v2: 'previous' does not match the entry before it (a past entry changed)"]

    same = {"v1": v1, "v2": {"previous": _entry_digest(v1), "inputs": dict(v1["inputs"])}}
    assert _ledger_problems(same) == ["two versions share the same inputs"]


def test_located_rule_violations_are_findings_only_aggregate_results_are_excluded() -> None:
    rules = _rules()

    assert (
        "Only aggregate results (a status, or a metric against a threshold) and the located coverage request "
        "above are excluded: a violation of a "
        "repository rule or guideline reported at a specific location in the changed code is a finding." in rules
    )


def test_only_the_listed_kinds_are_excluded_and_pull_request_metadata_gaps_are_findings() -> None:
    rules = _rules()

    assert (
        "Only the kinds listed below are excluded: they are not code-review findings, even when phrased as "
        "advice or placed inside a review body or issue comment. Other PR-level content is judged like any "
        "other comment of the bot." in rules
    )
    assert (
        "a remark or check result that says what the pull request's title, description, or linked issue "
        "lacks is a finding, wherever it sits: extract it." in rules
    )
    assert (
        "This rule does not cover checks of the pull request's title, description, or linked issue, even when "
        "they are listed with other check results (see above)." in rules
    )
    # remarks about the pull request itself are not excluded by any rule here (left to the maintainers)
    assert "Remarks about the pull request itself" not in rules
    assert "title, description, linked-issue, or scope checks" not in rules


def test_prompt_version_is_stamped_into_the_stored_model_name() -> None:
    from llm.prompts import EXTRACTION_PROMPT_VERSION
    from pipeline.analyze import stamped_model_name

    assert stamped_model_name("some-model") == f"some-model+{EXTRACTION_PROMPT_VERSION}"
    # no model name: nothing to stamp, so the stored value stays None rather than "unknown+<version>"
    assert stamped_model_name(None) is None
    assert stamped_model_name("") is None


def test_no_pinned_entry_on_the_base_branch_was_edited() -> None:
    """Editing the newest entry in place keeps the per-version test green without a version bump, so
    CI compares the table with the one at the merge-base (PROMPT_PINS_BASE_REF names the base: the
    pull request's base branch, or the commit a push started from)."""
    import os

    import pytest

    from pipeline.prompt_pins import NULL_SHA
    from pipeline.prompt_pins import changed_entries
    from pipeline.prompt_pins import pins_at_merge_base
    from pipeline.prompt_pins import unreachable_push_base

    base_ref = os.environ.get("PROMPT_PINS_BASE_REF")
    if not base_ref:
        pytest.skip("PROMPT_PINS_BASE_REF is not set (CI sets it to the base branch)")
    if base_ref == NULL_SHA:
        pytest.skip("the push created a new branch: there is no earlier commit to compare with")
    if unreachable_push_base(base_ref, os.path.dirname(__file__)):
        pytest.skip(f"the push's earlier commit {base_ref[:12]} is not in this clone (a force-push dropped it)")
    edited = changed_entries(pins_at_merge_base(base_ref, __file__), PINNED_INPUTS_BY_VERSION)
    assert not edited, f"{edited} changed on this branch: add a new EXTRACTION_PROMPT_VERSION instead"


def _scratch_repo(tmp_path):
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            check=True,
            capture_output=True,
        )

    (tmp_path / "llm").mkdir()
    (tmp_path / "tests").mkdir()
    git("init", "-q", "-b", "main")
    return git, tmp_path / "llm" / "prompts.py", tmp_path / "tests" / "test_prompts.py"


def test_the_merge_base_check_catches_an_entry_edited_in_place(tmp_path) -> None:
    from pipeline.prompt_pins import changed_entries
    from pipeline.prompt_pins import pins_at_merge_base

    git, prompts, pins = _scratch_repo(tmp_path)
    prompts.write_text('EXTRACTION_PROMPT_VERSION = "v1"\n')
    pins.write_text('PINNED_INPUTS_BY_VERSION = {"v1": {"previous": None, "inputs": {"P": "a"}}}\n')
    git("add", ".")
    git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "change")
    pins.write_text('PINNED_INPUTS_BY_VERSION = {"v1": {"previous": None, "inputs": {"P": "b"}}}\n')
    git("commit", "-q", "-am", "edit in place")

    base = pins_at_merge_base("main", str(pins))
    assert base == {"v1": {"previous": None, "inputs": {"P": "a"}}}
    assert changed_entries(base, {"v1": {"previous": None, "inputs": {"P": "b"}}}) == ["v1"]
    # appending a new version leaves the base entry as it was
    assert changed_entries(base, {**base, "v2": {"previous": "x", "inputs": {"P": "b"}}}) == []


def test_only_a_missing_raw_push_sha_counts_as_unreachable(tmp_path) -> None:
    import subprocess

    from pipeline.prompt_pins import unreachable_push_base

    git, prompts, _ = _scratch_repo(tmp_path)
    prompts.write_text('EXTRACTION_PROMPT_VERSION = "v1"\n')
    git("add", ".")
    git("commit", "-q", "-m", "base")
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    assert not unreachable_push_base(head, str(tmp_path))
    assert unreachable_push_base("1" * 40, str(tmp_path))  # e.g. the tip a force-push replaced
    # a named ref is left to the merge-base call, which fails loudly when it does not resolve
    assert not unreachable_push_base("origin/no-such-branch", str(tmp_path))


def test_the_merge_base_check_passes_only_a_base_without_a_prompt_version(tmp_path) -> None:
    import pytest

    from pipeline.prompt_pins import pins_at_merge_base

    git, prompts, pins = _scratch_repo(tmp_path)
    prompts.write_text("PROMPT = 'x'\n")
    git("add", ".")
    git("commit", "-q", "-m", "before the version stamp")
    git("checkout", "-q", "-b", "change")
    pins.write_text('PINNED_INPUTS_BY_VERSION = {"v1": {"previous": None, "inputs": {"P": "a"}}}\n')
    git("add", ".")
    git("commit", "-q", "-m", "add the table")
    assert pins_at_merge_base("main", str(pins)) == {}  # nothing to compare yet

    git("checkout", "-q", "main")
    prompts.write_text('EXTRACTION_PROMPT_VERSION = "v1"\n')
    git("commit", "-q", "-am", "a version but no table")
    git("checkout", "-q", "-b", "change2")
    pins.parent.mkdir(exist_ok=True)  # the checkout of main removed the untracked-empty tests/ dir
    pins.write_text('PINNED_INPUTS_BY_VERSION = {"v1": {"previous": None, "inputs": {"P": "a"}}}\n')
    git("add", ".")
    git("commit", "-q", "-m", "table appears")
    with pytest.raises(RuntimeError, match="missing at"):
        pins_at_merge_base("main", str(pins))


def test_the_merge_base_check_fails_when_prompts_py_moved_but_the_table_exists(tmp_path) -> None:
    import pytest

    from pipeline.prompt_pins import pins_at_merge_base

    git, _prompts, pins = _scratch_repo(tmp_path)
    (tmp_path / "llm" / "prompt_text.py").write_text('EXTRACTION_PROMPT_VERSION = "v1"\n')  # moved away
    pins.write_text('PINNED_INPUTS_BY_VERSION = {"v1": {"previous": None, "inputs": {"P": "a"}}}\n')
    git("add", ".")
    git("commit", "-q", "-m", "prompts.py moved")
    git("checkout", "-q", "-b", "change")
    git("commit", "-q", "--allow-empty", "-m", "anything")
    with pytest.raises(RuntimeError, match="has no EXTRACTION_PROMPT_VERSION"):
        pins_at_merge_base("main", str(pins))

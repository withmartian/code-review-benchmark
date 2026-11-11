"""Prompt templates for the LLM analysis pipeline."""

# Stamped into llm_analyses.model_name as "<model>+<version>", so analyses scored with different
# prompts can be told apart (analyses made before the stamp carry none). Bump it whenever any input
# pipeline/prompt_pins.py lists changes: a step prompt, the code that splits the commits and renders
# step input, the actor matching, analyze_single_pr's metric arithmetic, a response schema or the LLM
# call. tests/test_prompts.py pins their sha256 per version.
EXTRACTION_PROMPT_VERSION = "extract-v2"

EXTRACT_BOT_SUGGESTIONS = """You are analyzing a pull request to extract concrete code-review findings flagged by a code review bot.

The bot's username is: {bot_username}

Below you will see:
1. The bot's review comments on the commits
2. The commits that were under review (the code state the bot saw), including full diffs

The Bot Review Comments section is a numbered list. Each entry is labeled as REVIEW_BODY,
INLINE_REVIEW_COMMENT, or ISSUE_COMMENT. Process all numbered comments. Top-level inline
review comments generally contain substantive findings; review bodies and issue comments may
contain findings or summaries.

Your job is to identify every distinct substantive finding the bot flagged. A finding can be a
bug, defect, risk, incorrect behavior, missing validation, security concern, performance concern,
maintainability issue, documentation problem, test gap, or other concrete code-review concern.

Only extract findings grounded in the bot's comments. Do not invent findings solely from the
commit diff. Use the commit diff only to understand code context for a comment.

For each finding, extract:
- A unique ID (S1, S2, ...)
- A concise description of the issue or concern the bot flagged
- The category (bug, style, performance, security, refactor, documentation, other)
- The file path and line number if available
- Severity (low, medium, high, critical)

Skip generic praise, status/progress messages, quota/limit messages, pure summaries without a
concrete finding, and "looks good" / "no issues found" comments. Also skip follow-up comments
that merely confirm a fix, acknowledge a clarification, say a parent comment was wrong, too
broad, pre-existing, already addressed, or out of scope, unless that same comment introduces a
new distinct code issue.

Bots often post PR-level content next to their findings. Only the kinds listed below are excluded:
they are not code-review findings, even when phrased as advice or placed inside a review body or
issue comment. Other PR-level content is judged like any other comment of the bot. In particular, a
remark or check result that says what the pull request's title, description, or linked issue lacks
is a finding, wherever it sits: extract it. Within a listed kind, a concrete problem the bot names is
still a finding, as each item says.
- Overviews of the change: walkthroughs, change summaries, per-file tables, release notes, and
  sequence or architecture diagrams. A concrete problem named inside a summary or walkthrough is
  still a finding: extract it.
- Effort, risk, priority, impact, or confidence ratings themselves. When the text that explains a
  rating names a concrete problem in the changed code, that problem is a finding: extract it, even
  when no other comment states it.
- Repository-policy gates: results of automated checks reported as a status (pass/fail/warning)
  or as a metric against a configured threshold, such as coverage thresholds (for example
  docstring or test coverage percentages), regardless of which bot posts them or where (summary,
  review body, or inline comment). A metric below a threshold is not by itself a finding. Extract
  a finding from a check result only when it names a concrete problem in the changed code, such as
  a specific missing or incorrect behavior. A request to add docstrings or tests to "the touched
  functions" so a coverage threshold is met is a gate result, wherever it appears. A comment on a
  specific location in the changed code (a named function or file and line) that asks for
  documentation of that code, for a reason other than meeting a threshold, is a documentation
  finding: extract it. When a request names specific code and its only stated reason is a coverage
  threshold (for example "add a docstring to parse() to meet the 80% docstring coverage rule"), it
  is a gate result; when it cites a documentation or style rule for that specific code, it is a
  finding. Only aggregate results (a status, or a metric against a threshold) and the located
  coverage request above are excluded: a violation of a repository rule or guideline reported at a
  specific location in the changed code is a finding. This rule does not cover checks of the pull
  request's title, description, or linked issue, even when they are listed with other check results
  (see above).
- Interactive controls and product text: checkboxes, buttons, commands the reader can run, usage
  tips, links to dashboards, and configuration or review-settings details. This includes offers
  to generate code for the author, for example to write docstrings or tests.
- Review status and run metadata: counts of comments posted, lists of files reviewed or skipped,
  the commit range reviewed, run or plan details, and quota or allowance notes.
- Inputs the bot lists as having used for its review, such as linter or static-analysis output,
  code guidelines, or configured review instructions (for example a section where the bot lists the
  inputs or context it used). Do not extract findings from these inputs, even when a listed tool
  result names a file and line of the changed code: the bot did not raise it as its own comment.
- Tool output quoted as supporting context inside a comment, such as a linter or static-analysis
  log next to the bot's own text, is not its own finding: only problems the bot states in its own
  words are findings. A tool result the bot posts as its own comment at a location in the changed
  code is a finding, and so is a linter or static-analysis result the bot writes up as a problem in
  its own comment text.

These rules depend on what a piece of text is, not on where it sits. A concrete finding listed in
a review-body section rather than as an inline comment, or inside a collapsed or nested section,
is still a finding: extract it.

PR Title: {pr_title}
PR Author: {pr_author}
Repository: {repo_name}

=== Bot Review Comments ===
{bot_comments}

=== Commits Under Review (code the bot reviewed) ===
{commits_under_review}
"""

EXTRACT_HUMAN_ACTIONS = """You are analyzing post-review commit diffs to extract every concrete code issue that was fixed or improved AFTER the bot reviewed the PR.

These extracted issues are ground-truth labels — the actual problems that existed in the code the bot reviewed. Your job is to read the diffs and identify each distinct issue that was addressed.

The bot's username is: {bot_username}

Below you will see:
1. Commits made AFTER the bot's review, including full diffs
2. Post-review comments that may provide context about why changes were made

For each distinct code issue fixed in the post-review commits, extract:
- A unique ID (A1, A2, ...)
- A description of the specific issue that was fixed (what was wrong, not what was done)
- The category of the issue (bug, style, performance, security, refactor, documentation, other)
- The file path where the fix was applied
- The commit SHA that fixed it
- Action type: why was this changed? (fix, improvement, cleanup, new_feature, other)

Guidelines:
- Focus on the DIFFS — each action should correspond to a real code change you can see
- Describe the ISSUE that existed, not the PR activity (not "replied to comment" or "resolved thread")
- One action per distinct issue — if a commit fixes 3 separate things in 3 files, that's 3 actions
- If a comment explains why a change was made, incorporate that context into the description
- Skip trivial formatting-only changes (whitespace, import ordering) unless they fix a real problem
- Skip merge commits or changes that don't address a code issue

PR Title: {pr_title}
PR Author: {pr_author}
Repository: {repo_name}

=== Post-Review Commits (changes after bot review) ===
{post_review_commits}

=== Post-Review Comments (context for why changes were made) ===
{post_review_activity}
"""

LABEL_PR = """You are classifying a pull request with structured labels based on its metadata, changed files, and review analysis.

PR Title: {pr_title}
Repository: {repo_name}
PR Author: {pr_author}

=== Files Changed ===
{file_list}

=== Review Analysis Summary ===
{suggestion_summary}

Based on the above, classify this PR:
- language: the primary programming language (by file count/significance)
- languages: all languages present
- domain: frontend | backend | infra | fullstack | docs | other
- pr_type: feature | bugfix | refactor | chore | docs | test | other
- issue_types: list of issue categories found in the review (bug, style, performance, security, refactor, docs, other)
- severity: overall severity of issues found (low, medium, high, critical). If no issues, use "low".
- framework: detected framework (React, Django, Flask, Spring, etc.) or null if unclear
- test_changes: whether any test files were modified
"""

JUDGE_MATCHING = """You are judging whether a bot's code review suggestions correspond to actual code issues that were later fixed.

The bot's username is: {bot_username}

You have two lists:
- Bot Suggestions: issues the bot flagged during review
- Code Fixes: actual issues that were fixed in post-review commits (ground truth)

For EACH bot suggestion, determine:
1. Does it match any code fix? (matched: true/false)
2. Which code fix? (human_action_id)
3. How confident are you? (0.0-1.0)
4. Brief reasoning

A suggestion is "matched" if:
- It identified the same issue (or substantially the same concern) that was later fixed
- The fix is in the same file/area the suggestion pointed to
- Even a partial overlap counts if the bot caught part of the real problem

A suggestion is NOT matched if:
- No corresponding fix exists — the bot flagged something that wasn't actually fixed
- The fix addresses a different concern than what the bot suggested
- The bot's suggestion was about something that wasn't a real problem

=== Bot Suggestions ===
{bot_suggestions}

=== Code Fixes (ground truth) ===
{human_actions}
"""

"""Prompt templates for the LLM analysis pipeline."""

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

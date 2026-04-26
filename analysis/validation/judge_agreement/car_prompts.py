"""CAR (Comment Action Rate) judge prompts: thread-aware + contextualized.

Two versions:
  - v1 (original): lenient matching, counts indirect fixes
  - v2 (strict): requires same file/area, direct response, actionable comments only

Unlike the current matching judge which operates on extracted abstractions
(S1, A1 summary pairs), the CAR judge operates directly on raw artifacts:
  - The actual bot review comment text
  - The full comment thread (human replies, resolution markers)
  - The actual post-review commit diffs

The question is fundamentally different:
  Current: "Does extracted suggestion S_i semantically match extracted action A_j?"
  CAR:     "Was the concern in this bot comment addressed by subsequent code changes?"
"""

# v1: original lenient prompt (kept for comparison)
CAR_JUDGE_V1 = """You are evaluating whether a bot's code review comment was addressed by subsequent code changes.

You will see:
1. A bot review comment — the original text the bot posted during code review
2. The comment thread — any replies or discussion between the bot and humans
3. Post-review code diffs — actual code changes made after the bot's review

For the bot's comment, determine:
1. Was the concern addressed? (addressed: true/false)
   - "addressed" means the post-review code changes fix, improve, or otherwise respond to
     the issue the bot raised — even partially
   - Consider both direct fixes (code changed exactly as suggested) and indirect fixes
     (the concern was resolved in a different way)
   - If the developer explicitly acknowledged the comment (e.g. "fixed", "done", "good catch")
     AND made a relevant code change, that counts as addressed
   - If no post-review code change relates to this comment's concern, it is NOT addressed
2. How confident are you? (confidence: 0.0-1.0)
3. Brief reasoning explaining your judgment

Focus on the CODE DIFFS — your primary evidence is whether you can find a code change
that responds to the bot's concern. Thread context is secondary evidence.

PR Title: {pr_title}
Repository: {repo_name}
PR Author: {pr_author}
Bot Username: {bot_username}

=== Bot Review Comment ===
{bot_comment}

=== Comment Thread (replies and discussion) ===
{comment_thread}

=== Post-Review Code Diffs ===
{post_review_diffs}
"""

# v2: strict prompt — same file/area, direct response, no marginal matches
CAR_JUDGE_V2 = """You are evaluating whether a bot's code review comment was directly addressed by subsequent code changes.

You will see:
1. A bot review comment — the original text the bot posted during code review
2. The comment thread — any replies or discussion between the bot and humans
3. Post-review code diffs — actual code changes made after the bot's review

For the bot's comment, determine:
1. Was the concern directly addressed? (addressed: true/false)

   A comment is "addressed" ONLY if ALL of the following are true:
   - The code change is in the SAME FILE or closely related area that the comment references
   - The change is a DIRECT response to the specific concern raised — not a coincidental
     change in the same area that happens to touch related code
   - There is clear evidence of causation: the developer either acknowledged the comment
     (e.g. "fixed", "done", "good catch" in the thread) OR the code change precisely
     implements what the bot suggested

   A comment is NOT addressed if:
   - The code change is in a different file/area than what the comment references
   - The overlap between the comment's concern and the code change is marginal or coincidental
   - There is no developer acknowledgment AND the code change only loosely relates to the concern
   - The comment is a summary, observation, or praise — not an actionable suggestion
   - The comment describes what already exists in the code (e.g. summarizing a diff)
     rather than suggesting a change

2. How confident are you? (confidence: 0.0-1.0)
3. Brief reasoning explaining your judgment

IMPORTANT: Be strict. Only count clear, direct responses to the bot's specific concern.
Do not count indirect or coincidental code changes.

PR Title: {pr_title}
Repository: {repo_name}
PR Author: {pr_author}
Bot Username: {bot_username}

=== Bot Review Comment ===
{bot_comment}

=== Comment Thread (replies and discussion) ===
{comment_thread}

=== Post-Review Code Diffs ===
{post_review_diffs}
"""

# Actionability filter: determines if a comment is an actionable suggestion
# vs a summary, observation, or praise
CAR_ACTIONABILITY_FILTER = """You are classifying a bot code review comment.

Is this comment an ACTIONABLE SUGGESTION — meaning it identifies a specific issue,
bug, improvement, or change that the developer should make?

NOT actionable (return false):
- Summaries of what the code does or what the PR changes
- General praise ("looks good", "nice work")
- Informational observations without a suggested change
- Descriptions of existing behavior without suggesting improvement
- Auto-generated review summaries or changelogs

ACTIONABLE (return true):
- Points out a specific bug, issue, or concern
- Suggests a concrete code change
- Identifies a potential problem (security, performance, correctness)
- Recommends a refactoring or improvement

Bot Comment:
{bot_comment}
"""

# Default to v2 (strict)
CAR_JUDGE = CAR_JUDGE_V2

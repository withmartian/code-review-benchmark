"""CAR (Comment Action Rate) judge prompt: thread-aware + contextualized.

Unlike the current matching judge which operates on extracted abstractions
(S1, A1 summary pairs), the CAR judge operates directly on raw artifacts:
  - The actual bot review comment text
  - The full comment thread (human replies, resolution markers)
  - The actual post-review commit diffs

The question is fundamentally different:
  Current: "Does extracted suggestion S_i semantically match extracted action A_j?"
  CAR:     "Was the concern in this bot comment addressed by subsequent code changes?"
"""

CAR_JUDGE = """You are evaluating whether a bot's code review comment was addressed by subsequent code changes.

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

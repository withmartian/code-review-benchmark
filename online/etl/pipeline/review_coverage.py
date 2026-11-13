"""Which commits a bot reviewed, and which fixes were in code no review of the bot read.

The analysis splits a PR at the bot's first review and counts every fix made in a later commit
against the bot's recall. A fix of code that no review of the bot read before the fix is a miss
the bot was never given the chance to catch, whatever the reason it did not review those pushes:
a usage limit, a pause, a failed review, a skip by the bot's own policy (CodeRabbit's free OSS
plan skips repositories with too few stars after a manual review), automatic incremental reviews
turned off, or a fix pushed before the next review ran.

The commits a bot reviewed come from the bot's own record where it keeps one
(``RECORD_PATTERNS``): CodeRabbit states each review's range ("Reviewing files that changed from
the base of the PR and between X and Y"), in the review's body and in the summary comment it
rewrites on each review, including a review that posted nothing. The summary's current body and
its earlier revisions (its edit history) give every review's range. A completed review with no
record counts as a review of the whole pull request at the commit it was posted on, and so does
the last reviewed commit a notice of the bot names (``NOTICE_PATTERNS``).

``fix_code_origin`` then reads the stored commit patches to find the commits that wrote each line
a fix changed: the commit it was first written in and every commit that lightly edited it after.
A line counts as reviewed when a review of an earlier commit than the fix covered one of those
commits, since that review's diff showed the line: a review of the fixed state cannot have shown
the bot code the fix already removed.

To support another bot's record, add a ``RecordPattern`` row for its login.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re

from pipeline.actors import same_github_actor

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SHA = r"([0-9a-f]{7,40})"


def _reply_to(data: dict) -> object:
    """The comment an inline comment replies to: ``in_reply_to_id`` (GH Archive, REST) or
    ``thread_reply_to_id`` (the threads query, kept apart so step 1's reply filter is unchanged)."""
    return data.get("in_reply_to_id") or data.get("thread_reply_to_id")


# -- Notices that the bot did not review a push ----------------------------------


def _coderabbit_block(kind: str) -> re.Pattern[str]:
    """A CodeRabbit status block, delimited by its own HTML comment markers."""
    marker = re.escape(kind)
    return re.compile(
        rf"<!-- This is an auto-generated comment: {marker} by coderabbit\.ai -->"
        rf"(?P<block>.*?)"
        rf"<!-- end of auto-generated comment: {marker} by coderabbit\.ai -->",
        re.DOTALL | re.IGNORECASE,
    )


@dataclass(frozen=True)
class NoticePattern:
    """One notice format of one bot.

    pattern matches the comment body; its ``block`` group, when present, is the notice text.
    requires, when set, must also match the notice text. reviewed and unreviewed capture the
    last reviewed commit and the latest commit not reviewed, when the notice names them.
    in_place means the notice sits in a status comment the bot rewrites on each push, so the
    current body describes the latest push; other notices are tied to the time they were posted.
    """

    bot: str
    reason: str
    pattern: re.Pattern[str]
    in_place: bool
    requires: re.Pattern[str] | None = None
    reviewed: re.Pattern[str] | None = None
    unreviewed: re.Pattern[str] | None = None


_CODERABBIT = "coderabbitai[bot]"
_CR_RANGE = re.compile(rf"between {_SHA} and {_SHA}")

NOTICE_PATTERNS: tuple[NoticePattern, ...] = (
    # "Review limit reached": names the range it did not review, "between <last reviewed> and <head>".
    NoticePattern(
        bot=_CODERABBIT,
        reason="limit",
        pattern=_coderabbit_block("rate limited"),
        in_place=True,
        reviewed=_CR_RANGE,
        unreviewed=re.compile(rf"between [0-9a-f]{{7,40}} and {_SHA}"),
    ),
    # "Reviews paused": usually names "Last reviewed commit" and "Latest commit, not reviewed".
    NoticePattern(
        bot=_CODERABBIT,
        reason="pause",
        pattern=_coderabbit_block("review paused"),
        in_place=True,
        reviewed=re.compile(rf"Last reviewed commit:\s*{_SHA}"),
        unreviewed=re.compile(rf"Latest commit, not reviewed:\s*{_SHA}"),
    ),
    # "Review failed" (for example, the pull request was closed while the review ran).
    NoticePattern(bot=_CODERABBIT, reason="failure", pattern=_coderabbit_block("failure"), in_place=True),
    # A skip block that reports a failed incremental review ("We couldn't safely recover the
    # incremental review").
    NoticePattern(
        bot=_CODERABBIT,
        reason="failure",
        pattern=_coderabbit_block("skip review"),
        in_place=True,
        requires=re.compile(r"couldn.t safely recover the incremental review", re.IGNORECASE),
    ),
    # Every other skip block, by design or configuration, as the sample's summaries hold them:
    # "## Draft PR not reviewed" ("Draft PRs are not automatically reviewed by default."), and
    # "## Review skipped" with "Auto incremental reviews are disabled on this repository.", "Auto
    # reviews are disabled on base/target branches other than the default branch.", "Auto reviews
    # are disabled on this repository.", "Auto reviews are limited based on label configuration.",
    # "Review was skipped as selected files did not have any reviewable changes.", and the free plan's
    # "This repository does not receive automatic reviews because it has fewer than 10 stars."
    NoticePattern(
        bot=_CODERABBIT,
        reason="skip",
        pattern=_coderabbit_block("skip review"),
        in_place=True,
        requires=re.compile(r"^(?:(?!couldn.t safely recover the incremental review).)*$", re.IGNORECASE | re.DOTALL),
    ),
    # The placeholder while a review runs ("Currently processing new changes in this PR. This may
    # take a few minutes, please wait...", or the review-in-progress animation). It names the range
    # under way, which is not a record: the review has not finished.
    NoticePattern(
        bot=_CODERABBIT,
        reason="in progress",
        pattern=_coderabbit_block("review in progress"),
        in_place=True,
    ),
    # The reply to a review command the limit refused ("Action not completed ... Review rate limited").
    NoticePattern(
        bot=_CODERABBIT,
        reason="limit",
        pattern=re.compile(
            r"Action not completed.{0,400}?(?:Review rate limited|included review limit is currently reached)",
            re.DOTALL | re.IGNORECASE,
        ),
        in_place=False,
    ),
    # Other bots' notices name no commit and are posted once per push.
    NoticePattern(
        bot="chatgpt-codex-connector[bot]",
        reason="limit",
        pattern=re.compile(r"You have reached your Codex usage limits for code reviews", re.IGNORECASE),
        in_place=False,
    ),
    NoticePattern(
        bot="qodo-code-review[bot]",
        reason="pause",
        pattern=re.compile(r"Qodo reviews are paused for this user", re.IGNORECASE),
        in_place=False,
    ),
    NoticePattern(
        bot="claude[bot]",
        reason="limit",
        pattern=re.compile(
            r"Code review skipped:\*\*\s*your organization has reached its API usage limit", re.IGNORECASE
        ),
        in_place=False,
    ),
)


@dataclass(frozen=True)
class Notice:
    """A notice of one bot that it did not review a push."""

    reason: str
    in_place: bool
    timestamp: str
    reviewed_sha: str | None
    unreviewed_sha: str | None
    text: str


def _clean(body: str) -> str:
    return _HTML_COMMENT_RE.sub("", body or "").strip()


def _event_time(e: dict) -> str:
    data = e.get("data") or {}
    return data.get("updated_at") or e.get("timestamp") or ""


def _match_notices(body: str, chatbot_username: str) -> list[tuple[NoticePattern, str]]:
    out = []
    for p in NOTICE_PATTERNS:
        if not same_github_actor(p.bot, chatbot_username):
            continue
        for m in p.pattern.finditer(body or ""):
            text = m.groupdict().get("block") or m.group(0)
            if p.requires is None or p.requires.search(text):
                out.append((p, text))
    return out


def find_notices(events: list[dict], chatbot_username: str) -> list[Notice]:
    """The bot's limit, pause and failure notices in its reviews and issue comments."""
    out = []
    for e in events:
        if e.get("event_type") not in ("review", "issue_comment"):
            continue
        if not same_github_actor(e.get("actor"), chatbot_username):
            continue
        data = e.get("data") or {}
        # An earlier revision of an edited comment is tied to its edit time, like a timed notice.
        revision = data.get("revision_of") is not None
        for p, text in _match_notices(data.get("body") or "", chatbot_username):
            reviewed = p.reviewed.search(text) if p.reviewed else None
            unreviewed = p.unreviewed.search(text) if p.unreviewed else None
            if unreviewed is None and e.get("event_type") == "review":
                unreviewed_sha = data.get("commit_id")
            else:
                unreviewed_sha = unreviewed.group(1) if unreviewed else None
            out.append(
                Notice(
                    reason=p.reason,
                    in_place=p.in_place and not revision,
                    timestamp=_event_time(e),
                    reviewed_sha=reviewed.group(1) if reviewed else None,
                    unreviewed_sha=unreviewed_sha,
                    text=_clean(text),
                )
            )
    return out


# Notice reasons that describe a review under way, not a push left unreviewed: never named in an
# exclusion's label.
TRANSIENT_REASONS: tuple[str, ...] = ("in progress",)


def notice_reasons(events: list[dict], chatbot_username: str) -> list[str]:
    """The reasons of the bot's notices that say a push was not reviewed, for an exclusion's label."""
    return sorted({n.reason for n in find_notices(events, chatbot_username)} - set(TRANSIENT_REASONS))


def sha_index(commits: list[dict], sha: str | None) -> int | None:
    """Index of a commit by full SHA or by a prefix of at least 7 characters."""
    if not sha or len(sha) < 7:
        return None
    for i, c in enumerate(commits):
        full = c.get("sha", "")
        if full == sha or full.startswith(sha):
            return i
    return None


_VERDICT_STATES = ("approved", "changes_requested", "dismissed")


def _is_completed_review(data: dict, chatbot_username: str) -> bool:
    """Whether a review event of the bot is a review it completed, not a notice it posted as one.

    A review whose body is a notice (a rate limit, a pause, a skip) is not. Otherwise a review
    submitted as approved or changes requested is, whatever its body: an approval with an empty
    body and no inline comment still read the commit. So is a dismissed one: only an approval or a
    change request can be dismissed, and dismissing it does not undo the reading. Any other review (commented, or with no
    recorded state) counts when its body has text: an empty commented review is how a reply in a
    review thread is posted, and its commit does not show that a review ran; the inline comments
    of a review that has them count on their own.
    """
    body = data.get("body") or ""
    if _match_notices(body, chatbot_username):
        return False
    return str(data.get("state") or "").lower() in _VERDICT_STATES or bool(_clean(body))


def _completed_reviews(events: list[dict], commits: list[dict], chatbot_username: str) -> list[tuple[int, str]]:
    """(commit index, time) of each review the bot completed.

    A review the bot submitted that is not a notice (``_is_completed_review``), or an inline
    comment that does not reply to a thread.
    """
    out = []
    for e in events:
        if not same_github_actor(e.get("actor"), chatbot_username):
            continue
        data = e.get("data") or {}
        etype = e.get("event_type")
        if etype == "review":
            if not _is_completed_review(data, chatbot_username):
                continue
            i = sha_index(commits, data.get("commit_id"))
        elif etype == "review_comment" and not _reply_to(data):
            i = sha_index(commits, data.get("original_commit_id") or data.get("commit_id"))
        else:
            continue
        if i is not None:
            out.append((i, e.get("timestamp") or ""))
    return out


# -- What the bot reviewed, from its own record --------------------------------

# CodeRabbit blocks that repeat a review's range for a review that did not run or has not finished.
_CR_UNFINISHED = re.compile(
    r"<!-- This is an auto-generated comment: (?P<kind>review in progress|rate limited|skip review|review paused"
    r"|failure) by coderabbit\.ai -->.*?<!-- end of auto-generated comment: (?P=kind) by coderabbit\.ai -->",
    re.DOTALL | re.IGNORECASE,
)


@dataclass(frozen=True)
class RecordPattern:
    """One format in which a bot records the commits a review read.

    pattern's ``head`` group is the commit the review read; its optional ``start`` group is the
    commit the review's diff started from, so the review covered the commits after ``start`` up to
    ``head``. Without ``start``, the review read the whole pull request at ``head``. ignore, when
    set, matches the parts of a body where the same text describes a review that did not run or has
    not finished; records inside them do not count. in_issue_comments means the bot writes this
    record in a PR conversation comment (often a status comment it rewrites on each review), so the
    record cannot be read for a PR whose conversation comments were not read. always_in_comment
    means that comment carries the record whenever it exists (Greptile's summary names its last
    reviewed commit), so a comment without it is a missing record; CodeRabbit's summary carries a
    range only for some reviews (one that posted nothing), so for it only a missing comment is.
    """

    bot: str
    pattern: re.Pattern[str]
    ignore: re.Pattern[str] | None = None
    in_issue_comments: bool = False
    always_in_comment: bool = False


RECORD_PATTERNS: tuple[RecordPattern, ...] = (
    # "📥 Commits: Reviewing files that changed from the base of the PR and between X and Y." It is in
    # each review's body, and in the summary comment's "recent review" block, which a review that
    # posted nothing also rewrites. The first review's X is the PR's base, not one of its commits.
    RecordPattern(
        bot=_CODERABBIT,
        pattern=re.compile(
            r"Reviewing files that changed from the base of the PR and between "
            r"(?P<start>[0-9a-f]{7,40}) and (?P<head>[0-9a-f]{7,40})"
        ),
        ignore=_CR_UNFINISHED,
        in_issue_comments=True,
    ),
    # The summary comment's "Last reviewed commit: [<subject>](https://github.com/<o>/<r>/commit/<sha>)".
    RecordPattern(
        bot="greptile-apps[bot]",
        pattern=re.compile(
            r"Last reviewed commit: \[[^\n]*?\]\(https://github\.com/[^/\s]+/[^/\s]+/commit/(?P<head>[0-9a-f]{7,40})\)"
        ),
        in_issue_comments=True,
        always_in_comment=True,
    ),
    # The review summary's state marker, and the "Reviewed commit" line of a review.
    RecordPattern(
        bot="chatgpt-codex-connector[bot]",
        pattern=re.compile(r'codex-review-state:v1 \{"code_review":\{"commit_sha":"(?P<head>[0-9a-f]{7,40})"'),
        in_issue_comments=True,
    ),
    RecordPattern(
        bot="chatgpt-codex-connector[bot]",
        pattern=re.compile(r"\*\*Reviewed commit:\*\* `(?P<head>[0-9a-f]{7,40})`"),
    ),
    # The "Codex Review Summary" status comment, rewritten on each review: a completed review's row,
    # "| 📝 **Code Review** | ✅ **Completed** <time> | `21c5be8` | New commits |". A review that
    # found nothing posts no review, and the row is its only record. A running row is not one.
    RecordPattern(
        bot="chatgpt-codex-connector[bot]",
        pattern=re.compile(r"\| 📝 \*\*Code Review\*\* \| ✅ \*\*Completed\*\*[^|\n]*\| `(?P<head>[0-9a-f]{7,40})`"),
        in_issue_comments=True,
    ),
)


# Bots that post a review, or re-review, as a PR conversation comment (Claude posts each as a new
# comment, Qodo edits a persistent one), which names no commit the analysis can place.
REVIEWS_IN_ISSUE_COMMENTS: tuple[str, ...] = ("claude[bot]", "qodo-code-review[bot]")


def _listed_comment_reviewer(chatbot_username: str) -> bool:
    return any(same_github_actor(b, chatbot_username) for b in REVIEWS_IN_ISSUE_COMMENTS)


def _unlisted_comment_reviewer(chatbot_username: str) -> bool:
    """Whether the bot is in none of the tables: no conversation-review entry, no comment record, no
    CodeRabbit review content. Its conversation comment with text that is not a notice is a review
    (``_comment_review``), so it may post reviews that way (cubic, Gemini, ...)."""
    return not (
        _listed_comment_reviewer(chatbot_username)
        or record_in_issue_comments(chatbot_username)
        or any(same_github_actor(bot, chatbot_username) for bot, _ in COMMENT_REVIEW_CONTENT)
    )


def reviews_in_issue_comments(chatbot_username: str) -> bool:
    """Whether the bot may post its reviews as PR conversation comments: listed in
    ``REVIEWS_IN_ISSUE_COMMENTS`` (Claude, Qodo), or in none of the tables (``_unlisted_comment_reviewer``:
    cubic, Gemini, Copilot, ...)."""
    return _listed_comment_reviewer(chatbot_username) or _unlisted_comment_reviewer(chatbot_username)


def later_comment_review(events: list[dict], chatbot_username: str) -> bool:
    """Whether a bot that posts reviews as conversation comments has a conversation comment, or a
    revision of one, after its first review: a review it cannot place, so the PR leaves no fix out.
    For a bot in none of the tables, only a comment with text that is not a notice counts, as in
    ``_comment_review``.

    A comment's current body was last written at its ``updated_at``, not its creation time: Qodo
    re-reviews by editing its comment, and the revision matching the current body is not among the
    revisions (``_with_comment_revisions`` leaves it out as the current body's duplicate), so the
    edit shows only there. The earliest event still counts from its creation time.
    """
    if not reviews_in_issue_comments(chatbot_username):
        return False
    listed = _listed_comment_reviewer(chatbot_username)
    mine = [e for e in events if same_github_actor(e.get("actor"), chatbot_username)]
    times = [
        e.get("timestamp") or "" for e in mine if e.get("event_type") in ("review", "review_comment", "issue_comment")
    ]
    if not times:
        return False
    first = min(times)
    return any(
        e.get("event_type") == "issue_comment"
        and max(e.get("timestamp") or "", (e.get("data") or {}).get("updated_at") or "") > first
        and (listed or _is_comment_review_text((e.get("data") or {}).get("body") or "", chatbot_username))
        for e in mine
    )


def _is_comment_review_text(body: str, chatbot_username: str) -> bool:
    return bool(_clean(body)) and not _match_notices(body, chatbot_username)


def record_in_issue_comments(chatbot_username: str) -> bool:
    """Whether the bot writes any of its record in PR conversation comments (``RECORD_PATTERNS``)."""
    return any(p.in_issue_comments and same_github_actor(p.bot, chatbot_username) for p in RECORD_PATTERNS)


# The marker of the comment a bot keeps its record in, whatever state it is in. CodeRabbit's chat
# replies carry another marker ("auto-generated reply by CodeRabbit").
STATUS_COMMENT_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (_CODERABBIT, re.compile(r"<!-- This is an auto-generated comment: summarize by coderabbit\.ai -->")),
)


def _status_comment(body: str, patterns: list[RecordPattern], chatbot_username: str) -> bool:
    """Whether a conversation comment of the bot is the one it keeps its record in: it carries the
    bot's summary marker (``STATUS_COMMENT_MARKERS``), a record (naming any commit), an in-place notice, or
    review content (``COMMENT_REVIEW_CONTENT``)."""
    markers = STATUS_COMMENT_MARKERS + COMMENT_REVIEW_CONTENT
    if any(same_github_actor(bot, chatbot_username) and p.search(body) for bot, p in markers):
        return True
    if any(p.pattern.search(p.ignore.sub("", body) if p.ignore else body) for p in patterns):
        return True
    # A notice the bot writes into that comment (in_place); a reply notice, such as a review command
    # refused by the limit, is a comment of its own and does not show the summary is there.
    return any(p.in_place for p, _ in _match_notices(body, chatbot_username))


def comment_record_missing(events: list[dict], commits: list[dict], chatbot_username: str) -> bool:
    """Whether a bot that writes its record in conversation comments completed a review on the PR
    but the comment record of it is missing.

    A review here is a completed review (``_completed_reviews``) or a conversation comment of the
    bot that is not a notice (``_comment_review``): a Greptile review that found nothing is only its
    summary comment.

    A record in a review body (CodeRabbit and Codex write one there too) covers that review alone,
    while a review that posted nothing is recorded only in the comment, so only the comment record
    counts. It is missing when no comment names a commit of the PR in an ``in_issue_comments`` row
    and either the bot has no conversation comment on the PR at all (current body or earlier
    revision: the comment was deleted, or never among those read), or its comment always carries
    the record (``always_in_comment``), or no record of the bot is found anywhere on the PR (its
    reviews then cannot be told apart from silent ones).
    """
    patterns = [p for p in RECORD_PATTERNS if p.in_issue_comments and same_github_actor(p.bot, chatbot_username)]
    if not patterns:
        return False
    if not (_completed_reviews(events, commits, chatbot_username) or _comment_review(events, chatbot_username)):
        return False
    bodies = [
        (e.get("data") or {}).get("body") or ""
        for e in events
        if e.get("event_type") == "issue_comment" and same_github_actor(e.get("actor"), chatbot_username)
    ]
    for body in bodies:
        for p in patterns:
            for m in p.pattern.finditer(p.ignore.sub("", body) if p.ignore else body):
                if sha_index(commits, m.group("head")) is not None:
                    return False
    # Only the comment that holds the record shows it is still there: a chat reply or a command
    # acknowledgement of the bot does not stand in for a deleted summary.
    status = [b for b in bodies if _status_comment(b, patterns, chatbot_username)]
    if not status or any(p.always_in_comment for p in patterns):
        return True
    return not any(r.source == "record" for r in find_review_records(events, commits, chatbot_username))


@dataclass(frozen=True)
class ReviewRecord:
    """The commits one review of the bot read, as indices into the PR's commits.

    The review covered the commits after start up to head; start is -1 when it read the pull
    request from its base. source is "record" (the bot's own record), "record-start" (the commit a
    record's range starts from, which the bot names there as its last reviewed commit), "review" (a
    completed review with no record) or "notice" (the last reviewed commit a notice names); the last
    three are read as a review of the whole pull request at that commit.
    """

    start: int
    head: int
    source: str


def find_review_records(events: list[dict], commits: list[dict], chatbot_username: str) -> list[ReviewRecord]:
    """Every review of the bot that the bot's record, its completed reviews or its notices show.

    The record is read from the bot's reviews and from its comments, current bodies and earlier
    revisions alike. A record naming a head missing from the commit list (a rebased push) is dropped;
    a start missing from it (the PR's base, or an older push) is read as the base. The start X of a
    range "between X and Y" is the commit the bot last reviewed, as its limit notice reads the same
    sentence, so it adds a review at X ("record-start") where no record has X as its head: a review
    of X whose own record was not read (a revision GitHub no longer keeps) still counts. A completed
    review or a notice adds a review only at a commit that no record already shows reviewed.
    """
    out: set[ReviewRecord] = set()
    patterns = [p for p in RECORD_PATTERNS if same_github_actor(p.bot, chatbot_username)]
    for e in events:
        if e.get("event_type") not in ("review", "issue_comment"):
            continue
        if not same_github_actor(e.get("actor"), chatbot_username):
            continue
        body = (e.get("data") or {}).get("body") or ""
        for p in patterns:
            for m in p.pattern.finditer(p.ignore.sub("", body) if p.ignore else body):
                head = sha_index(commits, m.group("head"))
                if head is None:
                    continue
                start = sha_index(commits, m.groupdict().get("start"))
                out.add(ReviewRecord(start=-1 if start is None or start >= head else start, head=head, source="record"))
    heads = {r.head for r in out}
    fallback = [(r.start, "record-start") for r in sorted(out, key=lambda r: (r.head, r.start)) if r.start >= 0]
    fallback += [(i, "review") for i, _ in _completed_reviews(events, commits, chatbot_username)]
    fallback += [(sha_index(commits, n.reviewed_sha), "notice") for n in find_notices(events, chatbot_username)]
    for i, source in fallback:
        if i is not None and i not in heads:
            out.add(ReviewRecord(start=-1, head=i, source=source))
            heads.add(i)
    return sorted(out, key=lambda r: (r.head, r.start, r.source))


# What shows that a conversation comment of the bot is a review, for a bot whose comments are
# mostly not reviews. CodeRabbit's summary comment is a placeholder or a notice until a review runs
# (the in-progress placeholder, a limit or skip block), and it also posts chat replies and command
# acknowledgements: its comment is a review only with a record (``RECORD_PATTERNS``) or the review
# content its summary carries, the walkthrough ("<!-- walkthrough_start -->" then a
# "Walkthrough" or "Summary" block, or a "## Walkthrough" heading), the changes table ("## Changes")
# or the recent-review block. A bot not listed has its review only in such a comment (a Greptile
# summary, a Codex status comment, a Claude or Qodo review): any comment with text that is not a
# notice counts.
COMMENT_REVIEW_CONTENT: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        _CODERABBIT,
        re.compile(
            r"<!-- walkthrough_start -->\s*<details>\s*<summary>(?:<strong>)?[^<\w]*(?:Walkthrough|Summary)\b"
            r"|^## Walkthrough\b|^## Changes\b|<!-- recent_review_start -->",
            re.MULTILINE,
        ),
    ),
)


def _comment_review(events: list[dict], chatbot_username: str) -> bool:
    """Whether the bot posted a conversation comment, or a revision of one, that is a review: for a
    bot in ``COMMENT_REVIEW_CONTENT`` (CodeRabbit), one with a record or review content; for every
    other bot, listed in the tables or not (Greptile, Codex, Claude, Qodo, cubic, Gemini, ...), one
    with text that is not a notice. Its review may be that comment alone, record or no record: a
    bot the tables do not know is never taken to have posted only notices."""
    content = [p for bot, p in COMMENT_REVIEW_CONTENT if same_github_actor(bot, chatbot_username)]
    records = [p for p in RECORD_PATTERNS if p.in_issue_comments and same_github_actor(p.bot, chatbot_username)]
    for e in events:
        if e.get("event_type") != "issue_comment" or not same_github_actor(e.get("actor"), chatbot_username):
            continue
        body = (e.get("data") or {}).get("body") or ""
        if content:
            if any(p.search(body) for p in content) or any(
                p.pattern.search(p.ignore.sub("", body) if p.ignore else body) for p in records
            ):
                return True
        elif _clean(body) and not _match_notices(body, chatbot_username):
            return True
    return False


def completed_no_review(events: list[dict], commits: list[dict], chatbot_username: str) -> bool:
    """Whether the bot completed no review on the PR: it posted only notices (a rate limit, a skip).

    A review counts when the bot submitted one that is not a notice (``_is_completed_review``), left
    an inline comment that does not reply to a thread, or, for a bot that posts its reviews or its
    record as conversation comments, posted such a comment with text that is not a notice
    (``_comment_review``). A record or notice that shows a review (``find_review_records``), or names a
    commit since replaced (``rewritten_review_commits``), counts too. Wherever the review's commit
    is, placed or not.
    """
    for e in events:
        if not same_github_actor(e.get("actor"), chatbot_username):
            continue
        data = e.get("data") or {}
        etype = e.get("event_type")
        if etype == "review" and _is_completed_review(data, chatbot_username):
            return False
        if etype == "review_comment" and not _reply_to(data):
            return False
    if _comment_review(events, chatbot_username):
        return False
    return not (
        find_review_records(events, commits, chatbot_username)
        or rewritten_review_commits(events, commits, chatbot_username)
    )


# -- Files a review left out by configuration -------------------------------------


@dataclass(frozen=True)
class FileScopePattern:
    """The headings under which a bot's review body lists the files it left out by the repository's
    path filters and the files it read. Each list is a ``<details>`` block of `` * `path` `` items."""

    bot: str
    ignored: str
    selected: str


FILE_SCOPE_PATTERNS: tuple[FileScopePattern, ...] = (
    # CodeRabbit's "Review details": "⛔ Files ignored due to path filters (N)", each item
    # "`path` is excluded by `!**/*.lock`", and "📒 Files selected for processing (N)".
    FileScopePattern(
        bot=_CODERABBIT, ignored="Files ignored due to path filters", selected="Files selected for processing"
    ),
)

# A block's items run to its </details> or the next nested <summary>: the file lists sit inside an
# outer "Review details" block, which must not swallow the first of them.
_DETAILS_RE = re.compile(r"<summary>(?P<heading>[^<]*)</summary>(?P<body>(?:(?!<summary>|</details>).)*)", re.DOTALL)
_FILE_ITEM_RE = re.compile(r"^\s*[*-]\s*`([^`]+)`", re.MULTILINE)


def _norm_path(path: str) -> str:
    return path.strip().removeprefix("./").lstrip("/")


def path_filtered(path: str, events: list[dict], chatbot_username: str) -> bool:
    """Whether every review of the bot that lists the file lists it as left out by the repository's
    path filters (``FILE_SCOPE_PATTERNS``), and at least one does.

    A path filter is the repository's configuration, so the bot never read the file. A review lists
    only the files that changed in the range it read, so the review that shows the filter is often
    the review of the fix itself: the filter applied before it too. A review that lists the file
    among those it read, at any time, means the file was read, or the configuration changed: the
    fix counts. So does an inline comment of the bot on the file that does not reply to a thread:
    the bot reviewed it. So does the file listed as selected in a conversation comment of the bot or
    a revision of one: a review that posted nothing records its files only in the summary. A review
    that does not list the file (an incremental review of other files) says nothing.
    """
    patterns = [p for p in FILE_SCOPE_PATTERNS if same_github_actor(p.bot, chatbot_username)]
    target = _norm_path(path)
    if not patterns or not target:
        return False
    ignored = False
    for e in events:
        if not same_github_actor(e.get("actor"), chatbot_username):
            continue
        data = e.get("data") or {}
        if e.get("event_type") == "review_comment" and not _reply_to(data):
            if _norm_path(data.get("path") or "") == target:
                return False
            continue
        if e.get("event_type") not in ("review", "issue_comment"):
            continue
        comment = e.get("event_type") == "issue_comment"
        for m in _DETAILS_RE.finditer((e.get("data") or {}).get("body") or ""):
            listed = {_norm_path(f) for f in _FILE_ITEM_RE.findall(m.group("body"))}
            if target not in listed:
                continue
            for p in patterns:
                if p.selected in m.group("heading"):
                    return False
                # A conversation comment or a revision of one (the summary) can show the file was
                # read by a review that posted nothing; it is not taken as showing a filter.
                if p.ignored in m.group("heading") and not comment:
                    ignored = True
    return ignored


NO_COMMIT = "(no commit)"  # what rewritten_review_commits lists for a completed review with no commit


def rewritten_review_commits(events: list[dict], commits: list[dict], chatbot_username: str) -> list[str]:
    """The commits the bot reviewed or named that are no longer in the PR's commit list.

    A force-push (an amend or a rebase) replaces commits, and a review, record or notice naming a
    replaced commit cannot be placed: the rewritten commit then reads as never reviewed although its
    code was. The commits checked are the commits of the completed reviews and inline comments
    (``_completed_reviews``), each record's head, and each notice's last reviewed commit when the
    notice names it as such ("Last reviewed commit"). The start X of a range "between X and Y" is
    not checked: for a first review it is the PR's base, which is not one of its commits. A completed
    review with no commit (GitHub nulls it once the reviewed commit is gone) is listed as
    ``NO_COMMIT``.
    """
    named: list[str | None] = []
    patterns = [p for p in RECORD_PATTERNS if same_github_actor(p.bot, chatbot_username)]
    for e in events:
        if not same_github_actor(e.get("actor"), chatbot_username):
            continue
        data = e.get("data") or {}
        etype = e.get("event_type")
        if etype == "review" and _is_completed_review(data, chatbot_username):
            # GitHub nulls a review's commit once the reviewed commit object is gone (a force-push):
            # the review ran but cannot be placed. So is a GH Archive review event that no API review
            # filled in (the analysis copies the API's commit onto it by review id).
            named.append(data.get("commit_id") or NO_COMMIT)
        elif etype == "review_comment" and not _reply_to(data):
            # An inline comment of a review whose commit the answer gave as null (gone after a
            # force-push) is a review that cannot be placed. So is one read without its commit (a
            # review thread stored before the threads query asked for it): its review ran at a
            # commit that cannot be told, and dropping it would drop the review (a Greptile review
            # has an empty body and is placed by its inline comments alone).
            named.append(data.get("original_commit_id") or data.get("commit_id") or NO_COMMIT)
        if etype in ("review", "issue_comment"):
            body = data.get("body") or ""
            for p in patterns:
                named += [m.group("head") for m in p.pattern.finditer(p.ignore.sub("", body) if p.ignore else body)]
    for n in find_notices(events, chatbot_username):
        if n.reviewed_sha and not re.search(rf"between {n.reviewed_sha}\b", n.text):
            named.append(n.reviewed_sha)
    return sorted({s for s in named if s == NO_COMMIT or (s and len(s) >= 7 and sha_index(commits, s) is None)})


def reviewed_before(commit: int, fix: int, records: list[ReviewRecord]) -> bool:
    """Whether a review of a commit before ``fix`` covered ``commit``."""
    return any(r.start < commit <= r.head < fix for r in records)


# -- Where the code a fix changed was first written ----------------------------

_EDIT_RATIO = 0.8
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class _Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: tuple[tuple[str, str], ...]  # (" ", "-" or "+", the line stripped)


def _hunks(patch: str) -> list[_Hunk] | None:
    """The hunks of a unified diff, or None when a hunk's lines do not add up to its header."""
    out: list[_Hunk] = []
    header: tuple[int, int, int, int] | None = None
    lines: list[tuple[str, str]] = []

    def close() -> bool:
        if header is None:
            return True
        old = sum(1 for k, _ in lines if k != "+")
        new = sum(1 for k, _ in lines if k != "-")
        if (old, new) != (header[1], header[3]):
            return False
        out.append(_Hunk(*header, tuple(lines)))
        return True

    for raw in (patch or "").splitlines():
        if raw.startswith("@@"):
            m = _HUNK_RE.match(raw)
            if m is None or not close():
                return None
            a, b, c, d = m.groups()
            header = (int(a), 1 if b is None else int(b), int(c), 1 if d is None else int(d))
            lines = []
        elif raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        elif header is not None and raw[:1] in (" ", "-", "+"):
            lines.append((raw[:1], raw[1:].strip()))
        elif header is not None and raw == "":
            lines.append((" ", ""))
        else:
            return None
    return out if close() else None


def _file_hunks(f: dict) -> list[_Hunk] | None:
    """The hunks of a commit file entry's patch, or None when the patch is missing, does not add up,
    or is cut short: GitHub truncates a large patch between whole hunks, which then parse, so the
    lines they add and remove are checked against the entry's own additions and deletions."""
    hunks = _hunks(f.get("patch") or "") if f.get("patch") else None
    if hunks is None:
        return None
    for key, kind in (("additions", "+"), ("deletions", "-")):
        total = f.get(key)
        if isinstance(total, int) and total != sum(1 for h in hunks for k, _ in h.lines if k == kind):
            return None
    return hunks


def _removed_all(h: _Hunk) -> list[tuple[int, str]]:
    """(line number before the change, text) of each line a hunk removed."""
    out = []
    old = h.old_start
    for kind, text in h.lines:
        if kind == "-":
            out.append((old, text))
        if kind != "+":
            old += 1
    return out


def _removed_by_block(h: _Hunk) -> list[list[tuple[int, str]]]:
    """The ``_removed_all`` lines of a hunk by change block (the runs of removed and added lines
    between unchanged ones), indexed by block number: an unchanged line starts a new block."""
    out: list[list[tuple[int, str]]] = [[]]
    old = h.old_start
    for kind, text in h.lines:
        if kind == " ":
            out.append([])
        elif kind == "-":
            out[-1].append((old, text))
        if kind != "+":
            old += 1
    return out


# A letter or digit in any script: a line of identifiers such as `总数 += 值` carries code.
_CODE_RE = re.compile(r"[^\W_]")


def _removed(hunks: list[_Hunk]) -> list[tuple[int, str]]:
    """(line number before the change, text) of each removed line that carries code.

    Every line with a letter or a digit counts, however short (``x = 0``). A line of punctuation
    only (``}``, ``);``) is left out: which of several such lines a diff pairs with an older one is
    arbitrary, so its position says nothing about where the code around it was written.
    """
    return [(n, t) for h in hunks for n, t in _removed_all(h) if _CODE_RE.search(t)]


_DIFF_CONTEXT = 3  # the unchanged lines GitHub's patches carry around each change


def _blocks(h: _Hunk) -> list[tuple[bool, list[tuple[int, str]]]]:
    """Each change block of a hunk (a run of removed and added lines between unchanged ones).

    For each block: whether it must be placed by the code where it is (an empty list of that code
    then makes the fix's origin unknown), and that code, as (line number before the change, text).
    A block that changes no existing code line (it adds lines, removes
    only punctuation, or both) is, on each side on its own, by the unchanged lines that carry code
    in the run of unchanged lines next to it, or, when that run carries none (only closing braces),
    the nearest unchanged code line on that side within the hunk: a punctuation line it removes (a
    stray `;`) belongs to that code. When one side has no code line in the hunk, the code is empty
    and the fix's origin is unknown, unless the file itself has nothing on that side (the hunk
    starts at its first line, or ends with fewer unchanged lines than a diff's context, so at its
    last). A block that removes code is judged by the lines it removes, whatever it adds: its code
    lines, and the punctuation lines among them (``_punctuation_removed``), traced themselves.
    """
    rows: list[tuple[str, int, str]] = []
    old = h.old_start
    for kind, text in h.lines:
        rows.append((kind, old, text))
        if kind != "+":
            old += 1

    def code(part: list[tuple[str, int, str]]) -> list[tuple[int, str]]:
        return [(n, t) for k, n, t in part if k == " " and _CODE_RE.search(t)]

    out = []
    i = 0
    while i < len(rows):
        if rows[i][0] == " ":
            i += 1
            continue
        j = i
        while j < len(rows) and rows[j][0] != " ":
            j += 1
        a, b = i, j
        while a > 0 and rows[a - 1][0] == " ":
            a -= 1
        while b < len(rows) and rows[b][0] == " ":
            b += 1
        if any(k == "-" and _CODE_RE.search(t) for k, _, t in rows[i:j]):
            # Its code lines, and any punctuation line it also removes (``_punctuation_removed``),
            # are traced themselves.
            out.append((False, []))
        else:
            # Each side on its own: its unchanged run, extended to the nearest code line in the hunk
            # when the run holds only punctuation (closing braces). A side with no code line in the
            # hunk leaves the block's place unknown, unless the file has nothing on that side: the
            # hunk starts at the first line, or ends with fewer unchanged lines than a diff's context.
            before = code(rows[a:i]) or code(rows[:i])[-1:]
            after = code(rows[j:b]) or code(rows[j:])[:1]
            file_start = not before and h.old_start <= 1 and i - a == i
            file_end = not after and b == len(rows) and b - j < _DIFF_CONTEXT
            known = (before or file_start) and (after or file_end) and (before or after)
            out.append((True, before + after if known else []))
        i = j
    return out


def _punctuation_removed(hunks: list[_Hunk]) -> list[tuple[int, str]]:
    """(line number before the change, text) of each punctuation-only line removed in a change block
    that also removes code: it is part of what the block changes, a stray `;` as much as a line of
    code, so it is traced like one."""
    out = []
    for h in hunks:
        old = h.old_start
        block: list[tuple[str, int, str]] = []
        for kind, text in [*h.lines, (" ", "")]:
            if kind != " ":
                block.append((kind, old, text))
            else:
                if any(k == "-" and _CODE_RE.search(t) for k, _, t in block):
                    out += [(n, t) for k, n, t in block if k == "-" and not _CODE_RE.search(t)]
                block = []
            if kind != "+":
                old += 1
    return out


def _trace(hunks: list[_Hunk], line: int, text: str) -> tuple[str, int, str]:
    """Where line ``line`` (of text ``text``) after a commit came from in that commit.

    Returns ("added", 0, "") when the commit wrote it, ("kept", n, text) when it is line n before
    the commit, unchanged, ("edited", n, removed text) when it is a light edit of the removed line n
    of its change block (``_EDIT_RATIO``), and ("mismatch", 0, "") when the line at that position is not the
    line traced.
    """
    offset = 0
    for h in hunks:
        before = line <= h.new_start if h.new_len == 0 else line < h.new_start
        if before:
            return "kept", line - offset, text
        if line < h.new_start + h.new_len:
            # A light edit is matched only within its own change block: a more similar line removed
            # elsewhere in the hunk, in another function say, is not its predecessor.
            removed_by_block = _removed_by_block(h)
            block = 0
            old, new = h.old_start, h.new_start
            for kind, t in h.lines:
                if kind == " ":
                    block += 1
                if kind == "-":
                    old += 1
                    continue
                if new == line:
                    if t != text:
                        return "mismatch", 0, ""
                    if kind == " ":
                        return "kept", old, text
                    if not _CODE_RE.search(t):
                        # Which brace a diff pairs with an older one is arbitrary: an added punctuation
                        # line is the commit's own, never a light edit.
                        return "added", 0, ""
                    removed = removed_by_block[block]
                    best = max(removed, key=lambda r: SequenceMatcher(None, r[1], t).ratio(), default=None)
                    if best is not None and SequenceMatcher(None, best[1], t).ratio() >= _EDIT_RATIO:
                        return "edited", best[0], best[1]
                    return "added", 0, ""
                new += 1
                if kind == " ":
                    old += 1
        offset += h.new_len - h.old_len
    return "kept", line - offset, text


_MOVE_SPAN = 10  # the most consecutive lines a moved or re-wrapped line is compared as one
_WS_RE = re.compile(r"\s+")
_TRAILING_COMMA_RE = re.compile(r",(?=[)\]}]|$)")


def _ws(text: str) -> str:
    """The text as moved or re-wrapped code is compared: all whitespace removed, and the trailing
    commas a formatter adds when it wraps a call or a literal (before a closing bracket, or at the
    end) dropped."""
    return _TRAILING_COMMA_RE.sub("", _WS_RE.sub("", text))


def _span_keys(run: list[str], at: int | None = None) -> set[str]:
    """The text, with all whitespace removed, of each run of up to ``_MOVE_SPAN`` consecutive lines
    of ``run`` (those containing index ``at`` when it is given) that carries code, and of the whole
    run when it is longer: a statement re-wrapped over more lines than that is still matched whole."""
    keys = set()
    if len(run) > _MOVE_SPAN:
        whole = _ws("".join(run))
        if _CODE_RE.search(whole):
            keys.add(whole)
    for a in range(len(run)):
        for b in range(a + 1, min(len(run), a + _MOVE_SPAN) + 1):
            if at is not None and not a <= at < b:
                continue
            key = _ws("".join(run[a:b]))
            if _CODE_RE.search(key):
                keys.add(key)
    return keys


def _runs(h: _Hunk, kind: str, skip: frozenset[int] = frozenset()) -> list[list[str]]:
    """The texts of each run of consecutive lines of one kind ("-" or "+") in a hunk.

    A removed line whose number before the change is in ``skip`` is left out and ends its run.
    """
    out: list[list[str]] = []
    run: list[str] = []
    old = h.old_start
    for k, text in h.lines:
        if k == kind and not (k == "-" and old in skip):
            run.append(text)
        else:
            if run:
                out.append(run)
            run = []
        if k != "+":
            old += 1
    if run:
        out.append(run)
    return out


ALL_LINES: frozenset[int] = frozenset({-1})  # a ``skip`` that leaves out the whole of ``skip_path``


def _loose(text: str) -> str:
    """``_ws`` loosened to no whitespace and no comma at all: the loosened ``_ws`` key of some lines
    is a substring of the loosened text of any run that holds those lines."""
    return _WS_RE.sub("", text).replace(",", "")


class _RemovedRuns:
    """The removed runs of one file's patch. A run's ``_span_keys`` are built only when its loosened
    text holds a looked-for key (a cheap test that every hit passes), so a large rewritten file (a
    lockfile) costs one substring scan per look, not every window of every line."""

    def __init__(self, runs: list[list[str]]) -> None:
        self.runs = runs
        self.loose = [_loose("".join(run)) for run in runs]
        self.keys: dict[int, set[str]] = {}

    def hits(self, keys: set[str], loose: list[str]) -> bool:
        for i, text in enumerate(self.loose):
            if not any(k in text for k in loose):
                continue
            if i not in self.keys:
                self.keys[i] = _span_keys(self.runs[i])
            if self.keys[i] & keys:
                return True
        return False


class _RemovedCode:
    """The removed runs of a commit, in every file (``_removed_keys``)."""

    def __init__(self, files: list[_RemovedRuns]) -> None:
        self.files = files

    def hits(self, keys: set[str]) -> bool:
        """Whether a run of removed lines carries one of ``keys`` (``_span_keys`` of added lines)."""
        if not keys:
            return False
        loose = [_loose(k) for k in keys]
        return any(f.hits(keys, loose) for f in self.files)


def _removed_keys(
    detail: dict, skip_path: str | None = None, skip: frozenset[int] = frozenset()
) -> tuple[_RemovedCode, bool]:
    """Every run of removed lines a commit's patches hold, in any file, as a ``_RemovedCode`` that
    tells whether one carries given ``_span_keys``, and whether they are all of them: False when a
    file the commit removed lines from has no patch, or one whose hunks do not add up (GitHub leaves
    out the patch of a large diff). In file ``skip_path``, the removed lines numbered in ``skip``
    (before the change) are left out."""
    files: list[_RemovedRuns] = []
    complete = True
    for f in detail.get("files") or []:
        hunks = _file_hunks(f)
        if hunks is None:
            if f.get("deletions") or (f.get("patch") and f.get("deletions") is None):
                complete = False
            continue
        lines = skip if f.get("filename") == skip_path else frozenset()
        if lines is ALL_LINES:
            continue
        files.append(_removed_runs(f.get("patch") or "", hunks, lines))
    return _RemovedCode(files), complete


# The removed runs of one file's patch, shared by every action, file and traced line of the
# analyses in a process. Bounded: cleared when it grows past _REMOVED_RUNS_CACHE_SIZE patches.
_REMOVED_RUNS_CACHE: dict[tuple[str, frozenset[int]], _RemovedRuns] = {}
_REMOVED_RUNS_CACHE_SIZE = 512


def _removed_runs(patch: str, hunks: list[_Hunk], skip: frozenset[int]) -> _RemovedRuns:
    key = (patch, skip)
    cached = _REMOVED_RUNS_CACHE.get(key)
    if cached is None:
        if len(_REMOVED_RUNS_CACHE) >= _REMOVED_RUNS_CACHE_SIZE:
            _REMOVED_RUNS_CACHE.clear()
        cached = _RemovedRuns([run for h in hunks for run in _runs(h, "-", skip)])
        _REMOVED_RUNS_CACHE[key] = cached
    return cached


def _added_keys(hunks: list[_Hunk], line: int) -> set[str]:
    """``_span_keys`` of the run of added lines that holds line ``line`` after the commit, over the
    runs that include that line."""
    for h in hunks:
        if not h.new_start <= line < h.new_start + h.new_len:
            continue
        new = h.new_start
        run: list[str] = []
        at = None
        for kind, text in h.lines:
            if kind == "+":
                if new == line:
                    at = len(run)
                run.append(text)
            elif run:
                if at is not None:
                    break
                run = []
            if kind != "-":
                new += 1
        return _span_keys(run, at) if at is not None else set()
    return set()


def _file_entry(detail: dict | None, path: str) -> dict | None:
    for f in (detail or {}).get("files") or []:
        if f.get("filename") == path:
            return f
    return None


# Docs are told by extension, tests by test-file naming and test folders, generated files by
# specific markers. A folder named doc/, docs/, spec/ or specs/ does not make code in it a doc or a
# test (Go's go/doc package, a spec/ validator), nor does "generated" in a file's name.
_NOT_SOURCE_RE = re.compile(
    r"(^|/)(tests?|__tests__)/"  # test folders
    r"|(_test\.|\.test\.|\.spec\.|_spec\.|(^|/)test_[^/]*$)"  # test files
    r"|\.(md|mdx|rst|txt|adoc)$"  # docs
    r"|\.(png|jpe?g|gif|webp|ico|svg|pdf|log)$"  # images, documents and logs
    r"|(^|/)\.github/"  # CI
    r"|\.(ya?ml|jsonc?|json5|toml|lock)$"  # config and lock files
    r"|(^|/)(go\.sum|package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock|Gemfile\.lock)$"
    r"|\.generated\.|_generated\.go$|\.pb\.go$|(^|/)generated/",  # generated files
    re.IGNORECASE,
)


def _is_source(path: str) -> bool:
    """Whether a file is source code, for judging the other files of a fixing commit: not a test, a
    doc, a lock or generated file, or CI and config (.github/, YAML, JSON, TOML)."""
    return not _NOT_SOURCE_RE.search(path or "")


def fix_code_origin(
    action: dict, commits: list[dict], details_by_sha: dict[str, dict], records: list[ReviewRecord], first_reviewed: int
) -> tuple[str, str, tuple[str, ...]]:
    """Whether a review of the bot read the code a fix after its first review changed.

    The action's file is judged first (``_origin_in_file``). A fix commit's change spans all its
    files, so an "unreviewed" answer holds only when every other source file the commit touches
    (``_is_source``: not a test, a doc, a lock or generated file, or CI and config) is wholly in
    unreviewed code too: the lines it changes and the code its insertions go into, judged the same
    way (a new file changes no reviewed code; a file with lines but no readable patch, or a rename,
    cannot be judged). Otherwise the fix is "unknown" and kept. The side edits a fix commit makes
    to tests, docs and config are not taken for a fix of reviewed code.
    """
    origin, reason, shas = _origin_in_file(action, commits, details_by_sha, records, first_reviewed)
    if origin != "unreviewed":
        return origin, reason, shas
    fix = sha_index(commits, (action.get("commit_sha") or "").strip())
    others = [
        f.get("filename")
        for f in (details_by_sha.get(commits[fix].get("sha", "")) or {}).get("files") or []
        if f.get("filename") != action.get("file_path") and _is_source(f.get("filename"))
    ]
    for other in others:
        o, why, more = _origin_in_file(
            {**action, "file_path": other}, commits, details_by_sha, records, first_reviewed, True
        )
        if o != "unreviewed":
            return "unknown", f"the fixing commit also changes {other}, not wholly in unreviewed code: {why}", ()
        shas = tuple(sorted(set(shas) | set(more), key=lambda s: sha_index(commits, s) or 0))
    return origin, reason, shas


def renamed_since(path: str, commits: list[dict], details_by_sha: dict[str, dict], first: int, fix: int | None) -> bool:
    """Whether a commit from ``first`` up to the fix (every later commit when the fix is not in the
    list) renamed a file to ``path``, or has no details to tell: the file may have been read under
    its old name, which the stored details do not keep."""
    end = len(commits) - 1 if fix is None else fix
    for c in commits[max(first, 0) : end + 1]:
        detail = details_by_sha.get(c.get("sha", ""))
        if detail is None:
            return True
        if any(
            _norm_path(f.get("filename") or "") == _norm_path(path) and f.get("status") == "renamed"
            for f in detail.get("files") or []
        ):
            return True
    return False


def other_source_changes_reviewed_code(
    action: dict,
    commits: list[dict],
    details_by_sha: dict[str, dict],
    records: list[ReviewRecord],
    first_reviewed: int,
    events: list[dict],
    chatbot_username: str,
) -> str | None:
    """The first other source file of the fix's commit that is not wholly in unreviewed code (judged
    as ``fix_code_origin`` judges other files) and not left out by path filters itself, or None. A
    fix in a path-filtered file whose commit also changes reviewed source code (a rebuilt `dist/`
    beside its reviewed `src/`) may be the fix of that code, so it is kept."""
    fix = sha_index(commits, (action.get("commit_sha") or "").strip())
    if fix is None:
        return None
    for f in (details_by_sha.get(commits[fix].get("sha", "")) or {}).get("files") or []:
        other = f.get("filename")
        if other == action.get("file_path") or not _is_source(other):
            continue
        # A path-filtered file the bot never read is skipped, unless it was renamed since the first
        # review (read under its old name, perhaps).
        if path_filtered(other or "", events, chatbot_username) and not renamed_since(
            other or "", commits, details_by_sha, first_reviewed + 1, fix
        ):
            continue
        o, _, _ = _origin_in_file(
            {**action, "file_path": other}, commits, details_by_sha, records, first_reviewed, True
        )
        if o != "unreviewed":
            return other
    return None


def _branched_commit(commits: list[dict], first_reviewed: int, fix: int) -> str | None:
    """The first commit after ``first_reviewed``, up to the fix, that breaks the single line of
    history the trace reads the patches along: its first parent is not the commit listed before it,
    or it merges another commit of the pull request. Patches of sibling commits are taken against
    different trees, so read one after the other they can place a reviewed line in an unreviewed
    commit. A merge of the base branch (its other parent is not in the pull request) keeps the line.
    Commits stored without their parents (enriched before parents were read) are read as one line.
    """
    shas = {c.get("sha") for c in commits}
    for i in range(first_reviewed + 1, fix + 1):
        parents = commits[i].get("parents")
        if parents is None:
            continue
        if not parents or parents[0] != commits[i - 1].get("sha") or any(p in shas for p in parents[1:]):
            return commits[i].get("sha") or ""
    return None


def _origin_in_file(
    action: dict,
    commits: list[dict],
    details_by_sha: dict[str, dict],
    records: list[ReviewRecord],
    first_reviewed: int,
    other_file: bool = False,
) -> tuple[str, str, tuple[str, ...]]:
    """Whether a review of the bot read the code a fix after its first review changed in one file.

    other_file is True for a file of the fixing commit other than the action's: a new file there
    passes (it changes no reviewed code), a file without a readable patch that has changed lines
    cannot be judged, and a file the commit only adds lines to is judged by the code they go into
    rather than taken as unknown.

    The lines the fix changed are the lines its commit removed from the action's file, other than
    blank and punctuation-only lines (``_removed``). Each is traced by its position, through the
    hunks of the commits after ``first_reviewed`` (the commit the analysis split at) and before the
    fix, newest first, to the commit that first wrote it, so a copy of a reviewed line added
    elsewhere in the file is not taken for it. An added line that is a light edit of a line its
    hunk removed (``_EDIT_RATIO``) is traced back to that line, so a line edited for another reason
    keeps the code it carried. A line a commit adds is not taken as written by it when the commit
    removed the same code elsewhere, whitespace aside: the line, or a run of up to ``_MOVE_SPAN``
    consecutive added lines holding it, equals a run of removed lines in any hunk of any file of
    that commit. That is a move, an extraction into another file, or a re-wrap (a copy removes
    nothing), and the fix is "unknown" and kept, since where the line was first written is not
    followed. A line not written in those commits was in the code the first review
    read, or in the repository before the pull request. A line counts as reviewed when a review of
    a commit before the fix covered the commit that wrote it or any commit that later edited it
    (``reviewed_before``): that review's diff showed the line.

    A fix that only adds lines changes no earlier line, so its diff does not say where the code it
    fixes was written. The unchanged lines next to the additions are not a safe stand-in: a
    refactor can wrap reviewed logic in new lines. Such a fix is "unknown" and therefore kept.

    The fix's commit can change several places in the file, and every action in that file and
    commit gets the same answer, so the answer must hold for each place. Each change block (a run
    of removed and added lines between unchanged ones, ``_blocks``) that removes code is judged by
    the code lines it removes, whatever it adds: those are the lines the fix changed. A block that
    changes no existing code line (it adds lines, or removes only punctuation) is judged by the code
    it was inserted into: the code lines in the unchanged runs just before and after it, traced the
    same way. The fix is "unreviewed" only when every block passes: the code lines each block
    removes, or, for an insertion, the code around it, were all written in commits no review read.
    An insertion among code a review read, or with no code line around it, may be the fix of
    reviewed code (a null guard added to a reviewed parser in the same commit that rewrites an
    unreviewed exporter), so the fix is then "unknown" and kept. A block that replaces an unreviewed
    line is left out even when what it puts there also protects reviewed code beside it: the line
    it changed was never reviewed.

    Returns (origin, reason, commits): "unreviewed" when no line was reviewed, with the commits that
    wrote or edited the lines; "reviewed" when every line was; "mixed" when some were; and "unknown"
    when the history cannot tell (no file, no patch, a commit without details, a new file, a fix
    that only adds lines, an insertion among reviewed code or with no code around it, a line a
    commit moved, re-wrapped or put back, a patch whose hunks do not add up, or a line whose
    position does not hold the traced text).
    """
    fix = sha_index(commits, (action.get("commit_sha") or "").strip())
    if fix is None or fix <= first_reviewed:
        return "unknown", "the fixing commit is not after the first reviewed commit", ()
    branch = _branched_commit(commits, first_reviewed, fix)
    if branch is not None:
        return (
            "unknown",
            f"commit {branch[:12]} does not follow the one listed before it (a branch merged inside the "
            "pull request), so the patches are not one line of history",
            (),
        )
    path = action.get("file_path")
    if not path:
        return "unknown", "the fix names no file", ()
    entry = _file_entry(details_by_sha.get(commits[fix].get("sha", "")), path)
    if entry is None:
        return "unknown", "the fixing commit does not change the fix's file", ()
    if entry.get("status") == "added":
        if other_file:
            return "unreviewed", f"{path} is a new file", ()
        return "unknown", "the fix adds a new file", ()
    if entry.get("status") == "renamed":
        return "unknown", "the fixing commit renames the file, and the history is read by file name", ()
    if other_file and not entry.get("patch") and not entry.get("additions") and not entry.get("deletions"):
        return "unreviewed", f"no line of {path} changes", ()
    fix_hunks = _file_hunks(entry)
    if fix_hunks is None:
        return "unknown", "the fixing commit has no readable patch for the file (missing, malformed or cut short)", ()
    changed = _removed(fix_hunks)
    if not changed and not other_file:
        return (
            "unknown",
            "the fix only adds lines or removes punctuation, so where the code it fixes was written is not known",
            (),
        )
    # The code lines around each change block that changes no code line, in any hunk.
    # A punctuation line removed beside code is a line the fix changed too.
    changed += _punctuation_removed(fix_hunks)
    blocks = [b for h in fix_hunks for b in _blocks(h)]
    if any(by_place and not around for by_place, around in blocks):
        return "unknown", f"the fix also changes lines in {path} with no code line around them", ()
    beside = list(dict.fromkeys(b for _, around in blocks for b in around))

    history: list[tuple[int, list[_Hunk]]] = []
    renamed: set[int] = set()
    for i in range(first_reviewed + 1, fix):
        c = commits[i]
        detail = details_by_sha.get(c.get("sha", ""))
        if detail is None:
            return "unknown", f"no details for commit {c.get('sha', '')[:12]}", ()
        f = _file_entry(detail, path)
        if f is None:
            continue
        if not f.get("patch"):
            if f.get("additions") or f.get("deletions") or f.get("status") in ("added", "renamed"):
                return "unknown", f"commit {c.get('sha', '')[:12]} has no patch for the file", ()
            continue
        hunks = _file_hunks(f)
        if hunks is None:
            return (
                "unknown",
                f"the patch of commit {c.get('sha', '')[:12]} for the file does not add up or is cut short",
                (),
            )
        if f.get("status") == "renamed":
            renamed.add(i)
        history.append((i, hunks))

    # The commits that wrote each changed line (the commit that first wrote it, then each light edit
    # of it), or None when it was first written before the commits traced.
    writers: list[list[int] | None] = []
    removed_keys: dict[int, tuple[_RemovedCode, bool]] = {}

    def keys_removed(k: int) -> tuple[_RemovedCode, bool]:
        if k not in removed_keys:
            removed_keys[k] = _removed_keys(details_by_sha[commits[k]["sha"]])
        return removed_keys[k]

    # The same commit is checked for copies once per traced line, mostly with the same skipped lines.
    removed_keys_skipping: dict[tuple[int, frozenset[int]], _RemovedCode] = {}

    def keys_removed_skipping(k: int, skip: frozenset[int]) -> _RemovedCode:
        if (k, skip) not in removed_keys_skipping:
            removed_keys_skipping[(k, skip)] = _removed_keys(details_by_sha[commits[k]["sha"]], path, skip)[0]
        return removed_keys_skipping[(k, skip)]

    def unreadable(k: int) -> tuple[str, str, tuple[str, ...]]:
        return (
            "unknown",
            f"commit {commits[k]['sha'][:12]} removed lines whose patch is not readable, so whether a line "
            f"the fix reads in {path} was moved from them is not known",
            (),
        )

    for line, text in [*changed, *beside]:
        wrote: list[int] | None = []
        # Where this line stood, in the file, when each later commit removed it as it edited it:
        # those removals are of the line itself, not of code it copies. In the fix commit every
        # removal in the file is the fix's own change (a copy of a line the fix also rewrites is not a
        # move), so only its other files count.
        own: dict[int, frozenset[int]] = {fix: ALL_LINES}
        for i, hunks in reversed(history):
            at, added_text = line, text
            kind, line, text = _trace(hunks, line, text)
            if kind == "edited":
                own[i] = frozenset({line})
            if kind == "mismatch":
                return "unknown", f"a changed line is not where commit {commits[i]['sha'][:12]} puts it", ()
            # Before a rename the file had another name, which the stored details do not keep; the
            # path it was renamed to may have held another file earlier.
            if i in renamed and kind != "added":
                return (
                    "unknown",
                    f"commit {commits[i]['sha'][:12]} renamed {path}, so where a line the fix reads was first "
                    "written is not followed",
                    (),
                )
            # An exact move or re-wrap of code the commit removed elsewhere is checked before a light
            # edit is followed to its similar predecessor; an edit whose predecessor is the same text
            # (whitespace aside) is that line, not a move.
            moved = kind == "added" or (kind == "edited" and _ws(text) != _ws(added_text))
            # Computed once per traced line and commit, for the move, restore and copy checks.
            added_keys = _added_keys(hunks, at) if moved else set()
            if moved and keys_removed(i)[0].hits(added_keys):
                return (
                    "unknown",
                    f"commit {commits[i]['sha'][:12]} moved or re-wrapped a line the fix reads in {path} from "
                    "code it removed, so where that line was first written is not followed",
                    (),
                )
            if moved:
                # A line an earlier traced commit removed and this one put back (a revert, or a move whose
                # removal and addition are in different commits) is restored code, not new code, also
                # when it lands in place of a similar line.
                restored = next((k for k in range(first_reviewed + 1, i) if keys_removed(k)[0].hits(added_keys)), None)
                # A line this commit added as a copy of code a later commit, up to the fix, removed
                # (a move done as copy, then delete) is moved code too. The line's own earlier texts,
                # which those commits removed as they edited it, are not a copy.
                # A light edit is checked too: the copy can land in place of a similar line.
                copied = next(
                    (
                        k
                        for k in range(i + 1, fix + 1)
                        if keys_removed_skipping(k, own.get(k, frozenset())).hits(added_keys)
                    ),
                    None,
                )
                if copied is not None:
                    return (
                        "unknown",
                        f"commit {commits[i]['sha'][:12]} added in {path} a copy of code commit "
                        f"{commits[copied]['sha'][:12]} then removed, so where a line the fix reads was first written "
                        "is not followed",
                        (),
                    )
                # Where a commit's removed lines are not all readable, a move or a restoration cannot be
                # ruled out.
                partial = next((k for k in range(first_reviewed + 1, fix + 1) if not keys_removed(k)[1]), None)
                if restored is None and partial is not None:
                    return unreadable(partial)
                if restored is not None:
                    return (
                        "unknown",
                        f"commit {commits[i]['sha'][:12]} put back in {path} a line the fix reads that commit "
                        f"{commits[restored]['sha'][:12]} removed, so where that line was first written is not followed",
                        (),
                    )
            if kind in ("edited", "added"):
                wrote.append(i)
            if kind == "added":
                break
        else:
            wrote = None
        writers.append(wrote)

    def read(w: list[int] | None) -> bool:
        return w is None or any(reviewed_before(i, fix, records) for i in w)

    n = len(changed)
    unread = [w for w in writers[:n] if not read(w)]
    beside_read = sum(1 for w in writers[n:] if read(w))
    if len(unread) == n and not beside_read:
        shas = tuple(commits[i]["sha"] for i in sorted({i for w in writers if w is not None for i in w}))
        added = f" and the {len(beside)} lines around its other additions" if beside else ""
        return (
            "unreviewed",
            f"all {n} lines the fix changed in {path}{added} were written in commits no review of the bot "
            f"read before the fix ({', '.join(s[:12] for s in shas)})",
            shas,
        )
    if len(unread) == n:
        return (
            "unknown",
            f"all {n} lines the fix changed in {path} were written in commits no review of the bot read before "
            f"the fix, but it also adds lines among code a review read ({beside_read} of the {len(beside)} lines "
            "around those additions), so the fix may be of that code",
            (),
        )
    if not unread:
        return "reviewed", f"all {n} lines the fix changed in {path} were in code a review of the bot read", ()
    return "mixed", f"{len(unread)} of the {n} lines the fix changed in {path} were in code no review read", ()

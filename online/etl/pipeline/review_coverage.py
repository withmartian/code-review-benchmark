"""Which post-review commits a bot said it did not review, and which fixes were in code written in them.

The analysis splits a PR at the bot's first review and counts every fix made in a later commit
against the bot's recall. When the bot says, in its own comments, that it did not review a later
push because of a limit, a pause or a failed review, the fixes of bugs written in those unreviewed
commits are misses the bot was never given the chance to catch.

This module reads those notices deterministically, from a table of notice formats keyed by bot
login (``NOTICE_PATTERNS``), and finds the last commit the bot reviewed (``find_review_cutoff``).
Only explicit limit, pause and failure notices count. Notices that the bot skips a push by design
or by configuration (automatic or incremental reviews turned off, too few stars, nothing to
review) are not in the table and never move the cutoff. ``unreviewed_code_origin`` then reads the
stored commit patches to tell whether the lines a fix changed were first written after the cutoff.

To support another bot's notice, add a ``NoticePattern`` row for its login. A notice that names
no commit is placed with the bot's own reviews: the last reviewed commit is the latest one a
completed review of the bot was posted on.

A bot that rewrites its status comment in place keeps only the latest state in the current body:
once it reviews again after a limit, the earlier notice is gone. The earlier revisions of a
comment (its edit history) are read as notices posted at their edit time. A notice whose range a
later completed review covered opens a gap (``find_review_gaps``): the later review saw only the
final state of the gap's commits, so a fix made in the gap of code first written in the gap was
never in any diff the bot reviewed.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re

from pipeline.actors import same_github_actor

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SHA = r"([0-9a-f]{7,40})"


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
    # A skip block that reports a failed incremental review. Every other skip block is by design or
    # configuration and is not listed.
    NoticePattern(
        bot=_CODERABBIT,
        reason="failure",
        pattern=_coderabbit_block("skip review"),
        in_place=True,
        requires=re.compile(r"couldn.t safely recover the incremental review", re.IGNORECASE),
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


@dataclass(frozen=True)
class ReviewCutoff:
    """The last commit a bot reviewed, and the post-review commits after it that it did not review."""

    last_reviewed: str
    unreviewed: tuple[str, ...]
    reasons: tuple[str, ...]
    notice_reviewed: str | None
    reviews_reviewed: str | None


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


def sha_index(commits: list[dict], sha: str | None) -> int | None:
    """Index of a commit by full SHA or by a prefix of at least 7 characters."""
    if not sha or len(sha) < 7:
        return None
    for i, c in enumerate(commits):
        full = c.get("sha", "")
        if full == sha or full.startswith(sha):
            return i
    return None


def _completed_reviews(events: list[dict], commits: list[dict], chatbot_username: str) -> list[tuple[int, str]]:
    """(commit index, time) of each review the bot completed.

    A review with a body that is not a notice, or an inline comment that does not reply to a
    thread. A review with an empty body only carries inline comments (counted on their own) or
    thread replies, so its commit does not show that a review ran.
    """
    out = []
    for e in events:
        if not same_github_actor(e.get("actor"), chatbot_username):
            continue
        data = e.get("data") or {}
        etype = e.get("event_type")
        if etype == "review":
            body = data.get("body") or ""
            if not _clean(body) or _match_notices(body, chatbot_username):
                continue
            i = sha_index(commits, data.get("commit_id"))
        elif etype == "review_comment" and not data.get("in_reply_to_id"):
            i = sha_index(commits, data.get("original_commit_id") or data.get("commit_id"))
        else:
            continue
        if i is not None:
            out.append((i, e.get("timestamp") or ""))
    return out


def find_review_cutoff(
    events: list[dict], commits: list[dict], post_commits: list[dict], chatbot_username: str
) -> ReviewCutoff | None:
    """The last commit the bot reviewed, when it said it did not review a later push.

    The last reviewed commit is the latest of the commit the analysis split at, the last reviewed
    commit a notice names, and the latest commit a completed review of the bot was posted on. A
    notice that names the latest commit it did not review does not count when that commit is at
    or before the last reviewed commit (a later review covered it); a notice posted at a time (not
    rewritten in place) counts only if no review completed after it. Returns None when no notice
    counts or no post-review commit follows the last reviewed commit.
    """
    if not post_commits:
        return None
    notices = find_notices(events, chatbot_username)
    if not notices:
        return None
    split = sha_index(commits, post_commits[0].get("sha"))
    if split is None:
        return None
    completed = _completed_reviews(events, commits, chatbot_username)
    reviews_idx = max((i for i, _ in completed), default=None)
    last_review_time = max((t for _, t in completed), default="")

    live = [n for n in notices if n.in_place or n.timestamp > last_review_time]
    named = [i for i in (sha_index(commits, n.reviewed_sha) for n in live) if i is not None]
    cutoff = max([split - 1, *named, *([reviews_idx] if reviews_idx is not None else [])])

    reasons = set()
    for n in live:
        # A named head missing from the commit list is a later or rewritten push: the notice stands.
        head = sha_index(commits, n.unreviewed_sha)
        if head is not None and head <= cutoff:
            continue
        reasons.add(n.reason)
    if not reasons:
        return None

    post_shas = {c.get("sha") for c in post_commits}
    unreviewed = tuple(c["sha"] for c in commits[cutoff + 1 :] if c.get("sha") in post_shas)
    if not unreviewed:
        return None
    return ReviewCutoff(
        last_reviewed=commits[cutoff]["sha"],
        unreviewed=unreviewed,
        reasons=tuple(sorted(reasons)),
        notice_reviewed=commits[max(named)]["sha"] if named else None,
        reviews_reviewed=commits[reviews_idx]["sha"] if reviews_idx is not None else None,
    )


@dataclass(frozen=True)
class ReviewGap:
    """Post-review commits between two commits the bot reviewed, opened by a notice of the bot.

    last_reviewed is the commit the bot reviewed before the notice; closed_by is the first commit
    a later review of the bot covered. That review saw only the state at closed_by, so code written
    in the gap and changed again by closed_by never appeared in a diff the bot reviewed.
    """

    last_reviewed: str
    closed_by: str
    unreviewed: tuple[str, ...]
    reasons: tuple[str, ...]


def find_review_gaps(
    events: list[dict], commits: list[dict], post_commits: list[dict], chatbot_username: str
) -> list[ReviewGap]:
    """Gaps opened by a notice of the bot and closed by a later review, in commit order.

    A commit is known reviewed at a time when a completed review of the bot was posted on it then,
    or when a notice posted then names it as the last reviewed commit. For a timed notice (posted
    once, or an earlier revision of an edited comment), the gap starts at the latest commit known
    reviewed before the notice or named by it as the last reviewed. For a notice in the current body of a rewritten comment, it starts
    at the last reviewed commit the notice names. The gap ends at the first commit after its start
    known reviewed after the notice (for a current body, a completed review at or after the
    notice's head). No gap opens for a notice whose head is at or before the start, a notice before
    any review, a notice no later review covered (``find_review_cutoff`` reads those), or a gap
    with no commit inside it.
    """
    if not post_commits:
        return []
    notices = find_notices(events, chatbot_username)
    if not notices:
        return []
    split = sha_index(commits, post_commits[0].get("sha"))
    if split is None:
        return []
    completed = _completed_reviews(events, commits, chatbot_username)
    checkpoints = completed + [
        (i, n.timestamp) for n in notices if (i := sha_index(commits, n.reviewed_sha)) is not None
    ]
    gaps: dict[tuple[int, int], set[str]] = {}
    for n in notices:
        named = sha_index(commits, n.reviewed_sha)
        head = sha_index(commits, n.unreviewed_sha)
        if n.in_place:
            if named is None or head is None:
                continue
            start = named
            ends = [i for i, _ in completed if i >= head]
        else:
            before = [i for i, t in checkpoints if t < n.timestamp] + ([named] if named is not None else [])
            if not before:
                continue
            start = max(before)
            ends = [i for i, t in checkpoints if t > n.timestamp]
        start = max(start, split - 1)
        if head is not None and head <= start:
            continue
        ends = [i for i in ends if i > start]
        if not ends or min(ends) - start < 2:
            continue
        gaps.setdefault((start, min(ends)), set()).add(n.reason)
    post_shas = {c.get("sha") for c in post_commits}
    return [
        ReviewGap(
            last_reviewed=commits[start]["sha"],
            closed_by=commits[end]["sha"],
            unreviewed=tuple(c["sha"] for c in commits[start + 1 : end] if c.get("sha") in post_shas),
            reasons=tuple(sorted(reasons)),
        )
        for (start, end), reasons in sorted(gaps.items())
    ]


# -- Where the code a fix changed was first written ----------------------------

_SIGNIFICANT_RE = re.compile(r"\w{3,}")
_EDIT_RATIO = 0.8


def _significant(line: str) -> bool:
    """A line that identifies code (not a lone brace, a blank line or punctuation)."""
    return bool(_SIGNIFICANT_RE.search(line))


def _removed_lines(patch: str) -> list[str]:
    """The removed lines of a unified diff (stripped)."""
    return [raw[1:].strip() for raw in (patch or "").splitlines() if raw.startswith("-")]


def _added_lines(patch: str) -> dict[str, str | None]:
    """Each added line of a unified diff (stripped), with the removed line it is an edit of, if any.

    An added line is an edit of a removed line of the same hunk when the two are at least
    ``_EDIT_RATIO`` similar: then the code it carries was written before this commit.
    """
    out: dict[str, str | None] = {}
    removed: list[str] = []
    added: list[str] = []

    def flush() -> None:
        for line in added:
            best = max(removed, key=lambda r: SequenceMatcher(None, r, line).ratio(), default=None)
            if best is not None and SequenceMatcher(None, best, line).ratio() >= _EDIT_RATIO:
                out[line] = best
            else:
                out.setdefault(line, None)
        removed.clear()
        added.clear()

    for raw in (patch or "").splitlines():
        if raw.startswith("@@"):
            flush()
        elif raw.startswith("-"):
            removed.append(raw[1:].strip())
        elif raw.startswith("+"):
            added.append(raw[1:].strip())
    flush()
    return out


def _file_entry(detail: dict | None, path: str) -> dict | None:
    for f in (detail or {}).get("files") or []:
        if f.get("filename") == path:
            return f
    return None


def unreviewed_code_origin(
    action: dict, commits: list[dict], details_by_sha: dict[str, dict], cutoff_sha: str
) -> tuple[str, str]:
    """Where the code a fix made after the cutoff changed was first written.

    The lines the fix changed are the lines its commit removed from the action's file. Each is
    looked up in the lines the commits after the cutoff and before the fix added to that file. An
    added line that is a light edit of a line it replaced (``_EDIT_RATIO``) is traced back to that
    line, so a line edited after the cutoff for another reason keeps the code it carried from before.

    A fix that only adds lines changes no earlier line, so its diff does not say where the code it
    fixes was written. The unchanged lines next to the additions are not a safe stand-in: a
    refactor after the cutoff can wrap reviewed logic in new lines. Such a fix is "unknown" and
    therefore kept.

    Returns (origin, reason): "unreviewed" when every such line was added after the cutoff,
    "reviewed" when none was (it was in code the bot reviewed, or in the repository before the
    pull request), "mixed" when some were, and "unknown" when the history cannot tell (no file,
    no patch, a commit without details, a fix that only adds lines).
    """
    cut = sha_index(commits, cutoff_sha)
    fix = sha_index(commits, (action.get("commit_sha") or "").strip())
    if cut is None or fix is None or fix <= cut:
        return "unknown", "the fixing commit is not after the last reviewed commit"
    path = action.get("file_path")
    if not path:
        return "unknown", "the fix names no file"
    entry = _file_entry(details_by_sha.get(commits[fix].get("sha", "")), path)
    if entry is None:
        return "unknown", "the fixing commit does not change the fix's file"
    if entry.get("status") == "added":
        return "unknown", "the fix adds a new file"
    if not entry.get("patch"):
        return "unknown", "the fixing commit has no patch for the file"
    changed = [line for line in _removed_lines(entry["patch"]) if _significant(line)]
    if not changed:
        return "unknown", "the fix only adds lines, so where the code it fixes was written is not known"

    history: list[dict[str, str | None]] = []
    for c in commits[cut + 1 : fix]:
        detail = details_by_sha.get(c.get("sha", ""))
        if detail is None:
            return "unknown", f"no details for commit {c.get('sha', '')[:12]}"
        f = _file_entry(detail, path)
        if f is None:
            continue
        if not f.get("patch") and (f.get("additions") or f.get("status") in ("added", "renamed")):
            return "unknown", f"commit {c.get('sha', '')[:12]} has no patch for the file"
        history.append(_added_lines(f.get("patch") or ""))

    def written_after(line: str) -> bool:
        # Newest commit first: a line that edits an older line is traced back to it.
        for added in reversed(history):
            if line in added:
                origin = added[line]
                if origin is None:
                    return True
                line = origin
        return False

    n_after = sum(written_after(line) for line in changed)
    if n_after == len(changed):
        return (
            "unreviewed",
            f"all {len(changed)} lines the fix changed in {path} were added after the last reviewed commit",
        )
    if n_after == 0:
        return "reviewed", f"none of the {len(changed)} lines the fix changed in {path} were added after the cutoff"
    return "mixed", f"{n_after} of the {len(changed)} lines the fix changed in {path} were added after the cutoff"

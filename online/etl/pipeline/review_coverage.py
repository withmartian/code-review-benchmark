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
    not finished; records inside them do not count.
    """

    bot: str
    pattern: re.Pattern[str]
    ignore: re.Pattern[str] | None = None


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
    ),
    # The summary comment's "Last reviewed commit: [<subject>](https://github.com/<o>/<r>/commit/<sha>)".
    RecordPattern(
        bot="greptile-apps[bot]",
        pattern=re.compile(
            r"Last reviewed commit: \[[^\n]*?\]\(https://github\.com/[^/\s]+/[^/\s]+/commit/(?P<head>[0-9a-f]{7,40})\)"
        ),
    ),
    # The review summary's state marker, and the "Reviewed commit" line of a review.
    RecordPattern(
        bot="chatgpt-codex-connector[bot]",
        pattern=re.compile(r'codex-review-state:v1 \{"code_review":\{"commit_sha":"(?P<head>[0-9a-f]{7,40})"'),
    ),
    RecordPattern(
        bot="chatgpt-codex-connector[bot]",
        pattern=re.compile(r"\*\*Reviewed commit:\*\* `(?P<head>[0-9a-f]{7,40})`"),
    ),
)


@dataclass(frozen=True)
class ReviewRecord:
    """The commits one review of the bot read, as indices into the PR's commits.

    The review covered the commits after start up to head; start is -1 when it read the pull
    request from its base. source is "record" (the bot's own record), "review" (a completed review
    with no record, read as a review of the whole pull request at its commit) or "notice" (the last
    reviewed commit a notice names, read the same way).
    """

    start: int
    head: int
    source: str


def find_review_records(events: list[dict], commits: list[dict], chatbot_username: str) -> list[ReviewRecord]:
    """Every review of the bot that the bot's record, its completed reviews or its notices show.

    The record is read from the bot's reviews and from its comments, current bodies and earlier
    revisions alike. A record naming a head missing from the commit list (a rebased push) is dropped;
    a start missing from it (the PR's base, or an older push) is read as the base. A completed review
    or a notice adds a review only at a commit that no record already shows reviewed.
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
    fallback = [(i, "review") for i, _ in _completed_reviews(events, commits, chatbot_username)]
    fallback += [(sha_index(commits, n.reviewed_sha), "notice") for n in find_notices(events, chatbot_username)]
    for i, source in fallback:
        if i is not None and i not in heads:
            out.add(ReviewRecord(start=-1, head=i, source=source))
            heads.add(i)
    return sorted(out, key=lambda r: (r.head, r.start, r.source))


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


_CODE_RE = re.compile(r"[A-Za-z0-9]")


def _removed(hunks: list[_Hunk]) -> list[tuple[int, str]]:
    """(line number before the change, text) of each removed line that carries code.

    Every line with a letter or a digit counts, however short (``x = 0``). A line of punctuation
    only (``}``, ``);``) is left out: which of several such lines a diff pairs with an older one is
    arbitrary, so its position says nothing about where the code around it was written.
    """
    return [(n, t) for h in hunks for n, t in _removed_all(h) if _CODE_RE.search(t)]


def _context(h: _Hunk) -> list[tuple[int, str]]:
    """(line number before the change, text) of each unchanged line of a hunk that carries code."""
    out = []
    old = h.old_start
    for kind, text in h.lines:
        if kind == " " and _CODE_RE.search(text):
            out.append((old, text))
        if kind != "+":
            old += 1
    return out


def _trace(hunks: list[_Hunk], line: int, text: str) -> tuple[str, int, str]:
    """Where line ``line`` (of text ``text``) after a commit came from in that commit.

    Returns ("added", 0, "") when the commit wrote it, ("kept", n, text) when it is line n before
    the commit, unchanged, ("edited", n, removed text) when it is a light edit of the removed line n
    of its hunk (``_EDIT_RATIO``), and ("mismatch", 0, "") when the line at that position is not the
    line traced.
    """
    offset = 0
    for h in hunks:
        before = line <= h.new_start if h.new_len == 0 else line < h.new_start
        if before:
            return "kept", line - offset, text
        if line < h.new_start + h.new_len:
            removed = _removed_all(h)
            old, new = h.old_start, h.new_start
            for kind, t in h.lines:
                if kind == "-":
                    old += 1
                    continue
                if new == line:
                    if t != text:
                        return "mismatch", 0, ""
                    if kind == " ":
                        return "kept", old, text
                    best = max(removed, key=lambda r: SequenceMatcher(None, r[1], t).ratio(), default=None)
                    if best is not None and SequenceMatcher(None, best[1], t).ratio() >= _EDIT_RATIO:
                        return "edited", best[0], best[1]
                    return "added", 0, ""
                new += 1
                if kind == " ":
                    old += 1
        offset += h.new_len - h.old_len
    return "kept", line - offset, text


def _file_entry(detail: dict | None, path: str) -> dict | None:
    for f in (detail or {}).get("files") or []:
        if f.get("filename") == path:
            return f
    return None


def fix_code_origin(
    action: dict, commits: list[dict], details_by_sha: dict[str, dict], records: list[ReviewRecord], first_reviewed: int
) -> tuple[str, str, tuple[str, ...]]:
    """Whether a review of the bot read the code a fix after its first review changed.

    The lines the fix changed are the lines its commit removed from the action's file, other than
    blank and punctuation-only lines (``_removed``). Each is traced by its position, through the
    hunks of the commits after ``first_reviewed`` (the commit the analysis split at) and before the
    fix, newest first, to the commit that first wrote it, so a copy of a reviewed line added
    elsewhere in the file is not taken for it. An added line that is a light edit of a line its
    hunk removed (``_EDIT_RATIO``) is traced back to that line, so a line edited for another reason
    keeps the code it carried. A line not written in those commits was in the code the first review
    read, or in the repository before the pull request. A line counts as reviewed when a review of
    a commit before the fix covered the commit that wrote it or any commit that later edited it
    (``reviewed_before``): that review's diff showed the line.

    A fix that only adds lines changes no earlier line, so its diff does not say where the code it
    fixes was written. The unchanged lines next to the additions are not a safe stand-in: a
    refactor can wrap reviewed logic in new lines. Such a fix is "unknown" and therefore kept.

    The fix's commit can change several places in the file, and every action in that file and
    commit gets the same answer, so the answer must hold for each place. A hunk that changes no code
    line (it adds lines, or removes only punctuation) is judged by the code lines it leaves
    unchanged around its additions, traced the same way. The fix is "unreviewed" only when every
    hunk is wholly in code no review read: the lines each hunk changed, or, for a hunk that changes
    none, the lines around its additions. A hunk that adds lines among code a review read, or with
    no code line around its additions, may be the fix of reviewed code (a null guard added to a
    reviewed parser in the same commit that rewrites an unreviewed exporter), so the fix is then
    "unknown" and kept.

    Returns (origin, reason, commits): "unreviewed" when no line was reviewed, with the commits that
    wrote or edited the lines; "reviewed" when every line was; "mixed" when some were; and "unknown"
    when the history cannot tell (no file, no patch, a commit without details, a new file, a fix
    that only adds lines, a hunk that adds lines among reviewed code or with no code around them, a
    patch whose hunks do not add up, or a line whose position does not hold the traced text).
    """
    fix = sha_index(commits, (action.get("commit_sha") or "").strip())
    if fix is None or fix <= first_reviewed:
        return "unknown", "the fixing commit is not after the first reviewed commit", ()
    path = action.get("file_path")
    if not path:
        return "unknown", "the fix names no file", ()
    entry = _file_entry(details_by_sha.get(commits[fix].get("sha", "")), path)
    if entry is None:
        return "unknown", "the fixing commit does not change the fix's file", ()
    if entry.get("status") == "added":
        return "unknown", "the fix adds a new file", ()
    fix_hunks = _hunks(entry.get("patch") or "") if entry.get("patch") else None
    if fix_hunks is None:
        return "unknown", "the fixing commit has no readable patch for the file", ()
    changed = _removed(fix_hunks)
    if not changed:
        return "unknown", "the fix only adds lines, so where the code it fixes was written is not known", ()
    # The code lines around the additions of each hunk that changes no code line.
    beside_by_hunk = [_context(h) for h in fix_hunks if not _removed([h])]
    if any(not b for b in beside_by_hunk):
        return "unknown", f"the fix also adds lines in {path} with no code line around them", ()
    beside = [b for bs in beside_by_hunk for b in bs]

    history: list[tuple[int, list[_Hunk]]] = []
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
        hunks = _hunks(f["patch"])
        if hunks is None:
            return "unknown", f"the patch of commit {c.get('sha', '')[:12]} for the file does not add up", ()
        history.append((i, hunks))

    # The commits that wrote each changed line (the commit that first wrote it, then each light edit
    # of it), or None when it was first written before the commits traced.
    writers: list[list[int] | None] = []
    for line, text in [*changed, *beside]:
        wrote: list[int] | None = []
        for i, hunks in reversed(history):
            kind, line, text = _trace(hunks, line, text)
            if kind == "mismatch":
                return "unknown", f"a changed line is not where commit {commits[i]['sha'][:12]} puts it", ()
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

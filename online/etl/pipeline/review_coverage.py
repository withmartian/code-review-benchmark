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
    not finished; records inside them do not count. in_issue_comments means the bot writes this
    record in a PR conversation comment (often a status comment it rewrites on each review), so the
    record cannot be read for a PR whose conversation comments were not read.
    """

    bot: str
    pattern: re.Pattern[str]
    ignore: re.Pattern[str] | None = None
    in_issue_comments: bool = False


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
)


def record_in_issue_comments(chatbot_username: str) -> bool:
    """Whether the bot writes any of its record in PR conversation comments (``RECORD_PATTERNS``)."""
    return any(p.in_issue_comments and same_github_actor(p.bot, chatbot_username) for p in RECORD_PATTERNS)


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


def rewritten_review_commits(events: list[dict], commits: list[dict], chatbot_username: str) -> list[str]:
    """The commits the bot reviewed or named that are no longer in the PR's commit list.

    A force-push (an amend or a rebase) replaces commits, and a review, record or notice naming a
    replaced commit cannot be placed: the rewritten commit then reads as never reviewed although its
    code was. The commits checked are the commits of the completed reviews and inline comments
    (``_completed_reviews``), each record's head, and each notice's last reviewed commit when the
    notice names it as such ("Last reviewed commit"). The start X of a range "between X and Y" is
    not checked: for a first review it is the PR's base, which is not one of its commits.
    """
    named: list[str | None] = []
    patterns = [p for p in RECORD_PATTERNS if same_github_actor(p.bot, chatbot_username)]
    for e in events:
        if not same_github_actor(e.get("actor"), chatbot_username):
            continue
        data = e.get("data") or {}
        etype = e.get("event_type")
        if etype == "review":
            body = data.get("body") or ""
            if _clean(body) and not _match_notices(body, chatbot_username):
                named.append(data.get("commit_id"))
        elif etype == "review_comment" and not data.get("in_reply_to_id"):
            named.append(data.get("original_commit_id") or data.get("commit_id"))
        if etype in ("review", "issue_comment"):
            body = data.get("body") or ""
            for p in patterns:
                named += [m.group("head") for m in p.pattern.finditer(p.ignore.sub("", body) if p.ignore else body)]
    for n in find_notices(events, chatbot_username):
        if n.reviewed_sha and not re.search(rf"between {n.reviewed_sha}\b", n.text):
            named.append(n.reviewed_sha)
    return sorted({s for s in named if s and len(s) >= 7 and sha_index(commits, s) is None})


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


_DIFF_CONTEXT = 3  # the unchanged lines GitHub's patches carry around each change


def _blocks(h: _Hunk) -> list[tuple[bool, list[tuple[int, str]]]]:
    """Each change block of a hunk (a run of removed and added lines between unchanged ones).

    For each block: whether it is judged by the code where it is, and that code, as (line number
    before the change, text). A block that changes no existing code line (it adds lines, removes
    only punctuation, or both) is, on each side on its own, by the unchanged lines that carry code
    in the run of unchanged lines next to it, or, when that run carries none (only closing braces),
    the nearest unchanged code line on that side within the hunk: a punctuation line it removes (a
    stray `;`) belongs to that code. When one side has no code line in the hunk, the code is empty
    and the fix's origin is unknown, unless the file itself has nothing on that side (the hunk
    starts at its first line, or ends with fewer unchanged lines than a diff's context, so at its
    last). A block that removes code is judged by the code lines it removes alone, whatever it adds.
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


_MOVE_SPAN = 10  # the most consecutive lines a moved or re-wrapped line is compared as one
_WS_RE = re.compile(r"\s+")


def _ws(text: str) -> str:
    """The text with all whitespace removed, as moved or re-wrapped code is compared."""
    return _WS_RE.sub("", text)


def _span_keys(run: list[str], at: int | None = None) -> set[str]:
    """The text, with all whitespace removed, of each run of up to ``_MOVE_SPAN`` consecutive lines
    of ``run`` (those containing index ``at`` when it is given) that carries code."""
    keys = set()
    for a in range(len(run)):
        for b in range(a + 1, min(len(run), a + _MOVE_SPAN) + 1):
            if at is not None and not a <= at < b:
                continue
            key = _ws("".join(run[a:b]))
            if _CODE_RE.search(key):
                keys.add(key)
    return keys


def _runs(h: _Hunk, kind: str) -> list[list[str]]:
    """The texts of each run of consecutive lines of one kind ("-" or "+") in a hunk."""
    out: list[list[str]] = []
    run: list[str] = []
    for k, text in h.lines:
        if k == kind:
            run.append(text)
            continue
        if run:
            out.append(run)
        run = []
    if run:
        out.append(run)
    return out


def _removed_keys(detail: dict) -> tuple[set[str], bool]:
    """``_span_keys`` of every run of removed lines a commit's patches hold, in any file, and whether
    they are all of them: False when a file the commit removed lines from has no patch, or one whose
    hunks do not add up (GitHub leaves out the patch of a large diff)."""
    keys: set[str] = set()
    complete = True
    for f in detail.get("files") or []:
        hunks = _hunks(f.get("patch") or "") if f.get("patch") else None
        if hunks is None:
            if f.get("deletions") or (f.get("patch") and f.get("deletions") is None):
                complete = False
            continue
        for h in hunks:
            for run in _runs(h, "-"):
                keys |= _span_keys(run)
    return keys, complete


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
        return (
            "unknown",
            "the fix only adds lines or removes punctuation, so where the code it fixes was written is not known",
            (),
        )
    # The code lines around each change block that changes no code line, in any hunk.
    beside_by_block = [around for h in fix_hunks for by_place, around in _blocks(h) if by_place]
    if any(not b for b in beside_by_block):
        return "unknown", f"the fix also changes lines in {path} with no code line around them", ()
    beside = list(dict.fromkeys(b for bs in beside_by_block for b in bs))

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
    removed_keys: dict[int, tuple[set[str], bool]] = {}

    def keys_removed(k: int) -> tuple[set[str], bool]:
        if k not in removed_keys:
            removed_keys[k] = _removed_keys(details_by_sha[commits[k]["sha"]])
        return removed_keys[k]

    def unreadable(k: int) -> tuple[str, str, tuple[str, ...]]:
        return (
            "unknown",
            f"commit {commits[k]['sha'][:12]} removed lines whose patch is not readable, so whether a line "
            f"the fix reads in {path} was moved from them is not known",
            (),
        )

    for line, text in [*changed, *beside]:
        wrote: list[int] | None = []
        for i, hunks in reversed(history):
            at, added_text = line, text
            kind, line, text = _trace(hunks, line, text)
            if kind == "mismatch":
                return "unknown", f"a changed line is not where commit {commits[i]['sha'][:12]} puts it", ()
            # An exact move or re-wrap of code the commit removed elsewhere is checked before a light
            # edit is followed to its similar predecessor; an edit whose predecessor is the same text
            # (whitespace aside) is that line, not a move.
            moved = kind == "added" or (kind == "edited" and _ws(text) != _ws(added_text))
            if moved and _added_keys(hunks, at) & keys_removed(i)[0]:
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
                restored = next(
                    (k for k in range(first_reviewed + 1, i) if _added_keys(hunks, at) & keys_removed(k)[0]), None
                )
                # Where a commit's removed lines are not all readable, a move or a restoration cannot be
                # ruled out.
                partial = next((k for k in range(first_reviewed + 1, i + 1) if not keys_removed(k)[1]), None)
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

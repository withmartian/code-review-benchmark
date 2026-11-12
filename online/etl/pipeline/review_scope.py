"""Cut the answer key at the last commit the bot reviewed, when the bot says it stopped.

Some bots post a notice when they stop reviewing a PR (rate limit, auto-pause, a run still
in progress). Commits pushed after that point were never reviewed, so a fix made to code
that first appeared in those commits cannot be a recall miss of the bot.

`STOP_NOTICES` lists, per bot, the notice blocks that tell where it stopped: either the block
names the last reviewed commit, or (a skip that names none) the last reviewed commit is the
newest commit with a non-empty review record from the bot. A bot without an entry is scored
exactly as before. The rule that uses the commit is
`split_actions_at_last_reviewed`: an action is dropped only when the lines its fix removed
were all added after the last reviewed commit (traced back through the stored per-commit
patches), or its file was created after that commit. Anything else is kept.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from pipeline.actors import same_github_actor


@dataclass(frozen=True)
class StopNotice:
    kind: str
    block_re: re.Pattern[str]
    # Reads the last reviewed commit from the block; None = newest non-empty bot review record.
    sha_re: re.Pattern[str] | None
    # When set, the block counts only if it contains this text.
    requires: re.Pattern[str] | None = None


def _coderabbit_block(kind: str) -> re.Pattern[str]:
    return re.compile(
        rf"<!-- This is an auto-generated comment: {kind} by coderabbit\.ai -->(.*?)"
        rf"<!-- end of auto-generated comment: {kind} by coderabbit\.ai -->",
        re.DOTALL,
    )


# "Reviewing files that changed from the base of the PR and between A and B." -> A
_BETWEEN_RE = re.compile(r"between ([0-9a-f]{7,40}) and [0-9a-f]{7,40}")
_LAST_REVIEWED_RE = re.compile(r"Last reviewed commit: ([0-9a-f]{7,40})")

# Bot username -> notices that say where the bot stopped. Notices not listed here (e.g. a
# CodeRabbit "skip review" for "no reviewable changes", which is not a stop, or "failure")
# do not cut. Matched on the RAW comment body: the markers are HTML comments.
STOP_NOTICES: dict[str, list[StopNotice]] = {
    "coderabbitai[bot]": [
        StopNotice("rate limited", _coderabbit_block("rate limited"), _BETWEEN_RE),
        StopNotice("review paused", _coderabbit_block("review paused"), _LAST_REVIEWED_RE),
        StopNotice("review in progress", _coderabbit_block("review in progress"), _BETWEEN_RE),
        StopNotice(
            "skip review: incremental reviews disabled",
            _coderabbit_block("skip review"),
            None,
            re.compile(r"Auto incremental reviews are disabled on this repository"),
        ),
        StopNotice(
            "skip review: fewer than 10 stars",
            _coderabbit_block("skip review"),
            None,
            re.compile(r"does not receive automatic reviews because it has fewer than 10 stars"),
        ),
    ],
}


def _notices_for(chatbot_username: str) -> list[StopNotice]:
    for username, notices in STOP_NOTICES.items():
        if same_github_actor(username, chatbot_username):
            return notices
    return []


def _newest_reviewed_commit(
    reviews: list[dict], commits: list[dict], chatbot_username: str, hash_x: str | None
) -> str | None:
    """Newest commit (by PR commit order) with a non-empty review record from the bot, else hash X.

    Empty-body records are the bot's replies in comment threads, filed by GitHub as reviews of
    whatever the head was at the time, so they say nothing about what was reviewed.
    """
    best: tuple[int, str] | None = None
    for r in reviews:
        author = r.get("author") or (r.get("user") or {}).get("login", "")
        sha = r.get("commit_id")
        if not sha or not (r.get("body") or "").strip() or not same_github_actor(author, chatbot_username):
            continue
        idx = _index_of(commits, sha)
        if idx is not None and (best is None or idx > best[0]):
            best = (idx, commits[idx]["sha"])
    return best[1] if best else hash_x


def find_last_reviewed_commit(
    issue_comments: list[dict],
    chatbot_username: str,
    reviews: list[dict] | None = None,
    commits: list[dict] | None = None,
    hash_x: str | None = None,
) -> tuple[str, str] | None:
    """Return (sha, notice kind) from the last stop notice in the bot's issue comments, or None.

    Comments are read in stored order (the API returns them oldest first) and their bodies are
    the current, edited ones. The last notice that gives a commit wins. A notice without a commit
    of its own resolves through `_newest_reviewed_commit` (needs `reviews`, `commits`, `hash_x`).
    """
    notices = _notices_for(chatbot_username)
    if not notices:
        return None
    found: tuple[int, int, str, str] | None = None
    for ci, comment in enumerate(issue_comments):
        if not same_github_actor(comment.get("author"), chatbot_username):
            continue
        body = comment.get("body") or ""
        for notice in notices:
            for m in notice.block_re.finditer(body):
                block = m.group(1)
                if notice.requires is not None and not notice.requires.search(block):
                    continue
                if notice.sha_re is not None:
                    shas = notice.sha_re.findall(block)
                    sha = shas[-1] if shas else None
                else:
                    sha = _newest_reviewed_commit(reviews or [], commits or [], chatbot_username, hash_x)
                if sha and (found is None or (ci, m.start()) > found[:2]):
                    found = (ci, m.start(), sha, notice.kind)
    return (found[2], found[3]) if found else None


# -- Line provenance -----------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class _AmbiguousError(Exception):
    """Provenance cannot be traced from the stored patches."""


def _parse_hunks(patch: str) -> list[tuple[int, int, int, int, list[str]]]:
    """Parse a unified diff into (old_start, old_len, new_start, new_len, line kinds) per hunk.

    Zero-length ranges are normalised so `start` is the first line after the hunk position.
    """
    hunks: list[tuple[int, int, int, int, list[str]]] = []
    for line in patch.rstrip("\n").split("\n"):
        m = _HUNK_RE.match(line)
        if m:
            o_start, o_len = int(m.group(1)), int(m.group(2) or 1)
            n_start, n_len = int(m.group(3)), int(m.group(4) or 1)
            hunks.append((o_start + (o_len == 0), o_len, n_start + (n_len == 0), n_len, []))
        elif hunks and line[:1] in (" ", "+", "-"):
            hunks[-1][4].append(line[0])
        elif hunks and line == "":
            hunks[-1][4].append(" ")  # an empty context line may lose its leading space
    return hunks


def _removed_line_numbers(patch: str) -> list[int]:
    """Pre-image line numbers of the lines a patch removed."""
    removed = []
    for o_start, _o_len, _n_start, _n_len, kinds in _parse_hunks(patch):
        o = o_start
        for k in kinds:
            if k == "-":
                removed.append(o)
            if k != "+":
                o += 1
    return removed


def _map_to_parent(patch: str, n: int) -> int | None:
    """Map post-image line n of a patch to its pre-image line, or None if the patch added it."""
    offset = 0
    for o_start, o_len, n_start, n_len, kinds in _parse_hunks(patch):
        if n < n_start:
            break
        if n < n_start + n_len:
            o, w = o_start, n_start
            for k in kinds:
                if k == "+":
                    if w == n:
                        return None
                    w += 1
                elif k == "-":
                    o += 1
                else:
                    if w == n:
                        return o
                    o += 1
                    w += 1
            raise _AmbiguousError("hunk line count does not match its header")
        offset = (n_start + n_len) - (o_start + o_len)
    return n - offset


def _file_entry(detail: dict | None, path: str) -> dict | None:
    for f in (detail or {}).get("files", []):
        if f.get("filename") == path:
            return f
    return None


def _drop_reason(
    action: dict,
    commits: list[dict],
    details_by_sha: dict[str, dict],
    a_idx: int,
) -> str | None:
    """Return why an action is out of the reviewed scope, or None to keep it."""
    path = action.get("file_path")
    sha = action.get("commit_sha")
    if not path or not sha:
        return None
    f_idx = _index_of(commits, sha)
    if f_idx is None or f_idx <= a_idx:
        return None

    for i in range(a_idx + 1, f_idx + 1):
        entry = _file_entry(details_by_sha.get(commits[i]["sha"]), path)
        if entry is not None and entry.get("status") == "added":
            return "file_created_after_last_reviewed_commit"

    fix = _file_entry(details_by_sha.get(commits[f_idx]["sha"]), path)
    if fix is None or not fix.get("patch"):
        return None
    removed = _removed_line_numbers(fix["patch"])
    if not removed:
        return None  # pure addition

    try:
        for n in removed:
            line = n
            added_after_a = False
            for i in range(f_idx - 1, a_idx, -1):
                entry = _file_entry(details_by_sha.get(commits[i]["sha"]), path)
                if entry is None or not (entry.get("patch") or entry.get("additions") or entry.get("deletions")):
                    continue  # file untouched, or a mode-only change
                if entry.get("status") not in ("modified", "changed") or not entry.get("patch"):
                    raise _AmbiguousError(f"{path} {entry.get('status')} in {commits[i]['sha']}")
                mapped = _map_to_parent(entry["patch"], line)
                if mapped is None:
                    added_after_a = True
                    break
                line = mapped
            if not added_after_a:
                return None  # this line already existed at the last reviewed commit
    except _AmbiguousError:
        return None
    return "fixed_lines_added_after_last_reviewed_commit"


def _index_of(commits: list[dict], sha: str) -> int | None:
    """Index of a commit by exact or prefix SHA match (same rule as the review-commit split)."""
    for i, c in enumerate(commits):
        full = c.get("sha", "")
        if full and (full == sha or full.startswith(sha) or sha.startswith(full)):
            return i
    return None


def split_actions_at_last_reviewed(
    actions: list[dict],
    commits: list[dict],
    details_by_sha: dict[str, dict],
    last_reviewed_sha: str,
) -> tuple[list[dict], list[dict]]:
    """Split actions into (kept, dropped) at the last reviewed commit.

    Dropped actions carry `drop_reason` and `last_reviewed_commit`. If the commit is not in
    the PR's commit list, nothing is dropped.
    """
    a_idx = _index_of(commits, last_reviewed_sha)
    if a_idx is None:
        return actions, []
    kept, dropped = [], []
    for action in actions:
        reason = _drop_reason(action, commits, details_by_sha, a_idx)
        if reason is None:
            kept.append(action)
        else:
            dropped.append({**action, "drop_reason": reason, "last_reviewed_commit": commits[a_idx]["sha"]})
    return kept, dropped

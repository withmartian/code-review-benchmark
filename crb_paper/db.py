"""Read-only Cloud SQL access for the finetuning pipeline.

The dashboard's SQL (`online/api_service/src/db.rs`) is the source of
truth; this module mirrors that join shape for the columns the
`filters.py` and `pairs.py` modules need, returning row dicts in
Python.

Connections are forced read-only at the session level. Credentials come
from env vars `GCP_SQL_USER`, `GCP_SQL_PASSWORD`, `GCP_SQL_DB`, falling
back to loading `latentqa/.env` if present.

Connect via the Cloud SQL Proxy on `localhost:15432`:

    /tmp/cloud-sql-proxy feisty-gasket-486610-h2:us-central1:crb-main \\
        --port 15432

Run as a script for a smoke test:

    python crb_paper/db.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Iterator, Optional

import json

import psycopg2
import psycopg2.extras


# ---------------------------------------------------------------------------
# Env loading
# ---------------------------------------------------------------------------

# Default fallback location — the latentqa workspace's .env, where this
# project's credentials live for the user. If you've moved them, set the
# CRB_ENV_FILE env var to point at the new location.
_LATENTQA_ENV = Path(
    "~/Documents/martian-projects/codereview-latentqa/latentqa/.env"
).expanduser()


def load_env(path: Optional[Path] = None) -> Optional[Path]:
    """Best-effort load of a .env file. Returns the loaded path or None
    if nothing was loaded. Safe to call before every connect."""
    if all(os.environ.get(k) for k in ("GCP_SQL_USER", "GCP_SQL_PASSWORD")):
        return None  # already in env, no need to load

    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    if "CRB_ENV_FILE" in os.environ:
        candidates.append(Path(os.environ["CRB_ENV_FILE"]))
    candidates.append(_LATENTQA_ENV)

    try:
        from dotenv import load_dotenv  # type: ignore[import-untyped]
    except ImportError:
        return None

    for cand in candidates:
        if cand.exists():
            load_dotenv(cand, override=False)
            return cand
    return None


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect_readonly(
    *,
    host: str = "127.0.0.1",
    port: int = 15432,
    connect_timeout: int = 10,
) -> "psycopg2.extensions.connection":
    """Open a READ-ONLY connection through the Cloud SQL Proxy. The
    session is locked to read-only and autocommit is on, so any
    accidental DML raises immediately."""
    load_env()
    user = os.environ.get("GCP_SQL_USER")
    password = os.environ.get("GCP_SQL_PASSWORD")
    dbname = os.environ.get("GCP_SQL_DB", "postgres")
    if not user or not password:
        raise RuntimeError(
            "GCP_SQL_USER / GCP_SQL_PASSWORD not set. Either export them "
            "or place a .env file at one of: $CRB_ENV_FILE, "
            f"{_LATENTQA_ENV}"
        )
    conn = psycopg2.connect(
        host=host, port=port,
        user=user, password=password, dbname=dbname,
        connect_timeout=connect_timeout,
    )
    conn.set_session(readonly=True, autocommit=True)
    return conn


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

# Column shape mirrors `db.rs::RawRow` plus the fields `pairs.py` needs
# (pr_title, pr_created_at, bot_suggestions, matching_results, assembled).
# `assembled` is the unified-timeline JSON used to construct the diff
# downstream — we fetch it here even though step 3 (inspection) doesn't
# need it, so step 5/6 can reuse the same fetcher.
_BASE_COLUMNS = """
    p.id              AS pr_id,
    la.chatbot_id,
    c.github_username,
    c.display_name,
    la.precision,
    la.recall,
    p.bot_reviewed_at,
    p.pr_created_at,
    p.diff_lines,
    p.pr_author,
    p.repo_name,
    p.pr_title,
    pl.labels         AS pr_labels_json,
    (p.reviews IS NOT NULL AND p.reviews != '[]') AS has_reviews,
    p.engagement_signals,
    la.bot_suggestions,
    la.matching_results,
    p.assembled,
    p.commit_details
"""

_FROM_JOIN = """
    FROM llm_analyses la
    JOIN prs p        ON la.pr_id = p.id
    JOIN chatbots c   ON la.chatbot_id = c.id
    LEFT JOIN pr_labels pl ON pl.pr_id = la.pr_id
                          AND pl.chatbot_id = la.chatbot_id
"""


def fetch_pr_analyses(
    conn,
    *,
    only_merged: bool = True,
    limit: Optional[int] = None,
) -> list[dict]:
    """Fetch the (PR × bot) analyses join. Returns a list of dicts in
    the shape `filters.apply_filters` and `pairs.build_*` expect.

    When `only_merged=True` (the dashboard default) we exclude
    unmerged PRs to match leaderboard semantics.
    """
    where = "WHERE p.pr_merged = TRUE" if only_merged else ""
    sql = f"SELECT {_BASE_COLUMNS} {_FROM_JOIN} {where} ORDER BY p.bot_reviewed_at NULLS FIRST"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return [_normalize_row(dict(r)) for r in cur.fetchall()]


def fetch_ignored_chatbots(conn) -> set[str]:
    """The dashboard's `ignored_tools` table — bots whose results we
    drop from the leaderboard (and from training data unless explicitly
    re-included via `FilterParams.include_ignored`)."""
    with conn.cursor() as cur:
        cur.execute("SELECT github_username FROM ignored_tools")
        return {r[0] for r in cur.fetchall()}


def _normalize_row(row: dict) -> dict:
    """Add the field aliases `filters.py` and `pairs.py` expect on top
    of the raw column names. Keeps the original columns too so SQL
    callers can still see them."""
    row.setdefault("chatbot", row.get("github_username"))
    row.setdefault("diff", build_unified_diff(row.get("commit_details")))
    return row


def build_unified_diff(commit_details) -> str:
    """Concatenate per-file patches from `prs.commit_details` into a
    single unified-diff string. Each file gets a synthetic
    `diff --git a/<file> b/<file>` header before its `@@`-prefixed hunks.

    Multi-commit PRs may produce duplicate per-file sections (one per
    commit that touched the file); that's intentional — the model sees
    the actual revision history. The pairs.py long-prompt cutoff drops
    any prompt that exceeds the configured token limit.
    """
    if not commit_details:
        return ""
    if isinstance(commit_details, str):
        try:
            commits = json.loads(commit_details)
        except json.JSONDecodeError:
            return ""
    else:
        commits = commit_details

    parts: list[str] = []
    for commit in commits or []:
        for f in (commit.get("files") or []):
            patch = f.get("patch")
            if not patch:
                continue
            filename = f.get("filename") or "<unknown>"
            parts.append(f"diff --git a/{filename} b/{filename}\n{patch}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Smoke test — `python crb_paper/db.py`
# ---------------------------------------------------------------------------

def _smoke() -> None:
    print(f"Loading env from: {load_env() or '(env vars already set)'}")
    conn = connect_readonly()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user, current_database()")
            user, db = cur.fetchone()
            print(f"connected as {user} to {db}")

            for sql, label in [
                ("SELECT COUNT(*) FROM prs", "prs"),
                ("SELECT COUNT(*) FROM prs WHERE pr_merged = TRUE", "prs (merged)"),
                ("SELECT COUNT(*) FROM llm_analyses", "llm_analyses"),
                ("SELECT COUNT(*) FROM chatbots", "chatbots"),
                ("SELECT COUNT(*) FROM ignored_tools", "ignored_tools"),
            ]:
                cur.execute(sql)
                print(f"  {label:25s} {cur.fetchone()[0]}")

        ignored = fetch_ignored_chatbots(conn)
        print(f"ignored chatbots: {sorted(ignored)}")

        sample = fetch_pr_analyses(conn, limit=3)
        print(f"sample {len(sample)} rows. Keys: {sorted(sample[0].keys()) if sample else '<empty>'}")
        for r in sample:
            print(f"  pr_id={r['pr_id']} bot={r['github_username']} repo={r['repo_name']}")
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        _smoke()
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)

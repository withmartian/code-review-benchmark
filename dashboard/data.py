"""Database queries for the Streamlit dashboard — synchronous wrappers."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import streamlit as st


def _is_postgres(database_url: str) -> bool:
    return database_url.startswith("postgresql")


def _get_sync_connection(database_url: str):
    """Get a synchronous connection (SQLite or PostgreSQL)."""
    if _is_postgres(database_url):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(database_url, row_factory=dict_row)
    else:
        db_path = database_url.replace("sqlite:///", "")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn


def _fetchall(conn, sql: str, args=None) -> list[dict[str, Any]]:
    """Execute a query and return all rows as dicts."""
    cur = conn.execute(sql, args or ())
    rows = cur.fetchall()
    return [dict(r) for r in rows]


def _placeholder(database_url: str) -> str:
    """Return the correct placeholder style."""
    return "%s" if _is_postgres(database_url) else "?"


@st.cache_data(ttl=300)
def get_chatbots(database_url: str) -> list[dict[str, Any]]:
    conn = _get_sync_connection(database_url)
    try:
        return _fetchall(conn, "SELECT * FROM chatbots ORDER BY github_username")
    finally:
        conn.close()


@st.cache_data(ttl=300)
def get_analyses(database_url: str, chatbot_id: int | None = None) -> list[dict[str, Any]]:
    ph = _placeholder(database_url)
    conn = _get_sync_connection(database_url)
    try:
        base = """SELECT la.*, p.repo_name, p.pr_number, p.pr_url, p.pr_created_at,
                         p.bot_reviewed_at, p.diff_lines, c.github_username, c.display_name,
                         pl.labels as pr_labels_json
                  FROM llm_analyses la
                  JOIN prs p ON la.pr_id = p.id
                  JOIN chatbots c ON la.chatbot_id = c.id
                  LEFT JOIN pr_labels pl ON pl.pr_id = la.pr_id AND pl.chatbot_id = la.chatbot_id"""
        if chatbot_id is not None:
            return _fetchall(conn, f"{base} WHERE la.chatbot_id = {ph} ORDER BY la.analyzed_at DESC", (chatbot_id,))
        return _fetchall(conn, f"{base} ORDER BY la.analyzed_at DESC")
    finally:
        conn.close()


@st.cache_data(ttl=300)
def get_status_summary(database_url: str) -> list[dict[str, Any]]:
    conn = _get_sync_connection(database_url)
    try:
        return _fetchall(conn,
            """SELECT c.github_username, p.status, COUNT(*) as count
               FROM prs p
               JOIN chatbots c ON p.chatbot_id = c.id
               GROUP BY c.github_username, p.status
               ORDER BY c.github_username, p.status""")
    finally:
        conn.close()


def delete_prs(database_url: str, pr_ids: list[int]) -> int:
    """Delete PRs and their analyses from the database. Returns number deleted."""
    if not pr_ids:
        return 0
    ph = _placeholder(database_url)
    conn = _get_sync_connection(database_url)
    try:
        placeholders = ",".join(ph for _ in pr_ids)
        conn.execute(f"DELETE FROM llm_analyses WHERE pr_id IN ({placeholders})", tuple(pr_ids))
        cur = conn.execute(f"DELETE FROM prs WHERE id IN ({placeholders})", tuple(pr_ids))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


@st.cache_data(ttl=300)
def get_daily_metrics(database_url: str, chatbot_id: int | None = None) -> list[dict[str, Any]]:
    """Get daily average precision/recall/F1 for plotting."""
    ph = _placeholder(database_url)
    date_fn = "p.bot_reviewed_at::date" if _is_postgres(database_url) else "DATE(p.bot_reviewed_at)"
    conn = _get_sync_connection(database_url)
    try:
        base = f"""SELECT
                     {date_fn} as date,
                     c.github_username,
                     AVG(la.precision) as avg_precision,
                     AVG(la.recall) as avg_recall,
                     AVG(la.f_beta) as avg_f_beta,
                     COUNT(*) as pr_count
                   FROM llm_analyses la
                   JOIN prs p ON la.pr_id = p.id
                   JOIN chatbots c ON la.chatbot_id = c.id"""
        group = f"GROUP BY {date_fn}, c.github_username ORDER BY date"
        if chatbot_id is not None:
            return _fetchall(conn,
                f"{base} WHERE la.chatbot_id = {ph} AND la.precision IS NOT NULL {group}",
                (chatbot_id,))
        return _fetchall(conn, f"{base} WHERE la.precision IS NOT NULL {group}")
    finally:
        conn.close()

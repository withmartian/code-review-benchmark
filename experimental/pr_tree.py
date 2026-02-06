"""Generate a flat-timeline HTML visualization of PR events from assembled.json.

Usage:
    uv run python experimental/pr_tree.py output/baz-reviewer\\[bot\\]/withmartian/ares/71/
"""

import html
import json
import sys
from datetime import datetime
from pathlib import Path


def load_json(path: Path):
    with open(path) as f:
        return json.load(f)


def short_sha(sha: str) -> str:
    return sha[:7] if sha else "???"


def fmt_date(iso: str) -> str:
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00"):
        try:
            return datetime.strptime(iso, fmt).strftime("%b %d %H:%M")
        except ValueError:
            continue
    return iso[:16]


def parse_datetime(iso: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00"):
        try:
            return datetime.strptime(iso, fmt)
        except ValueError:
            continue
    return datetime.min


def esc(text: str) -> str:
    return html.escape(text or "")


def render_comment_body(body: str) -> str:
    """Render a comment body as HTML, preserving code blocks."""
    lines = body.split("\n")
    result = []
    in_code = False
    for line in lines:
        if line.startswith("```"):
            if in_code:
                result.append("</code></pre>")
                in_code = False
            else:
                lang = line[3:].strip()
                result.append(f'<pre><code class="lang-{esc(lang)}">')
                in_code = True
        elif in_code:
            result.append(esc(line))
        else:
            result.append(f"<p>{esc(line)}</p>" if line.strip() else "")
    if in_code:
        result.append("</code></pre>")
    return "\n".join(result)


def build_timeline(assembled: dict) -> list[dict]:
    """Merge events + review_threads into one sorted timeline."""
    items = []
    merged_shas = {
        e["data"]["sha"]
        for e in assembled["events"]
        if e["event_type"] == "commit"
    }

    # Collect commits sorted by timestamp so we can look up "most recent commit before X"
    commit_entries = []
    for ev in assembled["events"]:
        if ev["event_type"] == "commit":
            commit_entries.append(ev)
    commit_entries.sort(key=lambda e: parse_datetime(e["timestamp"]))

    for ev in assembled["events"]:
        et = ev["event_type"]
        if et == "commit":
            items.append({
                "kind": "commit",
                "timestamp": ev["timestamp"],
                "data": ev,
                "dead": False,
            })
        elif et == "issue_comment":
            items.append({
                "kind": "issue_comment",
                "timestamp": ev["timestamp"],
                "data": ev,
                "dead": False,
            })
        elif et == "review":
            state = ev["data"].get("state", "")
            body = ev["data"].get("body", "")
            if state in ("APPROVED", "CHANGES_REQUESTED") or body:
                dead = ev["data"].get("commit_id", "") not in merged_shas
                items.append({
                    "kind": "review",
                    "timestamp": ev["timestamp"],
                    "data": ev,
                    "dead": dead,
                })
        # Skip review_comment and pr_opened

    for thread in assembled.get("review_threads", []):
        if not thread.get("comments"):
            continue
        ts = thread["comments"][0]["created_at"]
        # Heuristic: thread is "dead" if the most recent commit before it is not in merged_shas
        dead = False
        thread_dt = parse_datetime(ts)
        latest_commit_before = None
        for ce in commit_entries:
            if parse_datetime(ce["timestamp"]) <= thread_dt:
                latest_commit_before = ce
            else:
                break
        if latest_commit_before:
            dead = latest_commit_before["data"]["sha"] not in merged_shas
        items.append({
            "kind": "thread",
            "timestamp": ts,
            "data": thread,
            "dead": dead,
        })

    items.sort(key=lambda x: parse_datetime(x["timestamp"]))
    return items


def generate_html(assembled: dict, timeline: list[dict]) -> str:
    pr_number = assembled.get("pr_number", "?")
    repo = assembled.get("repo_name", "")
    pr_title = assembled.get("pr_title", "")
    pr_merged = assembled.get("pr_merged")
    pr_author = assembled.get("pr_author", "")

    merge_label = ""
    if pr_merged is True:
        merge_label = " (merged)"
    elif pr_merged is False:
        merge_label = " (not merged)"

    title_part = f" &mdash; {esc(pr_title)}" if pr_title else ""
    author_part = f" by {esc(pr_author)}" if pr_author else ""

    parts = [f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>PR #{pr_number} Timeline</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 960px; margin: 2em auto; background: #0d1117; color: #c9d1d9; }}
h1 {{ color: #58a6ff; font-size: 1.4em; }}
.subtitle {{ color: #8b949e; font-size: 0.9em; margin-top: -0.8em; margin-bottom: 1.5em; }}
.timeline {{ padding-left: 0.5em; }}
.entry {{ margin: 0.3em 0; }}
.commit {{ color: #8b949e; font-size: 0.92em; padding: 0.15em 0; }}
.commit .sha {{ color: #58a6ff; font-family: monospace; }}
.commit .icon {{ color: #3fb950; }}
.review-entry {{ padding: 0.3em 0.6em; font-size: 0.92em; border-left: 3px solid transparent; margin: 0.3em 0; }}
.review-entry.APPROVED {{ border-left-color: #3fb950; }}
.review-entry.CHANGES_REQUESTED {{ border-left-color: #f85149; }}
.review-entry.dead {{ opacity: 0.5; }}
.review-entry .state {{ font-weight: 600; }}
.review-entry .state.APPROVED {{ color: #3fb950; }}
.review-entry .state.CHANGES_REQUESTED {{ color: #f85149; }}
.issue-comment {{ margin: 0.4em 0; padding: 0.5em 0.8em; border-left: 2px solid #30363d; background: #161b22; border-radius: 0 6px 6px 0; font-size: 0.9em; }}
.thread-entry {{ margin: 0.4em 0; }}
.thread-entry details {{ margin: 0; }}
.thread-entry summary {{ cursor: pointer; padding: 0.3em 0.5em; border-radius: 4px; font-size: 0.92em; border-left: 3px solid #3fb950; padding-left: 0.5em; }}
.thread-entry summary:hover {{ background: #161b22; }}
.thread-entry.dead summary {{ border-left-color: #f85149; opacity: 0.6; }}
.comment {{ margin: 0.3em 0 0.3em 1.5em; padding: 0.5em 0.8em; border-left: 2px solid #30363d; background: #161b22; border-radius: 0 6px 6px 0; font-size: 0.9em; }}
.comment .author {{ color: #58a6ff; font-weight: 600; }}
.comment .date {{ color: #484f58; font-size: 0.85em; margin-left: 0.5em; }}
.comment p {{ margin: 0.3em 0; }}
.comment pre {{ background: #0d1117; padding: 0.5em; border-radius: 4px; overflow-x: auto; font-size: 0.85em; }}
.diff-hunk {{ background: #161b22; padding: 0.4em; border-radius: 4px; font-family: monospace; font-size: 0.8em; white-space: pre-wrap; overflow-x: auto; margin: 0.3em 0 0.3em 1.5em; color: #8b949e; max-height: 12em; overflow-y: auto; }}
.diff-hunk .add {{ color: #3fb950; }}
.diff-hunk .del {{ color: #f85149; }}
.meta {{ color: #484f58; font-size: 0.85em; }}
.path {{ color: #d2a8ff; font-family: monospace; font-size: 0.85em; }}
</style></head><body>
<h1>PR #{pr_number}: {esc(repo)}{title_part}{esc(merge_label)}</h1>
<p class="subtitle">{author_part}</p>
<div class="timeline">
"""]

    for item in timeline:
        kind = item["kind"]

        if kind == "commit":
            ev = item["data"]
            sha = ev["data"]["sha"]
            msg = ev["data"]["message"].split("\n")[0]
            actor = ev.get("actor", "")
            ts = ev["timestamp"]
            parts.append(
                f'<div class="entry commit">'
                f'<span class="icon">\U0001f4dd</span> '
                f'<span class="sha">{short_sha(sha)}</span> '
                f'{esc(msg)} '
                f'<span class="meta">({esc(actor)}, {fmt_date(ts)})</span>'
                f'</div>'
            )

        elif kind == "issue_comment":
            ev = item["data"]
            actor = ev.get("actor", "?")
            body = ev["data"].get("body", "")
            ts = ev["timestamp"]
            parts.append(f'<div class="entry issue-comment">')
            parts.append(f'<span class="author">{esc(actor)}</span>')
            parts.append(f'<span class="date">{fmt_date(ts)}</span>')
            parts.append(render_comment_body(body))
            parts.append('</div>')

        elif kind == "review":
            ev = item["data"]
            state = ev["data"].get("state", "")
            body = ev["data"].get("body", "")
            actor = ev.get("actor", "?")
            ts = ev["timestamp"]
            dead_cls = " dead" if item["dead"] else ""
            state_icon = "\u2705" if state == "APPROVED" else "\u274c" if state == "CHANGES_REQUESTED" else ""
            parts.append(f'<div class="entry review-entry {esc(state)}{dead_cls}">')
            parts.append(
                f'{state_icon} <span class="state {esc(state)}">{esc(state)}</span> '
                f'by {esc(actor)} '
                f'<span class="meta">({fmt_date(ts)})</span>'
            )
            if body:
                parts.append(f'<div class="comment" style="margin-left:0.5em; margin-top:0.3em;">')
                parts.append(render_comment_body(body))
                parts.append('</div>')
            parts.append('</div>')

        elif kind == "thread":
            thread = item["data"]
            resolved = thread.get("is_resolved", False)
            resolved_by = thread.get("resolved_by", "")
            check = "\u2705" if resolved else "\u26aa"
            res_label = "resolved" if resolved else "open"
            if resolved and resolved_by:
                res_label = f"resolved by {resolved_by}"
            first_comment = thread["comments"][0]
            first_body = first_comment["body"][:100].replace("\n", " ")
            path = thread.get("path", "") or first_comment.get("path", "")
            n_comments = len(thread["comments"])
            dead_cls = " dead" if item["dead"] else ""

            parts.append(f'<div class="entry thread-entry{dead_cls}">')
            parts.append(f'<details><summary>{check} Thread ({res_label})')
            if path:
                parts.append(f' &mdash; <span class="path">{esc(path)}</span>')
            parts.append(f' <span class="meta">[{n_comments} comment{"s" if n_comments != 1 else ""}]</span>')
            parts.append('</summary>\n')

            # Diff hunk from first comment
            hunk = first_comment.get("diff_hunk", "")
            if hunk:
                hunk_html = []
                for hline in hunk.split("\n"):
                    if hline.startswith("+"):
                        hunk_html.append(f'<span class="add">{esc(hline)}</span>')
                    elif hline.startswith("-"):
                        hunk_html.append(f'<span class="del">{esc(hline)}</span>')
                    else:
                        hunk_html.append(esc(hline))
                parts.append(f'<div class="diff-hunk">{chr(10).join(hunk_html)}</div>')

            for comment in thread["comments"]:
                author = comment.get("author", "?")
                date = fmt_date(comment.get("created_at", ""))
                parts.append(f'<div class="comment"><span class="author">{esc(author)}</span>')
                parts.append(f'<span class="date">{date}</span>')
                parts.append(render_comment_body(comment["body"]))
                parts.append('</div>')

            parts.append('</details></div>')

    parts.append('</div></body></html>')
    return "\n".join(parts)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <pr-directory>", file=sys.stderr)
        sys.exit(1)

    pr_dir = Path(sys.argv[1])
    if not pr_dir.is_dir():
        print(f"Error: {pr_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    assembled_path = pr_dir / "assembled.json"
    if not assembled_path.exists():
        print(f"Error: {assembled_path} not found", file=sys.stderr)
        sys.exit(1)

    assembled = load_json(assembled_path)
    timeline = build_timeline(assembled)
    html_content = generate_html(assembled, timeline)

    out_path = pr_dir / "tree.html"
    out_path.write_text(html_content)
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()

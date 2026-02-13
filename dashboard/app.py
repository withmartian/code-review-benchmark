"""Streamlit dashboard for PR review analysis.

Run: streamlit run dashboard/app.py
"""

from __future__ import annotations

import json
import os

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
from dotenv import load_dotenv

load_dotenv(override=True)

from dashboard.data import delete_prs, get_analyses, get_chatbots, get_status_summary
from dashboard.plots import f_beta_over_time, precision_recall_scatter, status_summary_chart

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///pr_review.db")

st.set_page_config(page_title="PR Review Analysis", layout="wide")
st.title("PR Review Bot Analysis Dashboard")

# Sidebar
st.sidebar.header("Filters")

chatbots = get_chatbots(DATABASE_URL)
chatbot_options = {b["github_username"]: b["id"] for b in chatbots}

selected_chatbot = st.sidebar.selectbox(
    "Chatbot",
    options=["All"] + list(chatbot_options.keys()),
)

chatbot_id = chatbot_options.get(selected_chatbot) if selected_chatbot != "All" else None

# Date range
col1, col2 = st.sidebar.columns(2)
start_date = col1.date_input("Start Date", value=None)
end_date = col2.date_input("End Date", value=None)

# F-beta parameter
beta = st.sidebar.number_input("F-beta (\u03B2)", min_value=0.1, max_value=10.0, value=1.0, step=0.1)
min_daily_prs = st.sidebar.number_input("Min PRs per day", min_value=0, value=0, step=1, help="Hide days with fewer PRs than this from the F\u03B2 chart")

# -- Label filters --
# Pre-fetch analyses to extract label options
_all_analyses = get_analyses(DATABASE_URL, chatbot_id=chatbot_id)

# Diff lines filter
diff_over_2k = st.sidebar.checkbox("More than 2k LOC", value=False)
diff_range = st.sidebar.slider(
    "Diff lines range",
    min_value=0, max_value=2000, value=(0, 2000), step=50,
    help="Filter PRs by number of changed lines",
    disabled=diff_over_2k,
)


def _parse_labels(row):
    raw = row.get("pr_labels_json")
    if raw is None:
        return None
    lb = json.loads(raw) if isinstance(raw, str) else raw
    # Normalize all string values to lowercase for consistent filtering/display
    for k, v in lb.items():
        if isinstance(v, str):
            lb[k] = v.lower()
        elif isinstance(v, list):
            lb[k] = [x.lower() if isinstance(x, str) else x for x in v]
    return lb


_all_label_dicts = [_parse_labels(a) for a in _all_analyses]
_label_domains = sorted({lb["domain"] for lb in _all_label_dicts if lb})
_label_languages = sorted({lb["language"] for lb in _all_label_dicts if lb})
_label_pr_types = sorted({lb["pr_type"] for lb in _all_label_dicts if lb})
_label_severities = sorted({lb["severity"] for lb in _all_label_dicts if lb})

st.sidebar.header("Label Filters")
sel_domains = st.sidebar.multiselect("Domain", options=_label_domains)
sel_languages = st.sidebar.multiselect("Language", options=_label_languages)
sel_pr_types = st.sidebar.multiselect("PR Type", options=_label_pr_types)
sel_severities = st.sidebar.multiselect("Severity", options=_label_severities)


def _label_matches(row) -> bool:
    """Return True if the row passes all active label filters."""
    lb = _parse_labels(row)
    if lb is None:
        # No labels: include only if no label filters are active
        return not (sel_domains or sel_languages or sel_pr_types or sel_severities)
    if sel_domains and lb.get("domain") not in sel_domains:
        return False
    if sel_languages and lb.get("language") not in sel_languages:
        return False
    if sel_pr_types and lb.get("pr_type") not in sel_pr_types:
        return False
    if sel_severities and lb.get("severity") not in sel_severities:
        return False
    return True

# Apply label + diff_lines filters once — all charts and tables use this filtered list
def _diff_lines_ok(row) -> bool:
    dl = row.get("diff_lines")
    if dl is None:
        return not diff_over_2k and diff_range == (0, 2000)
    if diff_over_2k:
        return dl > 2000
    return diff_range[0] <= dl <= diff_range[1]

analyses = [a for a in _all_analyses if _label_matches(a) and _diff_lines_ok(a)]

start_str = str(start_date) if start_date else None
end_str = str(end_date) if end_date else None

# Status summary
st.header("Pipeline Status")
_any_label_filter = sel_domains or sel_languages or sel_pr_types or sel_severities
if _any_label_filter:
    # Compute status counts from label-filtered analyses (only analyzed PRs have labels)
    from collections import Counter
    _status_counts: Counter = Counter()
    for _a in analyses:
        _status_counts[(_a["github_username"], "analyzed")] += 1
    status_data = [{"github_username": k[0], "status": k[1], "count": v} for k, v in _status_counts.items()]
else:
    status_data = get_status_summary(DATABASE_URL)
if status_data:
    fig = status_summary_chart(status_data)
    if fig:
        st.plotly_chart(fig, use_container_width=True)
else:
    st.info("No data yet. Run the pipeline to populate the database.")

# F-beta over time — computed from filtered analyses
st.header("F\u03B2 Score Over Time")
st.caption("Includes all PRs with at least one bot suggestion (precision not null).")


def _build_daily_metrics(rows: list[dict]) -> list[dict]:
    """Aggregate filtered analyses into daily metrics for the F-beta chart."""
    import pandas as pd
    filtered = [r for r in rows if r.get("precision") is not None]
    if not filtered:
        return []
    df = pd.DataFrame(filtered)
    df["bot_reviewed_at"] = pd.to_datetime(df["bot_reviewed_at"], utc=True, errors="coerce")
    df["date"] = df["bot_reviewed_at"].dt.date
    agg = df.groupby(["date", "github_username"]).agg(
        avg_precision=("precision", "mean"),
        avg_recall=("recall", "mean"),
        avg_f_beta=("f_beta", "mean"),
        pr_count=("precision", "count"),
    ).reset_index()
    return agg.to_dict("records")


daily_metrics = _build_daily_metrics(analyses)
if min_daily_prs > 0:
    daily_metrics = [d for d in daily_metrics if d["pr_count"] >= min_daily_prs]
if daily_metrics:
    fig = f_beta_over_time(daily_metrics, start_date=start_str, end_date=end_str, beta=beta)
    if fig:
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No data in selected date range.")
else:
    st.info("No analysis results yet. Run the analyze job first.")

# Precision / Recall explorer
st.header("Precision / Recall Explorer")
st.caption("Includes only PRs with both % acted on and # of comments acted on defined (requires at least one bot suggestion and one code fix). PR count may be lower than the F\u03B2 chart.")
if analyses:
    fig = precision_recall_scatter(analyses, start_date=start_str, end_date=end_str, beta=beta)
    if fig:
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No data in selected date range.")

# Detailed analysis table
st.header("Analysis Results")
if analyses:
    import pandas as pd

    # Inject parsed label fields into each row for table display
    for a in analyses:
        lb = _parse_labels(a)
        a["label_language"] = lb.get("language", "") if lb else ""
        a["label_domain"] = lb.get("domain", "") if lb else ""
        a["label_pr_type"] = lb.get("pr_type", "") if lb else ""
        a["label_severity"] = lb.get("severity", "") if lb else ""

    df = pd.DataFrame(analyses)
    display_cols = [
        "github_username", "repo_name", "pr_number", "pr_url",
        "diff_lines",
        "total_bot_comments", "matched_bot_comments",
        "precision", "recall", "f_beta",
        "label_language", "label_domain", "label_pr_type", "label_severity",
        "model_name", "analyzed_at",
    ]
    available = [c for c in display_cols if c in df.columns]
    st.dataframe(
        df[available],
        use_container_width=True,
        column_config={
            "pr_url": st.column_config.LinkColumn("PR URL", display_text="View PR"),
            "diff_lines": st.column_config.NumberColumn("Diff Lines"),
            "total_bot_comments": st.column_config.NumberColumn("# Comments"),
            "matched_bot_comments": st.column_config.NumberColumn("# Acted On"),
            "precision": st.column_config.NumberColumn("% Acted On", format="%.2f"),
            "recall": st.column_config.NumberColumn("# Acted On (ratio)", format="%.2f"),
            "f_beta": st.column_config.NumberColumn("F\u03B2", format="%.2f"),
            "label_language": st.column_config.TextColumn("Language"),
            "label_domain": st.column_config.TextColumn("Domain"),
            "label_pr_type": st.column_config.TextColumn("PR Type"),
            "label_severity": st.column_config.TextColumn("Severity"),
        },
    )
    # Per-PR detail view
    st.header("PR Detail View")
    pr_labels = [f"{a['repo_name']}#{a['pr_number']}" for a in analyses]
    selected_pr = st.selectbox("Select a PR to inspect", options=pr_labels)
    if selected_pr:
        idx = pr_labels.index(selected_pr)
        row = analyses[idx]

        def _parse_json(val):
            if val is None:
                return []
            if isinstance(val, str):
                return json.loads(val)
            return val

        suggestions = _parse_json(row.get("bot_suggestions"))
        actions = _parse_json(row.get("human_actions"))
        matches = _parse_json(row.get("matching_results"))
        pr_lbl = _parse_labels(row)

        if pr_lbl:
            with st.expander("Labels", expanded=True):
                lbl_cols = st.columns(4)
                lbl_cols[0].metric("Language", pr_lbl.get("language", ""))
                lbl_cols[1].metric("Domain", pr_lbl.get("domain", ""))
                lbl_cols[2].metric("PR Type", pr_lbl.get("pr_type", ""))
                lbl_cols[3].metric("Severity", pr_lbl.get("severity", ""))
                extra = []
                if pr_lbl.get("languages"):
                    extra.append(f"**Languages:** {', '.join(pr_lbl['languages'])}")
                if pr_lbl.get("framework"):
                    extra.append(f"**Framework:** {pr_lbl['framework']}")
                if pr_lbl.get("issue_types"):
                    extra.append(f"**Issue types:** {', '.join(pr_lbl['issue_types'])}")
                extra.append(f"**Test changes:** {'Yes' if pr_lbl.get('test_changes') else 'No'}")
                st.markdown(" | ".join(extra))

        col_s, col_a = st.columns(2)

        with col_s:
            with st.expander(f"Bot Suggestions ({len(suggestions)})", expanded=True):
                if suggestions:
                    for s in suggestions:
                        loc = ""
                        if s.get("file_path"):
                            loc = f" `{s['file_path']}"
                            if s.get("line_number"):
                                loc += f":{s['line_number']}"
                            loc += "`"
                        st.markdown(f"**[{s['issue_id']}]** ({s.get('category', '?')}/{s.get('severity', '?')}){loc} — {s.get('description', '')}")
                else:
                    st.write("No suggestions extracted.")

        with col_a:
            with st.expander(f"Human Actions ({len(actions)})", expanded=True):
                if actions:
                    for a in actions:
                        loc = ""
                        if a.get("file_path"):
                            loc = f" `{a['file_path']}`"
                        st.markdown(f"**[{a['action_id']}]** ({a.get('category', '?')}/{a.get('action_type', '?')}){loc} — {a.get('description', '')}")
                else:
                    st.write("No actions extracted.")

        with st.expander(f"Matching Results ({len(matches)})", expanded=True):
            if matches:
                for m in matches:
                    icon = "+" if m.get("matched") else "-"
                    action_ref = f" -> [{m['human_action_id']}]" if m.get("human_action_id") else ""
                    conf = m.get("confidence", 0)
                    st.markdown(f"**{icon} [{m['bot_issue_id']}]{action_ref}** (confidence: {conf:.2f}) — {m.get('reasoning', '')}")
            else:
                st.write("No matching results.")
else:
    st.info("No analysis results available.")

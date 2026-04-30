"""Silver-preset filter port from `online/api_service/src/compute.rs`.

The dashboard's leaderboard runs in Rust over an in-memory snapshot of
PRs joined with `llm_analyses` and `pr_labels`. For the finetuning
pipeline (see `crb_paper/README.md`) we need the same predicates in
Python so the training set's inclusion criteria match the published
silver-tier results exactly.

Operates on a list of row dicts shaped like the SELECT in `db.rs`:

    pr_id, chatbot_id, github_username, precision, recall,
    bot_reviewed_at, diff_lines, pr_author, repo_name,
    pr_labels_json, has_reviews, engagement_signals

The DB connection layer is out of scope for this module — pass rows in,
get filtered rows out. A synthetic smoke test lives at the bottom under
`if __name__ == "__main__":`.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Optional


# Silver preset — matches the "silver" row in the paper's filter ladder.
SILVER_PRESET: dict = {
    "require_human_engagement": True,
    "exclude_bot_authored": True,
    "exclude_self_authored": True,
    "max_author_repo_prs": 50,
    "min_repo_contributors": 2,
}


@dataclass
class FilterParams:
    """Mirrors `model.rs::FilterParams`. Dashboard-only knobs (beta,
    min_prs_per_day, min_total_prs, min_scored_prs) are omitted because
    they aggregate over chatbots, not records — the training pipeline
    only cares about per-row inclusion."""

    start_date: Optional[date] = None
    end_date: Optional[date] = None
    chatbots: Optional[list[str]] = None
    languages: Optional[list[str]] = None
    domains: Optional[list[str]] = None
    pr_types: Optional[list[str]] = None
    severities: Optional[list[str]] = None
    diff_lines_min: Optional[int] = None
    diff_lines_max: Optional[int] = None
    include_ignored: bool = False
    exclude_self_authored: bool = False
    require_reviews: bool = False
    exclude_bot_authored: bool = False
    min_repo_contributors: Optional[int] = None
    max_author_repo_prs: Optional[int] = None
    require_human_engagement: bool = False
    min_human_reviewers: Optional[int] = None
    min_commits_after_review: Optional[int] = None

    @classmethod
    def silver(cls, **overrides) -> "FilterParams":
        return cls(**{**SILVER_PRESET, **overrides})


# Mirrors GENERAL_BOT_NAMES in db.rs. Used to flag PRs authored by bots
# that aren't review tools (dependabot etc.) so `exclude_bot_authored`
# catches them.
GENERAL_BOT_NAMES: frozenset[str] = frozenset({
    "dependabot", "renovate", "github-actions", "codecov", "mergify",
    "snyk-bot", "greenkeeper", "imgbot", "stale", "allcontributors",
    "semantic-release-bot", "github-advanced-security", "llamapreview",
    "ai-coding-guardrails", "qodo-free-for-open-source-projects",
    "amazon-q-developer", "sourceryai", "github-code-quality",
    "copilot-pull-request-review", "copilot-pull-request-reviewer",
    "raycastbot", "clawdbot", "cometactions", "kilo-code-bot",
    "codecov-comment",
})


def _is_bot_username(username: str, known_bots: set[str]) -> bool:
    lower = username.lower()
    return lower.endswith("[bot]") or lower in known_bots


def _build_known_bots(chatbot_usernames: Iterable[str]) -> set[str]:
    bots: set[str] = set(GENERAL_BOT_NAMES)
    for name in chatbot_usernames:
        lower = name.lower()
        bots.add(lower.removesuffix("[bot]"))
        bots.add(lower)
    return bots


def _params_seed(params: FilterParams) -> int:
    """Deterministic seed mirroring `compute.rs::params_seed` so the
    `max_author_repo_prs` random sample is stable across re-runs of the
    same filter spec."""
    payload = json.dumps(
        {k: (v.isoformat() if isinstance(v, (date, datetime)) else v)
         for k, v in params.__dict__.items()},
        sort_keys=True,
        default=str,
    )
    return int(hashlib.sha256(payload.encode()).hexdigest()[:16], 16)


def _engagement_signals(raw) -> tuple[bool, int, int]:
    if not raw:
        return False, 0, 0
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return False, 0, 0
    return (
        bool(obj.get("has_human_engagement", False)),
        int(obj.get("human_reviewer_count", 0) or 0),
        int(obj.get("commits_after_review", 0) or 0),
    )


def _labels(raw) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {}


def _normalize_label(value) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip().lower()
    return s or None


def _coerce_date(ts) -> Optional[date]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, date):
        return ts
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def apply_filters(
    rows: list[dict],
    params: FilterParams,
    ignored_chatbots: Optional[set[str]] = None,
) -> list[dict]:
    """Apply `params` to `rows` and return the matching subset in input
    order. Aggregates (`repo_contributor_counts`, `author_repo_prs`) are
    computed over the input rows, matching `db.rs::build_snapshot`."""
    # ---- 1. Build the bot-username vocabulary ---------------------------
    # Used by `exclude_bot_authored` to decide whether the PR's *author*
    # is itself a bot (dependabot etc.), separately from the bot that
    # reviewed it.
    ignored_chatbots = ignored_chatbots or set()
    chatbot_usernames = {r["github_username"] for r in rows}
    known_bots = _build_known_bots(chatbot_usernames)

    # ---- 2. Build aggregates --------------------------------------------
    # `repo_contributors[repo]` = set of unique author logins, used by
    # `min_repo_contributors`. `author_repo_prs[(repo, author, bot)]` =
    # list of pr_ids, used by `max_author_repo_prs` capping.
    repo_contributors: dict[str, set[str]] = defaultdict(set)
    author_repo_prs: dict[tuple[str, str, str], list[int]] = defaultdict(list)

    # ---- 3. Per-row enrichment (parse JSON columns once) ---------------
    enriched: list[dict] = []
    for r in rows:
        labels = _labels(r.get("pr_labels_json"))
        engaged, n_reviewers, n_commits = _engagement_signals(r.get("engagement_signals"))
        pr_author = r.get("pr_author") or ""
        author_lower = pr_author.lower() or None
        github_username = r["github_username"]
        repo = r["repo_name"]

        if author_lower:
            repo_contributors[repo].add(author_lower)
            author_repo_prs[(repo, author_lower, github_username)].append(r["pr_id"])

        enriched.append({
            **r,
            "_self_authored": (
                author_lower == github_username.lower() if author_lower else False
            ),
            "_pr_author_is_bot": (
                _is_bot_username(pr_author, known_bots) if pr_author else False
            ),
            "_has_human_engagement": engaged,
            "_human_reviewer_count": n_reviewers,
            "_commits_after_review": n_commits,
            "_language": _normalize_label(labels.get("language")),
            "_domain": _normalize_label(labels.get("domain")),
            "_pr_type": _normalize_label(labels.get("pr_type")),
            "_severity": _normalize_label(labels.get("severity")),
        })

    # ---- 4. Pre-compute the max_author_repo_prs sample -----------------
    # Cap = "no single (repo, author, bot) triple contributes more than N
    # PRs." For triples over the cap, pick N at random with a seed
    # derived from `params` so re-runs of the same spec are stable.
    capped_pr_ids: Optional[set[int]] = None
    if params.max_author_repo_prs is not None:
        rng = random.Random(_params_seed(params))
        capped_pr_ids = set()
        cap = params.max_author_repo_prs
        for prs in author_repo_prs.values():
            if len(prs) > cap:
                shuffled = list(prs)
                rng.shuffle(shuffled)
                capped_pr_ids.update(shuffled[:cap])
            else:
                capped_pr_ids.update(prs)

    # ---- 5. Apply per-row predicates -----------------------------------
    out: list[dict] = []
    for r in enriched:
        if not params.include_ignored and r["github_username"] in ignored_chatbots:
            continue

        if params.chatbots:
            allow = {c.lower() for c in params.chatbots}
            if r["github_username"].lower() not in allow:
                continue

        if params.start_date or params.end_date:
            d = _coerce_date(r.get("bot_reviewed_at"))
            if d is None:
                continue
            if params.start_date and d < params.start_date:
                continue
            if params.end_date and d > params.end_date:
                continue

        if params.languages and r["_language"] not in {l.lower() for l in params.languages}:
            continue
        if params.domains and r["_domain"] not in {d.lower() for d in params.domains}:
            continue
        if params.pr_types and r["_pr_type"] not in {p.lower() for p in params.pr_types}:
            continue
        if params.severities and r["_severity"] not in {s.lower() for s in params.severities}:
            continue

        dl = r.get("diff_lines")
        if dl is not None:
            if params.diff_lines_min is not None and dl < params.diff_lines_min:
                continue
            if params.diff_lines_max is not None and dl > params.diff_lines_max:
                continue

        if params.exclude_self_authored and r["_self_authored"]:
            continue
        if params.require_reviews and not r.get("has_reviews"):
            continue
        if params.exclude_bot_authored and r["_pr_author_is_bot"]:
            continue

        if params.min_repo_contributors is not None:
            n = len(repo_contributors.get(r["repo_name"], set()))
            if n < params.min_repo_contributors:
                continue

        if params.require_human_engagement and not r["_has_human_engagement"]:
            continue
        if params.min_human_reviewers is not None:
            if r["_human_reviewer_count"] < params.min_human_reviewers:
                continue
        if params.min_commits_after_review is not None:
            if r["_commits_after_review"] < params.min_commits_after_review:
                continue

        if capped_pr_ids is not None and (r.get("pr_author") or ""):
            if r["pr_id"] not in capped_pr_ids:
                continue

        out.append(r)

    return out


# ---------------------------------------------------------------------------
# Synthetic smoke test — `python crb_paper/filters.py`
# ---------------------------------------------------------------------------

def _smoke() -> None:
    def row(**kw) -> dict:
        base = dict(
            pr_id=0,
            chatbot_id=1,
            github_username="cubic-dev-ai",
            precision=0.5,
            recall=0.5,
            bot_reviewed_at=datetime(2026, 4, 1),
            diff_lines=100,
            pr_author="alice",
            repo_name="org/repo-a",
            pr_labels_json=json.dumps({"language": "python", "domain": "backend"}),
            has_reviews=True,
            engagement_signals=json.dumps({
                "has_human_engagement": True,
                "human_reviewer_count": 1,
                "commits_after_review": 1,
            }),
        )
        base.update(kw)
        return base

    rows: list[dict] = [
        # Passes silver preset.
        row(pr_id=1, pr_author="alice", repo_name="org/repo-a"),
        row(pr_id=2, pr_author="bob", repo_name="org/repo-a"),
        # Fails: self-authored (PR author == bot's username).
        row(pr_id=3, pr_author="cubic-dev-ai", repo_name="org/repo-a"),
        # Fails: no human engagement.
        row(
            pr_id=4,
            pr_author="carol",
            repo_name="org/repo-a",
            engagement_signals=json.dumps({
                "has_human_engagement": False,
                "human_reviewer_count": 0,
                "commits_after_review": 0,
            }),
        ),
        # Fails: bot-authored PR.
        row(pr_id=5, pr_author="dependabot[bot]", repo_name="org/repo-a"),
        # Fails: only 1 unique contributor in this repo.
        row(pr_id=6, pr_author="dave", repo_name="org/repo-solo"),
    ]

    silver = FilterParams.silver()
    kept = apply_filters(rows, silver)
    kept_ids = sorted(r["pr_id"] for r in kept)
    assert kept_ids == [1, 2], f"expected [1, 2], got {kept_ids}"

    # Date range filter.
    bounded = apply_filters(
        rows,
        FilterParams.silver(start_date=date(2026, 5, 1)),
    )
    assert bounded == [], f"expected [], got {[r['pr_id'] for r in bounded]}"

    # max_author_repo_prs cap is deterministic across re-runs.
    many = [
        row(pr_id=100 + i, pr_author="prolific", repo_name="org/repo-many")
        for i in range(20)
    ]
    # Add a second author so min_repo_contributors=2 passes.
    many.append(row(pr_id=200, pr_author="other", repo_name="org/repo-many"))

    capped_a = apply_filters(many, FilterParams.silver(max_author_repo_prs=5))
    capped_b = apply_filters(many, FilterParams.silver(max_author_repo_prs=5))
    a_ids = sorted(r["pr_id"] for r in capped_a)
    b_ids = sorted(r["pr_id"] for r in capped_b)
    assert a_ids == b_ids, f"sampling should be deterministic; got {a_ids} vs {b_ids}"
    # 5 from `prolific` + 1 from `other`.
    assert len(capped_a) == 6, f"expected 6, got {len(capped_a)}"

    print(f"OK: silver kept {len(kept)}/{len(rows)} rows; cap kept {len(capped_a)}/{len(many)}")


if __name__ == "__main__":
    _smoke()

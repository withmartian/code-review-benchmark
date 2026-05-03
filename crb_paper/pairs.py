"""Preference-pair (DPO) and SFT-row extraction from CRB analyses.

Step 2 of the gating plan in `crb_paper/README.md`.

Each input row is one (PR × bot) analysis joined with the PR's diff and
title. The accepted/rejected status of each suggestion comes from
`llm_analyses.matching_results` (the judge's decision on whether the
human acted on it).

This module supports two prompt shapes (selected at dataset prep time
via `prepare_jsonl.py --mode`):

  * **PR-level**     — prompt = the whole PR's unified diff. Within-bot
                       DPO pairs (chosen and rejected from the same
                       PR×bot, possibly different files).
  * **Hunk-level**   — prompt = just the changed hunk the suggestion
                       anchors to (via `file_path` + `line_number`).
                       Within-(PR, bot, file) DPO pairs.

Both shapes share the same SFT/DPO trainers and eval harness. See
`crb_paper/CHANGELOG.md` for why both exist.

Pure functions only.
"""

from __future__ import annotations

import json
import random
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Iterable, Optional


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = (
    "You are reviewing a pull request. Identify one specific issue with "
    "the change below and describe it in 1–3 sentences.\n"
    "\n"
    "PR title: {pr_title}\n"
    "Diff:\n"
    "{diff}\n"
)

HUNK_PROMPT_TEMPLATE = (
    "You are reviewing a code change. Identify one specific issue with "
    "the change below and describe it in 1–3 sentences.\n"
    "\n"
    "File: {file_path}\n"
    "Diff:\n"
    "{hunk}\n"
)


def build_prompt(pr_title: str, diff: str) -> str:
    """Format the PR-level SFT/DPO prompt for one PR."""
    return PROMPT_TEMPLATE.format(pr_title=pr_title or "", diff=diff or "")


def build_hunk_prompt(file_path: str, hunk: str) -> str:
    """Format the hunk-level SFT/DPO prompt — scoped to one file's
    changed hunk(s) instead of the whole PR diff."""
    return HUNK_PROMPT_TEMPLATE.format(
        file_path=file_path or "<unknown>", hunk=hunk or "",
    )


# ---------------------------------------------------------------------------
# Hunk extraction
# ---------------------------------------------------------------------------

# Matches one `@@ -a,b +c,d @@` header. Group 1 = new-file start line.
_HUNK_HEADER_RE = re.compile(r"^@@\s*-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s*@@", re.MULTILINE)


def _split_hunks(patch: str) -> list[tuple[int, int, str]]:
    """Split a unified-diff patch (single file's `commit_details[i].files[j].patch`)
    into individual hunks. Returns `[(new_start, new_length, hunk_text), ...]`
    where `hunk_text` is the full `@@ ... @@` block including header and body."""
    if not patch:
        return []
    headers = list(_HUNK_HEADER_RE.finditer(patch))
    out: list[tuple[int, int, str]] = []
    for i, m in enumerate(headers):
        start = int(m.group(1))
        length = int(m.group(2) or 1)
        body_start = m.start()
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(patch)
        out.append((start, length, patch[body_start:body_end]))
    return out


def extract_hunk_around(
    commit_details,
    file_path: str,
    line_number: Optional[int] = None,
) -> Optional[str]:
    """Find the unified-diff hunk in `commit_details` that anchors a
    suggestion at (`file_path`, `line_number`).

    Strategy:
      1. Filter `commit_details` to entries touching `file_path`.
      2. If `line_number` is provided, return the hunk whose new-file
         line range contains it.
      3. Otherwise (or if no hunk contains the line), return the
         longest hunk for that file as a best-effort fallback.

    Returns `None` if the file isn't present in `commit_details`.
    """
    if not commit_details:
        return None
    if isinstance(commit_details, str):
        try:
            commit_details = json.loads(commit_details)
        except json.JSONDecodeError:
            return None

    file_patches: list[str] = []
    for commit in commit_details or []:
        for f in (commit.get("files") or []):
            if f.get("filename") == file_path:
                patch = f.get("patch")
                if patch:
                    file_patches.append(patch)

    if not file_patches:
        return None

    all_hunks: list[tuple[int, int, str]] = []
    for patch in file_patches:
        all_hunks.extend(_split_hunks(patch))
    if not all_hunks:
        # No `@@` markers — return the raw patch text (rare; binary files etc.)
        return file_patches[0]

    if line_number is not None:
        for start, length, body in all_hunks:
            if start <= line_number < start + max(length, 1):
                return body

    # Fallback: longest hunk we found for that file.
    return max(all_hunks, key=lambda h: len(h[2]))[2]


# ---------------------------------------------------------------------------
# Tokenization (pluggable; whitespace default + real-tokenizer factory)
# ---------------------------------------------------------------------------

LenFn = Callable[[str], int]


def whitespace_len(text: str) -> int:
    """Cheap default tokenizer for testing. Under-counts BPE by ~2-3×
    for code-heavy text — use `make_qwen_len_fn()` for the real cap."""
    return len(text.split()) if text else 0


def make_qwen_len_fn(
    model_name: str = "Qwen/Qwen2.5-Coder-7B-Instruct",
) -> LenFn:
    """Return a `len_fn` that counts BPE tokens with the real Qwen
    tokenizer. Loads once and caches; subsequent calls are fast.

    Requires `transformers`. Imports lazily so callers that don't need
    it (e.g. unit tests on the pure-Python parts) can skip the install.
    """
    try:
        from transformers import AutoTokenizer  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "make_qwen_len_fn() needs `transformers` installed. "
            "pip install transformers"
        ) from e
    tokenizer = AutoTokenizer.from_pretrained(model_name)

    def _qwen_len(text: str) -> int:
        if not text:
            return 0
        return len(tokenizer(text, add_special_tokens=False)["input_ids"])

    return _qwen_len


# ---------------------------------------------------------------------------
# Suggestion / match parsing
# ---------------------------------------------------------------------------

def _parse_json_field(raw) -> list:
    """Parse a JSON-string-or-already-parsed column into a list."""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        # llm/schemas.py wraps these as `{"suggestions": [...]}` /
        # `{"matches": [...]}` — unwrap if present.
        for key in ("suggestions", "matches", "actions"):
            if key in parsed and isinstance(parsed[key], list):
                return parsed[key]
        return []
    return parsed if isinstance(parsed, list) else []


def split_accepted_rejected(
    suggestions_raw,
    matching_raw,
) -> tuple[list[dict], list[dict]]:
    """Split a (PR × bot)'s suggestions into accepted / rejected lists
    using the judge's `matching_results`. Suggestions absent from the
    match table are treated as rejected (defensive — should not happen
    for fully-analysed PRs)."""
    suggestions = _parse_json_field(suggestions_raw)
    matches = _parse_json_field(matching_raw)
    matched_ids: set[str] = {
        m["bot_issue_id"] for m in matches
        if isinstance(m, dict) and m.get("matched") is True
    }
    accepted, rejected = [], []
    for s in suggestions:
        if not isinstance(s, dict):
            continue
        if s.get("issue_id") in matched_ids:
            accepted.append(s)
        else:
            rejected.append(s)
    return accepted, rejected


# ---------------------------------------------------------------------------
# Per-row generation: SFT rows and DPO pairs
# ---------------------------------------------------------------------------

@dataclass
class PairConfig:
    """Knobs for DPO pair construction."""

    length_tolerance: float = 0.20
    """Drop pairs where |chosen_tokens - rejected_tokens| / max(...) > this."""

    max_per_pr_bot: Optional[int] = None
    """Cap pairs emitted per (PR, bot). None = unlimited."""

    max_prompt_tokens: Optional[int] = 12_000
    """Drop pairs whose prompt token count exceeds this. None = no cap."""

    bot_balance_quantile: float = 0.75
    """When balancing the dataset, cap each bot's pair count at this
    quantile of the per-bot pair counts. 1.0 = no balancing."""

    seed: int = 0xC0DECAFE


def _bookkeeping(row: dict) -> dict:
    """Pull the fields every output row carries (for split/cutoff).

    `bot_reviewed_at` is the canonical timestamp for the train/val split
    (matches the Rust dashboard's by-date organisation). `pr_created_at`
    is preserved alongside it but is NULL for ~74% of analyzed rows so
    it can't drive the split alone."""
    return {
        "pr_id": row["pr_id"],
        "repo_name": row["repo_name"],
        "pr_created_at": _isoformat(row.get("pr_created_at")),
        "bot_reviewed_at": _isoformat(row.get("bot_reviewed_at")),
        "chatbot": row["chatbot"],
    }


def _isoformat(ts) -> Optional[str]:
    if ts is None:
        return None
    if isinstance(ts, (date, datetime)):
        return ts.isoformat()
    return str(ts)


def generate_sft_rows_for_row(
    row: dict,
    *,
    len_fn: LenFn = whitespace_len,
) -> list[dict]:
    """One SFT row per accepted suggestion. Format matches the SFT
    schema in `README.md`."""
    accepted, _ = split_accepted_rejected(
        row.get("bot_suggestions"), row.get("matching_results")
    )
    prompt = build_prompt(row.get("pr_title", ""), row.get("diff", ""))
    bookkeeping = _bookkeeping(row)
    out = []
    for s in accepted:
        description = (s.get("description") or "").strip()
        if not description:
            continue
        out.append({
            **bookkeeping,
            "prompt": prompt,
            "response": description,
            "response_tokens": len_fn(description),
        })
    return out


def generate_pairs_for_row(
    row: dict,
    *,
    config: PairConfig = PairConfig(),
    len_fn: LenFn = whitespace_len,
) -> list[dict]:
    """All within-bot DPO pairs for one (PR, bot) analysis row,
    after applying the per-row knobs in `config` (length match,
    per-(PR,bot) cap). Bot-level balancing is dataset-wide and
    happens later in `balance_by_bot`."""
    accepted, rejected = split_accepted_rejected(
        row.get("bot_suggestions"), row.get("matching_results")
    )
    if not accepted or not rejected:
        return []

    prompt = build_prompt(row.get("pr_title", ""), row.get("diff", ""))
    bookkeeping = _bookkeeping(row)

    pairs: list[dict] = []
    for a in accepted:
        a_text = (a.get("description") or "").strip()
        if not a_text:
            continue
        a_tokens = len_fn(a_text)
        for r in rejected:
            r_text = (r.get("description") or "").strip()
            if not r_text:
                continue
            r_tokens = len_fn(r_text)

            # ±20% length match
            denom = max(a_tokens, r_tokens) or 1
            if abs(a_tokens - r_tokens) / denom > config.length_tolerance:
                continue

            pairs.append({
                **bookkeeping,
                "prompt": prompt,
                "chosen": a_text,
                "chosen_tokens": a_tokens,
                "rejected": r_text,
                "rejected_tokens": r_tokens,
                "condition": "filtered",
            })

    # Per-(PR, bot) cap — random sample, deterministic.
    if config.max_per_pr_bot is not None and len(pairs) > config.max_per_pr_bot:
        rng = random.Random(_pair_seed(config.seed, row))
        rng.shuffle(pairs)
        pairs = pairs[: config.max_per_pr_bot]

    return pairs


def _pair_seed(base: int, row: dict) -> int:
    """Stable seed per (PR, bot) so re-runs sample the same pairs."""
    return base ^ hash((row["pr_id"], row["chatbot"]))


# ---------------------------------------------------------------------------
# Hunk-level per-row generation: SFT rows and DPO pairs
# ---------------------------------------------------------------------------

def _bucket_by_file(suggestions: list[dict]) -> dict[str, list[dict]]:
    """Group suggestions by `file_path`. Suggestions without a file_path
    are dropped (they can't be anchored to a hunk)."""
    out: dict[str, list[dict]] = defaultdict(list)
    for s in suggestions:
        fp = s.get("file_path")
        if fp:
            out[fp].append(s)
    return out


def generate_hunk_sft_rows_for_row(
    row: dict,
    *,
    len_fn: LenFn = whitespace_len,
) -> list[dict]:
    """One SFT row per (accepted suggestion, anchored hunk) pair.

    Output shape mirrors `generate_sft_rows_for_row` plus a `file_path`
    bookkeeping field so we can stratify eval by file later."""
    accepted, _ = split_accepted_rejected(
        row.get("bot_suggestions"), row.get("matching_results")
    )
    bookkeeping = _bookkeeping(row)
    commit_details = row.get("commit_details")
    out: list[dict] = []
    for s in accepted:
        description = (s.get("description") or "").strip()
        file_path = s.get("file_path")
        if not description or not file_path:
            continue
        hunk = extract_hunk_around(commit_details, file_path, s.get("line_number"))
        if not hunk:
            continue
        prompt = build_hunk_prompt(file_path, hunk)
        out.append({
            **bookkeeping,
            "file_path": file_path,
            "prompt": prompt,
            "response": description,
            "response_tokens": len_fn(description),
        })
    return out


def generate_hunk_pairs_for_row(
    row: dict,
    *,
    config: PairConfig = PairConfig(),
    len_fn: LenFn = whitespace_len,
) -> list[dict]:
    """Within-(PR, bot, file) DPO pairs.

    For each file with at least one accepted AND one rejected suggestion
    from this bot, build a single hunk prompt (the file's hunk) and
    cross-product the accepted × rejected suggestions about that file.
    Length-match and per-(PR, bot) cap apply as in PR-level pairing.

    Suggestions without a `file_path` are dropped; suggestions whose
    file isn't present in `commit_details` are skipped."""
    accepted, rejected = split_accepted_rejected(
        row.get("bot_suggestions"), row.get("matching_results")
    )
    if not accepted or not rejected:
        return []

    bookkeeping = _bookkeeping(row)
    commit_details = row.get("commit_details")
    accepted_by_file = _bucket_by_file(accepted)
    rejected_by_file = _bucket_by_file(rejected)

    pairs: list[dict] = []
    for file_path in accepted_by_file.keys() & rejected_by_file.keys():
        # Pick one representative line from accepted suggestions to anchor
        # the hunk (same prompt for all pairs on this file).
        anchor_line = next(
            (s.get("line_number") for s in accepted_by_file[file_path]
             if s.get("line_number") is not None),
            None,
        )
        hunk = extract_hunk_around(commit_details, file_path, anchor_line)
        if not hunk:
            continue
        prompt = build_hunk_prompt(file_path, hunk)

        for a in accepted_by_file[file_path]:
            a_text = (a.get("description") or "").strip()
            if not a_text:
                continue
            a_tokens = len_fn(a_text)
            for r in rejected_by_file[file_path]:
                r_text = (r.get("description") or "").strip()
                if not r_text:
                    continue
                r_tokens = len_fn(r_text)
                denom = max(a_tokens, r_tokens) or 1
                if abs(a_tokens - r_tokens) / denom > config.length_tolerance:
                    continue
                pairs.append({
                    **bookkeeping,
                    "file_path": file_path,
                    "prompt": prompt,
                    "chosen": a_text,
                    "chosen_tokens": a_tokens,
                    "rejected": r_text,
                    "rejected_tokens": r_tokens,
                    "condition": "filtered",
                })

    if config.max_per_pr_bot is not None and len(pairs) > config.max_per_pr_bot:
        rng = random.Random(_pair_seed(config.seed, row))
        rng.shuffle(pairs)
        pairs = pairs[: config.max_per_pr_bot]
    return pairs


# ---------------------------------------------------------------------------
# Dataset-level orchestration
# ---------------------------------------------------------------------------

def filter_long_prompts(
    rows: list[dict],
    *,
    max_prompt_tokens: int,
    len_fn: LenFn = whitespace_len,
) -> list[dict]:
    """Drop rows whose `prompt` field exceeds `max_prompt_tokens`."""
    return [r for r in rows if len_fn(r["prompt"]) <= max_prompt_tokens]


def balance_by_bot(
    pairs: list[dict],
    *,
    quantile: float = 0.75,
    seed: int = 0xC0DECAFE,
) -> list[dict]:
    """Cap each bot's contribution at the `quantile` of per-bot pair
    counts. Quantile=1.0 disables balancing. The cap floor is the
    minimum bot's count — we never increase, only down-sample."""
    if quantile >= 1.0 or not pairs:
        return list(pairs)

    bot_counts = Counter(p["chatbot"] for p in pairs)
    counts = sorted(bot_counts.values())
    if len(counts) == 1:
        return list(pairs)
    cap = int(_quantile(counts, quantile))
    cap = max(cap, min(counts))  # floor at the smallest bot's count

    by_bot: dict[str, list[dict]] = defaultdict(list)
    for p in pairs:
        by_bot[p["chatbot"]].append(p)

    rng = random.Random(seed)
    out: list[dict] = []
    for bot, plist in by_bot.items():
        if len(plist) <= cap:
            out.extend(plist)
        else:
            sampled = plist.copy()
            rng.shuffle(sampled)
            out.extend(sampled[:cap])
    return out


def _quantile(sorted_values: list[int], q: float) -> float:
    """Linear-interpolated quantile, stdlib-only. `sorted_values` ascending."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def build_dpo_dataset(
    rows: list[dict],
    *,
    config: PairConfig = PairConfig(),
    len_fn: LenFn = whitespace_len,
) -> list[dict]:
    """End-to-end DPO dataset build from filtered (PR × bot) rows.

    Steps:
      1. Per-row pair generation (within-bot, length-matched).
      2. Long-prompt drop.
      3. Dataset-wide bot balancing.

    Output rows match the DPO schema in `README.md`."""
    # 1. Per-row pairs
    pairs: list[dict] = []
    for row in rows:
        pairs.extend(generate_pairs_for_row(row, config=config, len_fn=len_fn))

    # 2. Long-prompt cutoff
    if config.max_prompt_tokens is not None:
        pairs = filter_long_prompts(
            pairs, max_prompt_tokens=config.max_prompt_tokens, len_fn=len_fn,
        )

    # 3. Bot balancing
    pairs = balance_by_bot(
        pairs, quantile=config.bot_balance_quantile, seed=config.seed,
    )
    return pairs


def build_sft_dataset(
    rows: list[dict],
    *,
    max_prompt_tokens: Optional[int] = 12_000,
    len_fn: LenFn = whitespace_len,
) -> list[dict]:
    """End-to-end SFT dataset build (one row per accepted suggestion).
    No bot balancing for SFT — that's specific to preference learning."""
    out: list[dict] = []
    for row in rows:
        out.extend(generate_sft_rows_for_row(row, len_fn=len_fn))
    if max_prompt_tokens is not None:
        out = filter_long_prompts(
            out, max_prompt_tokens=max_prompt_tokens, len_fn=len_fn,
        )
    return out


def build_hunk_dpo_dataset(
    rows: list[dict],
    *,
    config: PairConfig = PairConfig(),
    len_fn: LenFn = whitespace_len,
) -> list[dict]:
    """End-to-end hunk-level DPO dataset build.

    Same shape as `build_dpo_dataset` but pairs within (PR, bot, file)
    instead of (PR, bot). Each pair's prompt is the file's hunk
    (resolved from `commit_details` via `extract_hunk_around`)."""
    pairs: list[dict] = []
    for row in rows:
        pairs.extend(generate_hunk_pairs_for_row(row, config=config, len_fn=len_fn))
    if config.max_prompt_tokens is not None:
        pairs = filter_long_prompts(
            pairs, max_prompt_tokens=config.max_prompt_tokens, len_fn=len_fn,
        )
    pairs = balance_by_bot(
        pairs, quantile=config.bot_balance_quantile, seed=config.seed,
    )
    return pairs


def build_hunk_sft_dataset(
    rows: list[dict],
    *,
    max_prompt_tokens: Optional[int] = 12_000,
    len_fn: LenFn = whitespace_len,
) -> list[dict]:
    """End-to-end hunk-level SFT dataset build."""
    out: list[dict] = []
    for row in rows:
        out.extend(generate_hunk_sft_rows_for_row(row, len_fn=len_fn))
    if max_prompt_tokens is not None:
        out = filter_long_prompts(
            out, max_prompt_tokens=max_prompt_tokens, len_fn=len_fn,
        )
    return out


# ---------------------------------------------------------------------------
# Synthetic smoke test — `python crb_paper/pairs.py`
# ---------------------------------------------------------------------------

def _smoke() -> None:
    def make_row(
        pr_id: int,
        chatbot: str,
        accepted: list[str],
        rejected: list[str],
        diff: str = "diff --git a/x b/x\n+pass",
        title: str = "fix: thing",
    ) -> dict:
        suggestions = [
            {"issue_id": f"S{i}", "description": d, "category": "bug",
             "severity": "medium"}
            for i, d in enumerate(accepted + rejected)
        ]
        accept_ids = {f"S{i}" for i in range(len(accepted))}
        matches = [
            {"bot_issue_id": s["issue_id"],
             "matched": s["issue_id"] in accept_ids,
             "confidence": 0.9, "reasoning": ""}
            for s in suggestions
        ]
        return {
            "pr_id": pr_id,
            "repo_name": "org/repo",
            "pr_title": title,
            "pr_created_at": datetime(2026, 4, 1),
            "chatbot": chatbot,
            "diff": diff,
            "bot_suggestions": json.dumps({"suggestions": suggestions}),
            "matching_results": json.dumps({"matches": matches}),
        }

    # 1. Basic 2x2: 2 accepted + 2 rejected → 4 candidate pairs;
    #    length-matched (all "short"/"long" mix) drops half.
    row = make_row(
        pr_id=1, chatbot="cubic-dev-ai",
        accepted=["short ok one", "much longer accepted suggestion text here"],
        rejected=["short bad", "another verbose rejected note that runs longer"],
    )
    pairs = generate_pairs_for_row(row, config=PairConfig(length_tolerance=0.20))
    # short(3)↔short(2): |3-2|/3=0.33 > 0.20 → drop
    # short(3)↔long(7):  |3-7|/7=0.57 → drop
    # long(6)↔short(2):  |6-2|/6=0.67 → drop
    # long(6)↔long(7):   |6-7|/7=0.14 → keep
    assert len(pairs) == 1, f"length match failed: got {len(pairs)} pairs"
    assert pairs[0]["chosen"].startswith("much longer")
    assert pairs[0]["rejected"].startswith("another verbose")

    # 2. SFT: 2 accepted → 2 rows.
    sft = generate_sft_rows_for_row(row)
    assert len(sft) == 2
    assert all("response" in r and "prompt" in r for r in sft)

    # 3. PR×bot with no rejected → 0 pairs.
    only_acc = make_row(pr_id=2, chatbot="cubic-dev-ai",
                        accepted=["a", "b"], rejected=[])
    assert generate_pairs_for_row(only_acc) == []

    # 4. Bot balancing: bot A has many pairs, bot B has few.
    rows_balanced = []
    for i in range(20):
        rows_balanced.append(make_row(
            pr_id=100 + i, chatbot="bot-a",
            accepted=["same length aa"], rejected=["same length bb"],
        ))
    for i in range(3):
        rows_balanced.append(make_row(
            pr_id=200 + i, chatbot="bot-b",
            accepted=["same length cc"], rejected=["same length dd"],
        ))
    ds = build_dpo_dataset(
        rows_balanced,
        config=PairConfig(bot_balance_quantile=0.5, max_prompt_tokens=None),
    )
    counts = Counter(p["chatbot"] for p in ds)
    # quantile(0.5) of [3, 20] = 11.5 → cap=11. bot-a capped at 11; bot-b kept (3).
    assert counts["bot-b"] == 3, f"bot-b should keep all 3, got {counts['bot-b']}"
    assert counts["bot-a"] <= 11, f"bot-a should be capped, got {counts['bot-a']}"

    # 5. Long-prompt drop.
    big_diff = " ".join(["x"] * 20_000)
    big_row = make_row(
        pr_id=3, chatbot="cubic-dev-ai",
        accepted=["len4 word here"], rejected=["len4 word also"],
        diff=big_diff,
    )
    ds_capped = build_dpo_dataset(
        [big_row],
        config=PairConfig(max_prompt_tokens=12_000, bot_balance_quantile=1.0),
    )
    assert ds_capped == [], f"expected long-prompt drop, got {len(ds_capped)}"

    print(
        f"OK: 2x2 → 1 length-matched pair; SFT 2 rows; "
        f"balanced bot-a={counts['bot-a']}, bot-b={counts['bot-b']}; "
        "long-prompt drop verified"
    )


if __name__ == "__main__":
    _smoke()

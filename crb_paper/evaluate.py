"""Evaluation harness for the CRB finetuning experiment.

Step 7 of the gating plan in `crb_paper/README.md`.

Loads a finetuned LoRA adapter (or just the base model with `--no-lora`),
samples k suggestions per held-out PR, calls the existing CRB matcher
to score against ground-truth `gold_human_actions`, and reports
precision / recall / F1 with 95% bootstrap confidence intervals.

The matcher is **inlined** here (not imported) — `online/etl/pipeline/
analyze.py::analyze_single_pr` is async and does PR enrichment we don't
need. We reuse:
  - `JUDGE_MATCHING` prompt template from `online/etl/llm/prompts.py`
  - `_format_suggestions` / `_format_actions` formatting from
    `online/etl/pipeline/analyze.py`
  - `MatchingResponse` schema from `online/etl/llm/schemas.py`

Real eval requires LLM API keys (`OPENAI_API_KEY` or whatever the
matcher LLM uses, plus `MARTIAN_API_KEY` if routing through Martian).
`--dry-run` uses stubs and needs neither LLM keys nor ML deps.

Usage:

    python3 crb_paper/evaluate.py \\
        --checkpoint runs/dpo_filtered \\
        --eval-file data/heldout_prs.jsonl \\
        --output results/eval_filtered.csv

Smoke test (no model, no API):

    python3 crb_paper/evaluate.py --dry-run --output /tmp/eval_smoke.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Hyperparameters
# ---------------------------------------------------------------------------

@dataclass
class EvalConfig:
    """All eval knobs. Override via CLI flags."""

    # --- Model -----------------------------------------------------------
    base_model: str = "Qwen/Qwen2.5-Coder-7B-Instruct"
    bf16: bool = True

    # --- Sampling --------------------------------------------------------
    k_samples: int = 5
    """Suggestions per PR. Higher → better recall, lower precision (more
    duplicates). 5 is a reasonable starting point; tune if precision
    looks too low at eval time."""
    temperature: float = 0.7
    """Generation temperature. 0.0 = greedy (only one suggestion possible
    per PR). 0.7 is a typical sampling temperature."""
    top_p: float = 0.95
    """Nucleus sampling threshold."""
    max_new_tokens: int = 128
    """Cap on suggestion length. Should comfortably exceed the p95 of
    `chosen` length from inspection (currently ~34 tokens)."""

    # --- Matcher ---------------------------------------------------------
    matcher_model: str = "openai/gpt-4o-mini"
    """LLM used to judge matches. Should be cheap; the matching prompt is
    short. Override with whatever the existing CRB pipeline uses."""

    # --- Bootstrap CIs ---------------------------------------------------
    bootstrap_resamples: int = 1_000
    """1k is plenty for 95% CIs at typical eval sizes (50–500 PRs)."""
    bootstrap_seed: int = 42

    # --- Misc ------------------------------------------------------------
    sample_seed: int = 42


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(__doc__ or "").strip(),
    )
    p.add_argument("--checkpoint", type=Path,
                   help="Path to LoRA adapter dir. Required unless --no-lora or --dry-run.")
    p.add_argument("--no-lora", action="store_true",
                   help="Evaluate the base model with no adapter applied.")
    p.add_argument("--eval-file", type=Path,
                   help="JSONL of held-out PRs. Required unless --dry-run.")
    p.add_argument("--output", type=Path, required=True,
                   help="Per-PR CSV output path.")
    # Most-likely-tuned overrides
    p.add_argument("--base-model", default=EvalConfig.base_model)
    p.add_argument("--k", type=int, default=EvalConfig.k_samples)
    p.add_argument("--temperature", type=float, default=EvalConfig.temperature)
    p.add_argument("--matcher-model", default=EvalConfig.matcher_model)
    p.add_argument("--seed", type=int, default=EvalConfig.sample_seed)
    p.add_argument("--dry-run", action="store_true",
                   help="Use stub generator + stub matcher; no model load, no API calls.")
    # W&B logging — separate run per eval, linked by name to the training run.
    p.add_argument("--wandb-project", default="crb-finetuning",
                   help="W&B project. Set to '' or pass --no-wandb to disable.")
    p.add_argument("--wandb-run-name", default=None,
                   help="W&B run name. Defaults to `eval-<checkpoint-basename>`.")
    p.add_argument("--no-wandb", action="store_true",
                   help="Disable W&B logging (CSV is still written).")
    return p.parse_args()


def config_from_args(args: argparse.Namespace) -> EvalConfig:
    return EvalConfig(
        base_model=args.base_model,
        k_samples=args.k,
        temperature=args.temperature,
        matcher_model=args.matcher_model,
        sample_seed=args.seed,
    )


# ---------------------------------------------------------------------------
# Per-PR scoring
# ---------------------------------------------------------------------------

@dataclass
class PRResult:
    pr_id: int
    repo_name: str
    num_sampled: int
    num_gold: int
    num_matched: int
    precision: float
    recall: float
    f1: float


def score_pr(num_sampled: int, num_gold: int, num_matched: int) -> tuple[float, float, float]:
    """Per-PR P / R / F1. Recall undefined when num_gold == 0; we skip
    the PR upstream when num_gold == 0 so this should be safe."""
    p = num_matched / num_sampled if num_sampled > 0 else 0.0
    r = num_matched / num_gold if num_gold > 0 else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
    return p, r, f1


# ---------------------------------------------------------------------------
# Inlined matcher (mirrors online/etl/pipeline/analyze.py + llm/prompts.py)
# ---------------------------------------------------------------------------

JUDGE_MATCHING_TEMPLATE = """You are judging whether a bot's code review suggestions correspond to actual code issues that were later fixed.

The bot's username is: {bot_username}

You have two lists:
- Bot Suggestions: issues the bot flagged during review
- Code Fixes: actual issues that were fixed in post-review commits (ground truth)

For EACH bot suggestion, determine:
1. Does it match any code fix? (matched: true/false)
2. Which code fix? (human_action_id)
3. How confident are you? (0.0-1.0)
4. Brief reasoning

A suggestion is "matched" if:
- It identified the same issue (or substantially the same concern) that was later fixed
- The fix is in the same file/area the suggestion pointed to
- Even a partial overlap counts if the bot caught part of the real problem

A suggestion is NOT matched if:
- No corresponding fix exists — the bot flagged something that wasn't actually fixed
- The fix addresses a different concern than what the bot suggested
- The bot's suggestion was about something that wasn't a real problem

=== Bot Suggestions ===
{bot_suggestions}

=== Code Fixes (ground truth) ===
{human_actions}
"""


def _format_suggestions(suggestions: list[dict]) -> str:
    lines = []
    for s in suggestions:
        loc = ""
        if s.get("file_path"):
            loc = f" ({s['file_path']}"
            if s.get("line_number"):
                loc += f":{s['line_number']}"
            loc += ")"
        lines.append(
            f"- [{s['issue_id']}] ({s.get('category', 'other')}/"
            f"{s.get('severity', 'medium')}){loc}: {s['description']}"
        )
    return "\n".join(lines) if lines else "(no suggestions)"


def _format_actions(actions: list[dict]) -> str:
    lines = []
    for a in actions:
        loc = f" ({a['file_path']})" if a.get("file_path") else ""
        lines.append(
            f"- [{a['action_id']}] ({a.get('category', 'other')}/"
            f"{a.get('action_type', 'other')}){loc}: {a['description']}"
        )
    return "\n".join(lines) if lines else "(no actions)"


def call_real_matcher(
    suggestions: list[dict],
    gold_actions: list[dict],
    *,
    matcher_model: str,
    bot_username: str = "finetune-eval",
) -> list[dict]:
    """Real matcher call — uses the existing CRB judge prompt and any
    OpenAI-compatible client. Returns a list of MatchResult-shaped dicts.
    Imports the LLM client lazily so --dry-run doesn't need it."""
    from openai import OpenAI  # type: ignore[import-not-found]

    prompt = JUDGE_MATCHING_TEMPLATE.format(
        bot_username=bot_username,
        bot_suggestions=_format_suggestions(suggestions),
        human_actions=_format_actions(gold_actions),
    )
    client = OpenAI()
    # Structured-output via response_format=json_schema would be cleaner,
    # but for skeleton portability we ask for JSON in the prompt and
    # parse loosely.
    completion = client.chat.completions.create(
        model=matcher_model.split("/", 1)[-1] if "/" in matcher_model else matcher_model,
        messages=[
            {"role": "system",
             "content": "Respond with a JSON object {\"matches\": [{\"bot_issue_id\": ..., "
                        "\"human_action_id\": ..., \"matched\": ..., \"confidence\": ..., "
                        "\"reasoning\": ...}, ...]}."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    raw = json.loads(completion.choices[0].message.content)
    return raw.get("matches", [])


# ---------------------------------------------------------------------------
# Stub matcher + generator for --dry-run
# ---------------------------------------------------------------------------

def stub_generator(prompt: str, k: int, *, seed: int) -> list[str]:
    """Deterministic suggestions for the dry-run path."""
    rng = random.Random(seed ^ hash(prompt))
    pool = [
        "Validate that x is non-null before returning.",
        "Add a unit test covering the new branch.",
        "Use a more descriptive variable name than `x`.",
        "Document the return value in the docstring.",
        "Handle the empty-input edge case explicitly.",
        "Avoid swallowing exceptions silently.",
    ]
    return rng.sample(pool, min(k, len(pool)))


def stub_matcher(
    suggestions: list[dict],
    gold_actions: list[dict],
    **_kw,
) -> list[dict]:
    """Substring-overlap matching: a suggestion 'matches' a gold action if
    they share a noun-ish word (>=4 chars). Deterministic; no LLM needed."""
    matches = []
    for s in suggestions:
        s_words = {w.lower().strip(".,") for w in s["description"].split() if len(w) >= 4}
        best = None
        for a in gold_actions:
            a_words = {w.lower().strip(".,") for w in a["description"].split() if len(w) >= 4}
            if s_words & a_words:
                best = a
                break
        matches.append({
            "bot_issue_id": s["issue_id"],
            "human_action_id": best["action_id"] if best else None,
            "matched": best is not None,
            "confidence": 1.0 if best else 0.0,
            "reasoning": "stub: substring overlap" if best else "stub: no overlap",
        })
    return matches


# ---------------------------------------------------------------------------
# Bootstrap CIs
# ---------------------------------------------------------------------------

def bootstrap_ci(
    values: list[float],
    *,
    n_resamples: int,
    seed: int,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Percentile bootstrap CI for the mean of `values`."""
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(n_resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo_idx = int((1 - confidence) / 2 * n_resamples)
    hi_idx = int((1 + confidence) / 2 * n_resamples) - 1
    return (means[lo_idx], means[hi_idx])


# ---------------------------------------------------------------------------
# Generator dispatch
# ---------------------------------------------------------------------------

def real_generator_factory(
    cfg: EvalConfig,
    checkpoint: Optional[Path],
    no_lora: bool,
) -> Callable[[str], list[str]]:
    """Returns a `prompt -> [suggestion_strings]` callable that loads the
    model lazily on first use."""
    state: dict = {"model": None, "tokenizer": None}

    def generate(prompt: str) -> list[str]:
        if state["model"] is None:
            try:
                import torch  # noqa: F401
                from peft import PeftModel
                from transformers import AutoModelForCausalLM, AutoTokenizer
            except ImportError as e:
                print(
                    f"ML deps missing ({e.name}). Install with:\n"
                    "  pip install transformers peft accelerate torch\n",
                    file=sys.stderr,
                )
                sys.exit(0)
            tok = AutoTokenizer.from_pretrained(cfg.base_model)
            if tok.pad_token is None:
                tok.pad_token = tok.eos_token
            base = AutoModelForCausalLM.from_pretrained(
                cfg.base_model,
                torch_dtype="bfloat16" if cfg.bf16 else "float16",
                device_map="auto",
            )
            if no_lora or checkpoint is None:
                state["model"] = base
            else:
                state["model"] = PeftModel.from_pretrained(base, str(checkpoint))
            state["tokenizer"] = tok

        tok = state["tokenizer"]
        model = state["model"]
        messages = [{"role": "user", "content": prompt}]
        inputs = tok.apply_chat_template(
            messages, return_tensors="pt", add_generation_prompt=True,
        ).to(model.device)
        outputs = model.generate(
            inputs,
            do_sample=True,
            temperature=cfg.temperature,
            top_p=cfg.top_p,
            max_new_tokens=cfg.max_new_tokens,
            num_return_sequences=cfg.k_samples,
            pad_token_id=tok.pad_token_id,
        )
        decoded = tok.batch_decode(outputs[:, inputs.shape[-1]:], skip_special_tokens=True)
        return [d.strip() for d in decoded]

    return generate


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _synthetic_eval_rows() -> list[dict]:
    """Two mock PRs for --dry-run."""
    return [
        {
            "pr_id": 1,
            "repo_name": "org/example-repo",
            "prompt": "You are reviewing a PR.\nPR title: fix null pointer\nDiff:\n+    return None",
            "gold_human_actions": [
                {"action_id": "A1", "description": "Validate null before returning",
                 "category": "bug", "action_type": "fix"},
                {"action_id": "A2", "description": "Add unit test for null branch",
                 "category": "test", "action_type": "improvement"},
            ],
        },
        {
            "pr_id": 2,
            "repo_name": "org/example-repo",
            "prompt": "You are reviewing a PR.\nPR title: refactor utils\nDiff:\n+    pass",
            "gold_human_actions": [
                {"action_id": "A1", "description": "Use descriptive variable names",
                 "category": "refactor", "action_type": "improvement"},
            ],
        },
    ]


def main() -> None:
    args = parse_args()
    cfg = config_from_args(args)

    # 1. Load eval rows
    if args.dry_run:
        eval_rows = _synthetic_eval_rows()
    else:
        if args.eval_file is None:
            raise SystemExit("--eval-file required (or pass --dry-run).")
        if not args.no_lora and args.checkpoint is None:
            raise SystemExit("--checkpoint required (or pass --no-lora / --dry-run).")
        eval_rows = [
            json.loads(line)
            for line in args.eval_file.read_text().splitlines() if line.strip()
        ]

    # 2. Pick generator + matcher
    if args.dry_run:
        gen = lambda p: stub_generator(p, cfg.k_samples, seed=cfg.sample_seed)  # noqa: E731
        matcher = stub_matcher
    else:
        gen = real_generator_factory(cfg, args.checkpoint, args.no_lora)
        matcher = lambda s, g: call_real_matcher(s, g, matcher_model=cfg.matcher_model)  # noqa: E731

    # 3. Score each PR
    results: list[PRResult] = []
    for row in eval_rows:
        sample_strings = gen(row["prompt"])
        suggestions = [
            {"issue_id": f"S{i+1}", "description": text, "category": "other", "severity": "medium"}
            for i, text in enumerate(sample_strings)
        ]
        match_results = matcher(suggestions, row["gold_human_actions"])
        num_matched = sum(1 for m in match_results if m.get("matched"))
        p, r, f1 = score_pr(len(suggestions), len(row["gold_human_actions"]), num_matched)
        results.append(PRResult(
            pr_id=row["pr_id"],
            repo_name=row["repo_name"],
            num_sampled=len(suggestions),
            num_gold=len(row["gold_human_actions"]),
            num_matched=num_matched,
            precision=p, recall=r, f1=f1,
        ))

    # 4. Write per-PR CSV
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pr_id", "repo_name", "num_sampled", "num_gold",
                    "num_matched", "precision", "recall", "f1"])
        for r in results:
            w.writerow([r.pr_id, r.repo_name, r.num_sampled, r.num_gold,
                        r.num_matched,
                        f"{r.precision:.4f}", f"{r.recall:.4f}", f"{r.f1:.4f}"])

    # 5. Summary with bootstrap CIs
    ps = [r.precision for r in results]
    rs = [r.recall for r in results]
    fs = [r.f1 for r in results]
    p_mean = statistics.mean(ps) if ps else 0.0
    r_mean = statistics.mean(rs) if rs else 0.0
    f_mean = statistics.mean(fs) if fs else 0.0
    p_ci = bootstrap_ci(ps, n_resamples=cfg.bootstrap_resamples, seed=cfg.bootstrap_seed)
    r_ci = bootstrap_ci(rs, n_resamples=cfg.bootstrap_resamples, seed=cfg.bootstrap_seed + 1)
    f_ci = bootstrap_ci(fs, n_resamples=cfg.bootstrap_resamples, seed=cfg.bootstrap_seed + 2)

    print(f"\nEval over {len(results)} PR(s) — wrote {args.output}")
    print(f"  precision: {p_mean:.4f}  95% CI [{p_ci[0]:.4f}, {p_ci[1]:.4f}]")
    print(f"  recall:    {r_mean:.4f}  95% CI [{r_ci[0]:.4f}, {r_ci[1]:.4f}]")
    print(f"  f1:        {f_mean:.4f}  95% CI [{f_ci[0]:.4f}, {f_ci[1]:.4f}]")

    # 6. W&B logging (best-effort; CSV is the source of truth)
    if not args.no_wandb and not args.dry_run and args.wandb_project:
        _log_to_wandb(
            project=args.wandb_project,
            run_name=args.wandb_run_name,
            checkpoint=args.checkpoint,
            cfg=cfg,
            results=results,
            metrics={
                "eval/precision_mean": p_mean,
                "eval/precision_ci_low": p_ci[0],
                "eval/precision_ci_high": p_ci[1],
                "eval/recall_mean": r_mean,
                "eval/recall_ci_low": r_ci[0],
                "eval/recall_ci_high": r_ci[1],
                "eval/f1_mean": f_mean,
                "eval/f1_ci_low": f_ci[0],
                "eval/f1_ci_high": f_ci[1],
                "eval/num_prs": len(results),
            },
        )


def _log_to_wandb(
    *,
    project: str,
    run_name: Optional[str],
    checkpoint: Optional[Path],
    cfg: EvalConfig,
    results: list[PRResult],
    metrics: dict,
) -> None:
    """Open a fresh W&B run named to link with the training run, log
    aggregate metrics + a per-PR table, and finish. Best-effort — if
    wandb isn't installed or login fails, skip silently."""
    try:
        import wandb  # type: ignore[import-not-found]
    except ImportError:
        print("(wandb not installed; skipping W&B logging)", file=sys.stderr)
        return

    name = run_name
    if name is None:
        # Auto-derive: checkpoint dir's basename, prefixed with `eval-`.
        # E.g. runs/dpo_filtered -> eval-dpo_filtered.
        if checkpoint is not None:
            name = f"eval-{Path(checkpoint).name}"
        else:
            from datetime import datetime
            name = f"eval-base-{datetime.now():%Y%m%d-%H%M%S}"

    config = {
        "base_model": cfg.base_model,
        "checkpoint": str(checkpoint) if checkpoint else None,
        "k_samples": cfg.k_samples,
        "temperature": cfg.temperature,
        "top_p": cfg.top_p,
        "matcher_model": cfg.matcher_model,
        "bootstrap_resamples": cfg.bootstrap_resamples,
        "num_prs": len(results),
    }

    try:
        run = wandb.init(project=project, name=name, config=config,
                         job_type="eval", reinit=True)
        wandb.log(metrics)
        # Per-PR table for inspection in the UI.
        table = wandb.Table(columns=["pr_id", "repo_name", "num_sampled",
                                     "num_gold", "num_matched",
                                     "precision", "recall", "f1"])
        for r in results:
            table.add_data(r.pr_id, r.repo_name, r.num_sampled, r.num_gold,
                           r.num_matched, r.precision, r.recall, r.f1)
        wandb.log({"eval/per_pr": table})
        wandb.summary.update(metrics)
        run.finish()
        print(f"(logged to W&B run `{name}` in project `{project}`)")
    except Exception as e:
        print(f"(W&B logging failed: {e}; CSV still written)", file=sys.stderr)


if __name__ == "__main__":
    main()

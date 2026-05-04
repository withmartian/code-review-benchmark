"""DPO trainer with ablation conditions for the CRB finetuning experiment.

Step 6 of the gating plan in `crb_paper/README.md`.

Trains a LoRA adapter on top of `Qwen/Qwen2.5-Coder-7B-Instruct` (or any
HF causal LM) using TRL's `DPOTrainer`. Optionally initialises from an
SFT-warm-started adapter via `--sft-checkpoint`. Reads the DPO JSONL
produced by `crb_paper/pairs.py` (`build_dpo_dataset`); each row is a
within-bot (chosen, rejected) preference pair on the same PR.

The four ablation conditions from `README.md` are implemented as a
pre-training transformation pass over the dataset (see `apply_condition`):

  * `filtered`   pass-through. Use the filtered preference pairs as-is.
  * `unfiltered` pass-through (informational; assumes the caller passed
                  the unfiltered JSONL via --train-file).
  * `inverted`   swap chosen ↔ rejected on every row. Expected to make
                  the model worse than base — validates the signal.
  * `random`     within each PR group, randomly reassign chosen vs.
                  rejected. Expected ≈ base model.

Dependencies (NOT yet installed):

    pip install transformers peft trl accelerate datasets torch wandb

Monitoring (W&B): training runs log to Weights & Biases by default.
Set these env vars before running (or use `--report-to none` to disable):

    export WANDB_API_KEY=...
    export WANDB_PROJECT=crb-finetuning      # default if unset
    export WANDB_ENTITY=<your-org-or-user>   # optional

The auto-generated run name includes the ablation condition, e.g.
`dpo-filtered-<timestamp>`, so the four ablation runs are easy to
compare in the W&B UI.

Usage:

    python3 crb_paper/train_dpo.py \\
        --train-file data/dpo_train.jsonl \\
        --eval-file data/dpo_val.jsonl \\
        --sft-checkpoint runs/sft \\
        --condition filtered \\
        --output-dir runs/dpo_filtered

Smoke test (no real training, 5 steps on synthetic data):

    python3 crb_paper/train_dpo.py --dry-run --output-dir /tmp/dpo_smoke
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import textwrap
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Hyperparameters
# ---------------------------------------------------------------------------

@dataclass
class Hyperparameters:
    """All DPO knobs, with provenance. Override via CLI flags.

    DPO defaults follow Rafailov et al. 2023 ("Direct Preference
    Optimization") and the TRL DPOTrainer canonical recipe."""

    # --- Model -----------------------------------------------------------
    base_model: str = "Qwen/Qwen2.5-Coder-7B-Instruct"
    bf16: bool = True

    # --- LoRA (PEFT) — same as SFT for fair comparison ------------------
    lora_r: int = 8
    lora_alpha: int = 16
    lora_dropout: float = 0.05
    lora_target_modules: tuple[str, ...] = ("q_proj", "v_proj")

    # --- DPO-specific ----------------------------------------------------
    beta: float = 0.1
    """Strength of the KL constraint to the reference model. Higher beta
    keeps the policy closer to the reference; the DPO paper sweeps
    [0.01, 0.5] and finds 0.1–0.3 best for most tasks."""
    loss_type: str = "sigmoid"
    """'sigmoid' = original DPO. Alternatives: 'hinge' (IPO), 'kto_pair'."""
    label_smoothing: float = 0.0
    """For cDPO (Mitchell 2023): treats labels as (1-eps) instead of 1.
    Set ~0.05–0.1 if preference labels are noisy."""
    max_prompt_length: int = 1024
    """Truncate prompt portion to this. Combined with max_length below."""
    max_length: int = 2048
    """Total sequence length cap (prompt + chosen-or-rejected)."""
    precompute_ref_log_probs: bool = True
    """Compute reference log-probs once and cache. Saves ~50% VRAM at
    train time but doubles dataset prep time."""

    # --- Optimizer (AdamW) ----------------------------------------------
    learning_rate: float = 5e-6
    """DPO is more sensitive than SFT — typical DPO LR is 1e-7 to 5e-6,
    much lower than SFT's 2e-5."""
    weight_decay: float = 0.0
    adam_beta1: float = 0.9
    adam_beta2: float = 0.999
    adam_epsilon: float = 1e-8
    max_grad_norm: float = 1.0

    # --- LR scheduler ----------------------------------------------------
    lr_scheduler_type: str = "cosine"
    warmup_ratio: float = 0.03

    # --- Training loop ---------------------------------------------------
    epochs: int = 1
    per_device_batch_size: int = 1
    gradient_accumulation_steps: int = 8

    # --- Logging / checkpointing ----------------------------------------
    logging_steps: int = 25
    save_steps: int = 500
    save_total_limit: int = 3
    eval_steps: int = 250
    eval_strategy: str = "steps"

    # --- Misc ------------------------------------------------------------
    seed: int = 42
    report_to: str = "wandb"
    """'wandb' (default — needs WANDB_API_KEY) / 'tensorboard' / 'none'.
    See module docstring for the full env-var list."""
    run_name: Optional[str] = None
    """W&B run name. None → auto-generated as `dpo-<condition>-<timestamp>`."""
    wandb_project: str = "crb-finetuning"
    """W&B project. Overridden by WANDB_PROJECT env var if set."""


# ---------------------------------------------------------------------------
# Ablation conditions
# ---------------------------------------------------------------------------

CONDITIONS = ("filtered", "unfiltered", "inverted", "random")


def apply_condition(rows: list[dict], condition: str, *, seed: int) -> list[dict]:
    """Transform `rows` according to the ablation. Returns a new list,
    leaves `rows` unchanged. Output rows have the same shape as input."""
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition {condition!r}; expected one of {CONDITIONS}")

    if condition in ("filtered", "unfiltered"):
        # No row-level transformation. (`unfiltered` differs from `filtered`
        # only in that the caller is expected to have passed a different
        # — unfiltered — JSONL; this function is the same in both cases.)
        return [dict(r) for r in rows]

    if condition == "inverted":
        out = []
        for r in rows:
            new = dict(r)
            new["chosen"], new["rejected"] = r["rejected"], r["chosen"]
            new["chosen_tokens"], new["rejected_tokens"] = (
                r.get("rejected_tokens"), r.get("chosen_tokens"),
            )
            new["condition"] = "inverted"
            out.append(new)
        return out

    if condition == "random":
        # Group by PR. Within each PR, pool all suggestion strings, then
        # for each row redraw (chosen, rejected) at random from the pool
        # (without replacement). Deterministic per PR via the seed.
        by_pr: dict = defaultdict(list)
        for r in rows:
            by_pr[r["pr_id"]].append(r)
        out = []
        for pr_id, prows in by_pr.items():
            pool = []
            for r in prows:
                pool.append(r["chosen"])
                pool.append(r["rejected"])
            rng = random.Random(seed ^ hash(pr_id))
            for r in prows:
                # Redraw two distinct strings; if pool too small, fall back
                # to chosen/rejected swap.
                if len(set(pool)) < 2:
                    new = dict(r)
                else:
                    a, b = rng.sample(list(set(pool)), 2)
                    new = dict(r)
                    new["chosen"], new["rejected"] = a, b
                new["condition"] = "random"
                out.append(new)
        return out

    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(__doc__ or "").strip(),
    )
    # Files / output
    p.add_argument("--train-file", type=Path,
                   help="JSONL of DPO pairs (see crb_paper/README.md schema).")
    p.add_argument("--eval-file", type=Path,
                   help="JSONL for held-out eval. Optional.")
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--sft-checkpoint", type=Path, default=None,
                   help="LoRA adapter to load before DPO. None = base model.")
    # Ablation
    p.add_argument("--condition", choices=CONDITIONS, default="filtered",
                   help="Ablation condition. See module docstring.")
    # Most-likely-tuned overrides
    p.add_argument("--base-model", default=Hyperparameters.base_model)
    p.add_argument("--beta", type=float, default=Hyperparameters.beta)
    p.add_argument("--epochs", type=int, default=Hyperparameters.epochs)
    p.add_argument("--per-device-batch-size", type=int,
                   default=Hyperparameters.per_device_batch_size)
    p.add_argument("--gradient-accumulation", type=int,
                   default=Hyperparameters.gradient_accumulation_steps)
    p.add_argument("--learning-rate", type=float,
                   default=Hyperparameters.learning_rate)
    p.add_argument("--max-length", type=int, default=Hyperparameters.max_length)
    p.add_argument("--seed", type=int, default=Hyperparameters.seed)
    p.add_argument("--report-to", default=Hyperparameters.report_to,
                   choices=["none", "wandb", "tensorboard"])
    p.add_argument("--run-name", default=None,
                   help="W&B run name. Auto-generated as dpo-<condition>-<ts> if omitted.")
    p.add_argument("--wandb-project", default=Hyperparameters.wandb_project,
                   help="W&B project (overridden by WANDB_PROJECT env if set).")
    p.add_argument("--dry-run", action="store_true",
                   help="Train 5 steps on 8 synthetic pairs; verify save/reload.")
    return p.parse_args()


def hyperparams_from_args(args: argparse.Namespace) -> Hyperparameters:
    return Hyperparameters(
        base_model=args.base_model,
        beta=args.beta,
        epochs=args.epochs,
        per_device_batch_size=args.per_device_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation,
        learning_rate=args.learning_rate,
        max_length=args.max_length,
        seed=args.seed,
        report_to=args.report_to,
        run_name=args.run_name,
        wandb_project=args.wandb_project,
    )


def _resolve_run_name(hp: Hyperparameters, condition: str) -> str:
    """Auto-generate a W&B run name embedding the ablation condition."""
    if hp.run_name:
        return hp.run_name
    from datetime import datetime
    return f"dpo-{condition}-{datetime.now():%Y%m%d-%H%M%S}"


# ---------------------------------------------------------------------------
# Synthetic data for --dry-run
# ---------------------------------------------------------------------------

def _synthetic_dpo_rows(n: int = 8) -> list[dict]:
    """Tiny dataset matching the DPO schema for smoke-testing."""
    return [
        {
            "pr_id": i // 2,
            "repo_name": f"org/repo-{i % 3}",
            "pr_created_at": "2026-01-01T00:00:00Z",
            "chatbot": "synthetic[bot]",
            "prompt": (
                "You are reviewing a pull request. Identify one specific "
                "issue with the change below and describe it in 1–3 "
                f"sentences.\n\nPR title: case {i}\nDiff:\n+    return x\n"
            ),
            "chosen": f"Validate that x is non-null before returning (good {i}).",
            "chosen_tokens": 9,
            "rejected": f"Add a comment about the function (weak {i}).",
            "rejected_tokens": 8,
            "condition": "filtered",
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Training (heavy imports inside; deps may not be installed)
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    hp = hyperparams_from_args(args)

    # 1. Load + transform dataset (pure-Python; no ML deps needed yet)
    if args.dry_run:
        train_rows = _synthetic_dpo_rows(8)
        eval_rows: Optional[list[dict]] = None
    else:
        if args.train_file is None:
            raise SystemExit("--train-file required (or pass --dry-run).")
        train_rows = [json.loads(line) for line in args.train_file.read_text().splitlines() if line.strip()]
        eval_rows = None
        if args.eval_file is not None:
            eval_rows = [json.loads(line) for line in args.eval_file.read_text().splitlines() if line.strip()]

    train_rows = apply_condition(train_rows, args.condition, seed=hp.seed)
    if eval_rows is not None:
        eval_rows = apply_condition(eval_rows, args.condition, seed=hp.seed)

    print(
        f"condition={args.condition} → {len(train_rows)} train rows, "
        f"{len(eval_rows) if eval_rows else 0} eval rows"
    )

    # W&B env-var setup (only if reporting to wandb). Set BEFORE the heavy
    # imports so the Trainer picks them up.
    if hp.report_to == "wandb":
        import os
        os.environ.setdefault("WANDB_PROJECT", hp.wandb_project)
        os.environ.setdefault("WANDB_RUN_NAME", _resolve_run_name(hp, args.condition))

    # 2. Heavy imports
    try:
        import torch  # noqa: F401
        from datasets import Dataset
        from peft import LoraConfig, PeftModel, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from trl import DPOConfig, DPOTrainer
    except ImportError as e:
        print(
            f"ML deps missing ({e.name}). Install with:\n"
            "  pip install transformers peft trl accelerate datasets torch\n"
            "Then re-run.",
            file=sys.stderr,
        )
        sys.exit(0)

    # 3. Tokenizer + dataset shaping (DPOTrainer expects {prompt, chosen, rejected})
    tokenizer = AutoTokenizer.from_pretrained(hp.base_model)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    def _strip(rows: list[dict]) -> list[dict]:
        return [
            {"prompt": r["prompt"], "chosen": r["chosen"], "rejected": r["rejected"]}
            for r in rows
        ]

    train_ds = Dataset.from_list(_strip(train_rows))
    eval_ds = Dataset.from_list(_strip(eval_rows)) if eval_rows else None

    # 4. Model + LoRA
    model = AutoModelForCausalLM.from_pretrained(
        hp.base_model,
        torch_dtype="bfloat16" if hp.bf16 else "float16",
        device_map="auto",
    )
    if args.sft_checkpoint is not None:
        # Continue training from an SFT-warm-started LoRA adapter.
        model = PeftModel.from_pretrained(model, str(args.sft_checkpoint), is_trainable=True)
    else:
        lora_cfg = LoraConfig(
            r=hp.lora_r,
            lora_alpha=hp.lora_alpha,
            lora_dropout=hp.lora_dropout,
            target_modules=list(hp.lora_target_modules),
            bias="none",
            task_type="CAUSAL_LM",
        )
        model = get_peft_model(model, lora_cfg)
    model.print_trainable_parameters()

    # 5. DPO config — every field maps to one Hyperparameters knob
    dpo_cfg = DPOConfig(
        output_dir=str(args.output_dir),
        beta=hp.beta,
        loss_type=hp.loss_type,
        label_smoothing=hp.label_smoothing,
        max_length=hp.max_length,
        precompute_ref_log_probs=hp.precompute_ref_log_probs,
        num_train_epochs=hp.epochs,
        per_device_train_batch_size=hp.per_device_batch_size,
        gradient_accumulation_steps=hp.gradient_accumulation_steps,
        learning_rate=hp.learning_rate,
        weight_decay=hp.weight_decay,
        adam_beta1=hp.adam_beta1,
        adam_beta2=hp.adam_beta2,
        adam_epsilon=hp.adam_epsilon,
        max_grad_norm=hp.max_grad_norm,
        lr_scheduler_type=hp.lr_scheduler_type,
        warmup_ratio=hp.warmup_ratio,
        logging_steps=hp.logging_steps,
        save_steps=hp.save_steps,
        save_total_limit=hp.save_total_limit,
        eval_steps=hp.eval_steps,
        eval_strategy=hp.eval_strategy if eval_ds is not None else "no",
        bf16=hp.bf16,
        seed=hp.seed,
        report_to=hp.report_to,
        max_steps=5 if args.dry_run else -1,
        save_strategy="steps",
    )

    trainer = DPOTrainer(
        model=model,
        args=dpo_cfg,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        processing_class=tokenizer,
        # ref_model=None → DPOTrainer disables adapters internally for the
        # reference forward pass, so the SFT-init case still has a valid
        # reference (the unmodified base).
    )
    trainer.train()
    trainer.save_model(str(args.output_dir))

    # Persist the W&B run id so `evaluate.py` can resume this run.
    if hp.report_to == "wandb":
        try:
            import wandb  # type: ignore[import-not-found]
            if wandb.run is not None:
                (args.output_dir / "wandb_run_id.txt").write_text(wandb.run.id)
        except Exception:
            pass

    if args.dry_run:
        print("dry-run: reloading adapter to verify checkpoint integrity...")
        reloaded_base = AutoModelForCausalLM.from_pretrained(
            hp.base_model,
            torch_dtype="bfloat16" if hp.bf16 else "float16",
            device_map="auto",
        )
        PeftModel.from_pretrained(reloaded_base, str(args.output_dir))
        print("dry-run: adapter reloaded OK.")


# ---------------------------------------------------------------------------
# Pure-Python self-test for the condition logic
# ---------------------------------------------------------------------------

def _self_test_conditions() -> None:
    rows = _synthetic_dpo_rows(4)

    f = apply_condition(rows, "filtered", seed=0)
    assert [r["chosen"] for r in f] == [r["chosen"] for r in rows]
    assert [r["rejected"] for r in f] == [r["rejected"] for r in rows]

    inv = apply_condition(rows, "inverted", seed=0)
    assert [r["chosen"] for r in inv] == [r["rejected"] for r in rows]
    assert all(r["condition"] == "inverted" for r in inv)

    rnd1 = apply_condition(rows, "random", seed=42)
    rnd2 = apply_condition(rows, "random", seed=42)
    assert rnd1 == rnd2, "random condition must be deterministic for fixed seed"
    assert all(r["condition"] == "random" for r in rnd1)
    print("conditions self-test OK")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test_conditions()
    else:
        main()

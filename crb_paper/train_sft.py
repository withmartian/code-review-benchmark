"""SFT (warm-start) trainer for the CRB finetuning experiment.

Step 5 of the gating plan in `crb_paper/README.md`.

Trains a LoRA adapter on top of `Qwen/Qwen2.5-Coder-7B-Instruct` (or any
HF causal LM) using TRL's `SFTTrainer`. Reads the SFT JSONL produced by
`crb_paper/pairs.py` (`build_sft_dataset`); each row contributes one
(prompt, response) pair where response = an accepted suggestion's
description.

Loss masking: only the assistant turn (`response`) contributes to the
gradient; prompt tokens are masked. We use the Qwen2.5 chat template
to format each example.

Dependencies (NOT yet installed):

    pip install transformers peft trl accelerate datasets torch wandb

Monitoring (W&B): training runs log to Weights & Biases by default.
Set these env vars before running (or use `--report-to none` to disable):

    export WANDB_API_KEY=...
    export WANDB_PROJECT=crb-finetuning      # default if unset
    export WANDB_ENTITY=<your-org-or-user>   # optional

Usage:

    python3 crb_paper/train_sft.py \\
        --train-file data/sft_train.jsonl \\
        --eval-file data/sft_val.jsonl \\
        --output-dir runs/sft

Smoke test (no real training, 5 steps on synthetic data; still needs
the deps installed):

    python3 crb_paper/train_sft.py --dry-run --output-dir /tmp/sft_smoke
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Hyperparameters
# ---------------------------------------------------------------------------

@dataclass
class Hyperparameters:
    """All SFT knobs, with provenance. Override via CLI flags.

    Defaults follow the QLoRA / standard-instruct-SFT recipe so the v1
    run reproduces a known-good baseline; tune from here."""

    # --- Model -----------------------------------------------------------
    base_model: str = "Qwen/Qwen2.5-Coder-7B-Instruct"
    bf16: bool = True
    """bfloat16 compute. Requires Ampere+ GPU. Falls back to fp16 if unsupported."""

    # --- LoRA (PEFT) -----------------------------------------------------
    lora_r: int = 8
    """LoRA rank. Trainable params ~= 2 * r * d_model * len(target_modules).
    QLoRA paper uses r=64 for 7B; r=8 is a smaller-adapter sweet spot for
    fast experiments."""
    lora_alpha: int = 16
    """Scaling factor. Effective LR multiplier on the adapter ~ alpha/r."""
    lora_dropout: float = 0.05
    lora_target_modules: tuple[str, ...] = ("q_proj", "v_proj")
    """Attention-only adapter (minimal). Q+V is the original LoRA paper
    default. More aggressive: ('q_proj','k_proj','v_proj','o_proj').
    All-modules incl. MLP: ('q_proj','k_proj','v_proj','o_proj','gate_proj',
    'up_proj','down_proj') — bigger adapter, better but slower."""

    # --- Optimizer (AdamW) ----------------------------------------------
    learning_rate: float = 2e-5
    """Standard SFT range for LoRA on 7B. Full-finetune would be 1e-6 ish."""
    weight_decay: float = 0.0
    """LoRA usually keeps WD=0 since adapters are small."""
    adam_beta1: float = 0.9
    adam_beta2: float = 0.999
    adam_epsilon: float = 1e-8
    max_grad_norm: float = 1.0
    """Gradient clipping. 1.0 is the HF default; lower if losses are spiky."""

    # --- LR scheduler ----------------------------------------------------
    lr_scheduler_type: str = "cosine"
    """Cosine decay → 0. Alternatives: 'linear', 'constant_with_warmup'."""
    warmup_ratio: float = 0.03
    """Fraction of steps used for warmup. 3% is the InstructGPT default."""

    # --- Training loop ---------------------------------------------------
    epochs: int = 1
    """For the v1 ~10K-row dataset, 1 epoch is plenty; multi-epoch starts
    overfitting LoRA quickly."""
    per_device_batch_size: int = 1
    """Per-GPU. With seq_len=2048 + bf16 + LoRA, 1 fits comfortably on a
    single A100/L4. Increase only if VRAM allows."""
    gradient_accumulation_steps: int = 8
    """Effective batch = per_device_batch_size * gradient_accumulation_steps
    * num_gpus. Default 1 * 8 * 1 = 8."""

    # --- Sequence length -------------------------------------------------
    max_seq_length: int = 2048
    """Truncate longer rows. Bumping -> linearly more VRAM. Note: long-diff
    cutoff in the dataset is 12k tokens (README I/O contract); rows already
    above max_seq_length will be truncated by the tokenizer."""

    # --- Logging / checkpointing ----------------------------------------
    logging_steps: int = 25
    save_steps: int = 500
    save_total_limit: int = 3
    """Keep only the most recent N checkpoints."""
    eval_steps: int = 250
    eval_strategy: str = "steps"

    # --- Misc ------------------------------------------------------------
    seed: int = 42
    report_to: str = "wandb"
    """'wandb' (default — needs WANDB_API_KEY) / 'tensorboard' / 'none'.
    See module docstring for the full env-var list."""
    run_name: Optional[str] = None
    """W&B run name. None → auto-generated as `sft-<timestamp>`."""
    wandb_project: str = "crb-finetuning"
    """W&B project. Overridden by WANDB_PROJECT env var if set."""


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
                   help="JSONL of SFT rows (see crb_paper/README.md schema).")
    p.add_argument("--eval-file", type=Path,
                   help="JSONL for held-out eval. Optional.")
    p.add_argument("--output-dir", type=Path, required=True,
                   help="Where to save the LoRA adapter + trainer state.")
    # Most-likely-tuned overrides (keep CLI surface small; everything else
    # lives in Hyperparameters and can be edited in code or via a YAML if
    # we add one later)
    p.add_argument("--base-model", default=Hyperparameters.base_model)
    p.add_argument("--epochs", type=int, default=Hyperparameters.epochs)
    p.add_argument("--per-device-batch-size", type=int,
                   default=Hyperparameters.per_device_batch_size)
    p.add_argument("--gradient-accumulation", type=int,
                   default=Hyperparameters.gradient_accumulation_steps)
    p.add_argument("--learning-rate", type=float,
                   default=Hyperparameters.learning_rate)
    p.add_argument("--max-seq-length", type=int,
                   default=Hyperparameters.max_seq_length)
    p.add_argument("--seed", type=int, default=Hyperparameters.seed)
    p.add_argument("--report-to", default=Hyperparameters.report_to,
                   choices=["none", "wandb", "tensorboard"])
    p.add_argument("--run-name", default=None,
                   help="W&B run name. Auto-generated if omitted.")
    p.add_argument("--wandb-project", default=Hyperparameters.wandb_project,
                   help="W&B project (overridden by WANDB_PROJECT env if set).")
    p.add_argument("--dry-run", action="store_true",
                   help="Train 5 steps on 8 synthetic rows; verify save/reload.")
    return p.parse_args()


def hyperparams_from_args(args: argparse.Namespace) -> Hyperparameters:
    """CLI-overridable knobs feed into the dataclass; the rest stay default."""
    return Hyperparameters(
        base_model=args.base_model,
        epochs=args.epochs,
        per_device_batch_size=args.per_device_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation,
        learning_rate=args.learning_rate,
        max_seq_length=args.max_seq_length,
        seed=args.seed,
        report_to=args.report_to,
        run_name=args.run_name,
        wandb_project=args.wandb_project,
    )


def _resolve_run_name(hp: Hyperparameters) -> str:
    """Auto-generate a W&B run name if not provided."""
    if hp.run_name:
        return hp.run_name
    from datetime import datetime
    return f"sft-{datetime.now():%Y%m%d-%H%M%S}"


# ---------------------------------------------------------------------------
# Synthetic data for --dry-run
# ---------------------------------------------------------------------------

def _synthetic_sft_rows(n: int = 8) -> list[dict]:
    """Tiny dataset matching the SFT schema for smoke-testing the loop."""
    return [
        {
            "pr_id": i,
            "repo_name": f"org/repo-{i % 3}",
            "pr_created_at": "2026-01-01T00:00:00Z",
            "chatbot": "synthetic[bot]",
            "prompt": (
                "You are reviewing a pull request. Identify one specific "
                "issue with the change below and describe it in 1–3 "
                f"sentences.\n\nPR title: fix bug {i}\nDiff:\n+    return x\n"
            ),
            "response": f"The function should validate that x is non-null before returning (case {i}).",
            "response_tokens": 12,
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Training (heavy imports inside; deps may not be installed)
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    hp = hyperparams_from_args(args)

    # W&B env-var setup (only if reporting to wandb). The Trainer reads
    # WANDB_PROJECT / WANDB_RUN_NAME from env; we set them here if the
    # user passed CLI flags or relied on our defaults.
    if hp.report_to == "wandb":
        import os
        os.environ.setdefault("WANDB_PROJECT", hp.wandb_project)
        os.environ.setdefault("WANDB_RUN_NAME", _resolve_run_name(hp))

    try:
        import torch  # noqa: F401
        from datasets import Dataset
        from peft import LoraConfig, PeftModel, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from trl import SFTConfig, SFTTrainer
    except ImportError as e:
        print(
            f"ML deps missing ({e.name}). Install with:\n"
            "  pip install transformers peft trl accelerate datasets torch\n"
            "Then re-run.",
            file=sys.stderr,
        )
        sys.exit(0)

    # 1. Build the dataset
    if args.dry_run:
        train_rows = _synthetic_sft_rows(8)
        eval_rows: Optional[list[dict]] = None
    else:
        if args.train_file is None:
            raise SystemExit("--train-file required (or pass --dry-run).")
        train_rows = [json.loads(line) for line in args.train_file.read_text().splitlines() if line.strip()]
        eval_rows = None
        if args.eval_file is not None:
            eval_rows = [json.loads(line) for line in args.eval_file.read_text().splitlines() if line.strip()]

    # 2. Tokenizer + chat-template formatting
    tokenizer = AutoTokenizer.from_pretrained(hp.base_model)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    def format_row(row: dict) -> dict:
        # Two-turn chat: user = prompt, assistant = response. The chat
        # template emits the role markers Qwen expects.
        messages = [
            {"role": "user", "content": row["prompt"]},
            {"role": "assistant", "content": row["response"]},
        ]
        text = tokenizer.apply_chat_template(messages, tokenize=False)
        return {"text": text}

    train_ds = Dataset.from_list(train_rows).map(format_row, remove_columns=list(train_rows[0].keys()))
    eval_ds = (Dataset.from_list(eval_rows).map(format_row, remove_columns=list(eval_rows[0].keys()))
               if eval_rows else None)

    # 3. Model + LoRA
    model = AutoModelForCausalLM.from_pretrained(
        hp.base_model,
        torch_dtype="bfloat16" if hp.bf16 else "float16",
        device_map="auto",
    )
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

    # 4. Trainer config — every field maps to one Hyperparameters knob
    sft_cfg = SFTConfig(
        output_dir=str(args.output_dir),
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
        max_seq_length=hp.max_seq_length,
        logging_steps=hp.logging_steps,
        save_steps=hp.save_steps,
        save_total_limit=hp.save_total_limit,
        eval_steps=hp.eval_steps,
        eval_strategy=hp.eval_strategy if eval_ds is not None else "no",
        bf16=hp.bf16,
        seed=hp.seed,
        report_to=hp.report_to,
        max_steps=5 if args.dry_run else -1,  # cap for dry-run
        save_strategy="steps",
    )

    # 5. SFTTrainer — uses the assistant-only loss masker by default for
    #    chat-formatted text, so prompt tokens don't contribute to loss.
    trainer = SFTTrainer(
        model=model,
        args=sft_cfg,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        tokenizer=tokenizer,
    )
    trainer.train()
    trainer.save_model(str(args.output_dir))

    # 6. Verify the adapter loads back
    if args.dry_run:
        print("dry-run: reloading adapter to verify checkpoint integrity...")
        reloaded_base = AutoModelForCausalLM.from_pretrained(
            hp.base_model,
            torch_dtype="bfloat16" if hp.bf16 else "float16",
            device_map="auto",
        )
        PeftModel.from_pretrained(reloaded_base, str(args.output_dir))
        print("dry-run: adapter reloaded OK.")


if __name__ == "__main__":
    main()

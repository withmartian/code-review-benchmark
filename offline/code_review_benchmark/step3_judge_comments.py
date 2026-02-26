#!/usr/bin/env python3
"""
Evaluate tool reviews against golden comments using LLM as judge.

Uses extracted candidates if available, otherwise falls back to raw comments.
Calculates precision (TP / candidates) and recall (TP / golden).

Candidates and evaluations are stored per-model in results/{model}/.
"""

import asyncio
from dataclasses import dataclass
from dataclasses import field
import json
import os
from pathlib import Path

from openai import AsyncOpenAI
from tqdm import tqdm

RESULTS_DIR = Path("results")
BENCHMARK_DATA_FILE = RESULTS_DIR / "benchmark_data.json"
BATCH_SIZE = 40
LLM_CALL_TIMEOUT = 30  # seconds per individual LLM call
REVIEW_TIMEOUT = 1800  # seconds per full review evaluation (30 min)
IGNORED_COMMAND_COMMENTS = {"/propel review"}


JUDGE_PROMPT = """You are evaluating AI code review tools.
Determine if the candidate issue matches the golden (expected) comment.

Golden Comment (the issue we're looking for):
{golden_comment}

Candidate Issue (from the tool's review):
{candidate}

Instructions:
- Determine if the candidate identifies the SAME underlying issue as the golden comment
- Accept semantic matches - different wording is fine if it's the same problem
- Focus on whether they point to the same bug, concern, or code issue

Respond with ONLY a JSON object:
{{"reasoning": "brief explanation", "match": true/false, "confidence": 0.0-1.0}}"""


@dataclass
class EvaluationState:
    """Track evaluation progress for restarts."""

    completed: dict = field(default_factory=dict)

    def is_done(self, golden_url: str, tool: str) -> bool:
        if golden_url not in self.completed or tool not in self.completed[golden_url]:
            return False
        result = self.completed[golden_url][tool]
        return result.get("errors_count", 0) == 0

    def mark_done(self, golden_url: str, tool: str, result: dict):
        if golden_url not in self.completed:
            self.completed[golden_url] = {}
        self.completed[golden_url][tool] = result

    def save(self, path: Path):
        with open(path, "w") as f:
            json.dump(self.completed, f, indent=2)

    @classmethod
    def load(cls, path: Path) -> "EvaluationState":
        state = cls()
        if path.exists():
            with open(path) as f:
                state.completed = json.load(f)
        return state


def load_dotenv():
    """Load .env file into environment."""
    env_path = Path(".env")
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def sanitize_model_name(model: str) -> str:
    """Sanitize model name for use as directory name."""
    return model.strip().replace("/", "_")


def get_model_dir() -> Path:
    """Get the model-specific results directory, creating it if needed."""
    model = os.environ.get("MARTIAN_MODEL", "openai/gpt-4o-mini")
    model_dir = RESULTS_DIR / sanitize_model_name(model)
    model_dir.mkdir(parents=True, exist_ok=True)
    return model_dir


def is_ignored_candidate(candidate: str | None) -> bool:
    """True when candidate text is non-review command chatter."""
    if not candidate:
        return False
    normalized = " ".join(candidate.strip().split()).lower()
    return normalized in IGNORED_COMMAND_COMMENTS


class LLMJudge:
    def __init__(self, structured_output: bool = False):
        load_dotenv()

        api_key = os.environ.get("MARTIAN_API_KEY")
        base_url = os.environ.get("MARTIAN_BASE_URL", "https://api.withmartian.com/v1")

        if not api_key:
            raise ValueError("MARTIAN_API_KEY environment variable required")

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = os.environ.get("MARTIAN_MODEL", "openai/gpt-4o-mini")
        self.structured_output = structured_output

        print(f"Judge model: {self.model}")
        print(f"Base URL: {base_url}")
        if structured_output:
            print("Structured output: enabled")

    async def call_llm(self, prompt: str, max_retries: int = 3) -> dict:
        for attempt in range(max_retries):
            try:
                kwargs = {
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a precise code review evaluator. Always respond with valid JSON.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.0,
                }
                if self.structured_output:
                    kwargs["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "match_result",
                            "strict": True,
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "reasoning": {"type": "string"},
                                    "match": {"type": "boolean"},
                                    "confidence": {"type": "number"},
                                },
                                "required": ["reasoning", "match", "confidence"],
                                "additionalProperties": False,
                            },
                        },
                    }

                response = await asyncio.wait_for(
                    self.client.chat.completions.create(**kwargs),
                    timeout=LLM_CALL_TIMEOUT,
                )

                content = response.choices[0].message.content.strip()

                if not self.structured_output and content.startswith("```"):
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]
                    content = content.strip()

                return json.loads(content)

            except TimeoutError:
                if attempt == max_retries - 1:
                    return {"error": f"Timed out after {LLM_CALL_TIMEOUT}s"}
                await asyncio.sleep(2**attempt)

            except json.JSONDecodeError:
                if attempt == max_retries - 1:
                    return {"error": "JSON parse failed"}
                await asyncio.sleep(1)

            except Exception as e:
                if attempt == max_retries - 1:
                    return {"error": str(e)}
                await asyncio.sleep(2**attempt)

        return {"error": "Max retries exceeded"}

    async def match_comment(self, golden_comment: str, candidate: str) -> dict:
        prompt = JUDGE_PROMPT.format(golden_comment=golden_comment, candidate=candidate)
        return await self.call_llm(prompt)


async def process_batch(tasks: list, batch_size: int = BATCH_SIZE) -> list:
    results = []
    for i in range(0, len(tasks), batch_size):
        batch = tasks[i : i + batch_size]
        batch_results = await asyncio.gather(*batch, return_exceptions=True)
        results.extend(batch_results)
        if i + batch_size < len(tasks):
            await asyncio.sleep(0.5)
    return results


def get_candidates(review: dict, all_candidates: dict, golden_url: str) -> list[str]:
    """Get candidate texts - use model-specific candidates or fall back to raw comments."""
    tool = review["tool"]

    # Prefer model-specific candidates file
    if golden_url in all_candidates and tool in all_candidates[golden_url]:
        candidates = all_candidates[golden_url][tool]
        return [c["text"] for c in candidates if c.get("text") and not is_ignored_candidate(c["text"])]

    # Fall back to raw comment bodies
    comments = review.get("review_comments", [])
    return [c["body"] for c in comments if c.get("body") and not is_ignored_candidate(c["body"])]


async def evaluate_review(
    judge: LLMJudge,
    golden_comments: list[dict],
    candidates: list[str],
) -> dict:
    """Evaluate candidates against golden comments. Returns precision and recall metrics."""
    candidates = [candidate for candidate in candidates if not is_ignored_candidate(candidate)]

    if not golden_comments:
        return {
            "skipped": True,
            "reason": "No golden comments",
        }

    if not candidates:
        return {
            "skipped": False,
            "true_positives": [],
            "false_positives": [],
            "false_negatives": [
                {"golden_comment": gc["comment"], "severity": gc.get("severity")} for gc in golden_comments
            ],
            "errors": [],
            "total_candidates": 0,
            "total_golden": len(golden_comments),
            "tp": 0,
            "fp": 0,
            "fn": len(golden_comments),
            "errors_count": 0,
            "precision": 0.0,
            "recall": 0.0,
        }

    # Create matching tasks: each golden comment vs each candidate
    tasks = []
    task_meta = []

    for gc in golden_comments:
        for candidate in candidates:
            tasks.append(judge.match_comment(gc["comment"], candidate))
            task_meta.append(
                {
                    "golden": gc["comment"],
                    "golden_severity": gc.get("severity"),
                    "candidate": candidate,
                }
            )

    # Process all comparisons
    results = await process_batch(tasks)

    # Build match matrix
    # Initialize all golden comments as unmatched
    golden_matched = {
        gc["comment"]: {
            "severity": gc.get("severity"),
            "matched": False,
            "best_confidence": 0.0,
            "matched_candidate": None,
        }
        for gc in golden_comments
    }
    candidate_matched = dict.fromkeys(candidates, False)
    errors = []

    for i, result in enumerate(results):
        meta = task_meta[i]
        golden = meta["golden"]
        candidate = meta["candidate"]

        if isinstance(result, Exception):
            errors.append({"golden": golden, "candidate": candidate, "error": str(result)})
            continue
        if result.get("error"):
            errors.append({"golden": golden, "candidate": candidate, "error": result["error"]})
            continue

        if result.get("match") and result.get("confidence", 0) > golden_matched[golden]["best_confidence"]:
            golden_matched[golden]["matched"] = True
            golden_matched[golden]["best_confidence"] = result["confidence"]
            golden_matched[golden]["matched_candidate"] = candidate
            golden_matched[golden]["reasoning"] = result.get("reasoning")
            candidate_matched[candidate] = True

    # Calculate metrics
    true_positives = []
    false_negatives = []

    for golden, info in golden_matched.items():
        if info["matched"]:
            true_positives.append(
                {
                    "golden_comment": golden,
                    "severity": info["severity"],
                    "matched_candidate": info["matched_candidate"],
                    "confidence": info["best_confidence"],
                    "reasoning": info.get("reasoning"),
                }
            )
        else:
            false_negatives.append(
                {
                    "golden_comment": golden,
                    "severity": info["severity"],
                }
            )

    # False positives: candidates that didn't match any golden
    false_positives = [{"candidate": c} for c, matched in candidate_matched.items() if not matched]

    total_candidates = len(candidates)
    total_golden = len(golden_comments)
    tp_count = len(true_positives)

    precision = tp_count / total_candidates if total_candidates > 0 else 0.0
    recall = tp_count / total_golden if total_golden > 0 else 0.0

    return {
        "skipped": False,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "errors": errors,
        "total_candidates": total_candidates,
        "total_golden": total_golden,
        "tp": tp_count,
        "fp": len(false_positives),
        "fn": len(false_negatives),
        "errors_count": len(errors),
        "precision": precision,
        "recall": recall,
    }


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Evaluate reviews with LLM judge")
    parser.add_argument("--tool", help="Only evaluate specific tool")
    parser.add_argument("--limit", type=int, help="Limit number of evaluations")
    parser.add_argument("--force", action="store_true", help="Re-evaluate even if already done")
    parser.add_argument("--structured", action="store_true", help="Use structured output (json_schema response_format)")
    args = parser.parse_args()

    load_dotenv()

    if not BENCHMARK_DATA_FILE.exists():
        print(f"Error: {BENCHMARK_DATA_FILE} not found")
        return

    with open(BENCHMARK_DATA_FILE) as f:
        benchmark_data = json.load(f)

    print(f"Loaded {len(benchmark_data)} PRs")

    # Load model-specific candidates and evaluations
    model_dir = get_model_dir()
    candidates_file = model_dir / "candidates.json"
    evaluations_file = model_dir / "evaluations.json"

    print(f"Model dir: {model_dir}")

    # Load candidates
    all_candidates = {}
    if candidates_file.exists():
        with open(candidates_file) as f:
            all_candidates = json.load(f)
        print(f"Loaded candidates from {candidates_file}")

    # Load state
    state = EvaluationState.load(evaluations_file)

    # If --force with --tool, only clear that tool's evaluations
    if args.force and args.tool:
        for golden_url in state.completed:
            if args.tool in state.completed[golden_url]:
                del state.completed[golden_url][args.tool]
        print(f"Cleared evaluations for {args.tool}")
    elif args.force:
        state = EvaluationState()
        print("Cleared all evaluations")

    already_done = sum(len(tools) for tools in state.completed.values())
    print(f"Resuming: {already_done} evaluations completed")

    judge = LLMJudge(structured_output=args.structured)

    # Build work items list
    work_items = []
    for golden_url, entry in benchmark_data.items():
        golden_comments = entry.get("golden_comments", [])
        for review in entry.get("reviews", []):
            tool = review["tool"]
            if args.tool and tool != args.tool:
                continue
            work_items.append((golden_url, entry, golden_comments, review, tool))

    evaluated = 0
    skipped = 0
    timed_out = 0

    pbar = tqdm(work_items, desc="Judging", unit="review")
    try:
        for golden_url, _entry, golden_comments, review, tool in pbar:
            if not args.force and state.is_done(golden_url, tool):
                skipped += 1
                pbar.set_postfix(eval=evaluated, skip=skipped, timeout=timed_out)
                continue

            if args.limit and evaluated >= args.limit:
                pbar.set_description("Limit reached")
                break

            pbar.set_postfix(tool=tool, eval=evaluated, skip=skipped, timeout=timed_out)

            candidates = get_candidates(review, all_candidates, golden_url)

            try:
                result = await asyncio.wait_for(
                    evaluate_review(judge, golden_comments, candidates),
                    timeout=REVIEW_TIMEOUT,
                )
            except TimeoutError:
                timed_out += 1
                pbar.set_postfix(tool=tool, eval=evaluated, skip=skipped, timeout=timed_out)
                continue

            result["tool"] = tool
            result["repo_name"] = review.get("repo_name")
            result["pr_url"] = review.get("pr_url")

            state.mark_done(golden_url, tool, result)
            state.save(evaluations_file)

            evaluated += 1
    except KeyboardInterrupt:
        pbar.close()
        state.save(evaluations_file)
        print(f"\nInterrupted — saved {evaluated} new evaluations to {evaluations_file}")
        print("Re-run to continue where you left off.")
        return

    # Summary
    print("\n" + "=" * 60)
    if timed_out:
        print(f"Done — {timed_out} review(s) timed out. Re-run to retry them.")
    else:
        print("Evaluation complete!")
    print(f"Results saved to: {evaluations_file}")

    # Aggregate metrics per tool, collect error details
    tool_metrics = {}
    error_details = []
    for golden_url, tools in state.completed.items():
        for tool, result in tools.items():
            if result.get("skipped"):
                continue
            if tool not in tool_metrics:
                tool_metrics[tool] = {"tp": 0, "fp": 0, "fn": 0, "errors": 0, "count": 0}
            tool_metrics[tool]["tp"] += result.get("tp", 0)
            tool_metrics[tool]["fp"] += result.get("fp", 0)
            tool_metrics[tool]["fn"] += result.get("fn", 0)
            tool_metrics[tool]["errors"] += result.get("errors_count", 0)
            tool_metrics[tool]["count"] += 1
            for err in result.get("errors", []):
                error_details.append({"tool": tool, "pr": golden_url, "error": err.get("error", "unknown")})

    print("\nAggregate metrics by tool:")
    print(f"{'Tool':<12} {'Precision':>10} {'Recall':>10} {'F1':>10} {'Reviews':>8} {'Errors':>8}")
    print("-" * 60)
    for tool in sorted(tool_metrics.keys()):
        m = tool_metrics[tool]
        precision = m["tp"] / (m["tp"] + m["fp"]) if (m["tp"] + m["fp"]) > 0 else 0
        recall = m["tp"] / (m["tp"] + m["fn"]) if (m["tp"] + m["fn"]) > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
        print(f"{tool:<12} {precision:>10.1%} {recall:>10.1%} {f1:>10.1%} {m['count']:>8} {m['errors']:>8}")

    if error_details:
        print(f"\nError details ({len(error_details)} total):")
        print("-" * 60)
        for ed in error_details:
            pr_short = ed["pr"].split("/pull/")[1] if "/pull/" in ed["pr"] else ed["pr"]
            repo = ed["pr"].split("/")[-3] if "/" in ed["pr"] else "?"
            print(f"  {ed['tool']:<12} {repo}/#{pr_short:<20} {ed['error']}")


if __name__ == "__main__":
    asyncio.run(main())

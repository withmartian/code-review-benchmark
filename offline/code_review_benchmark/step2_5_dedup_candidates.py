#!/usr/bin/env python3
"""
Deduplicate extracted candidates before judging.

For each (PR, tool) pair, sends all candidates in a single LLM call and asks
it to group duplicates — candidates that express the same underlying concern,
even if worded differently or spread across files.

Dedup groups are saved as lists of candidate indices. Step 3 uses these groups
to propagate candidate_matched to siblings, preventing duplicate candidates
from being counted as false positives.

Output: results/{model}/dedup_groups.json

Usage:
  uv run python -m code_review_benchmark.step2_5_dedup_candidates
  uv run python -m code_review_benchmark.step2_5_dedup_candidates --tool qodo --force
"""

import asyncio
import json
import os
from pathlib import Path

from openai import AsyncOpenAI

RESULTS_DIR = Path("results")
BATCH_SIZE = 30
LLM_CALL_TIMEOUT = 30
MAX_RETRIES = 3

# Only deduplicate when there are at least 2 candidates
MIN_CANDIDATES = 2

STRICT_PROMPT = """You are identifying duplicate code review comments.

Below is a numbered list of issues extracted from an AI tool's code review.
Some tools post the same issue in both a summary comment and an inline comment,
creating near-identical duplicates. Your job is to find those duplicates.

Two candidates are duplicates ONLY IF:
- They describe the same problem AND
- A single code change would fix both (i.e., they would be one bug report)

Two candidates are NOT duplicates if:
- They describe the same TYPE of bug but in different files, functions, or
  classes (e.g., "negative slicing in OptimizedCursorPaginator" vs "negative
  slicing in BasePaginator" are separate issues — fixing one does not fix
  the other)
- They describe related but distinct problems (e.g., "returns wrong type" vs
  "caller crashes because of wrong type" are separate issues)

When in doubt, keep candidates separate — it is better to leave a duplicate
ungrouped than to incorrectly merge two distinct issues.

Candidates:
{candidates}

Return ONLY a JSON object where each group is a list of 0-based indices.
Singletons (no duplicate) must still appear as single-element groups.

Example for 4 candidates where 0 and 2 are duplicates:
{{"groups": [[0, 2], [1], [3]]}}

Your response:"""

DEDUP_PROMPT = STRICT_PROMPT


def load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def sanitize_model_name(model: str) -> str:
    return model.strip().replace("/", "_")


def get_model_dir() -> Path:
    model = os.environ.get("MARTIAN_MODEL", "openai/gpt-4o-mini")
    model_dir = RESULTS_DIR / sanitize_model_name(model)
    model_dir.mkdir(parents=True, exist_ok=True)
    return model_dir



def _parse_groups_response(content: str, n_candidates: int) -> list[list[int]] | None:
    """
    Parse and validate the LLM grouping response.
    Returns None if the response is invalid.
    """
    # Strip markdown code fences if present
    if content.startswith("```"):
        parts = content.split("```")
        content = parts[1] if len(parts) > 1 else content
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None

    if "groups" not in data or not isinstance(data["groups"], list):
        return None

    groups = data["groups"]

    # Validate: every index 0..n-1 must appear exactly once
    seen: set[int] = set()
    for group in groups:
        if not isinstance(group, list):
            return None
        for idx in group:
            if not isinstance(idx, int):
                return None
            if idx < 0 or idx >= n_candidates:
                return None
            if idx in seen:
                return None
            seen.add(idx)

    if seen != set(range(n_candidates)):
        return None

    return groups


class DedupLLM:
    def __init__(self) -> None:
        load_dotenv()
        api_key = os.environ.get("MARTIAN_API_KEY")
        base_url = os.environ.get("MARTIAN_BASE_URL", "https://api.withmartian.com/v1")
        if not api_key:
            raise ValueError("MARTIAN_API_KEY environment variable required")
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = os.environ.get("MARTIAN_MODEL", "openai/gpt-4o-mini")
        print(f"Dedup model: {self.model}")
        print(f"Base URL: {base_url}")

    async def dedup_candidates(
        self,
        candidates: list[str],
        prompt_template: str,
    ) -> list[list[int]] | None:
        """
        Group duplicate candidates via a single LLM call with retries.
        Returns a list of groups (each group is a list of indices), or None on
        total failure (caller should fall back to all-singletons).
        """
        n = len(candidates)
        numbered = "\n".join(f"{i}. {text}" for i, text in enumerate(candidates))
        prompt = prompt_template.format(candidates=numbered)

        for attempt in range(MAX_RETRIES):
            try:
                response = await asyncio.wait_for(
                    self.client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "You group duplicate code review comments. "
                                    "Always respond with valid JSON only."
                                ),
                            },
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.0,
                    ),
                    timeout=LLM_CALL_TIMEOUT,
                )
                content = response.choices[0].message.content.strip()
                groups = _parse_groups_response(content, n)
                if groups is not None:
                    return groups
                # Parse succeeded but validation failed — retry
                print(f"    Retry {attempt + 1}/{MAX_RETRIES}: invalid group structure in response")

            except TimeoutError:
                print(f"    Retry {attempt + 1}/{MAX_RETRIES}: timed out")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)

            except json.JSONDecodeError:
                print(f"    Retry {attempt + 1}/{MAX_RETRIES}: JSON parse failed")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(1)

            except Exception as e:
                print(f"    Retry {attempt + 1}/{MAX_RETRIES}: {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)

        return None  # All retries exhausted


async def process_batch(tasks: list, batch_size: int = BATCH_SIZE) -> list:
    results = []
    for i in range(0, len(tasks), batch_size):
        batch = tasks[i : i + batch_size]
        batch_results = await asyncio.gather(*batch, return_exceptions=True)
        results.extend(batch_results)
        if i + batch_size < len(tasks):
            await asyncio.sleep(0.2)
    return results


async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Deduplicate extracted candidates before judging"
    )
    parser.add_argument("--tool", help="Only deduplicate a specific tool")
    parser.add_argument(
        "--force", action="store_true", help="Re-run even if groups already exist"
    )
    args = parser.parse_args()

    load_dotenv()

    model_dir = get_model_dir()
    candidates_file = model_dir / "candidates.json"
    output_file = model_dir / "dedup_groups.json"

    if not candidates_file.exists():
        print(f"Error: {candidates_file} not found. Run step2 first.")
        return

    with open(candidates_file) as f:
        all_candidates: dict = json.load(f)

    # Load existing output for incremental runs
    if output_file.exists():
        with open(output_file) as f:
            all_groups: dict = json.load(f)
        print(f"Loaded {len(all_groups)} existing entries from {output_file}")
        if args.force and args.tool:
            # Only clear the specific tool being forced, preserve all others
            for pr_url in all_groups:
                all_groups[pr_url].pop(args.tool, None)
            print(f"Cleared existing groups for tool: {args.tool}")
        elif args.force:
            all_groups = {}
            print("Cleared all existing groups")
    else:
        all_groups = {}

    print(f"Output: {output_file}")

    dedup_llm = DedupLLM()

    # Build work list: (golden_url, tool, candidates_list)
    work_items = []
    for golden_url, tools in all_candidates.items():
        for tool, candidates in tools.items():
            if args.tool and tool != args.tool:
                continue
            if len(candidates) < MIN_CANDIDATES:
                continue
            # Skip if already done (unless --force)
            if not args.force and golden_url in all_groups and tool in all_groups.get(golden_url, {}):
                continue
            texts = [c["text"] for c in candidates if c.get("text")]
            if len(texts) < MIN_CANDIDATES:
                continue
            work_items.append((golden_url, tool, texts))

    print(f"Reviews to deduplicate: {len(work_items)}")

    if not work_items:
        print("Nothing to process.")
        if not output_file.exists():
            with open(output_file, "w") as f:
                json.dump(all_groups, f, indent=2)
        return

    # Process in batches
    tasks = [
        dedup_llm.dedup_candidates(texts, DEDUP_PROMPT)
        for _, _, texts in work_items
    ]
    results = await process_batch(tasks)

    success = 0
    failed = 0
    fallback = 0

    for (golden_url, tool, texts), result in zip(work_items, results, strict=True):
        if isinstance(result, Exception):
            print(f"  Exception for {tool} @ {golden_url}: {result}")
            result = None

        if result is None:
            # Fall back to all-singletons — no dedup, but no crash
            groups = [[i] for i in range(len(texts))]
            fallback += 1
        else:
            groups = result
            success += 1

        if golden_url not in all_groups:
            all_groups[golden_url] = {}
        all_groups[golden_url][tool] = groups

        # Periodic save
        if (success + fallback) % 50 == 0:
            with open(output_file, "w") as f:
                json.dump(all_groups, f, indent=2)
            print(f"  Saved progress: {success} ok, {failed} errors, {fallback} fallbacks")

    with open(output_file, "w") as f:
        json.dump(all_groups, f, indent=2)

    print("\nDone!")
    print(f"  Succeeded : {success}")
    print(f"  Fallbacks : {fallback}  (all-singletons, no dedup applied)")
    print(f"  Errors    : {failed}")
    print(f"  Saved to  : {output_file}")


if __name__ == "__main__":
    asyncio.run(main())

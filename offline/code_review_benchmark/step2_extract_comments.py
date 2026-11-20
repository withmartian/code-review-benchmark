#!/usr/bin/env python3
"""
Extract candidate code review comments from tool reviews.

Uses LLM to extract individual issues from all review comments.

Candidates are stored per-model in results/{model}/candidates.json.
"""

import asyncio
import json
import os
from pathlib import Path

from openai import AsyncOpenAI

RESULTS_DIR = Path("results")
BENCHMARK_DATA_FILE = RESULTS_DIR / "benchmark_data.json"
BATCH_SIZE = 50


EXTRACT_PROMPT = """You are analyzing an AI code review comment to extract individual issues mentioned.

The comment may discuss multiple distinct problems. Extract each separate issue as a standalone item.

Code Review Comment:
{comment}

Instructions:
- Extract each distinct code issue, bug, or concern mentioned
- Each issue should be a single, specific problem (not a general observation)
- Ignore meta-commentary like "I found 2 issues" - extract the actual issues
- Ignore sign-offs, greetings, or formatting instructions
- If the comment contains no actionable code review issues, return an empty list

Example input:
"Found several problems: 1) The getUserById function doesn't handle null input, which will cause a crash.
2) The cache key uses user.name but should use user.id for uniqueness.
Also, consider adding retry logic for the API call."

Example output:
{{"issues": [
  "getUserById function doesn't handle null input, causing potential crash",
  "Cache key uses user.name instead of user.id, breaking uniqueness",
  "Missing retry logic for API call"
]}}

Respond with ONLY a JSON object:
{{"issues": ["issue 1", "issue 2", ...]}}"""


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


class CandidateExtractor:
    def __init__(self):
        load_dotenv()

        api_key = os.environ.get("MARTIAN_API_KEY")
        base_url = os.environ.get("MARTIAN_BASE_URL", "https://api.withmartian.com/v1")

        if not api_key:
            raise ValueError("MARTIAN_API_KEY environment variable required")

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = os.environ.get("MARTIAN_MODEL", "openai/gpt-4o-mini")

        print(f"Model: {self.model}")
        print(f"Base URL: {base_url}")
        print(f"Batch size: {BATCH_SIZE}")

    async def call_llm(self, prompt: str, max_retries: int = 3) -> dict:
        """Call LLM API with retry logic."""
        for attempt in range(max_retries):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": "You extract code review issues from comments. Always respond with valid JSON.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.0,
                )

                content = response.choices[0].message.content.strip()

                # Remove markdown code blocks if present
                if content.startswith("```"):
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]
                    content = content.strip()

                result = json.loads(content)
                # Validate response has issues key
                if "issues" not in result:
                    result = {"issues": [], "error": "Missing issues key"}
                return result

            except json.JSONDecodeError as e:
                if attempt == max_retries - 1:
                    return {"issues": [], "error": f"JSON parse failed: {e}"}
                await asyncio.sleep(1)

            except Exception as e:
                if attempt == max_retries - 1:
                    return {"issues": [], "error": str(e)}
                await asyncio.sleep(2**attempt)

        return {"issues": [], "error": "Max retries exceeded"}

    async def extract_from_comment(self, comment_body: str) -> dict:
        """Extract issues from a single comment. Returns dict with issues and optional error."""
        if not comment_body or len(comment_body.strip()) < 20:
            return {"issues": [], "skipped": True}

        prompt = EXTRACT_PROMPT.format(comment=comment_body)
        return await self.call_llm(prompt)


def get_all_comment_text(review_comments: list[dict]) -> str:
    """Combine all comment bodies into a single text for extraction."""
    bodies = [c["body"] for c in review_comments if c.get("body")]
    return "\n\n---\n\n".join(bodies)


def get_comment_text_for_review(golden_url: str, tool: str, review_comments: list[dict]) -> str:
    """Get comment text — use parsed file if available, otherwise raw comments."""
    parsed_file = RESULTS_DIR / f"parsed_{tool}.json"

    if parsed_file.exists():
        with open(parsed_file) as f:
            parsed_data = json.load(f)
        review = parsed_data.get("reviews", {}).get(golden_url)
        if review is not None:
            # Parsed file has an entry for this PR — use it even if empty
            # (empty means the parser found no actionable comments)
            return review.get("rendered_markdown", "")

    # Fallback: original behavior (no parsed file for this tool)
    return get_all_comment_text(review_comments)


async def process_batch(tasks: list, batch_size: int = BATCH_SIZE) -> list:
    """Process async tasks in batches."""
    results = []
    for i in range(0, len(tasks), batch_size):
        batch = tasks[i : i + batch_size]
        batch_results = await asyncio.gather(*batch, return_exceptions=True)
        results.extend(batch_results)
        if i + batch_size < len(tasks):
            await asyncio.sleep(0.2)
    return results


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Extract candidate comments from reviews")
    parser.add_argument("--tool", help="Only process specific tool")
    parser.add_argument("--limit", type=int, help="Limit number of extractions")
    parser.add_argument("--force", action="store_true", help="Re-extract even if candidates exist")
    args = parser.parse_args()

    if not BENCHMARK_DATA_FILE.exists():
        print(f"Error: {BENCHMARK_DATA_FILE} not found")
        return

    with open(BENCHMARK_DATA_FILE) as f:
        data = json.load(f)

    print(f"Loaded {len(data)} PRs")

    extractor = CandidateExtractor()

    # Load/init model-specific candidates file
    model_dir = get_model_dir()
    candidates_file = model_dir / "candidates.json"
    print(f"Candidates file: {candidates_file}")

    if candidates_file.exists():
        with open(candidates_file) as f:
            all_candidates = json.load(f)
    else:
        all_candidates = {}

    # Handle --force: clear candidates for specified tool (or all)
    if args.force:
        if args.tool:
            for golden_url in all_candidates:
                all_candidates[golden_url].pop(args.tool, None)
            print(f"Cleared candidates for tool: {args.tool}")
        else:
            all_candidates = {}
            print("Cleared all candidates")

    # Collect all reviews that need LLM extraction
    extraction_tasks = []  # (golden_url, tool, all_comments_text)

    for golden_url, entry in data.items():
        for review in entry.get("reviews", []):
            tool = review["tool"]

            if args.tool and tool != args.tool:
                continue

            # Skip if already has candidates for this (golden_url, tool)
            if golden_url in all_candidates and tool in all_candidates[golden_url]:
                continue

            comments = review.get("review_comments", [])
            all_text = get_comment_text_for_review(golden_url, tool, comments)

            if all_text and len(all_text.strip()) >= 20:
                extraction_tasks.append((golden_url, tool, all_text))

    if args.limit:
        extraction_tasks = extraction_tasks[: args.limit]

    print(f"Reviews needing LLM extraction: {len(extraction_tasks)}")

    if not extraction_tasks:
        print("Nothing to process.")
        return

    # Create async tasks for LLM extractions
    async_tasks = [extractor.extract_from_comment(text) for _, _, text in extraction_tasks]

    print(f"Processing {len(async_tasks)} extractions in batches of {BATCH_SIZE}...")

    # Process all in parallel batches
    results = await process_batch(async_tasks)

    # Apply results and save
    success_count = 0
    error_count = 0

    for idx, result in enumerate(results):
        golden_url, tool, _ = extraction_tasks[idx]

        # Handle exceptions from gather
        if isinstance(result, Exception):
            print(f"  Error for {tool}: {result}")
            error_count += 1
            continue

        # Check for API errors
        if result.get("error"):
            print(f"  Error for {tool}: {result['error']}")
            error_count += 1
            continue

        # Build candidates from extracted issues
        candidates = []
        for issue in result.get("issues", []):
            candidates.append(
                {
                    "text": issue,
                    "path": None,
                    "line": None,
                    "source": "extracted",
                }
            )

        # Save candidates
        if golden_url not in all_candidates:
            all_candidates[golden_url] = {}
        all_candidates[golden_url][tool] = candidates
        success_count += 1

        # Save periodically (every 50 successful extractions)
        if success_count % 50 == 0:
            with open(candidates_file, "w") as f:
                json.dump(all_candidates, f, indent=2)
            print(f"  Saved progress: {success_count} successful, {error_count} errors")

    # Final save
    with open(candidates_file, "w") as f:
        json.dump(all_candidates, f, indent=2)

    print("\nDone!")
    print(f"  Successful: {success_count}")
    print(f"  Errors: {error_count}")
    print(f"  Data saved to {candidates_file}")


if __name__ == "__main__":
    asyncio.run(main())

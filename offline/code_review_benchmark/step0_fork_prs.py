#!/usr/bin/env python3
"""
GitHub PR Forker.

This tool clones a repository and recreates a pull request (PR) in your
organization for AI code review. You can either process a single PR URL
or provide a JSON file containing multiple PR entries (as produced by
`golden_comments/*.json`) and the script will process each PR in a simple
loop with a text progress indicator.

Usage:
    # Single PR
    python pr_forker.py <PR_URL> --org <ORG_NAME> --name <AI_TOOL_NAME>

    # Batch from file (array of objects with `url` keys)
    python pr_forker.py --file golden_comments/cal_dot_com.json --org <ORG> --name <AI_TOOL>

Example:
    python pr_forker.py https://github.com/owner/repo/pull/123 --org my-org --name coderabbit
"""

import argparse
from datetime import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time

from dotenv import load_dotenv
import requests


class GitHubPRForker:
    def __init__(self, token: str, org: str):
        self.token = token
        self.org = org
        self.base_url = "https://api.github.com"
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        self._verify_auth()

    def _request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{endpoint}"
        return requests.request(method, url, headers=self.headers, **kwargs)

    def _verify_auth(self):
        response = self._request("GET", "/user")
        if response.status_code != 200:
            raise Exception(f"Auth failed: {response.json().get('message')}")

    def parse_pr_url(self, pr_url: str) -> tuple[str, str, int]:
        match = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
        if match:
            owner, repo, pr_number = match.groups()
            return owner, repo.replace(".git", ""), int(pr_number)
        raise ValueError(f"Invalid PR URL: {pr_url}")

    def get_pr_details(self, owner: str, repo: str, pr_number: int) -> dict:
        response = self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")
        if response.status_code != 200:
            raise Exception(f"Failed to fetch PR: {response.json().get('message')}")
        return response.json()

    def repo_exists(self, repo_name: str) -> bool:
        return self._request("GET", f"/repos/{self.org}/{repo_name}").status_code == 200

    def create_repo(self, repo_name: str):
        # Create as private initially — public repos have mandatory push protection
        # enforced at the platform level that can't be disabled via API. We make
        # it public after pushing.
        response = self._request(
            "POST",
            f"/orgs/{self.org}/repos",
            json={"name": repo_name, "private": True, "auto_init": False},
        )
        if response.status_code != 201:
            raise Exception(f"Failed to create repo: {response.json().get('message')}")

    def make_repo_public(self, repo_name: str):
        """Make repo public after pushing — avoids push protection on public repos."""
        response = self._request(
            "PATCH",
            f"/repos/{self.org}/{repo_name}",
            json={"private": False},
        )
        if response.status_code not in (200, 204):
            print(f"Warning: Could not make repo public: {response.json().get('message')}")

    def disable_actions(self, repo_name: str):
        """Disable GitHub Actions for the repository."""
        response = self._request(
            "PUT",
            f"/repos/{self.org}/{repo_name}/actions/permissions",
            json={"enabled": False},
        )
        if response.status_code not in (200, 204):
            print(f"Warning: Could not disable actions: {response.json().get('message')}")

    def disable_push_protection(self, repo_name: str):
        """Disable secret scanning push protection to allow pushing test fixtures with token-like strings."""
        response = self._request(
            "PATCH",
            f"/repos/{self.org}/{repo_name}",
            json={
                "security_and_analysis": {
                    "secret_scanning_push_protection": {"status": "disabled"}
                }
            },
        )
        if response.status_code not in (200, 204):
            print(f"Warning: Could not disable push protection: {response.json().get('message')}")

    def create_pull_request(
        self, repo: str, title: str, body: str, head: str, base: str
    ) -> dict:
        response = self._request(
            "POST",
            f"/repos/{self.org}/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
        if response.status_code != 201:
            err = response.json()
            raise Exception(
                f"Failed to create PR: {err.get('message')} - {err.get('errors')}"
            )
        return response.json()

    def generate_repo_name(
        self, original_repo: str, pr_number: int, ai_tool_name: str, config_prefix: str | None = None
    ) -> str:
        date_str = datetime.now().strftime("%Y%m%d")
        tool_slug = re.sub(r"[^a-zA-Z0-9]+", "-", ai_tool_name.lower()).strip("-")[:30]
        if config_prefix:
            return f"{config_prefix}__{original_repo}__{tool_slug}__PR{pr_number}__{date_str}"
        return f"{original_repo}__{tool_slug}__PR{pr_number}__{date_str}"

    def run_git(self, tmpdir: str, *args) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", tmpdir, *args], capture_output=True, text=True)

    def process_pr(self, pr_url: str, ai_tool_name: str, config_prefix: str | None = None) -> dict:
        owner, repo, pr_number = self.parse_pr_url(pr_url)
        print(f"\nProcessing PR #{pr_number} from {owner}/{repo}")

        pr = self.get_pr_details(owner, repo, pr_number)
        pr_title = pr["title"]
        pr_body = pr["body"] or ""
        base_branch = pr["base"]["ref"]
        base_sha = pr["base"]["sha"]

        print(f"  Title: {pr_title}")
        print(f"  Base: {base_branch} ({base_sha[:7]})")

        new_repo_name = self.generate_repo_name(repo, pr_number, ai_tool_name, config_prefix)
        if self.repo_exists(new_repo_name):
            raise Exception(
                f"Repository {self.org}/{new_repo_name} already exists. Delete it first."
            )

        pr_branch_name = f"pr-{pr_number}"

        with tempfile.TemporaryDirectory() as tmpdir:
            clone_url = f"https://github.com/{owner}/{repo}.git"

            # Clone
            print(f"\nCloning {owner}/{repo}...")
            result = subprocess.run(
                ["git", "clone", clone_url, tmpdir], capture_output=True, text=True
            )
            if result.returncode != 0:
                raise Exception(f"Clone failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")

            # Fetch PR
            print(f"Fetching PR #{pr_number}...")
            result = self.run_git(
                tmpdir, "fetch", "origin", f"pull/{pr_number}/head:pr-head"
            )
            if result.returncode != 0:
                raise Exception(f"Fetch failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")

            # Checkout base SHA as the base branch
            print("Setting up base branch...")
            self.run_git(tmpdir, "checkout", base_sha)
            self.run_git(tmpdir, "checkout", "-b", base_branch + "-forked")

            # Checkout PR head as the PR branch
            print("Setting up PR branch...")
            self.run_git(tmpdir, "checkout", "pr-head")
            self.run_git(tmpdir, "checkout", "-b", pr_branch_name)

            # Create remote repo and disable actions
            print(f"\nCreating repository {self.org}/{new_repo_name}...")
            self.create_repo(new_repo_name)
            print("Disabling GitHub Actions...")
            self.disable_actions(new_repo_name)
            time.sleep(2)

            # Add remote and push
            push_url = f"https://x-access-token:{self.token}@github.com/{self.org}/{new_repo_name}.git"
            self.run_git(tmpdir, "remote", "add", "target", push_url)

            print(f"Pushing {base_branch}...")
            result = self.run_git(
                tmpdir, "push", "target", f"{base_branch}-forked:{base_branch}"
            )
            if result.returncode != 0:
                raise Exception(f"Push base failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")

            print(f"Pushing {pr_branch_name}...")
            result = self.run_git(tmpdir, "push", "target", pr_branch_name)
            if result.returncode != 0:
                raise Exception(f"Push PR branch failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")

        # Make repo public now that all pushes are done
        print("Making repository public...")
        self.make_repo_public(new_repo_name)

        # Create PR
        print("Creating PR...")
        new_pr = self.create_pull_request(
            repo=new_repo_name,
            title=pr_title,
            body=pr_body,
            head=pr_branch_name,
            base=base_branch,
        )

        print("\n" + "=" * 60)
        print("SUCCESS!")
        print(f"New PR: {new_pr['html_url']}")
        print("=" * 60)

        return {"new_pr_url": new_pr["html_url"]}


def _load_pr_urls_from_file(path: str) -> list[str]:
    """Load PR URLs from a golden comments JSON file.

    The expected format is a JSON array where each element is an object
    containing at least a `url` field pointing to a GitHub PR, e.g.:

    [
      {"pr_title": "...", "url": "https://github.com/org/repo/pull/123", "comments": [...]},
      ...
    ]

    Args:
        path: Filesystem path to the JSON file.

    Returns:
        A list of PR URL strings.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    urls: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                url = item.get("url") or item.get("pr_url")
                if isinstance(url, str) and url:
                    urls.append(url)
    return urls


def main():
    """CLI entrypoint: process a single PR or a batch file."""
    load_dotenv(".env", verbose=True)

    parser = argparse.ArgumentParser(description="Clone PR(s) to your org for AI review")
    parser.add_argument("pr_url", nargs="?", help="GitHub PR URL (for single run)")
    parser.add_argument("--file", help="Path to golden comments JSON to batch process")
    parser.add_argument("--org", required=True, help="Target organization")
    parser.add_argument("--name", required=True, help="AI tool name for repo naming")
    parser.add_argument(
        "--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub token"
    )
    args = parser.parse_args()

    if not args.token:
        print("Error: Set GITHUB_TOKEN or use --token")
        sys.exit(1)

    if not args.pr_url and not args.file:
        print("Error: provide a PR URL or --file path", file=sys.stderr)
        sys.exit(1)

    try:
        forker = GitHubPRForker(args.token, args.org)

        if args.file:
            urls = _load_pr_urls_from_file(args.file)
            if not urls:
                print("No PR URLs found in file.", file=sys.stderr)
                sys.exit(1)

            # Extract config prefix from filename (e.g., "cal_dot_com.json" -> "cal_dot_com")
            config_prefix = os.path.splitext(os.path.basename(args.file))[0]

            total = len(urls)
            failures = 0
            for idx, url in enumerate(urls, start=1):
                bar_width = 30
                filled = int((idx - 1) / total * bar_width)
                bar = "#" * filled + "-" * (bar_width - filled)
                print(f"[{bar}] {idx-1}/{total} completed", end="\r", flush=True)

                print(f"\n--- [{idx}/{total}] Processing: {url}")
                try:
                    forker.process_pr(url, args.name, config_prefix)
                except Exception as exc:
                    failures += 1
                    print(f"Error processing {url}: {exc}", file=sys.stderr)

            # Final bar update
            bar = "#" * 30
            print(f"[{bar}] {total}/{total} completed")
            if failures:
                print(f"Completed with {failures} failure(s).", file=sys.stderr)
        else:
            forker.process_pr(args.pr_url, args.name)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

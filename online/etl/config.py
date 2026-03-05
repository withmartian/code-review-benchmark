"""Configuration dataclass for the PR review dataset builder."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import os
import uuid

from dotenv import load_dotenv

load_dotenv(override=True)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _parse_token_list(val: str) -> list[str]:
    """Parse a comma-separated token list or a file with one token per line."""
    if not val:
        return []
    if os.path.isfile(val):
        with open(val) as f:
            return [line.strip() for line in f if line.strip()]
    return [t.strip() for t in val.split(",") if t.strip()]


@dataclass
class Config:
    """Legacy config — used by the original filesystem-based pipeline (main.py)."""

    target_user: str
    gcp_project: str
    github_token: str
    start_date: str  # YYYY-MM-DD
    end_date: str  # YYYY-MM-DD
    output_dir: str  # base output directory, default "output"
    phase: str  # "all", "bq-extract", "gh-enrich", "assemble"
    max_prs: int | None
    bq_dry_run: bool
    min_stars: int
    min_pr_number: int
    verbose: bool
    force_refetch: bool

    @property
    def user_dir(self) -> str:
        """Base directory for this user's data: {output_dir}/{target_user}/"""
        return os.path.join(self.output_dir, self.target_user)

    @property
    def target_prs_path(self) -> str:
        """Path to the target PRs list (also serves as 01_find_prs result)."""
        return os.path.join(self.user_dir, "01_find_prs.json")

    def bq_suffix_start(self) -> str:
        """Convert start_date (YYYY-MM-DD) to BQ table suffix (YYMMDD)."""
        parts = self.start_date.split("-")
        return f"{parts[0][2:]}{parts[1]}{parts[2]}"

    def bq_suffix_end(self) -> str:
        """Convert end_date (YYYY-MM-DD) to BQ table suffix (YYMMDD)."""
        parts = self.end_date.split("-")
        return f"{parts[0][2:]}{parts[1]}{parts[2]}"


DEFAULT_CHATBOT_USERNAMES = [
    "augmentcode[bot]",
    "baz-reviewer[bot]",
    "chatgpt-codex-connector[bot]",
    "claude[bot]",
    "coderabbitai[bot]",
    "Copilot",
    "cursor[bot]",
    "entelligence-ai-pr-reviews[bot]",
    "factory-droid[bot]",
    "gemini-code-assist[bot]",
    "graphite-app[bot]",
    "greptile-apps[bot]",
    "kiloconnect[bot]",
    "propel-code-bot[bot]",
    "qodo-code-review[bot]",
    "devin-ai-integration[bot]",
]


@dataclass
class DBConfig:
    """Config for the new DB-backed pipeline."""

    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///pr_review.db"))
    github_token: str = field(default_factory=lambda: _env("GITHUB_TOKEN"))
    github_tokens: list[str] = field(default_factory=lambda: _parse_token_list(_env("GITHUB_TOKENS")))
    gcp_project: str = field(default_factory=lambda: _env("GCP_PROJECT"))
    martian_base_url: str = field(default_factory=lambda: _env("MARTIAN_BASE_URL"))
    martian_api_key: str = field(default_factory=lambda: _env("MARTIAN_API_KEY"))
    martian_model_name: str = field(default_factory=lambda: _env("MARTIAN_MODEL_NAME"))
    worker_id: str = field(default_factory=lambda: _env("WORKER_ID", f"worker-{uuid.uuid4().hex[:8]}"))
    lock_timeout_minutes: int = field(default_factory=lambda: int(_env("LOCK_TIMEOUT_MINUTES", "30")))
    max_pr_commits: int = field(default_factory=lambda: int(_env("MAX_PR_COMMITS", "50")))
    max_pr_changed_lines: int = field(default_factory=lambda: int(_env("MAX_PR_CHANGED_LINES", "2000")))
    f_beta: float = field(default_factory=lambda: float(_env("F_BETA", "1.0")))
    verbose: bool = False

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

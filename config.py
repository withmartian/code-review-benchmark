"""Configuration dataclass for the PR review dataset builder."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
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

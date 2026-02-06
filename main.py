"""CLI entrypoint for the PR review dataset builder."""

from __future__ import annotations

import argparse
import logging
import sys
import time

from config import Config

logger = logging.getLogger("pr_review_dataset")


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="Build a dataset of PR review activity from GitHub Archive and GitHub API.",
    )
    parser.add_argument("--user", required=True, help="GitHub username to find review activity for")
    parser.add_argument("--gcp-project", required=True, help="Google Cloud project ID for BigQuery billing")
    parser.add_argument("--github-token", default="", help="GitHub personal access token (required for gh-enrich phase)")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--output-dir", default="output", help="Base output directory (default: output)")
    parser.add_argument("--phase", default="all", choices=["all", "bq-extract", "gh-enrich", "assemble"],
                        help="Run only one phase")
    parser.add_argument("--max-prs", type=int, default=None, help="Limit to first N PRs (for testing)")
    parser.add_argument("--min-stars", type=int, default=0, help="Minimum repo stars filter")
    parser.add_argument("--min-pr-number", type=int, default=0, help="Only include PRs with number >= this value (proxy for repo activity)")
    parser.add_argument("--bq-dry-run", action="store_true", help="Only estimate BQ query cost, don't execute")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--force-refetch", action="store_true", help="Delete cached event files and re-fetch from BigQuery")

    args = parser.parse_args()

    return Config(
        target_user=args.user,
        gcp_project=args.gcp_project,
        github_token=args.github_token,
        start_date=args.start,
        end_date=args.end,
        output_dir=args.output_dir,
        phase=args.phase,
        max_prs=args.max_prs,
        bq_dry_run=args.bq_dry_run,
        min_stars=args.min_stars,
        min_pr_number=args.min_pr_number,
        verbose=args.verbose,
        force_refetch=args.force_refetch,
    )


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    config = parse_args()
    setup_logging(config.verbose)

    logger.info(f"PR Review Dataset Builder")
    logger.info(f"  Target user: {config.target_user}")
    logger.info(f"  Date range: {config.start_date} to {config.end_date}")
    logger.info(f"  Output: {config.user_dir}/")
    logger.info(f"  Phase: {config.phase}")
    if config.max_prs:
        logger.info(f"  Max PRs: {config.max_prs}")
    if config.min_stars > 0:
        logger.info(f"  Min stars: {config.min_stars}")
    if config.min_pr_number > 0:
        logger.info(f"  Min PR number: {config.min_pr_number}")

    start_time = time.time()
    total_prs = 0
    total_api_calls = 0
    assembled_count = 0

    # Phase 1: BigQuery extraction
    if config.phase in ("all", "bq-extract"):
        logger.info("=" * 60)
        logger.info("PHASE 1: BigQuery Extraction")
        logger.info("=" * 60)
        from bq_extract import run_bq_extract
        prs = run_bq_extract(config)
        total_prs = len(prs)

    # Phase 2: GitHub API enrichment
    if config.phase in ("all", "gh-enrich"):
        if not config.github_token:
            logger.error("--github-token is required for gh-enrich phase")
            sys.exit(1)
        logger.info("=" * 60)
        logger.info("PHASE 2: GitHub API Enrichment")
        logger.info("=" * 60)
        from gh_enrich import run_gh_enrich
        total_api_calls = run_gh_enrich(config)

    # Phase 3: Assembly
    if config.phase in ("all", "assemble"):
        logger.info("=" * 60)
        logger.info("PHASE 3: Assembly")
        logger.info("=" * 60)
        from assemble import run_assemble
        assembled_count = run_assemble(config)

    # Summary
    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info("=" * 60)
    if total_prs:
        logger.info(f"  Target PRs found: {total_prs}")
    if total_api_calls:
        logger.info(f"  GitHub API calls: {total_api_calls}")
    if assembled_count:
        logger.info(f"  PRs assembled: {assembled_count}")
        logger.info(f"  Output: {config.user_dir}/")
    logger.info(f"  Elapsed: {elapsed:.1f}s")


if __name__ == "__main__":
    main()

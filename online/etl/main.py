"""CLI entrypoint for the PR review dataset builder.

Supports two modes:
1. Legacy filesystem mode (original --user/--start/--end pipeline)
2. New DB-backed mode via subcommands: discover, enrich, analyze, import, dashboard
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import UTC
import logging
import sys
import time

from config import DEFAULT_CHATBOT_USERNAMES
from config import Config
from config import DBConfig
from config import _parse_token_list

logger = logging.getLogger("pr_review_dataset")


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# -- Legacy filesystem mode ----------------------------------------------------


def parse_legacy_args(args: list[str]) -> Config:
    parser = argparse.ArgumentParser(
        description="Build a dataset of PR review activity from GitHub Archive and GitHub API.",
    )
    parser.add_argument("--user", required=True, help="GitHub username to find review activity for")
    parser.add_argument("--gcp-project", required=True, help="Google Cloud project ID for BigQuery billing")
    parser.add_argument(
        "--github-token", default="", help="GitHub personal access token (required for gh-enrich phase)"
    )
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--output-dir", default="output", help="Base output directory (default: output)")
    parser.add_argument(
        "--phase", default="all", choices=["all", "bq-extract", "gh-enrich", "assemble"], help="Run only one phase"
    )
    parser.add_argument("--max-prs", type=int, default=None, help="Limit to first N PRs (for testing)")
    parser.add_argument("--min-stars", type=int, default=0, help="Minimum repo stars filter")
    parser.add_argument("--min-pr-number", type=int, default=0)
    parser.add_argument("--bq-dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--force-refetch", action="store_true")
    parsed = parser.parse_args(args)
    return Config(
        target_user=parsed.user,
        gcp_project=parsed.gcp_project,
        github_token=parsed.github_token,
        start_date=parsed.start,
        end_date=parsed.end,
        output_dir=parsed.output_dir,
        phase=parsed.phase,
        max_prs=parsed.max_prs,
        bq_dry_run=parsed.bq_dry_run,
        min_stars=parsed.min_stars,
        min_pr_number=parsed.min_pr_number,
        verbose=parsed.verbose,
        force_refetch=parsed.force_refetch,
    )


def run_legacy(config: Config) -> None:
    setup_logging(config.verbose)
    logger.info("PR Review Dataset Builder (legacy filesystem mode)")
    logger.info(f"  Target user: {config.target_user}")
    logger.info(f"  Date range: {config.start_date} to {config.end_date}")
    logger.info(f"  Output: {config.user_dir}/")
    logger.info(f"  Phase: {config.phase}")
    if config.max_prs:
        logger.info(f"  Max PRs: {config.max_prs}")

    start_time = time.time()
    total_prs = 0
    total_api_calls = 0
    assembled_count = 0

    if config.phase in ("all", "bq-extract"):
        logger.info("=" * 60)
        logger.info("PHASE 1: BigQuery Extraction")
        logger.info("=" * 60)
        from bq_extract import run_bq_extract

        prs = run_bq_extract(config)
        total_prs = len(prs)

    if config.phase in ("all", "gh-enrich"):
        if not config.github_token:
            logger.error("--github-token is required for gh-enrich phase")
            sys.exit(1)
        logger.info("=" * 60)
        logger.info("PHASE 2: GitHub API Enrichment")
        logger.info("=" * 60)
        from gh_enrich import run_gh_enrich

        total_api_calls = run_gh_enrich(config)

    if config.phase in ("all", "assemble"):
        logger.info("=" * 60)
        logger.info("PHASE 3: Assembly")
        logger.info("=" * 60)
        from assemble import run_assemble

        assembled_count = run_assemble(config)

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


# -- New DB-backed subcommands ------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PR Review Dataset Builder",
    )
    sub = parser.add_subparsers(dest="command")

    # Legacy mode (no subcommand, uses --user)
    # Handled by checking if --user is in sys.argv

    # discover
    p_disc = sub.add_parser("discover", help="Discover PRs into DB (Search API or BigQuery)")
    p_disc.add_argument("--chatbot", help="GitHub username of the chatbot")
    p_disc.add_argument(
        "--all", action="store_true", dest="all_chatbots", help="Discover for all registered chatbots"
    )
    p_disc.add_argument(
        "--source",
        choices=["search-api", "bq"],
        default="search-api",
        help="Discovery source: 'search-api' (default, GitHub Search API) or 'bq' (BigQuery/GH Archive)",
    )
    p_disc.add_argument("--days-back", type=int, default=7)
    p_disc.add_argument("--start-date", help="YYYY-MM-DD")
    p_disc.add_argument("--end-date", help="YYYY-MM-DD")
    p_disc.add_argument("--min-pr-number", type=int, default=0)
    p_disc.add_argument("--max-prs-per-day", type=int, default=500, help="Random sample cap per day (default: 500)")
    p_disc.add_argument("--display-name", help="Display name for chatbot")
    p_disc.add_argument("--database-url", help="Override DATABASE_URL")
    p_disc.add_argument("--gcp-project", help="Override GCP_PROJECT")
    p_disc.add_argument("--verbose", action="store_true")

    # enrich
    p_enr = sub.add_parser("enrich", help="Enrich pending PRs via GitHub API")
    p_enr.add_argument("--chatbot", help="Specific chatbot, or use --all")
    p_enr.add_argument("--all", action="store_true", dest="all_chatbots", help="Enrich for all registered chatbots")
    p_enr.add_argument("--one-shot", action="store_true")
    p_enr.add_argument("--max-prs", type=int)
    p_enr.add_argument("--max-pr-commits", type=int, help="Skip PRs with more commits than this (default: 50)")
    p_enr.add_argument(
        "--max-pr-changed-lines", type=int, help="Skip PRs with more changed lines than this (default: 2000)"
    )
    p_enr.add_argument("--database-url")
    p_enr.add_argument("--github-token")
    p_enr.add_argument("--github-tokens", help="Comma-separated tokens or path to file (one per line)")
    p_enr.add_argument("--verbose", action="store_true")

    # analyze
    p_ana = sub.add_parser("analyze", help="Run LLM analysis on assembled PRs")
    p_ana.add_argument("--chatbot", help="Specific chatbot, or use --all")
    p_ana.add_argument("--all", action="store_true", dest="all_chatbots")
    p_ana.add_argument("--limit", type=int, default=100)
    p_ana.add_argument("--since", help="Inclusive lower bound on bot_reviewed_at (e.g. '7d', '2026-02-05')")
    p_ana.add_argument(
        "--until",
        help=(
            "Exclusive upper bound on bot_reviewed_at (e.g. '2d', '2026-04-19'). "
            "With --since 2026-04-18 --until 2026-04-19 you get just 2026-04-18."
        ),
    )
    p_ana.add_argument(
        "--sort",
        choices=["reviewed", "sweep"],
        default="reviewed",
        help="Sort order: 'reviewed' (default, bot_reviewed_at DESC) or 'sweep' (assembled_at DESC, catches late-merged PRs).",
    )
    p_ana.add_argument(
        "--max-per-day", type=int, default=None,
        help="Cap PRs per bot_reviewed_at date (random sample within each day). Requires --since.",
    )
    p_ana.add_argument("--database-url")
    p_ana.add_argument("--verbose", action="store_true")

    # import
    p_imp = sub.add_parser("import", help="Import filesystem data into DB")
    p_imp.add_argument("--output-dir", default="output")
    p_imp.add_argument("--chatbot", help="Only import specific chatbot")
    p_imp.add_argument("--database-url")
    p_imp.add_argument("--verbose", action="store_true")

    # label
    p_lbl = sub.add_parser("label", help="Generate labels for analyzed PRs")
    p_lbl.add_argument("--chatbot", help="Specific chatbot, or use --all")
    p_lbl.add_argument("--all", action="store_true", dest="all_chatbots")
    p_lbl.add_argument("--limit", type=int, default=100)
    p_lbl.add_argument("--since", help="Inclusive lower bound on bot_reviewed_at (e.g. '7d', '2026-02-05')")
    p_lbl.add_argument(
        "--until",
        help="Exclusive upper bound on bot_reviewed_at (e.g. '2d', '2026-04-19')",
    )
    p_lbl.add_argument(
        "--sort",
        choices=["reviewed", "sweep"],
        default="reviewed",
        help="Sort order: 'reviewed' (bot_reviewed_at DESC, default) or 'sweep' (analyzed_at DESC, for catching stragglers).",
    )
    p_lbl.add_argument(
        "--max-per-day", type=int, default=None,
        help="Cap PRs per bot_reviewed_at date (random sample within each day). Requires --since.",
    )
    p_lbl.add_argument("--database-url")
    p_lbl.add_argument("--verbose", action="store_true")

    # volumes
    p_vol = sub.add_parser("volumes", help="Fetch PR volume counts from BigQuery or GitHub Search API")
    p_vol.add_argument("--chatbot", help="GitHub username of the chatbot")
    p_vol.add_argument(
        "--all", action="store_true", dest="all_chatbots", help="Fetch volumes for all registered chatbots"
    )
    p_vol.add_argument(
        "--source", choices=["bq", "search-api"], default="search-api",
        help="Data source: 'bq' for BigQuery/GH Archive (legacy), 'search-api' for GitHub Search API (default)",
    )
    p_vol.add_argument(
        "--weekly", action="store_true",
        help="(search-api only) Query in weekly chunks instead of daily. ~7x fewer API calls, less accurate per-day.",
    )
    p_vol.add_argument("--days-back", type=int, default=7)
    p_vol.add_argument("--start-date", help="YYYY-MM-DD")
    p_vol.add_argument("--end-date", help="YYYY-MM-DD")
    p_vol.add_argument("--database-url", help="Override DATABASE_URL")
    p_vol.add_argument("--gcp-project", help="Override GCP_PROJECT")
    p_vol.add_argument("--verbose", action="store_true")

    # backfill
    p_bf = sub.add_parser("backfill", help="Backfill computed columns (e.g. diff_lines)")
    p_bf.add_argument("--database-url")
    p_bf.add_argument("--batch-size", type=int, default=5000)
    p_bf.add_argument("--verbose", action="store_true")

    # backfill-pr-author
    p_bfa = sub.add_parser("backfill-pr-author", help="Backfill pr_author from BQ events, commits, and optionally GitHub API")
    p_bfa.add_argument("--database-url")
    p_bfa.add_argument("--limit", type=int, default=None, help="Limit PRs to process (for testing)")
    p_bfa.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    p_bfa.add_argument("--use-api", action="store_true", help="Also fetch from GitHub API for unresolved PRs")
    p_bfa.add_argument("--use-commits", action="store_true", help="Also try git commit author (low-confidence, off by default)")
    p_bfa.add_argument("--verbose", action="store_true")

    # backfill-metadata
    p_bfm = sub.add_parser("backfill-metadata", help="Backfill pr_merged and repo_id from pr_api_raw and bq_events")
    p_bfm.add_argument("--database-url")
    p_bfm.add_argument("--limit", type=int, default=None, help="Limit PRs to process")
    p_bfm.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    p_bfm.add_argument("--verbose", action="store_true")

    # backfill-api-raw
    p_bar = sub.add_parser("backfill-api-raw", help="Fetch pr_api_raw from GitHub API for PRs missing it, sets pr_merged + repo_id")
    p_bar.add_argument("--database-url")
    p_bar.add_argument("--limit", type=int, default=None, help="Limit PRs to process")
    p_bar.add_argument("--status-filter", default="analyzed", help="Only fetch for PRs with this status (default: analyzed)")
    p_bar.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    p_bar.add_argument("--verbose", action="store_true")

    # backfill-engagement
    p_bfe = sub.add_parser(
        "backfill-engagement",
        help="Compute engagement_signals for assembled PRs (missing by default, or all with --force)",
    )
    p_bfe.add_argument("--database-url")
    p_bfe.add_argument("--chatbot", help="Only this github_username (e.g. Copilot)")
    p_bfe.add_argument(
        "--force",
        action="store_true",
        help="Recompute even when engagement_signals is already set (needed after actor-alias fixes)",
    )
    p_bfe.add_argument("--since", help="Inclusive lower bound on bot_reviewed_at (e.g. '60d', '2026-06-20')")
    p_bfe.add_argument("--until", help="Exclusive upper bound on bot_reviewed_at (e.g. '2026-08-21')")
    p_bfe.add_argument("--limit", type=int, default=None, help="Limit PRs to process")
    p_bfe.add_argument("--batch-size", type=int, default=5000)
    p_bfe.add_argument("--status-filter", default="analyzed", help="Only process PRs with this status (default: analyzed)")
    p_bfe.add_argument("--dry-run", action="store_true")
    p_bfe.add_argument("--verbose", action="store_true")

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Launch Streamlit dashboard")
    p_dash.add_argument("--port", type=int, default=8501)

    return parser


async def cmd_discover(args: argparse.Namespace) -> None:
    from datetime import datetime
    from datetime import timedelta

    from db.connection import DBAdapter
    from db.repository import PRRepository
    from db.schema import create_tables
    from pipeline.discover import discover_prs
    from pipeline.discover import discover_prs_batch
    from pipeline.discover import discover_prs_search_api
    from pipeline.discover import discover_prs_search_api_batch

    cfg = DBConfig(verbose=args.verbose)
    if args.database_url:
        cfg.database_url = args.database_url
    if args.gcp_project:
        cfg.gcp_project = args.gcp_project

    end_date = args.end_date or datetime.now(UTC).strftime("%Y-%m-%d")
    start_date = args.start_date or (datetime.now(UTC) - timedelta(days=args.days_back)).strftime("%Y-%m-%d")

    if not args.chatbot and not args.all_chatbots:
        logger.error("Specify --chatbot or --all")
        return

    use_search_api = args.source == "search-api"
    logger.info(f"Discovery source: {'Search API' if use_search_api else 'BigQuery'}")

    db = DBAdapter(cfg.database_url)
    await db.connect()
    try:
        await create_tables(db)
        if args.all_chatbots:
            repo = PRRepository(db)
            chatbots = await repo.get_all_chatbots()
            db_usernames = {bot["github_username"] for bot in chatbots}
            usernames = sorted(db_usernames | set(DEFAULT_CHATBOT_USERNAMES))
            logger.info(f"Batch discovering PRs for {len(usernames)} chatbots")
            if use_search_api:
                await discover_prs_search_api_batch(
                    cfg,
                    db,
                    usernames,
                    start_date,
                    end_date,
                    max_prs_per_day=args.max_prs_per_day,
                )
            else:
                await discover_prs_batch(
                    cfg,
                    db,
                    usernames,
                    start_date,
                    end_date,
                    min_pr_number=args.min_pr_number,
                    max_prs_per_day=args.max_prs_per_day,
                )
        else:
            if use_search_api:
                await discover_prs_search_api(
                    cfg,
                    db,
                    args.chatbot,
                    start_date,
                    end_date,
                    max_prs_per_day=args.max_prs_per_day,
                    display_name=args.display_name,
                )
            else:
                await discover_prs(
                    cfg,
                    db,
                    args.chatbot,
                    start_date,
                    end_date,
                    min_pr_number=args.min_pr_number,
                    max_prs_per_day=args.max_prs_per_day,
                    display_name=args.display_name,
                )
    finally:
        await db.close()


async def cmd_enrich(args: argparse.Namespace) -> None:
    from db.connection import DBAdapter
    from db.repository import PRRepository
    from db.schema import create_tables
    from pipeline.enrich import enrich_loop

    cfg = DBConfig(verbose=args.verbose)
    if args.database_url:
        cfg.database_url = args.database_url
    if args.github_token:
        cfg.github_token = args.github_token
    if args.max_pr_commits is not None:
        cfg.max_pr_commits = args.max_pr_commits
    if args.max_pr_changed_lines is not None:
        cfg.max_pr_changed_lines = args.max_pr_changed_lines

    # Build token list: CLI --github-tokens > env GITHUB_TOKENS > single token fallback
    tokens: list[str] = []
    if args.github_tokens:
        tokens = _parse_token_list(args.github_tokens)
    elif cfg.github_tokens:
        tokens = cfg.github_tokens
    if not tokens and cfg.github_token:
        tokens = [cfg.github_token]
    cfg.github_tokens = tokens

    if not cfg.github_tokens:
        logger.error("GITHUB_TOKEN or GITHUB_TOKENS required")
        return

    if not args.chatbot and not args.all_chatbots:
        logger.error("Specify --chatbot or --all")
        return

    db = DBAdapter(cfg.database_url)
    await db.connect()
    try:
        await create_tables(db)
        repo = PRRepository(db)

        if args.all_chatbots:
            chatbots = await repo.get_all_chatbots()
            if not chatbots:
                logger.error("No chatbots found. Run discover first.")
                return
            logger.info(f"Enriching PRs for {len(chatbots)} chatbot(s)")
        else:
            bot = await repo.get_chatbot(args.chatbot)
            if not bot:
                logger.error(f"Chatbot '{args.chatbot}' not found. Run discover first.")
                return
            chatbots = [bot]

        if len(chatbots) > 1 and not args.one_shot:
            # Daemon mode with multiple chatbots: round-robin one-shot passes
            total = 0
            while True:
                any_work = False
                for chatbot in chatbots:
                    enriched = await enrich_loop(
                        cfg,
                        db,
                        chatbot["id"],
                        chatbot_username=chatbot["github_username"],
                        max_prs=args.max_prs,
                        one_shot=True,
                    )
                    if enriched > 0:
                        any_work = True
                        total += enriched
                    if args.max_prs and total >= args.max_prs:
                        logger.info(f"Reached max_prs limit ({args.max_prs})")
                        return
                if not any_work:
                    logger.info("No pending PRs for any chatbot, sleeping 5 minutes...")
                    await asyncio.sleep(300)
        else:
            for chatbot in chatbots:
                logger.info(f"--- Enriching for {chatbot['github_username']} ---")
                enriched = await enrich_loop(
                    cfg,
                    db,
                    chatbot["id"],
                    chatbot_username=chatbot["github_username"],
                    max_prs=args.max_prs,
                    one_shot=args.one_shot,
                )
                logger.info(f"Enriched {enriched} PRs")
    finally:
        await db.close()


def _parse_time_bound(value: str | None) -> str | None:
    """Parse a CLI time bound: relative ("7d") or absolute ("2026-02-05") -> ISO timestamp.

    Returns None when value is falsy. Relative values are anchored to "now" (UTC).
    Bare dates ("YYYY-MM-DD") are normalized to midnight UTC so asyncpg can bind
    them to a timestamptz column — without this the bare-date form is forwarded
    as a raw string and asyncpg raises DataError.
    """
    if not value:
        return None
    from datetime import datetime
    from datetime import timedelta
    import re

    m = re.match(r"^(\d+)d$", value)
    if m:
        return (datetime.now(UTC) - timedelta(days=int(m.group(1)))).isoformat()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return f"{value}T00:00:00+00:00"
    return value


async def cmd_analyze(args: argparse.Namespace) -> None:
    from db.connection import DBAdapter
    from db.repository import PRRepository
    from db.schema import create_tables
    from pipeline.analyze import analyze_prs

    cfg = DBConfig(verbose=args.verbose)
    if args.database_url:
        cfg.database_url = args.database_url
    if not cfg.martian_api_key:
        logger.error("MARTIAN_API_KEY required")
        return

    since = _parse_time_bound(args.since)
    until = _parse_time_bound(args.until)
    sort_by = args.sort
    max_per_day: int | None = args.max_per_day
    if max_per_day is not None and not since:
        logger.error("--max-per-day requires --since")
        return
    if max_per_day is not None and sort_by == "sweep":
        logger.error("--max-per-day is incompatible with --sort sweep")
        return
    if sort_by == "sweep":
        if since or until:
            logger.warning("--since/--until are ignored in sweep mode (sweep processes all unanalyzed PRs by assembled_at)")
        logger.info("Sweep mode: sorting by assembled_at DESC")
    else:
        if since:
            logger.info(f"Filtering PRs reviewed since {since}")
        if until:
            logger.info(f"Filtering PRs reviewed before {until} (exclusive)")
    if max_per_day is not None:
        logger.info(f"Per-day cap: {max_per_day} PRs per bot per day (--limit ignored)")

    db = DBAdapter(cfg.database_url)
    await db.connect()
    try:
        await create_tables(db)
        repo = PRRepository(db)

        if args.all_chatbots:
            chatbots = await repo.get_all_chatbots()
            for bot in chatbots:
                await analyze_prs(
                    cfg, db, bot["id"], bot["github_username"],
                    limit=args.limit, since=since, until=until,
                    sort_by=sort_by, max_per_day=max_per_day,
                )
        elif args.chatbot:
            bot = await repo.get_chatbot(args.chatbot)
            if not bot:
                logger.error(f"Chatbot '{args.chatbot}' not found.")
                return
            await analyze_prs(
                cfg, db, bot["id"], bot["github_username"],
                limit=args.limit, since=since, until=until,
                sort_by=sort_by, max_per_day=max_per_day,
                )
        else:
            logger.error("Specify --chatbot or --all")
    finally:
        await db.close()


async def cmd_label(args: argparse.Namespace) -> None:
    from db.connection import DBAdapter
    from db.repository import PRRepository
    from db.schema import create_tables
    from pipeline.label import label_prs

    cfg = DBConfig(verbose=args.verbose)
    if args.database_url:
        cfg.database_url = args.database_url
    if not cfg.martian_api_key:
        logger.error("MARTIAN_API_KEY required")
        return

    since = _parse_time_bound(args.since)
    until = _parse_time_bound(args.until)
    sort_by = args.sort
    max_per_day: int | None = args.max_per_day
    if max_per_day is not None and not since:
        logger.error("--max-per-day requires --since")
        return
    if max_per_day is not None and sort_by == "sweep":
        logger.error("--max-per-day is incompatible with --sort sweep")
        return
    if sort_by == "sweep":
        if since or until:
            logger.warning("--since/--until are ignored in sweep mode (sweep processes all unlabeled PRs by analyzed_at)")
        logger.info("Sweep mode: sorting by analyzed_at DESC")
    else:
        if since:
            logger.info(f"Filtering PRs reviewed since {since}")
        if until:
            logger.info(f"Filtering PRs reviewed before {until} (exclusive)")
    if max_per_day is not None:
        logger.info(f"Per-day cap: {max_per_day} PRs per bot per day (--limit ignored)")

    db = DBAdapter(cfg.database_url)
    await db.connect()
    try:
        await create_tables(db)
        repo = PRRepository(db)

        if args.all_chatbots:
            chatbots = await repo.get_all_chatbots()
            for bot in chatbots:
                await label_prs(
                    cfg, db, bot["id"], bot["github_username"],
                    limit=args.limit, since=since, until=until,
                    sort_by=sort_by, max_per_day=max_per_day,
                )
        elif args.chatbot:
            bot = await repo.get_chatbot(args.chatbot)
            if not bot:
                logger.error(f"Chatbot '{args.chatbot}' not found.")
                return
            await label_prs(
                cfg, db, bot["id"], bot["github_username"],
                limit=args.limit, since=since, until=until,
                sort_by=sort_by, max_per_day=max_per_day,
            )
        else:
            logger.error("Specify --chatbot or --all")
    finally:
        await db.close()


async def cmd_volumes(args: argparse.Namespace) -> None:
    from datetime import datetime
    from datetime import timedelta

    from db.connection import DBAdapter
    from db.repository import PRRepository
    from db.schema import create_tables

    cfg = DBConfig(verbose=args.verbose)
    if args.database_url:
        cfg.database_url = args.database_url
    if args.gcp_project:
        cfg.gcp_project = args.gcp_project

    end_date = args.end_date or datetime.now(UTC).strftime("%Y-%m-%d")
    start_date = args.start_date or (datetime.now(UTC) - timedelta(days=args.days_back)).strftime("%Y-%m-%d")

    if not args.chatbot and not args.all_chatbots:
        logger.error("Specify --chatbot or --all")
        return

    db = DBAdapter(cfg.database_url)
    await db.connect()
    try:
        await create_tables(db)
        if args.all_chatbots:
            repo = PRRepository(db)
            chatbots = await repo.get_all_chatbots()
            db_usernames = {bot["github_username"] for bot in chatbots}
            usernames = sorted(db_usernames | set(DEFAULT_CHATBOT_USERNAMES))
        else:
            usernames = [args.chatbot]

        source = args.source
        logger.info(f"Fetching PR volumes ({source}) for {len(usernames)} chatbot(s): {start_date} to {end_date}")

        if source == "search-api":
            from pipeline.volumes import fetch_pr_volumes_search_api
            count = await fetch_pr_volumes_search_api(
                cfg, db, usernames, start_date, end_date, weekly=args.weekly,
            )
        else:
            from pipeline.volumes import fetch_pr_volumes
            count = await fetch_pr_volumes(cfg, db, usernames, start_date, end_date)

        logger.info(f"Done: {count} volume rows upserted")
    finally:
        await db.close()


async def cmd_backfill(args: argparse.Namespace) -> None:
    from db.connection import DBAdapter
    from db.repository import PRRepository
    from db.schema import create_tables

    cfg = DBConfig(verbose=args.verbose)
    if args.database_url:
        cfg.database_url = args.database_url

    db = DBAdapter(cfg.database_url)
    await db.connect()
    try:
        await create_tables(db)
        repo = PRRepository(db)
        remaining = await repo.count_missing_diff_lines()
        if remaining == 0:
            logger.info("Nothing to backfill — all PRs already have diff_lines")
            return
        logger.info(f"Backfilling diff_lines for {remaining} PRs")
        total = 0
        while True:
            updated = await repo.backfill_diff_lines(batch_size=args.batch_size)
            total += updated
            if updated > 0:
                pct = min(100, total * 100 // remaining)
                bar = "=" * (pct // 2) + " " * (50 - pct // 2)
                print(f"\r  [{bar}] {pct}% ({total}/{remaining})", end="", flush=True)
            if updated < args.batch_size:
                break
        print()  # newline after progress bar
        logger.info(f"Backfill complete: {total} PRs updated")
    finally:
        await db.close()


async def cmd_backfill_pr_author(args: argparse.Namespace) -> None:
    from db.connection import DBAdapter
    from db.schema import create_tables
    from pipeline.backfill_pr_author import backfill_pr_author

    cfg = DBConfig(verbose=args.verbose)
    if args.database_url:
        cfg.database_url = args.database_url

    db = DBAdapter(cfg.database_url)
    await db.connect()
    try:
        await create_tables(db)
        stats = await backfill_pr_author(
            cfg,
            db,
            limit=args.limit,
            dry_run=args.dry_run,
            use_api=args.use_api,
            use_commits=args.use_commits,
        )

        mode = "DRY RUN" if args.dry_run else "DONE"
        logger.info(
            f"{mode} — backfill: total_missing={stats['total_missing']} "
            f"from_bq={stats['updated_from_bq']} from_commits={stats['updated_from_commits']} "
            f"from_api={stats['updated_from_api']} still_missing={stats['still_missing']} "
            f"roles_updated={stats['roles_updated']}"
        )
    finally:
        await db.close()


async def cmd_backfill_metadata(args: argparse.Namespace) -> None:
    """Backfill pr_merged from pr_api_raw, repo_id from pr_api_raw, and fix assembled.pr_merged."""
    import json as json_mod

    from db.connection import DBAdapter
    from db.schema import create_tables

    cfg = DBConfig(verbose=args.verbose)
    if args.database_url:
        cfg.database_url = args.database_url

    db = DBAdapter(cfg.database_url)
    await db.connect()
    try:
        await create_tables(db)
        limit_clause = f"LIMIT {args.limit}" if args.limit else ""
        stats = {"merged_set": 0, "merged_corrected": 0, "assembled_fixed": 0, "repo_id_set": 0}

        # Phase 1a: backfill pr_merged from pr_api_raw (NULL rows)
        rows = await db.fetchall(f"""
            SELECT id, repo_name, pr_number, pr_merged, pr_api_raw
            FROM prs
            WHERE pr_api_raw IS NOT NULL AND pr_merged IS NULL
            ORDER BY id {limit_clause}
        """)
        logger.info(f"Phase 1a: {len(rows)} PRs with pr_api_raw but pr_merged IS NULL")
        for i, row in enumerate(rows):
            api_data = json_mod.loads(row["pr_api_raw"])
            api_merged = api_data.get("merged")
            if api_merged is not None:
                if args.dry_run:
                    logger.info(f"  [DRY] {row['repo_name']}#{row['pr_number']} (id={row['id']}): pr_merged=NULL -> {api_merged}")
                else:
                    await db.execute(*db._translate_params(
                        "UPDATE prs SET pr_merged = $1 WHERE id = $2",
                        (api_merged, row["id"]),
                    ))
                stats["merged_set"] += 1
            if (i + 1) % 10_000 == 0:
                logger.info(f"  Phase 1a progress: {i + 1}/{len(rows)}")

        # Phase 1b: fix pr_merged=False that should be True (API is authoritative)
        rows = await db.fetchall(f"""
            SELECT id, repo_name, pr_number, pr_api_raw
            FROM prs
            WHERE pr_api_raw IS NOT NULL AND pr_merged = false
            ORDER BY id {limit_clause}
        """)
        logger.info(f"Phase 1b: {len(rows)} PRs with pr_merged=False, checking against API")
        for i, row in enumerate(rows):
            api_data = json_mod.loads(row["pr_api_raw"])
            if api_data.get("merged") is True:
                if args.dry_run:
                    logger.info(f"  [DRY] {row['repo_name']}#{row['pr_number']} (id={row['id']}): pr_merged=False -> True")
                else:
                    await db.execute(*db._translate_params(
                        "UPDATE prs SET pr_merged = $1 WHERE id = $2",
                        (True, row["id"]),
                    ))
                stats["merged_corrected"] += 1
            if (i + 1) % 10_000 == 0:
                logger.info(f"  Phase 1b progress: {i + 1}/{len(rows)}")

        # Phase 2: fix assembled.pr_merged to match prs.pr_merged (patch assembled JSON)
        # Process in batches to avoid OOM on large datasets
        batch_size = 5_000
        phase2_total = 0
        last_id = 0
        count_row = await db.fetchone(
            "SELECT COUNT(*) as cnt FROM prs WHERE pr_merged IS NOT NULL AND assembled IS NOT NULL"
        )
        logger.info(f"Phase 2: ~{count_row['cnt']} assembled PRs to check assembled.pr_merged consistency")
        while True:
            batch_limit = f"LIMIT {min(batch_size, args.limit - phase2_total)}" if args.limit else f"LIMIT {batch_size}"
            rows = await db.fetchall(f"""
                SELECT id, repo_name, pr_number, pr_merged, assembled
                FROM prs
                WHERE pr_merged IS NOT NULL AND assembled IS NOT NULL AND id > {last_id}
                ORDER BY id
                {batch_limit}
            """)
            if not rows:
                break
            for row in rows:
                assembled = json_mod.loads(row["assembled"])
                if assembled.get("pr_merged") != row["pr_merged"]:
                    assembled["pr_merged"] = row["pr_merged"]
                    if args.dry_run:
                        logger.info(
                            f"  [DRY] {row['repo_name']}#{row['pr_number']} (id={row['id']}): "
                            f"assembled.pr_merged -> {row['pr_merged']}"
                        )
                    else:
                        await db.execute(*db._translate_params(
                            "UPDATE prs SET assembled = $1 WHERE id = $2",
                            (json_mod.dumps(assembled), row["id"]),
                        ))
                    stats["assembled_fixed"] += 1
                last_id = row["id"]
            phase2_total += len(rows)
            logger.info(f"  Phase 2 progress: {phase2_total}/{count_row['cnt']}")
            if args.limit and phase2_total >= args.limit:
                break

        # Phase 3: backfill repo_id from pr_api_raw (base.repo.id)
        phase3_total = 0
        last_id = 0
        count_row = await db.fetchone(
            "SELECT COUNT(*) as cnt FROM prs WHERE repo_id IS NULL AND pr_api_raw IS NOT NULL"
        )
        logger.info(f"Phase 3: {count_row['cnt']} PRs with pr_api_raw but no repo_id")
        while True:
            batch_limit = f"LIMIT {min(batch_size, args.limit - phase3_total)}" if args.limit else f"LIMIT {batch_size}"
            rows = await db.fetchall(f"""
                SELECT id, repo_name, pr_number, pr_api_raw
                FROM prs
                WHERE repo_id IS NULL AND pr_api_raw IS NOT NULL AND id > {last_id}
                ORDER BY id
                {batch_limit}
            """)
            if not rows:
                break
            for row in rows:
                api_data = json_mod.loads(row["pr_api_raw"])
                repo_obj = api_data.get("base", {}).get("repo", {})
                repo_id = repo_obj.get("id")
                if repo_id:
                    if args.dry_run:
                        logger.info(f"  [DRY] {row['repo_name']}#{row['pr_number']} (id={row['id']}): repo_id={repo_id}")
                    else:
                        await db.execute(*db._translate_params(
                            "UPDATE prs SET repo_id = $1 WHERE id = $2",
                            (repo_id, row["id"]),
                        ))
                    stats["repo_id_set"] += 1
                last_id = row["id"]
            phase3_total += len(rows)
            logger.info(f"  Phase 3 progress: {phase3_total}/{count_row['cnt']}")
            if args.limit and phase3_total >= args.limit:
                break

        mode = "DRY RUN" if args.dry_run else "DONE"
        logger.info(
            f"{mode} — metadata backfill: "
            f"merged_set={stats['merged_set']}, merged_corrected={stats['merged_corrected']}, "
            f"assembled_fixed={stats['assembled_fixed']}, repo_id_set={stats['repo_id_set']}"
        )

        # Summary of what's still missing
        still_null = await db.fetchone("SELECT COUNT(*) as cnt FROM prs WHERE pr_merged IS NULL")
        no_repo_id = await db.fetchone("SELECT COUNT(*) as cnt FROM prs WHERE repo_id IS NULL")
        logger.info(
            f"Remaining gaps: pr_merged NULL={still_null['cnt']}, repo_id NULL={no_repo_id['cnt']} "
            f"(these need API fetch or re-discover to resolve)"
        )
    finally:
        await db.close()


async def cmd_backfill_api_raw(args: argparse.Namespace) -> None:
    """Fetch pr_api_raw from GitHub API for PRs missing it. Sets pr_merged + repo_id."""
    import json as json_mod
    import time as time_mod

    from db.connection import DBAdapter
    from db.schema import create_tables
    from pipeline.enrich import RateLimitExhaustedError
    from pipeline.enrich import TokenPool

    cfg = DBConfig(verbose=args.verbose)
    if args.database_url:
        cfg.database_url = args.database_url

    db = DBAdapter(cfg.database_url)
    await db.connect()
    try:
        await create_tables(db)
        limit_clause = f"LIMIT {args.limit}" if args.limit else ""
        status_filter = args.status_filter

        rows = await db.fetchall(
            f"""
            SELECT id, repo_name, pr_number
            FROM prs
            WHERE pr_api_raw IS NULL
              AND status = '{status_filter}'
            ORDER BY id
            {limit_clause}
            """
        )
        logger.info(f"Found {len(rows)} {status_filter} PRs missing pr_api_raw")

        if not rows:
            return

        if args.dry_run:
            for row in rows:
                logger.info(f"  [DRY] {row['repo_name']}#{row['pr_number']} (id={row['id']})")
            logger.info(f"[DRY RUN] Would fetch pr_api_raw for {len(rows)} PRs")
            return

        tokens = cfg.github_tokens if cfg.github_tokens else [cfg.github_token]
        pool = TokenPool(tokens)
        n_tokens = pool.size
        n_workers = n_tokens * 10
        logger.info(f"Using {n_tokens} token(s), {n_workers} workers")

        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        stop_event = asyncio.Event()
        updated = 0
        skipped = 0

        async def _worker(worker_id: int) -> None:
            nonlocal updated, skipped
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    break

                pr_id = item["id"]
                repo_name = item["repo_name"]
                pr_number = item["pr_number"]

                try:
                    owner, repo = repo_name.split("/", 1)
                except ValueError:
                    skipped += 1
                    queue.task_done()
                    continue

                gh = None
                try:
                    while True:
                        gh = pool.get()
                        if gh is None:
                            wait = max(0, pool.earliest_reset() - time_mod.time()) + 5
                            logger.warning(f"Worker {worker_id}: all tokens rate-limited, sleeping {wait:.0f}s")
                            await asyncio.sleep(wait)
                            continue
                        try:
                            resp = await gh.rest_get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
                            if resp is None:
                                skipped += 1
                                break

                            data = resp.json()
                            pr_merged = data.get("merged")
                            repo_id = (data.get("base") or {}).get("repo", {}).get("id")

                            await db.execute(*db._translate_params(
                                "UPDATE prs SET pr_api_raw = $1, pr_merged = COALESCE($2, pr_merged), "
                                "repo_id = COALESCE(repo_id, $3) WHERE id = $4",
                                (json_mod.dumps(data), pr_merged, repo_id, pr_id),
                            ))
                            updated += 1
                            break
                        except RateLimitExhaustedError as e:
                            pool.mark_limited(gh, e.reset_at)
                            logger.info(f"Worker {worker_id}: token rate-limited, rotating ({pool.status_summary()})")
                            gh = None
                            continue
                finally:
                    if gh is not None:
                        pool.release(gh)
                    queue.task_done()

        async def _progress_logger() -> None:
            while not stop_event.is_set():
                await asyncio.sleep(15)
                total_done = updated + skipped
                pct = total_done * 100 // len(rows) if rows else 0
                logger.info(
                    f"API progress: {total_done}/{len(rows)} ({pct}%) "
                    f"[updated={updated} skipped={skipped}] "
                    f"| Tokens: {pool.status_summary()}"
                )

        for row in rows:
            await queue.put(row)
        for _ in range(n_workers):
            await queue.put(None)

        workers = [asyncio.create_task(_worker(i)) for i in range(n_workers)]
        progress_task = asyncio.create_task(_progress_logger())

        await queue.join()
        stop_event.set()
        await asyncio.gather(*workers)
        progress_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await progress_task

        await pool.close()
        logger.info(f"DONE — backfill-api-raw: updated={updated}, skipped={skipped}")
    finally:
        await db.close()


async def cmd_backfill_engagement(args: argparse.Namespace) -> None:
    """Compute engagement_signals from assembled timelines.

    Default: only PRs with NULL engagement_signals.
    --force: recompute existing rows (needed after actor-alias fixes so Copilot
    reviews from copilot-pull-request-reviewer are recognized).
    """
    import json as json_mod
    import time

    from db.connection import DBAdapter
    from db.schema import create_tables
    from pipeline.quality import compute_engagement_signals

    cfg = DBConfig(verbose=args.verbose)
    db_url = args.database_url or cfg.database_url
    db = DBAdapter(db_url)
    await db.connect()
    await create_tables(db)

    try:
        where = ["p.assembled IS NOT NULL", "p.status = $1"]
        params: list[object] = [args.status_filter]
        if not args.force:
            where.append("p.engagement_signals IS NULL")
        if args.chatbot:
            params.append(args.chatbot)
            where.append(f"c.github_username = ${len(params)}")
        since = _parse_time_bound(args.since)
        until = _parse_time_bound(args.until)
        if since:
            params.append(since)
            where.append(f"p.bot_reviewed_at >= ${len(params)}")
        if until:
            params.append(until)
            where.append(f"p.bot_reviewed_at < ${len(params)}")
        where_sql = " AND ".join(where)

        count_row = await db.fetchone(
            f"""
            SELECT COUNT(*) as cnt
            FROM prs p
            JOIN chatbots c ON c.id = p.chatbot_id
            WHERE {where_sql}
            """,
            tuple(params),
        )
        total = count_row["cnt"] if count_row else 0
        scope = "recompute" if args.force else "missing"
        bot = args.chatbot or "all bots"
        window = ""
        if since or until:
            window = f" [{since or '...'} .. {until or '...'}]"
        logger.info(f"Found {total} assembled PRs ({scope}) for {bot}{window}")

        if total == 0:
            return

        batch_size = args.batch_size
        updated = 0
        skipped = 0
        last_id = 0
        last_log = time.time()
        limit = args.limit

        while True:
            if limit is not None and updated >= limit:
                break

            fetch_size = batch_size
            if limit is not None:
                fetch_size = min(batch_size, limit - updated)

            fetch_params = [*params, last_id, fetch_size]
            id_placeholder = f"${len(params) + 1}"
            limit_placeholder = f"${len(params) + 2}"
            rows = await db.fetchall(
                f"""
                SELECT p.id, p.assembled, p.pr_author, c.github_username AS chatbot
                FROM prs p
                JOIN chatbots c ON c.id = p.chatbot_id
                WHERE {where_sql}
                  AND p.id > {id_placeholder}
                ORDER BY p.id
                LIMIT {limit_placeholder}
                """,
                tuple(fetch_params),
            )

            if not rows:
                break

            if args.dry_run:
                for row in rows[:10]:
                    assembled = json_mod.loads(row["assembled"])
                    signals = compute_engagement_signals(
                        assembled, row["chatbot"], pr_author=row.get("pr_author"),
                    )
                    logger.info(
                        f"  [DRY] id={row['id']}: reviewers={signals['human_reviewer_count']} "
                        f"comments={signals['human_comment_count']} "
                        f"rounds={signals['back_and_forth_rounds']} "
                        f"commits={signals['commits_after_review']} "
                        f"engaged={signals['has_human_engagement']}"
                    )
                logger.info(f"DRY RUN — would process {total} PRs (showed first {min(10, len(rows))})")
                return

            for row in rows:
                try:
                    assembled = json_mod.loads(row["assembled"])
                except (TypeError, json_mod.JSONDecodeError):
                    skipped += 1
                    last_id = row["id"]
                    continue
                signals = compute_engagement_signals(
                    assembled, row["chatbot"], pr_author=row.get("pr_author"),
                )
                signals_json = json_mod.dumps(signals)
                await db.execute(
                    *db._translate_params(
                        "UPDATE prs SET engagement_signals = $1 WHERE id = $2",
                        (signals_json, row["id"]),
                    )
                )
                updated += 1
                last_id = row["id"]

            now = time.time()
            if now - last_log >= 15:
                logger.info(f"  Progress: {updated}/{total}")
                last_log = now

        logger.info(f"DONE — backfill-engagement: {updated} PRs updated, {skipped} skipped")
    finally:
        await db.close()


async def cmd_import(args: argparse.Namespace) -> None:
    from migration.import_filesystem import import_all

    cfg = DBConfig(verbose=args.verbose)
    db_url = args.database_url or cfg.database_url
    await import_all(args.output_dir, db_url, chatbot_filter=args.chatbot)


def cmd_dashboard(args: argparse.Namespace) -> None:
    import subprocess

    subprocess.run(
        ["streamlit", "run", "dashboard/app.py", "--server.port", str(args.port)],
        check=True,
    )


def main() -> None:
    # Detect legacy mode: if --user is in argv, use legacy parser
    if "--user" in sys.argv:
        config = parse_legacy_args(sys.argv[1:])
        run_legacy(config)
        return

    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    verbose = getattr(args, "verbose", False)
    setup_logging(verbose)

    if args.command == "dashboard":
        cmd_dashboard(args)
    elif args.command == "discover":
        asyncio.run(cmd_discover(args))
    elif args.command == "enrich":
        asyncio.run(cmd_enrich(args))
    elif args.command == "analyze":
        asyncio.run(cmd_analyze(args))
    elif args.command == "label":
        asyncio.run(cmd_label(args))
    elif args.command == "volumes":
        asyncio.run(cmd_volumes(args))
    elif args.command == "backfill":
        asyncio.run(cmd_backfill(args))
    elif args.command == "backfill-pr-author":
        asyncio.run(cmd_backfill_pr_author(args))
    elif args.command == "backfill-metadata":
        asyncio.run(cmd_backfill_metadata(args))
    elif args.command == "backfill-api-raw":
        asyncio.run(cmd_backfill_api_raw(args))
    elif args.command == "backfill-engagement":
        asyncio.run(cmd_backfill_engagement(args))
    elif args.command == "import":
        asyncio.run(cmd_import(args))


if __name__ == "__main__":
    main()

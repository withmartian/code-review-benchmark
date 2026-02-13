"""CLI entrypoint for the PR review dataset builder.

Supports two modes:
1. Legacy filesystem mode (original --user/--start/--end pipeline)
2. New DB-backed mode via subcommands: discover, enrich, analyze, import, dashboard
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from config import DEFAULT_CHATBOT_USERNAMES, Config, DBConfig

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
    parser.add_argument("--github-token", default="", help="GitHub personal access token (required for gh-enrich phase)")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--output-dir", default="output", help="Base output directory (default: output)")
    parser.add_argument("--phase", default="all", choices=["all", "bq-extract", "gh-enrich", "assemble"],
                        help="Run only one phase")
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
    p_disc = sub.add_parser("discover", help="Discover PRs from BigQuery into DB")
    p_disc.add_argument("--chatbot", help="GitHub username of the chatbot")
    p_disc.add_argument("--all", action="store_true", dest="all_chatbots", help="Discover for all registered chatbots (single BQ scan)")
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
    p_enr.add_argument("--chatbot", required=True)
    p_enr.add_argument("--one-shot", action="store_true")
    p_enr.add_argument("--max-prs", type=int)
    p_enr.add_argument("--max-pr-commits", type=int, help="Skip PRs with more commits than this (default: 50)")
    p_enr.add_argument("--max-pr-changed-lines", type=int, help="Skip PRs with more changed lines than this (default: 2000)")
    p_enr.add_argument("--database-url")
    p_enr.add_argument("--github-token")
    p_enr.add_argument("--verbose", action="store_true")

    # analyze
    p_ana = sub.add_parser("analyze", help="Run LLM analysis on assembled PRs")
    p_ana.add_argument("--chatbot", help="Specific chatbot, or use --all")
    p_ana.add_argument("--all", action="store_true", dest="all_chatbots")
    p_ana.add_argument("--limit", type=int, default=100)
    p_ana.add_argument("--since", help="Only analyze PRs reviewed since this date (e.g. '7d', '2026-02-05')")
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
    p_lbl.add_argument("--since", help="Only label PRs reviewed since this date (e.g. '7d', '2026-02-05')")
    p_lbl.add_argument("--database-url")
    p_lbl.add_argument("--verbose", action="store_true")

    # backfill
    p_bf = sub.add_parser("backfill", help="Backfill computed columns (e.g. diff_lines)")
    p_bf.add_argument("--database-url")
    p_bf.add_argument("--batch-size", type=int, default=5000)
    p_bf.add_argument("--verbose", action="store_true")

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Launch Streamlit dashboard")
    p_dash.add_argument("--port", type=int, default=8501)

    return parser


async def cmd_discover(args: argparse.Namespace) -> None:
    from datetime import datetime, timedelta, timezone

    from db.connection import DBAdapter
    from db.repository import PRRepository
    from db.schema import create_tables
    from pipeline.discover import discover_prs, discover_prs_batch

    cfg = DBConfig(verbose=args.verbose)
    if args.database_url:
        cfg.database_url = args.database_url
    if args.gcp_project:
        cfg.gcp_project = args.gcp_project

    end_date = args.end_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = args.start_date or (datetime.now(timezone.utc) - timedelta(days=args.days_back)).strftime("%Y-%m-%d")

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
            logger.info(f"Batch discovering PRs for {len(usernames)} chatbots")
            await discover_prs_batch(
                cfg, db, usernames, start_date, end_date,
                min_pr_number=args.min_pr_number,
                max_prs_per_day=args.max_prs_per_day,
            )
        else:
            await discover_prs(cfg, db, args.chatbot, start_date, end_date,
                               min_pr_number=args.min_pr_number,
                               max_prs_per_day=args.max_prs_per_day,
                               display_name=args.display_name)
    finally:
        await db.close()


async def cmd_enrich(args: argparse.Namespace) -> None:
    from db.connection import DBAdapter
    from db.repository import PRRepository
    from db.schema import create_tables
    from pipeline.assemble import assemble_enriched_prs
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
    if not cfg.github_token:
        logger.error("GITHUB_TOKEN required")
        return

    db = DBAdapter(cfg.database_url)
    await db.connect()
    try:
        await create_tables(db)
        repo = PRRepository(db)
        chatbot = await repo.get_chatbot(args.chatbot)
        if not chatbot:
            logger.error(f"Chatbot '{args.chatbot}' not found. Run discover first.")
            return

        enriched = await enrich_loop(cfg, db, chatbot["id"], max_prs=args.max_prs, one_shot=args.one_shot)
        logger.info(f"Enriched {enriched} PRs")

        assembled = await assemble_enriched_prs(db, chatbot["id"], chatbot["github_username"])
        logger.info(f"Assembled {assembled} PRs")
    finally:
        await db.close()


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

    # Parse --since: supports relative ("7d") or absolute ("2026-02-05")
    since = None
    if args.since:
        import re
        from datetime import datetime, timedelta, timezone
        m = re.match(r"^(\d+)d$", args.since)
        if m:
            since = (datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))).isoformat()
        else:
            since = args.since
        logger.info(f"Filtering PRs reviewed since {since}")

    db = DBAdapter(cfg.database_url)
    await db.connect()
    try:
        await create_tables(db)
        repo = PRRepository(db)

        if args.all_chatbots:
            chatbots = await repo.get_all_chatbots()
            for bot in chatbots:
                await analyze_prs(cfg, db, bot["id"], bot["github_username"], limit=args.limit, since=since)
        elif args.chatbot:
            bot = await repo.get_chatbot(args.chatbot)
            if not bot:
                logger.error(f"Chatbot '{args.chatbot}' not found.")
                return
            await analyze_prs(cfg, db, bot["id"], bot["github_username"], limit=args.limit, since=since)
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

    # Parse --since
    since = None
    if args.since:
        import re
        from datetime import datetime, timedelta, timezone
        m = re.match(r"^(\d+)d$", args.since)
        if m:
            since = (datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))).isoformat()
        else:
            since = args.since
        logger.info(f"Filtering PRs reviewed since {since}")

    db = DBAdapter(cfg.database_url)
    await db.connect()
    try:
        await create_tables(db)
        repo = PRRepository(db)

        if args.all_chatbots:
            chatbots = await repo.get_all_chatbots()
            for bot in chatbots:
                await label_prs(cfg, db, bot["id"], bot["github_username"], limit=args.limit, since=since)
        elif args.chatbot:
            bot = await repo.get_chatbot(args.chatbot)
            if not bot:
                logger.error(f"Chatbot '{args.chatbot}' not found.")
                return
            await label_prs(cfg, db, bot["id"], bot["github_username"], limit=args.limit, since=since)
        else:
            logger.error("Specify --chatbot or --all")
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
    elif args.command == "backfill":
        asyncio.run(cmd_backfill(args))
    elif args.command == "import":
        asyncio.run(cmd_import(args))


if __name__ == "__main__":
    main()

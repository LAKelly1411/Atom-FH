"""
PA ATOMIC Food Hygiene Prototype — Main runner
Usage:
  python run.py                          # run the full daily pipeline
  python run.py --resolve-authorities    # look up and save LA IDs from FSA API
  python run.py --list-authorities       # print all FSA authority names
  python run.py --ingest-only            # ingestion only
  python run.py --detect-only            # change detection only (requires prior ingest)
  python run.py --feed-only              # generate digest only (requires prior detect)
  python run.py --date 2024-12-01        # run pipeline for a specific date
  python run.py --dry-run                # run everything but don't send emails
"""

import argparse
import logging
import os
import sys
from datetime import date

# Ensure src is importable
sys.path.insert(0, os.path.dirname(__file__))

from src.db import init_db
from src.ingest import run_ingestion, resolve_authority_ids, list_all_authority_names
from src.detect import run_change_detection, summarise_changes
from src.feed import run_feed


def configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def get_recipients() -> list[str]:
    """
    Load email recipients from environment or fall back to config.
    Set DIGEST_RECIPIENTS as comma-separated emails in your environment.
    """
    env_val = os.environ.get("DIGEST_RECIPIENTS", "")
    if env_val:
        return [r.strip() for r in env_val.split(",") if r.strip()]
    return []


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PA ATOMIC Food Hygiene Pipeline"
    )
    parser.add_argument("--resolve-authorities", action="store_true",
                        help="Resolve LA names to FSA IDs and save to config")
    parser.add_argument("--list-authorities", action="store_true",
                        help="Print all FSA authority names and IDs")
    parser.add_argument("--ingest-only", action="store_true")
    parser.add_argument("--detect-only", action="store_true")
    parser.add_argument("--feed-only", action="store_true")
    parser.add_argument("--date", type=str, default=None,
                        help="Override run date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip email sending")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    configure_logging(args.verbose)
    logger = logging.getLogger("run")
    run_date = args.date or date.today().isoformat()

    # ── Utility commands ──────────────────────────────────────────────────────

    if args.list_authorities:
        list_all_authority_names()
        return

    if args.resolve_authorities:
        logger.info("Resolving authority IDs from FSA API...")
        resolved = resolve_authority_ids(save=True)
        logger.info("Resolved %s authorities: %s",
                    len(resolved),
                    ", ".join(f"{k} ({v})" for k, v in resolved.items()))
        return

    # ── Pipeline ──────────────────────────────────────────────────────────────

    logger.info("=== PA ATOMIC pipeline start | date: %s ===", run_date)
    init_db()

    recipients = [] if args.dry_run else get_recipients()

    # Step 1: Ingestion
    if not args.detect_only and not args.feed_only:
        logger.info("--- Step 1: Ingestion ---")
        summary = run_ingestion(pull_date=run_date)
        total = sum(summary.values())
        logger.info("Ingestion complete: %s records across %s authorities",
                    total, len(summary))
        for name, count in summary.items():
            logger.info("  %-40s %s records", name, count)

    # Step 2: Change detection
    if not args.ingest_only and not args.feed_only:
        logger.info("--- Step 2: Change detection ---")
        changes = run_change_detection(today_date=run_date)
        summary = summarise_changes(changes)
        logger.info("Changes: %s", summary)
    elif args.feed_only:
        # Load changes from DB for the given date
        from src.db import get_connection, get_changes_for_date
        conn = get_connection()
        rows = get_changes_for_date(conn, run_date)
        conn.close()
        changes = [dict(r) for r in rows]
        logger.info("Loaded %s changes from DB for %s", len(changes), run_date)

    # Step 3: Daily feed
    if not args.ingest_only and not args.detect_only:
        logger.info("--- Step 3: Daily feed ---")
        output_path = run_feed(
            changes=changes,
            recipients=recipients if recipients else None,
            run_date=run_date,
        )
        logger.info("Digest saved: %s", output_path)
        if not recipients:
            logger.info("(No recipients configured — email not sent. "
                        "Set DIGEST_RECIPIENTS env var to enable.)")

    logger.info("=== PA ATOMIC pipeline complete ===")


if __name__ == "__main__":
    main()

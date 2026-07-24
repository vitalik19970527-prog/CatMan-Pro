#!/usr/bin/env python3
"""
GSM Parser Scheduler - runs parser on schedule from database config.
Reads schedule from app_update_policy table (gsm_parser_schedule).
Designed for deployment on VPS with cron or systemd timer.

Usage:
  # Run once and exit
  python gsm_parser_scheduler.py --run-once

  # Run in daemon mode (infinite loop checking schedule)
  python gsm_parser_scheduler.py --daemon

  # Show current schedule
  python gsm_parser_scheduler.py --show-schedule
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from typing import Optional

# Add workspace to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import DataManager
from logger import logger

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def init_db() -> Optional[DataManager]:
    """Initialize database connection from environment."""
    try:
        db = DataManager()
        if not db._use_postgres():
            logger.error("PostgreSQL not configured (missing DATABASE_URL)")
            return None
        # Test connection — GSM Parser config/dataset lives in the GENERAL
        # database (shared across all companies), not a per-company one, so
        # there's no set_company() to call here; _pg() would always raise
        # "No active company selected".
        with db._general_pg().cursor() as cur:
            cur.execute("SELECT 1")
        logger.info("Database connected successfully")
        return db
    except Exception as exc:
        logger.exception("Failed to initialize database: %s", exc)
        return None


def run_parser_once(db: DataManager) -> bool:
    """Execute parser once and record results in DB."""
    try:
        logger.info("Starting GSM Parser execution...")
        from gsm_parser_runner import run_gsm_parser

        # Run parser (currently returns stub)
        ok, msg = run_gsm_parser("__global__", "", "")
        logger.info("Parser execution result: %s", msg)

        # Record execution timestamp
        db.update_gsm_parser_run_timestamp()
        logger.info("Parser execution recorded in database")
        return True
    except Exception as exc:
        logger.exception("Parser execution failed: %s", exc)
        return False


def show_schedule(db: DataManager) -> None:
    """Display current schedule configuration."""
    config = db.get_gsm_parser_schedule()
    enabled = config.get("enabled", False)
    run_times = config.get("run_times", [])
    last_run = config.get("last_run")
    next_run = config.get("next_run")

    print("\n=== GSM Parser Schedule ===")
    print(f"Enabled:   {enabled}")
    print(f"Run Times: {', '.join(run_times)}")
    print(f"Last Run:  {last_run or 'Never'}")
    print(f"Next Run:  {next_run or 'Not calculated'}")
    print()


def should_run_now(run_times: list[str]) -> bool:
    """Check if current time matches any scheduled run time."""
    now = datetime.utcnow()
    current_hm = now.strftime("%H:%M")

    for run_time in run_times:
        run_time = str(run_time or "").strip()
        if run_time == current_hm:
            logger.info("Scheduled run time matched: %s", current_hm)
            return True
    return False


def run_daemon(db: DataManager, check_interval: int = 60) -> None:
    """Run in daemon mode: continuously check schedule and execute."""
    logger.info(
        "Starting GSM Parser Daemon (check interval: %d seconds)", check_interval
    )

    last_run_minute = None

    while True:
        try:
            # Independent of the scrape schedule below: flush any Telegram
            # report that got queued by a run inside "Не турбувати" quiet
            # hours and whose send time has now arrived.
            try:
                from gsm_parser_runner import _flush_due_telegram_notifications
                _flush_due_telegram_notifications(db, logger.info)
            except Exception:
                logger.exception("Failed to flush pending Telegram notifications")

            config = db.get_gsm_parser_schedule()
            enabled = config.get("enabled", False)
            run_times = config.get("run_times", [])

            now = datetime.utcnow()
            current_minute = now.strftime("%H:%M")

            if not enabled:
                if current_minute != last_run_minute:
                    logger.debug("Parser disabled, waiting...")
                    last_run_minute = current_minute
            elif should_run_now(run_times):
                if current_minute != last_run_minute:
                    logger.info("Executing scheduled parser run at %s", current_minute)
                    run_parser_once(db)
                    last_run_minute = current_minute
                else:
                    # Already ran this minute, skip
                    pass
            else:
                if current_minute != last_run_minute:
                    logger.debug(
                        "Waiting for next run time (current: %s, next: %s)",
                        current_minute,
                        run_times,
                    )
                    last_run_minute = current_minute

            time.sleep(check_interval)
        except KeyboardInterrupt:
            logger.info("Daemon interrupted by user")
            break
        except Exception as exc:
            logger.exception("Error in daemon loop: %s", exc)
            time.sleep(check_interval)


def generate_crontab_entry(run_times: list[str]) -> str:
    """Generate crontab entry for the scheduled times."""
    if not run_times:
        return "# No run times configured"

    entries = []
    for run_time in run_times:
        run_time = str(run_time or "").strip()
        try:
            h, m = map(int, run_time.split(":"))
            entry = f"{m} {h} * * * cd /path/to/app && /usr/bin/python3 gsm_parser_scheduler.py --run-once >> /var/log/gsm_parser.log 2>&1"
            entries.append(entry)
        except Exception:
            logger.warning("Invalid time format: %s", run_time)

    return "\n".join(entries)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="GSM Parser Scheduler - execute parser on schedule from database"
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run parser once and exit",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run in daemon mode (continuous scheduling)",
    )
    parser.add_argument(
        "--show-schedule",
        action="store_true",
        help="Display current schedule configuration",
    )
    parser.add_argument(
        "--generate-crontab",
        action="store_true",
        help="Generate crontab entry for deployment",
    )
    parser.add_argument(
        "--check-interval",
        type=int,
        default=60,
        help="Daemon check interval in seconds (default: 60)",
    )

    args = parser.parse_args()

    db = init_db()
    if not db:
        sys.exit(1)

    try:
        if args.run_once:
            logger.info("Running parser once...")
            ok = run_parser_once(db)
            sys.exit(0 if ok else 1)

        elif args.daemon:
            run_daemon(db, check_interval=args.check_interval)

        elif args.show_schedule:
            show_schedule(db)

        elif args.generate_crontab:
            config = db.get_gsm_parser_schedule()
            run_times = config.get("run_times", [])
            crontab = generate_crontab_entry(run_times)
            print("\n=== Crontab Entry ===")
            print(crontab)
            print("\nUpdate /etc/crontab or use 'crontab -e' and add the above lines.")
            print()

        else:
            parser.print_help()

    finally:
        db.close()


if __name__ == "__main__":
    main()

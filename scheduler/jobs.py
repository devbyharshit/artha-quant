"""APScheduler daemon orchestrating daily operational workflow in Asia/Kolkata timezone."""
import sys
import pytz
from apscheduler.schedulers.blocking import BlockingScheduler

from configs.logger import logger
from scheduler.auth_check import check_auth_health
from scheduler.watchdog import verify_eod_execution
from main import run_eod_scan

IST = pytz.timezone("Asia/Kolkata")


def scheduled_auth_job():
    logger.info("Triggering scheduled morning auth check (08:30 IST)...")
    check_auth_health()


def scheduled_eod_scan_job():
    logger.info("Triggering scheduled EOD market scan (16:00 IST)...")
    run_eod_scan(dry_run=False)


def scheduled_watchdog_job():
    logger.info("Triggering scheduled EOD watchdog check (16:15 IST)...")
    verify_eod_execution()


def start_scheduler():
    """Initializes and runs the blocking scheduler daemon."""
    scheduler = BlockingScheduler(timezone=IST)

    # 1. Morning Auth Check at 08:30 IST (Mon-Fri)
    scheduler.add_job(scheduled_auth_job, "cron", hour=8, minute=30, day_of_week="mon-fri")

    # 2. Main EOD Scanner at 16:00 IST (Mon-Fri)
    scheduler.add_job(scheduled_eod_scan_job, "cron", hour=16, minute=0, day_of_week="mon-fri")

    # 3. Watchdog Verification at 16:15 IST (Mon-Fri)
    scheduler.add_job(scheduled_watchdog_job, "cron", hour=16, minute=15, day_of_week="mon-fri")

    logger.info("Artha Quant APScheduler running. Configured for Asia/Kolkata timezone (Mon-Fri).")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped by operator.")


if __name__ == "__main__":
    start_scheduler()

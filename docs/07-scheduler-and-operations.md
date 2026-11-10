# 07 — Scheduler & Production Operations

## 1. Daily Operational Timeline (IST)

The system operates strictly around the **National Stock Exchange (NSE)** cash market session (09:15 to 15:30 IST):

```
08:30 IST ──► [Morning Auth Health Check]
              Verifies kite_session.json exists and tests `kite.profile()`.
              If expired, triggers immediate Telegram alert to developer.

09:00 IST ──► [Auth Cut-off Watchdog]
              If token is still expired, issues high-priority warning:
              "Kite session not renewed. EOD scan at 16:00 will fail."

09:15–15:30 ──► [Trading Session]
              System idle (no intraday scans in Phase 1 MVP).

15:45 IST ──► [NSE Post-Market Settlement]
              Exchange finalizes 1D daily candles, weighted average prices, and volume.

16:00 IST ──► [Main End-of-Day (EOD) Scan Job]
              1. Download confirmed 1D bars for universe.
              2. Validate data and update local Parquet store.
              3. Run strategy indicators and signals.
              4. Apply risk filters and sizing limits.
              5. Record to SQLite and dispatch Telegram alert cards.

16:15 IST ──► [Watchdog Heartbeat Verification]
              Checks SQLite for today's run record.
              Sends confirmation heartbeat: "EOD Scan completed successfully. 2 signals emitted."
              If no record: sends SOS alert: "CRITICAL: EOD Scan failed to execute."
```

---

## 2. Scheduling Implementations

### Option A: Systemd Timer / Cron (Recommended for Linux VPS)
The most resilient scheduling mechanism on Linux is `cron` or a `systemd` service with a timer. Unlike an in-memory Python process, it cannot crash, leak memory, or get terminated by the OS out-of-memory killer:

```cron
# /etc/cron.d/artha_quant
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
CRON_TZ=Asia/Kolkata

# 1. Morning Auth Check (Mon-Fri at 08:30 IST)
30 8 * * 1-5 artha cd /opt/artha-quant && /opt/artha-quant/.venv/bin/python -m scheduler.auth_check >> logs/cron.log 2>&1

# 2. Main EOD Scanner (Mon-Fri at 16:00 IST)
00 16 * * 1-5 artha cd /opt/artha-quant && /opt/artha-quant/.venv/bin/python -m scheduler.jobs >> logs/cron.log 2>&1

# 3. Watchdog Health Check (Mon-Fri at 16:15 IST)
15 16 * * 1-5 artha cd /opt/artha-quant && /opt/artha-quant/.venv/bin/python -m scheduler.watchdog >> logs/cron.log 2>&1
```

### Option B: APScheduler (Python Process)
If running inside a long-lived Python daemon:

```python
# scheduler/scheduler.py
from apscheduler.schedulers.blocking import BlockingScheduler
from loguru import logger
import pytz

def start_scheduler():
    ist = pytz.timezone("Asia/Kolkata")
    scheduler = BlockingScheduler(timezone=ist)
    
    scheduler.add_job(auth_check_job, "cron", hour=8, minute=30, day_of_week="mon-fri")
    scheduler.add_job(run_eod_scan,    "cron", hour=16, minute=0,  day_of_week="mon-fri")
    scheduler.add_job(watchdog_job,    "cron", hour=16, minute=15, day_of_week="mon-fri")
    
    logger.info("APScheduler initialized in Asia/Kolkata timezone. Awaiting jobs...")
    scheduler.start()
```

---

## 3. Hosting & Infrastructure Guidelines

### Why a Personal Laptop is Unsuitable for Production:
* Power management puts laptops to sleep when idle or closed.
* Wi-Fi disconnects or power interruptions cause silent scan drops.
* An unattended trading scanner requires an always-on host.

### Recommended Production VPS Specification:
* **Provider:** DigitalOcean Droplet, Hetzner Cloud, or AWS Lightsail.
* **Specs:** 1 vCPU, 2 GB RAM, 25 GB SSD storage (costs ~₹350–₹500/month).
* **OS:** Ubuntu 24.04 LTS.
* **Timezone Configuration:**
  ```bash
  sudo timedatectl set-timezone Asia/Kolkata
  timedatectl # Verify Time zone is Asia/Kolkata (IST, +0530)
  ```

---

## 4. Telemetry, Logging & Failure Recovery

### Log Rotation Policy (`loguru`)
```python
from loguru import logger
import sys

logger.remove()
# 1. Console Output
logger.add(sys.stdout, level="INFO", format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")

# 2. Rotating JSON File
logger.add(
    "logs/artha_quant_{time:YYYY-MM-DD}.log",
    rotation="00:00",        # New file daily at midnight
    retention="30 days",     # Keep 1 month of logs
    compression="zip",
    level="DEBUG",
    backtrace=True,
    diagnose=True
)
```

### Automated Error Escalation:
All unhandled exceptions in the scheduler trigger an immediate exception dispatch to your dedicated Telegram Health Channel:

```python
try:
    execute_scan()
except Exception as exc:
    logger.exception("Fatal crash during EOD scan")
    telegram_health_bot.send_message(
        f"🚨 *CRITICAL FAILURE IN ARTHA QUANT*\n\n"
        f"Error: `{str(exc)}`\n"
        f"Timestamp: `{datetime.now(ist).isoformat()}`\n"
        f"Check server logs immediately."
    )
    sys.exit(1)
```

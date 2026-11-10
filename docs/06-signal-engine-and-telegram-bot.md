# 06 — Signal Engine & Telegram Alerts

## 1. Signal Engine Architecture & Deduplication

The signal engine translates strategy logic into actionable trade records. Its primary architectural imperative is **strict idempotency**: running the scanner multiple times must never produce duplicate records or spam alert notifications.

### Deduplication Hashing
Every signal receives a deterministic cryptographic hash:

$$\text{signal\_hash} = \text{SHA256}(\text{symbol} + \text{"\_"} + \text{strategy\_name} + \text{"\_"} + \text{bar\_timestamp})$$

* Example: `SHA256("RELIANCE_TrendBreakout_v1_2026-11-06T15:30:00+05:30")`

---

## 2. SQLite Signal Ledger Specification

All signals are recorded in `signals/signals.db`:

```sql
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_hash TEXT UNIQUE NOT NULL,      -- Deterministic dedup key
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bar_timestamp TEXT NOT NULL,
    action TEXT NOT NULL,                  -- 'BUY' or 'SELL'
    entry_ref REAL NOT NULL,
    stop_loss REAL NOT NULL,
    target REAL NOT NULL,
    risk_reward_ratio REAL NOT NULL,
    calculated_qty INTEGER NOT NULL,
    risk_budget REAL NOT NULL,
    capital_required REAL NOT NULL,
    status TEXT DEFAULT 'PENDING',         -- 'PENDING', 'SENT', 'FAILED', 'VETOED'
    veto_reason TEXT,
    telegram_sent_at TEXT,
    created_at TEXT NOT NULL
);
```

### Atomic Recording Flow
```python
# signals/ledger.py
import sqlite3
from typing import Optional

def record_signal_if_unique(db_path: str, signal: dict) -> bool:
    """
    Inserts signal into SQLite. Returns True if new, False if already exists.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO signals (
                signal_hash, symbol, strategy, timeframe, bar_timestamp,
                action, entry_ref, stop_loss, target, risk_reward_ratio,
                calculated_qty, risk_budget, capital_required, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', datetime('now'))
        """, (
            signal["signal_hash"], signal["symbol"], signal["strategy"],
            signal["timeframe"], signal["bar_timestamp"], signal["action"],
            signal["entry_ref"], signal["stop_loss"], signal["target"],
            signal["risk_reward_ratio"], signal["calculated_qty"],
            signal["risk_budget"], signal["capital_required"]
        ))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # Unique constraint on signal_hash violated -> already processed
        return False
    finally:
        conn.close()
```

---

## 3. Telegram Bot Configuration

### Setup Instructions
1. Open Telegram, search for `@BotFather`, and send `/newbot`.
2. Follow prompts to name your bot (e.g., `ArthaQuantAlertsBot`).
3. Save the returned `TELEGRAM_BOT_TOKEN`.
4. Create a dedicated private Telegram Channel or Group for trading alerts (e.g., `Artha Quant Alerts`).
5. Add your new bot as an Administrator with message-posting rights.
6. Retrieve your channel `TELEGRAM_CHAT_ID`:
   * Forward a message from the channel to `@userinfobot`, or query `https://api.telegram.org/bot<TOKEN>/getUpdates`.
7. **Best Practice:** Create a second channel for **System Health & Errors** (`TELEGRAM_HEALTH_CHAT_ID`) so system crashes or token expiry warnings never get mixed with trade entries.

---

## 4. Actionable Alert Card Format

The alert card is formatted in clean Markdown to enable immediate verification and manual execution on Zerodha Kite:

```
🟢 BUY SIGNAL — RELIANCE (NSE)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Strategy  : TrendBreakout_v1
Timeframe : 1D (Bar Closed 15:30 IST)
Status    : Actionable for Tomorrow Open

Trade Parameters
────────────────────────────
Entry Ref : ₹2,850.00
Stop Loss : ₹2,780.00  (-2.46%)
Target    : ₹3,025.00  (+6.14%)
R:R Ratio : 1 : 2.5

Position Sizing (Dual-Cap)
────────────────────────────
Suggested Qty : 14 shares
Risk Budget   : ₹980.00 (0.98% of capital)
Capital Req   : ₹39,900.00 (9.98% cap)

Account Snapshot
────────────────────────────
Available Cash : ₹1,42,500.00
Post-Trade Cash: ₹1,02,600.00
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ Place order manually on Kite. Check market depth before executing.
```

---

## 5. Alert Fatigue & Ranking Rules

Flooding a trader with 15 simultaneous alerts leads to execution panic and emotional decision-making. 

### Sessional Alert Controls:
* **Max Alerts per Session:** Cap at **maximum 3 alerts per day**.
* **Ranking Objective:** If 6 stocks produce valid buy signals on the same day:
  1. Filter out any stock violating sector concentration (max 3 per sector).
  2. Rank remaining candidate signals by **Risk:Reward Ratio** and **Liquidity (20-day ADV)**.
  3. Dispatch the top 3 and log the remainder to SQLite as `SKIPPED_CAP_EXCEEDED`.
* **Zero Guesswork Scoring:** Do **not** use subjective or uncalibrated "AI confidence scores" to rank trades. Stick to auditable, mathematical metrics.

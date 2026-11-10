# 01 — Architecture & System Design

## 1. System Overview & Core Tenets

Artha Quant is architected as an **isolated, modular quantitative engine** engineered around three non-negotiable principles:
1. **Stateless Strategy Logic:** Strategies act as pure functions over immutable historical dataframes. They do not know about brokers, sockets, or alert channels.
2. **Deterministic Idempotency:** Any scheduled job or scan can be executed repeatedly without producing duplicated signals, corrupting state, or sending duplicate alerts.
3. **Fail-Closed Risk Control:** If any component (market data, token, margin calculation, indicator) fails or produces anomalies, the system logs a high-severity alert to an operational channel and drops the signal. It never guesses or falls back to unsafe defaults.

---

## 2. High-Level Architecture Diagram

```
                               ┌─────────────────────────────┐
                               │       System Scheduler      │
                               │   (APScheduler / Cron Job)  │
                               └──────────────┬──────────────┘
                                              │ Triggers at 16:00 IST
                                              ▼
┌─────────────────────────┐      ┌───────────────────────────┐      ┌─────────────────────────┐
│   Kite Auth Provider    │ ───► │   Data Ingestion &        │ ───► │     Strategy Engine     │
│  (Session Management)   │      │   Parquet Store Manager   │      │   (Vectorized Signals)  │
└─────────────────────────┘      └─────────────┬─────────────┘      └────────────┬────────────┘
                                               │                                 │
                                               ▼                                 ▼
                                 ┌───────────────────────────┐      ┌─────────────────────────┐
                                 │   Data Quality Validator  │      │  Risk & Position Sizer  │
                                 │ (Splits, Gaps, Outliers)  │      │ (Risk-Budget + Hard Cap)│
                                 └───────────────────────────┘      └────────────┬────────────┘
                                                                                 │
                                                                                 ▼
┌─────────────────────────┐      ┌───────────────────────────┐      ┌─────────────────────────┐
│    Telegram Notifier    │ ◄─── │   SQLite Signal Ledger    │ ◄─── │   Signal Deduplication  │
│  (Actionable Trade Card)│      │  (Audit, Dedup & History) │      │      & Filter Gate      │
└─────────────────────────┘      └───────────────────────────┘      └─────────────────────────┘
```

---

## 3. End-of-Day (EOD) Scan Sequence

```
Cron (16:00 IST) ──► Scheduler.run_eod_scan()
                           │
                           ├──► 1. Check Kite Session validity
                           │       ├── Valid: Proceed
                           │       └── Expired: Log error, trigger Telegram SOS alert, abort.
                           │
                           ├──► 2. For each symbol in Universe:
                           │       ├── Fetch latest closed daily bar from Kite API (Rate limit 3/s)
                           │       ├── Validate bar integrity (High >= Open, Close >= Low, Volume > 0)
                           │       ├── Append to local Parquet store (atomic write)
                           │       └── Load combined history (e.g., last 300 bars)
                           │
                           ├──► 3. StrategyEngine.evaluate(symbol_df)
                           │       └── Compute indicators via pandas-ta on confirmed bar only
                           │       └── If Buy/Sell condition met: emit SignalEvent
                           │
                           ├──► 4. RiskEngine.evaluate(SignalEvent)
                           │       ├── Check Portfolio Drawdown (< 15%)
                           │       ├── Check Sector Concentration (< 3 open per sector)
                           │       ├── Check Pairwise Correlation (< 0.75)
                           │       └── Compute Position Size: min(qty_by_risk, qty_by_cap)
                           │
                           ├──► 5. SignalLedger.record_if_unique(SignalEvent)
                           │       └── SQLite INSERT OR IGNORE via UNIQUE(symbol, strategy, timestamp)
                           │       └── Returns: True if newly recorded, False if duplicate
                           │
                           └──► 6. If True: TelegramNotifier.send_signal_card(SignalEvent)
```

---

## 4. Module Boundaries & Responsibilities

| Module | Directory | Responsibility |
|---|---|---|
| **Config & Environment** | `configs/` | Strict schema validation of environment variables and strategy YAML definitions. |
| **Authentication** | `scheduler/` | Kite Connect login lifecycle, session file serialization, and token expiry verification. |
| **Data Ingestion** | `data/loaders/` | Rate-limited Kite Connect historical data fetcher, pagination, and chunking. |
| **Data Cleaning** | `data/cleaners/` | Outlier detection, zero-volume handling, split/bonus validation. |
| **Data Store** | `data/store/` | Fast, partitioned local Parquet storage (`data/store/{timeframe}/{symbol}.parquet`). |
| **Strategy Engine** | `strategies/` | Abstract base class and strategy implementations producing boolean signal series. |
| **Backtesting Engine**| `backtesting/` | `vectorbt` backtesting harness, granular Zerodha cost calculations, and 20% STCG tax model. |
| **Risk Management** | `risk/` | Dual-constraint position sizing, portfolio drawdown breakers, and correlation filters. |
| **Signal Ledger** | `signals/` | SQLite transaction logging, deduplication hashing, and audit trails. |
| **Alerts** | `alerts/` | Telegram bot client, Markdown card formatting, and rate limiting. |

---

## 5. Storage Architecture

### A. Market Data (Parquet)
* **Format:** Apache Parquet with Snappy compression via `pyarrow`.
* **Pathing:** `data/store/1D/{symbol}.parquet` (e.g., `data/store/1D/RELIANCE.parquet`).
* **Schema:**
  * `timestamp` (datetime64[ns, Asia/Kolkata], Index)
  * `open` (float64)
  * `high` (float64)
  * `low` (float64)
  * `close` (float64)
  * `volume` (int64)

### B. Signal Ledger & State (SQLite)
* **File:** `signals/signals.db`
* **Table:** `signals`
* **Schema:**
  ```sql
  CREATE TABLE IF NOT EXISTS signals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      signal_hash TEXT UNIQUE NOT NULL,      -- SHA256(symbol + strategy + timestamp)
      symbol TEXT NOT NULL,
      strategy TEXT NOT NULL,
      timeframe TEXT NOT NULL,
      bar_timestamp TEXT NOT NULL,
      signal_action TEXT NOT NULL,           -- 'BUY' or 'SELL'
      entry_price REAL NOT NULL,
      stop_loss REAL NOT NULL,
      target_price REAL NOT NULL,
      calculated_qty INTEGER NOT NULL,
      risk_amount REAL NOT NULL,
      capital_required REAL NOT NULL,
      status TEXT DEFAULT 'PENDING',         -- 'PENDING', 'NOTIFIED', 'SKIPPED'
      created_at TEXT NOT NULL
  );
  ```

---

## 6. Defensive Engineering & Edge Cases

1. **Sub-second Kite Rate Limits:** Kite Connect allows up to 3 requests/second on the historical API. Calls must be routed through a decorator providing a minimum 330ms delay between consecutive requests.
2. **Bar State Ambiguity:** The scanner **only** triggers at 16:00 IST after the NSE cash session (09:15–15:30 IST) has definitively settled. This guarantees the 1D candle is fully finalized and immune to tick revision.
3. **Network Resilience:** External API calls (Kite data fetch, Telegram sendMessage) are wrapped in exponential backoff retry policies (3 attempts: 1s, 2s, 4s).

# 08 — Progressive Implementation WBS

## Master Sprint Roadmap

This document serves as the granular engineering execution tracker. Work must proceed sequentially through these sprints; no sprint may be considered complete until its **Definition of Done (DoD)** and automated test suite pass.

---

### Sprint 0: Environment Setup & Core Foundations [COMPLETED ✅]
**Duration:** 1–2 Days  
**Objective:** Stand up the workspace, dependency management, configuration validation, and structured logging.

#### Tasks:
- [x] **Task 0.1: Initialize Python Environment & Project Layout**
  - Setup virtualenv (`python3 -m venv .venv`).
  - Create standardized folder structure:
    ```
    data/loaders/ data/cleaners/ data/store/
    strategies/ backtesting/ risk/ signals/ alerts/ scheduler/ configs/ logs/ tests/
    ```
- [x] **Task 0.2: Pin Dependencies (`requirements.txt`)**
  - Pin versions: `kiteconnect`, `vectorbt`, `pandas-ta`, `pyarrow`, `python-telegram-bot`, `apscheduler`, `loguru`, `pydantic-settings`, `pytest`.
- [x] **Task 0.3: Pydantic Configuration Model (`configs/config.py`)**
  - Define strict settings model reading `.env`:
    - `KITE_API_KEY`, `KITE_API_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_HEALTH_CHAT_ID`, `PORTFOLIO_CAPITAL`.
  - Validate non-empty secrets on application startup.
- [x] **Task 0.4: Structured Logger Setup (`configs/logger.py`)**
  - Configure `loguru` with dual sinks: console + daily rotating file (`logs/artha_{time:YYYY-MM-DD}.log`).

**Definition of Done:**
* `pytest tests/test_config.py` passes.
* Attempting to run without `.env` raises an informative Pydantic validation error.

---

### Sprint 1: Kite Authentication & Session Life Support [COMPLETED ✅]
**Duration:** 2 Days  
**Objective:** Deliver an error-free, interactive token exchange CLI that verifies session validity.

#### Tasks:
- [x] **Task 1.1: Local Redirect Token Capture (`scheduler/token_refresh.py`)**
  - Spawn lightweight local server on `http://127.0.0.1:8080`.
  - Open Zerodha login URL in user's default browser automatically via `webbrowser.open()`.
  - Capture query parameter `?request_token=XYZ` on redirect.
  - Exchange for `access_token` via `kite.generate_session()`.
  - Save session metadata to `kite_session.json`.
- [x] **Task 1.2: Kite Client Factory (`data/loaders/kite_client.py`)**
  - Factory function `get_kite_client() -> KiteConnect`.
  - Checks if `kite_session.json` exists and token expiry date matches today's date.
  - Runs ping `kite.profile()`. If expired, raises `KiteSessionExpiredError`.
- [x] **Task 1.3: Morning Auth Health Check Script (`scheduler/auth_check.py`)**
  - Checks session at 08:30 IST.
  - Sends alert via Telegram if session expired or missing.

**Definition of Done:**
* Developer can run `python -m scheduler.token_refresh`, log in via browser once, and obtain a working session.
* Calling `get_kite_client()` succeeds without manual token copy-pasting.

---

### Sprint 2: Data Ingestion, Quality Checks & Parquet Lake [COMPLETED ✅]
**Duration:** 3 Days  
**Objective:** Ingest 5+ years of historical 1D data for initial universe into atomic Parquet files with rate limits.

#### Tasks:
- [x] **Task 2.1: Rate Limiter & Kite Ingestion (`data/loaders/kite_loader.py`)**
  - Implement token-bucket rate limiter enforcing $\ge 330\text{ ms}$ interval (3 req/sec cap).
  - Implement `fetch_historical_data(symbol, interval='day', from_date, to_date)`.
- [x] **Task 2.2: Initial Universe Configuration (`configs/universe.yaml`)**
  - Populate list of top 20 liquid large-caps with Kite instrument tokens:
    - RELIANCE, TCS, HDFCBANK, INFY, ICICIBANK, BHARTIARTL, ITC, LT, SBIN, HINDUNILVR, etc.
- [x] **Task 2.3: Data Validation Engine (`data/cleaners/validator.py`)**
  - Enforce `High >= Open, Close >= Low`, flag zero-volume candles, detect ATR outlier jumps.
- [x] **Task 2.4: Atomic Parquet Store Manager (`data/store/parquet_store.py`)**
  - Implement `save_bars_atomic(symbol, df, timeframe='1D')`.
  - Implement incremental sync: read latest date in Parquet, fetch missing slice, append, deduplicate, save.

**Definition of Done:**
* Running `python -m data.loaders.sync_history` downloads 5 years of daily data for all 20 universe stocks into `data/store/1D/{symbol}.parquet` without hitting 429 errors.
* Data integrity test validates 0 corrupted rows.

---

### Sprint 3: Strategy Engine & Pine Script Parity Verification [COMPLETED ✅]
**Duration:** 3–4 Days  
**Objective:** Recreate your TradingView strategy in Python and mathematically prove 100% signal parity.

#### Tasks:
- [x] **Task 3.1: Base Strategy Abstract Class (`strategies/base_strategy.py`)**
  - Standardized interface: `calculate_indicators(df)` and `generate_signals(df)`.
- [x] **Task 3.2: Implement Strategy Logic (`strategies/trend_breakout.py`)**
  - Replicate indicators using `pandas-ta` (e.g. Wilder's RMA for RSI/ATR).
  - Strictly enforce `.shift(1)` on indicators to evaluate confirmed closed bars.
  - Generate signals: `signal` (1, -1, 0), `stop_loss`, `target`.
- [x] **Task 3.3: Pine Parity Test Suite (`tests/test_pine_parity.py`)**
  - Load benchmark CSV exported directly from TradingView.
  - Assert indicator delta $< 0.05\%$ on every bar.
  - Assert 100% exact match on entry/exit signal timestamps.

**Definition of Done:**
* Pytest suite passes: Strategy output produces zero mismatched signals when fed TradingView benchmark bars.

---

### Sprint 4: Backtesting Engine with Exact Indian Cost & Tax Model [COMPLETED ✅]
**Duration:** 4 Days  
**Objective:** Backtest the strategy using `vectorbt` net of all Zerodha delivery costs, slippage, and 20% STCG tax vs Nifty 50 TRI.

#### Tasks:
- [x] **Task 4.1: Zerodha Equity Delivery Cost Calculator (`backtesting/cost_model.py`)**
  - Code exact formula: STT (0.1%), Exchange fee, Stamp duty (0.015%), SEBI fee, flat DP charges (₹15.93 per sell), 18% GST.
- [x] **Task 4.2: Slippage Engine (`backtesting/slippage.py`)**
  - Apply 0.05% base slippage for large-caps + market impact scaling.
- [x] **Task 4.3: Capital Gains Tax Drag Model (`backtesting/tax_model.py`)**
  - Compute 20% STCG on closed profitable trades and track net-of-tax equity curve.
- [x] **Task 4.4: VectorBT Backtest Harness (`backtesting/engine.py`)**
  - Run backtest across universe with portfolio reinvestment.
  - Benchmark against Nifty 50 TRI.
  - Compute metrics: Net CAGR, Max Drawdown, Sharpe, Expectancy, Profit Factor.
- [x] **Task 4.5: Walk-Forward & Overfit Validator (`backtesting/walk_forward.py`)**
  - Split data: 70% In-Sample, 30% Out-of-Sample holdout.
  - Ensure OOS Sharpe $\ge 60\%$ of in-sample.

**Definition of Done:**
* Generated backtest tear sheet shows comprehensive performance net of all statutory costs and taxes.
* Strategy either passes all 8 Quantitative Acceptance Gates or is rejected.

---

### Sprint 5: Defensive Risk Engine & Position Sizer [COMPLETED ✅]
**Duration:** 2 Days  
**Objective:** Implement dual-constraint position sizing and portfolio circuit breakers.

#### Tasks:
- [x] **Task 5.1: Position Sizer (`risk/position_sizer.py`)**
  - Implement `calculate_position_size(capital, risk_pct, max_position_pct, entry, stop)`.
  - Enforce `min(qty_risk, qty_cap)`.
- [x] **Task 5.2: Portfolio Circuit Breakers (`risk/circuit_breakers.py`)**
  - Check portfolio drawdown against 15% halt threshold.
  - Enforce maximum 3 active trades per sector.
  - Check 60-day price correlation ($< 0.75$) against active holdings.
- [x] **Task 5.3: Unit Test Risk Engine (`tests/test_risk.py`)**
  - Test tight stop-loss scenario: verify quantity is capped by capital allocation, preventing oversized bets.

**Definition of Done:**
* 100% test coverage on `risk/` module.
* Position sizer never exceeds `max_position_pct` of capital under any input condition.

---

### Sprint 6: Live EOD Scanner, SQLite Ledger & Telegram Bot [COMPLETED ✅]
**Duration:** 3 Days  
**Objective:** Automate the daily scan at 16:00 IST and dispatch actionable Telegram alert cards with zero duplicate spam.

#### Tasks:
- [x] **Task 6.1: SQLite Signal Ledger (`signals/ledger.py`)**
  - Setup SQLite database (`signals/signals.db`).
  - Implement deterministic SHA-256 deduplication on `(symbol, strategy, bar_timestamp)`.
- [x] **Task 6.2: Telegram Notifier (`alerts/telegram_bot.py`)**
  - Build asynchronous client using `python-telegram-bot` or `requests`.
  - Implement formatted Markdown trade card template.
  - Configure error forwarding to separate Health Channel.
- [x] **Task 6.3: Main EOD Workflow Orchestration (`main.py`)**
  - Wire components together: Token check $\rightarrow$ Ingest latest bar $\rightarrow$ Strategy $\rightarrow$ Risk $\rightarrow$ SQLite dedup $\rightarrow$ Telegram.
- [x] **Task 6.4: Scheduler & Watchdog (`scheduler/jobs.py`)**
  - Schedule EOD job at 16:00 IST (Mon–Fri).
  - Schedule Watchdog verification at 16:15 IST.

**Definition of Done:**
* Running `python main.py --dry-run` executes full pipeline and dispatches a test alert card to your Telegram channel.
* Re-running the scan immediately outputs "Duplicate signal skipped" and sends 0 messages.

---

### Sprint 7: Forward Paper Trading & Parity Reconciler [IMPLEMENTED ✅ — 3-MONTH GATE ACTIVE]
**Duration:** 3 Months Forward  
**Objective:** Run live scanner unattended and reconcile live alerts with historical backtest data.

#### Tasks:
- [x] **Task 7.1: Automated Forward Trade Tracker (`signals/reconciler.py`)**
  - Tracks every signal sent in SQLite.
  - Daily EOD check compares simulated execution at next day's open against subsequent high/low/close.
- [x] **Task 7.2: Divergence Audit**
  - Compare paper-traded performance against backtester over identical forward window.
  - Confirm 0% signal divergence.

**Definition of Done:**
* 14 consecutive trading days of unattended execution without a single unhandled crash or missed scan.
* 3 months of paper trade parity confirmed before any real money is committed.

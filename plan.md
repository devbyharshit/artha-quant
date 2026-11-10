# Trading Alert & Backtesting System — Kite Edition (v2, post-review)

> Revised 6 Nov 2026 after a critical financial review. Changes from v1: realistic costs, corrected GTT design, regulatory section, tax-aware backtesting, stricter acceptance criteria, and an MVP-first build order.

---

## 1. Objective

Build a personal system that:
- Replicates your TradingView strategies in Python
- Backtests them net of real Indian costs and tax
- Sends Telegram alerts for BUY/SELL signals on closed candles
- Removes dependency on TradingView alerts

**It is:** a strategy validation system first, an alert system second.
**It is not:** an auto-trading bot, a signal service for other people, or a source of edge by itself. The pipeline only stops you from deploying bad strategies. You still need a strategy worth deploying.

---

## 2. Realistic Cost (v1 said ₹0 — that was wrong)

| Item | Cost | Notes |
|---|---|---|
| Kite Connect (paid) | ₹500/month per API key | Includes live and historical data. The free Personal API has orders/positions/funds only, **no market data**. |
| Brokerage-linked credit | −₹500 if you generate ₹2,000+ brokerage in a month | Effective Oct 2026; credits renew the following month. Only helps if you trade actively. |
| Hosting | ₹300–800/month | A laptop that sleeps cannot run a scanner. Small VPS or always-on machine. |
| Static IP | ₹200–600/month | Only if you place orders via API (see Section 3). |
| **Realistic total** | **~₹800–1,500/month** | Verify the Kite Connect price in your developer console; older posts quote ₹2,000. |

---

## 3. Regulation (SEBI retail algo framework, effective 1 Apr 2026)

- Individuals can run up to 10 orders/second per segment per exchange without exchange registration, but need a **static IP dedicated to the API key**.
- A **read-only alert system** (data in, Telegram out, you place orders manually) very likely sits outside this. **Anything that places orders via API, including GTT**, likely does not. Confirm with Zerodha support before building order-placing code.
- **Do not share your signals with other people** (friends, a Telegram group, paid or free). That moves into investment adviser / research analyst territory, which requires SEBI registration. Personal use is fine.

---

## 4. Build Order — MVP First

Do not build everything. Each stage must pass before the next begins.

| Stage | Scope | Pass criterion |
|---|---|---|
| **MVP** | One strategy, 15–20 liquid large-caps, **daily bars**, end-of-day scan, Telegram alert. No GTT, no WebSocket, no intraday. | Runs unattended for 2 weeks without silent failure |
| **Validate** | Backtest net of full costs and tax vs buy-and-hold Nifty | Beats the index after costs and tax, 100+ trades, survives holdout |
| **Paper trade** | 3+ months, compare live signals to backtest signals bar by bar | Signals match; results within expected range |
| **Small real money** | Only after paper trading passes | Position caps enforced |
| **Later (optional)** | Intraday, KiteTicker WebSocket, extra strategies, Optuna, Monte Carlo, order automation | Only once a strategy has proven itself |

Before building: check whether **Streak** (Zerodha), **Chartink** alerts, or TradingView's free tier already cover your need.

---

## 5. Tech Stack

| Component | Tool | Notes |
|---|---|---|
| Indian data | `kiteconnect` | Requires paid Kite Connect |
| Crypto data | `python-binance` | Optional, add later |
| US stocks | `yfinance` | Optional, add later |
| Indicators | `pandas-ta` (+ `ta-lib` if needed) | |
| Backtesting | `vectorbt` | |
| Storage | Parquet (`pyarrow`) + SQLite | SQLite for signal log and dedup |
| Alerts | `python-telegram-bot` | |
| Scheduler | `APScheduler` or cron | |
| Config | `python-dotenv` + YAML | |
| Logging | `loguru` | |
| Later only | `optuna`, Jupyter + `plotly` | Skip until a strategy survives validation |

```bash
pip install kiteconnect vectorbt pandas-ta pyarrow python-telegram-bot apscheduler loguru python-dotenv
```

---

## 6. Project Structure

```
project/
├── data/
│   ├── loaders/        # kite_loader.py (add binance/yfinance later)
│   ├── cleaners/       # validator.py, adjustments.py
│   └── store/          # Parquet, partitioned by symbol + timeframe
├── strategies/         # base_strategy.py + one file per strategy
├── backtesting/        # engine.py, cost_model.py, slippage.py, walk_forward.py
├── signals/            # engine.py, dedup.py, reconcile.py
├── risk/               # position_sizer.py (with caps)
├── alerts/             # telegram_bot.py, formatter.py
├── scheduler/          # token_refresh.py, jobs.py
├── configs/            # .env, assets.yaml, strategies.yaml
└── main.py
```

---

## 7. Data Layer

### Kite API facts (verify against current docs before relying on them)

| Item | Value |
|---|---|
| Historical API rate limit | 3 requests/sec — wrap all calls in a rate limiter |
| Token expiry | Daily, around 6am IST — refresh is mandatory |
| Minute-data history limits | **Not verified** — read the current historical API docs |
| WebSocket instrument cap | Per-connection limits apply — check docs before scaling |
| Expired option contracts | Not available for intraday history |

### Token refresh (build first)

If your scanner runs unattended, an expired token fails silently. The safest approach is a manual daily login that stores the session: open the login URL, complete it once, paste the `request_token`, and let the script exchange it.

```python
# scheduler/token_refresh.py
from kiteconnect import KiteConnect
import os, json

def exchange_token(request_token: str):
    kite = KiteConnect(api_key=os.getenv("KITE_API_KEY"))
    data = kite.generate_session(request_token, api_secret=os.getenv("KITE_API_SECRET"))
    json.dump(data, open("kite_session.json", "w"))
    return data["access_token"]
```

Fully automated TOTP login sits in a grey area with Zerodha's terms; do not assume it is allowed.

### Fetching historical data

```python
# data/loaders/kite_loader.py
from datetime import datetime, timedelta
import pandas as pd, time

def fetch_ohlcv(kite, instrument_token, interval, days_back):
    to_date = datetime.now()
    from_date = to_date - timedelta(days=days_back)
    time.sleep(0.35)  # stay under 3 requests/sec
    data = kite.historical_data(instrument_token, from_date, to_date, interval)
    df = pd.DataFrame(data).set_index("date")
    return df  # open, high, low, close, volume
```

### Corporate actions — do not assume adjusted data

Before trusting any series, test how Kite handles splits and bonuses on a stock where you know the history. If prices are unadjusted, apply adjustments yourself. A single unadjusted split looks like a 50% crash and will corrupt a backtest.

### Data quality checks

- Gaps vs NSE trading calendar
- Outliers (high/low spread > 3× ATR)
- Zero-volume bars flagged, not silently kept
- `high ≥ open, close ≥ low` on every bar
- Stale-data check before every scan

---

## 8. Strategy Migration (Pine Script → Python)

- Recreate indicators with `pandas-ta`; compare values against TradingView bar by bar.
- Only confirmed bars may trigger signals (`barstate.isconfirmed` equivalent).
- Watch Pine quirks: `security()` lookahead, `[1]` history referencing (`shift(1)`), calculation on bar close vs every tick.
- Expect small data differences between Kite and TradingView; define an acceptable tolerance up front.
- Budget more time here than in any other stage.

---

## 9. Backtesting

### Full cost model (Zerodha, equity delivery) — verify on Zerodha's charges page

| Cost | Approx. value |
|---|---|
| Brokerage | Free on delivery; ₹20 or 0.03% (lower) per order for intraday |
| STT | 0.1% on buy and sell (delivery); 0.025% on sell (intraday) |
| Exchange charges (NSE) | ~0.003% |
| SEBI charges | ₹10 per crore |
| Stamp duty | ~0.015% on buy (delivery) |
| DP charges | Flat fee per scrip on delivery sells, plus GST |
| GST | 18% on brokerage + exchange + SEBI charges |
| Slippage | See below |

### Slippage by liquidity tier

| Tier | Assumption |
|---|---|
| Nifty 50 / large-cap | 0.05–0.1% |
| Midcap | 0.2–0.5% |
| Smallcap / illiquid | 0.5–2%+, or exclude (skip if average daily volume is too low to fill) |

```
slippage = base_bps + (order_size_₹ / avg_daily_volume_₹) × impact_factor
```

### Tax (confirm current rates; they change in budgets)

At the last rates I am confident of: short-term capital gains 20%, long-term 12.5% above ₹1.25 lakh per year; intraday profit is business income at your slab. Frequent trading means mostly short-term gains. **Report results net of tax and compare against simply holding a Nifty index fund.**

### Market constraints

- Circuit limits (5/10/20%) can prevent exits
- T+1 delivery settlement
- Survivorship bias: today's Nifty members are today's winners. Use point-in-time constituents or state plainly that results are inflated

### Acceptance criteria (a strategy must pass all)

| Test | Requirement |
|---|---|
| Sample size | 100–200+ trades |
| Benchmark | Beats buy-and-hold Nifty **after costs and tax** |
| Payoff | Judge win rate together with average win / average loss, never alone |
| Holdout | Final period untouched until one single test |
| Multiple testing | Count every variant tried; apply a deflated Sharpe or equivalent correction |
| Walk-forward | Windows long enough to contain enough trades; OOS Sharpe consistently ≥ ~60% of in-sample |
| Regimes | Reviewed across bull, bear and sideways periods |
| Parameters | ≤3 free parameters |

If results degrade sharply out of sample, the strategy is curve-fitted. Reject it.

---

## 10. Risk Rules

### Position sizing — two limits, both must hold

```python
# risk/position_sizer.py
def position_size(portfolio, risk_pct, max_position_pct, entry, stop):
    risk_qty = int((portfolio * risk_pct / 100) / abs(entry - stop))
    cap_qty = int((portfolio * max_position_pct / 100) / entry)
    return min(risk_qty, cap_qty)

# defaults: risk_pct = 1.0–1.5, max_position_pct = 10–15
```

Risk-per-trade sizing alone produces oversized positions when stops are tight. Example from v1: 18 shares with a ₹70 stop at 1.5% risk implied an ₹84,000 portfolio but put ~60% of it in one stock. The cap fixes this.

### Other rules

| Rule | Threshold |
|---|---|
| Drawdown circuit breaker | Pause new alerts at −15% from peak |
| Correlation filter | Block second signal if 60-day correlation > 0.75 |
| Sector cap | Max 3 open signals per sector |
| India VIX | Reduce size when elevated; thresholds are judgement calls — backtest them, do not assume |
| Margin check | Confirm required margin vs available (Kite margins API) before alerting |

---

## 11. Signal Engine & Alerts

- Closed candles only; deduplicate by hash of (symbol, strategy, bar timestamp) in SQLite.
- Cap alerts per session to avoid fatigue.
- **Confidence score is a hypothesis, not a feature.** Do not rank by an invented score until you have shown that higher scores precede better trades in your own data. Until then, rank by simple, auditable criteria.
- **Reconcile live and backtest candles.** Candles built from WebSocket ticks can differ from the historical API's candles (volume, last tick). Compare them before trusting live intraday signals.

### Alert format

```
BUY SIGNAL — RELIANCE (NSE)
Strategy  : <name>
Timeframe : 1D (closed candle)
Entry ref : ₹2,850 (last close)
Stop      : ₹2,780 (−2.5%)
Target    : ₹3,050 (+7%)
Suggested qty : 12 (risk 1.0%, position ≤ 10% of capital)
Margin needed : ₹34,200 (available: ₹1,24,000)
```

You place the order and the stop yourself.

### Stop-loss: GTT limitations (corrected from v1)

- A **sell GTT requires shares you already hold**, so it can only be placed after the buy fills.
- GTT works for **delivery (CNC) only**, not intraday (MIS).
- A triggered GTT executes only if its limit order fills. On a gap-down it may trigger and still not fill, and you must then place it again.
- Treat GTT as a convenience, not guaranteed protection. If you automate it, that is order placement via API and the SEBI rules in Section 3 apply.

---

## 12. Automation

- MVP: one daily cron job after market close, plus a daily health check
- Telegram alert if no scan has run by an expected time
- Log exceptions to file; forward critical errors to a separate Telegram chat
- Intraday scans and KiteTicker come later, only after the daily system has run cleanly

```python
# scheduler/jobs.py (MVP)
from apscheduler.schedulers.blocking import BlockingScheduler
s = BlockingScheduler(timezone="Asia/Kolkata")
s.add_job(eod_scan,     "cron", hour=16, minute=0,  day_of_week="mon-fri")
s.add_job(health_check, "cron", hour=17, minute=0,  day_of_week="mon-fri")
s.start()
```

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| Stale or expired Kite token | Daily token step; health check |
| Overfitting / data snooping | Trade-count minimum, holdout, multiple-testing correction |
| Unadjusted corporate actions | Test on known cases; adjust manually if needed |
| Survivorship / look-ahead in universe | Point-in-time constituents or disclose inflation |
| Backtest vs live divergence | Paper trading; reconcile candles |
| Oversized positions | Hard per-position cap |
| Gap-down beyond stop | Smaller size; do not rely on GTT |
| Regulatory | Personal use only; confirm API/order rules with Zerodha |
| Tax drag | Report net-of-tax results vs index fund |

---

## 14. Future Scope (only after a strategy passes validation)

- Intraday scanning with KiteTicker
- Order automation (subject to SEBI static IP and registration rules)
- Additional strategies and asset classes
- Dashboard

---

## 15. Principle

Edge comes from **testing, validation and consistency**, not from infrastructure. The question the system exists to answer is: *does this strategy beat simply holding the index, after costs, tax and honest out-of-sample testing?* If the answer is no, the right output is to stop.

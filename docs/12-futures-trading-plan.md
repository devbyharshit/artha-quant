# 12 — Futures Trading: Design

**Status:** Built and integrated (2026-11-10). The original Kite-only data-availability blocker (§5, first run) is **resolved** (§5, 2026-11-10 follow-up): the data layer now sources real multi-year historical prices from NSE's own bhavcopy archives, confirmed by a real run producing 744/751 tradeable overlay days per symbol (vs. 12 total before). The follow-up run still produced zero trades, but for a *different, now well-understood* reason — a position-sizing/capital-parameter mismatch (§5), not missing data. This framework is **still not yet validated** — not because of insufficient data anymore, but because the default capital/margin/allocation parameters make every large-cap single-stock-futures lot in this test unaffordable. This is not a "strategy failed" result like `docs/10`/`docs/09` either — it's "data gap closed, next blocker identified and precisely diagnosed."

## 1. Why this exists

`range_filtered_trend` and `gaussian_channel` passed cash-equity delivery walk-forward validation (`docs/10-strategy-validation-findings.md` §4). The next hypothesis to test, structurally different from both the single-stock cash-equity work and the pairs-trading pivot: **run the same validated signal logic on NSE stock futures instead of cash delivery**, and see whether the picture changes once execution moves to an instrument built for this (no overnight-short restriction relevant here since these are long-only strategies, but real leverage, real lot-size granularity, and a different cost/tax regime).

**Explicitly not a new hypothesis about the signal itself** — the four existing strategies' `calculate_indicators`/`generate_signals` logic is reused completely unchanged. Only the execution venue, position sizing, cost model, and tax treatment change.

## 2. Scope

**Goals**
- Backtest `trend_breakout`, `range_filtered_trend`, `gaussian_channel`, and `mean_reversion` — unmodified — on NSE single-stock futures (NFO segment), with a cost/margin/lot-size model realistic enough to trust the result.
- Reuse `WalkForwardValidator`'s gate philosophy so results are comparable to the cash-equity findings in `docs/10`.
- Keep this additive: no changes to any existing cash-equity code path.

**Non-goals (this phase)**
- Tax modeling. F&O P&L is taxed as **business income** at the trader's personal slab rate, not capital gains — this system has no model of a user's overall income/slab, and fabricating one (e.g. assuming a flat 30% bracket) would be a guess dressed up as a number. No tax is applied, same proxy posture as the pairs engine, and labeled the same way in every report.
- Margin-efficiency optimization. Real margin (SPAN + exposure) is computed by the exchange/broker daily and varies by symbol and volatility; it isn't available for historical backtesting. Phase 1 uses a flat approximation (below) specifically to get a usable, non-trivial backtest — refining it into a realistic margin model is an explicit follow-on, not part of this phase.
- Shorting. The four strategies are long-only by construction; futures *enable* a short leg, but adding one is a different, separate hypothesis (a genuinely new signal: "do these indicators have a mirror-image short edge"), not part of "run the same strategies on futures."
- Intraday/MIS. EOD daily bars only, matching every other strategy in this system.

## 3. Design decisions

### 3.1 Contract handling — indicators on cash history, execution on the active futures contract

A single NFO futures contract is only listed for its own ~2-3 month life (near/next/far month), nowhere near enough history for these strategies' warm-up (`gaussian_channel`'s/`range_filtered_trend`'s 200-bar WMA, `range_filtered_trend`'s 87-period recursive Kalman filter, etc.). So indicator/signal calculation is **not** done on futures price history at all — it runs on the same deep cash-equity history this system already has synced (`calculate_indicators`/`generate_signals`, completely unchanged). Only **execution** (fills, P&L, lot-size sizing, costs, forced expiry exit) happens against futures prices.

Concretely: for every date in the cash-equity backtest window, there is one "active" (front-month) futures contract, resolved by rollover rule (see below), with its own open/high/low/close for that date. The strategy's signal — computed from cash history — is read at a bar's close exactly as today, but **filled on the active futures contract's own next-bar open**, not the cash price. Any open position is **forced closed 3 trading days before the active contract's expiry** (a conservative, liquidity-respecting rollover buffer), and a fresh entry on the new front-month contract follows if the (cash-computed) signal is still active. This is why `FuturesBacktestEngine` takes the cash OHLCV (for the strategy) and a parallel futures-overlay table (for execution) rather than a futures-only series — no back-adjusted continuous splice is needed or used.

**Known risk, not yet resolved:** Kite's instrument catalog may only expose currently-listed-and-upcoming NFO contracts, not a deep historical record of expired contracts' tokens/lot-sizes. If true historical per-contract data isn't obtainable, the data-layer builder should report that clearly rather than silently approximating — see the data-layer task.

### 3.2 Position sizing — lot-size-rounded, against an approximate margin requirement

Futures are traded in fixed lot sizes (e.g. "500 shares" per `RELIANCEFUT` lot, size set by the exchange and occasionally revised) — not continuous share quantities. Sizing must round to whole lots.

**Margin approximation:** capital required per lot is modeled as **20% of the lot's notional value** (a simple flat-rate stand-in for real SPAN + exposure margin, which commonly runs ~10-20% for liquid large-cap single-stock futures, but is volatility- and symbol-dependent in reality). This is a deliberate, named approximation — real margin isn't available for historical dates. Sizing against *unleveraged full notional* was considered and rejected: at this system's typical ₹100,000/symbol capital, most large-cap futures lots cost far more than that in full notional (e.g. a `RELIANCE` lot can run ₹700,000+), which would make nearly every signal un-affordable and the backtest uninformative. A flat margin approximation is what makes this phase's result mean anything at all, and is named as an approximation everywhere it's used, not quietly assumed.

Sizing otherwise reuses the existing `risk_based`/`full_equity` modes' budget logic, just rounding the resulting quantity down to the nearest whole lot (0 if less than one lot fits).

### 3.3 Cost model — new, futures-specific statutory rates

Reuses the shape of `IndianCostModel` (one `calculate()` classmethod, itemized `TradeCostBreakdown`) but with NSE equity-derivatives rates, which differ from cash-equity delivery:
- STT: **0.05% on the sell side only** (vs. 0.1% both sides for cash delivery). This was raised from 0.02% by the Finance Act 2026, effective 2026-04-01 (NSE circular FATAX73524) — the build initially used a stale 0.0125% estimate, corrected after a web-search check during integration.
- Brokerage: Zerodha F&O futures is ₹20 flat or 0.03% of turnover, whichever is lower, per executed order (vs. ₹0 for cash delivery).
- Exchange transaction charges, SEBI charges, stamp duty (buy side only), GST: same statutory structure as cash equity, different (lower) published rates for the F&O segment.
- No DP charges (F&O isn't held in a demat holding).

### 3.4 Validation bar

New `FuturesWalkForwardValidator`, reusing `WalkForwardValidator`'s train/test split, fold structure, and the **same gates and thresholds** as the single-stock cash-equity validator (including the Nifty buy-and-hold benchmark gate — these are still long-only, directional, single-stock strategies, same comparison basis as `docs/10`, unlike the market-neutral pairs case).

## 4. Architecture

```
data/loaders/
├── futures_instruments.py   # underlying symbol -> list of historical NFO contracts (token, tradingsymbol, lot_size, expiry)
└── sync_futures_history.py  # per-contract historical sync, reusing KiteDataLoader; builds the daily futures-overlay table

backtesting/
├── futures_cost_model.py    # IndianFuturesCostModel (§3.3)
├── futures_engine.py        # FuturesBacktestEngine (§3.1-§3.2): takes cash OHLCV (for the strategy, unchanged) + the futures-overlay table (for execution)
├── futures_walk_forward.py  # FuturesWalkForwardValidator (§3.4)
└── run_futures_walk_forward.py  # CLI
```

The **futures-overlay table** is the interface between the data layer and the engine: a DataFrame indexed by date (same index as the cash OHLCV) with columns `fut_open, fut_high, fut_low, fut_close, lot_size, expiry, tradingsymbol`, one row per trading day, reflecting whichever contract is front-month-active that day under the rollover rule. Dates where no contract data is available (e.g. before futures existed for that symbol) are left out — the engine simply can't trade those dates and should skip them, not error.

## 5. Integration results (2026-11-10)

Built in three parallel chunks (data layer, cost model, engine/validator), then integrated and smoke-tested end to end against real data (`python -m backtesting.run_futures_walk_forward --strategy range_filtered_trend --symbols RELIANCE TCS HDFCBANK`). 163/163 tests passing across the whole repo.

**Confirmed blocker, not hypothetical:** Kite's `kite.instruments("NFO")` catalog only returns currently-listed-and-upcoming contracts — for RELIANCE, exactly 3 (Nov 2026, Dec 2026, Jan 2027), nothing older. There is no way to fetch genuinely historical NFO futures OHLCV through Kite; it was never retained. The data layer (`data/loaders/futures_instruments.py`, `sync_futures_history.py`) handles this gracefully (an approximated last-Thursday-of-month expiry calendar, current lot size applied retroactively, degrades to "no data" rather than erroring) — but an approximated *calendar* doesn't manufacture historical *prices* that were never captured while a contract was live.

**Concrete effect:** building the overlay table for RELIANCE across its full 2011-2026 cash history produced exactly **12 tradeable days**, all within the last two weeks (2026-10-26 to 2026-11-09) — the life of the two most recent real contracts. Every other date across 15 years of cash history has no futures price data at all. The CLI run above completes correctly (no crashes, correct plumbing) but every gate fails on zero trades — not because the signal doesn't work, but because there's effectively no data to trade on yet.

**One real bug found and fixed during integration** (none of the three agents' own tests caught it, since each used simpler synthetic fixtures): a tz-aware (cash history, `Asia/Kolkata`) vs. tz-naive (approximated contract expiry) `Timestamp` comparison crashed `select_active_contract` the first time it ran against real data. Fixed in `sync_futures_history.py`, regression test added.

**What would actually close this gap:** NSE publishes free historical F&O daily bhavcopy archives (public, by date, no API key, going back years) — the standard workaround for exactly this Kite limitation. Building an ingester for that is a distinct, scoped follow-on (new data source, new parsing, mapping bhavcopy's own contract/symbol convention to this system's), not a tweak to what's here. Until that exists (or enough live-contract history simply accumulates over months of real usage), this framework has nothing meaningful to validate.

### 5.1 Follow-up (2026-11-10): NSE bhavcopy wired in — data gap closed, a new blocker found

`data/loaders/nse_bhavcopy.py` (a from-scratch, live-verified ingester for NSE's public historical F&O bhavcopy archives, covering ~2011-present) was wired into `build_futures_overlay` (`data/loaders/sync_futures_history.py`), replacing Kite as the price source entirely. `get_historical_futures_bars(symbol, start, end)` is called once per symbol for the whole requested window (it caches per trading day on disk internally) and reshaped in-memory into the per-day candidate lists the existing `select_active_contract` rollover rule consumes — that rule itself is unchanged. Real exchange lot sizes are used wherever NSE's archive carries them (its current UDiFF format, from 2024-07-08 onward); for earlier dates, where NSE's own legacy archive format never published lot size at all, the overlay builder falls back to **the current lot size** for the symbol, resolved via `futures_instruments.get_futures_contracts`'s existing Kite-catalog lookup (reused, not reimplemented) — the same style of approximation this system already used elsewhere, now scoped to only the pre-2024-07 gap instead of the entire history. Kite is no longer used for any OHLC price at all; it is only consulted for that lot-size fallback.

**Real run, scoped to a representative 3-year window (2023-11-01 → 2026-11-09, straddling the 2024-07-08 NSE legacy/UDiFF format cutover) rather than the full 2011-2026 history** — a cold full-history backfill is many thousands of individual NSE HTTP requests at NSE's observed ~1-1.5s/request pace (with occasional connection timeouts handled by the module's existing retry/backoff), which is unreasonably slow for one verification pass; the on-disk bhavcopy cache this run populated makes any future run (full-history or otherwise) for these symbols dramatically faster, since only new dates need a real fetch:

- **Overlay rows:** RELIANCE 744, TCS 744, HDFCBANK 744 — out of 751 cash-equity trading days in the window (**2,232 total overlay rows**, vs. **12 total** in the original Kite-only run across all of history). Overlay date range matched the cash window exactly: 2023-11-01 → 2026-11-09, including "today" (2026-11-09) with no gap, confirming NSE's bhavcopy has no meaningful publication lag.
- **Lot sizes seen were real, multiple, and changed over time** (exactly the historical-revision behavior the old always-current-lot-size approximation couldn't capture): RELIANCE 250 and 500, TCS 175 and 225, HDFCBANK 550, 650, and 1100 — confirming the UDiFF-vs-fallback lot-size path works and that real lot-size revisions are now visible for the first time.
- **36 distinct monthly contracts per symbol** appeared across the window (e.g. `RELIANCE23NOVFUT` through `RELIANCE26SEPFUT`), i.e. real month-to-month rollover across ~3 years, not a handful of weeks.
- **Walk-forward split:** in-sample 2024-08-21 → 2025-05-05, out-of-sample 2025-05-06 → 2026-11-09, 4 six-month OOS folds.
- **Trades: 0 in-sample, 0 out-of-sample, for all three symbols.** All 7 gates evaluated meaningfully this time (not trivial zero-data skips) and all failed except "Max drawdown" (0.0%, vacuously within the 20% limit): Sample size (0 OOS trades, need 100), Beats Nifty buy-and-hold (OOS Sharpe 0.00 vs Nifty 0.25), Walk-forward retention (0.00 vs in-sample 0.00), Profit factor (0.00, need 1.30), Expectancy (+0.00%, need +0.20%), Consistency across folds (0/4 positive).

**Root cause of the zero trades, confirmed by direct calculation, is not a data problem:** `calculate_indicators`/`generate_signals` on the same trimmed cash history *did* produce real, non-trivial signals (RELIANCE 7 signal flips, TCS 9, HDFCBANK 11, over the 3-year window, several landing inside the OOS period — e.g. RELIANCE long 2025-10-27 → short 2026-01-08). The reason none became an executed futures trade is `calculate_futures_position_size` (`backtesting/futures_cost_model.py`): at the CLI's default `capital_per_symbol=₹100,000`, `allocation_pct=20%`, `margin_pct=20%`, the per-lot margin for these large-cap symbols at realistic current prices vastly exceeds the ₹20,000 capital budget allotted per position — e.g. RELIANCE @ ₹1,450/share × 500-share lot × 20% margin = ₹145,000 required for just **one** lot, against a ₹20,000 cap (confirmed directly: `calculate_futures_position_size(100000, 20.0, 1450.0, 500)` → `num_lots=0`). The same is true for TCS (₹112,000 required per lot) and HDFCBANK (₹110,000 required per lot) at representative prices. Every signal in the window was therefore sized down to 0 lots — this is a deterministic consequence of the sizing formula given these parameters and today's real prices, not a bug and not a data gap.

**What this means:** the original data blocker is fully closed — the overlay table now has years of real NSE futures prices, correct real lot sizes where NSE publishes them, and correct rollover across real contract sequences. The next thing standing between this framework and an actual walk-forward verdict is **capital/margin/allocation-parameter tuning** (§3.2's "left to the builder" already flagged the margin approximation as a stated default, not an independently derived one) — either a larger `capital_per_symbol`, a higher `allocation_pct`, or revisiting whether position sizing should be per-lot-affordable at all for single large-cap stocks at this capital scale, is a distinct, scoped follow-on from here, not a data-layer change.

## 6. Left to the builder

- Exact rollover buffer (3 trading days) and margin approximation (20% of notional) are stated defaults, not independently derived — fine to adjust with a documented reason (e.g. too many forced-exit-before-reentry whipsaws at 3 days; too few affordable lots at 20%).
- Whether `FuturesBacktestEngine` shares code with `BacktestEngine` via a common base, or is written standalone — pick whichever keeps the lot-size/rollover logic clearly separate from the cash-equity engine's intrabar stop logic (these engines solve different problems and forcing a shared base class is not required).
- Test coverage shape: unit tests for lot-size rounding, forced-exit-before-expiry, and cost calculation at minimum, following this repo's existing `tests/` conventions (see `tests/test_walk_forward.py`'s `bars()`/`Scripted` pattern).

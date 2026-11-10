# 09 — Pairs / Relative-Value Trading: Plan (Draft, Brainstorming Checkpoint)

**Status:** Not yet designed in full — brainstorming was started and paused. This document is a resume point, not an approved spec. Nothing in this doc has been built yet.

## 1. Why this exists

Four single-stock technical strategies were built and walk-forward validated against a 53-symbol NSE large-cap universe (`configs/universe.yaml`):

| Strategy | Premise | Result |
|---|---|---|
| `trend_breakout` | Donchian breakout + 200-EMA filter + ATR stop | OOS profit factor collapsed to ~0.2-0.3 |
| `range_filtered_trend` | Kalman filter + Supertrend regime (Pine port) | Looked strong in-sample (+8.6% avg), OOS profit factor 1.68→0.34 pooled; 0.09 on the best-sector subset |
| `gaussian_channel` | N-pole Gaussian filter + Stoch RSI (Pine port) | Net negative, OOS profit factor ~1.0 |
| `mean_reversion` | Bollinger lower-band dip + 200-EMA trend filter + ATR stop | Roughly flat/negative, OOS profit factor 0.23-0.26 |

All four failed decisively under `backtesting/walk_forward.py`'s 70/30 IS/OOS split, even when restricted to the single sector subset (Utilities/Telecom/Energy/Automobile) that looked best across all four. See `backtesting/run_walk_forward.py` and the analysis session history for the full evidence trail. Conclusion reached: there is no demonstrated single-stock technical edge on this universe/timeframe with the approaches tried so far.

**The pivot:** test a structurally different hypothesis — relative value between two correlated/related stocks (pairs trading) — rather than another single-stock indicator variant, per `plan.md`'s own philosophy of changing the question rather than re-tuning the same one.

## 2. What's already decided

- **Research-first scope.** Classic pairs trading needs a simultaneous long leg + short leg held for days/weeks. Indian cash-equity rules don't allow retail to hold an overnight short (only intraday MIS square-off, or going through F&O/stock futures for a real multi-day short) — and F&O has entirely different cost (margin, lot size, futures brokerage/STT) and tax treatment (business income, not capital gains) than everything this system currently models (`backtesting/cost_model.py`, `backtesting/tax_model.py` are both cash-equity-delivery-only).
- **Decision:** build the research/signal-validation version first — backtest the long/short spread as a proxy using the existing cash-equity infrastructure (acknowledging this isn't directly executable as-is), to cheaply test whether relative-value has real OOS edge at all. Only invest in F&O-aware cost/tax/margin modeling if the signal actually survives walk-forward. This mirrors how `range_filtered_trend`/`gaussian_channel` were tested with `full_equity` sizing as a faithful-to-source proxy before any deployment-realism work.
- **Reuse, don't rebuild:** `WalkForwardValidator`, the Parquet data store, `configs/universe.yaml`'s sector tags, and the existing 53-symbol (soon more, see §4) universe should all carry over. The backtest *engine* itself will likely need a new variant (see open questions) since `BacktestEngine` is single-symbol, single-leg, long-only.
- The F&O-vs-research-only question was explicitly parked by the user ("let's not get into that") — treat as research-first per above unless revisited.

## 3. Open design questions (not yet resolved — pick up here)

1. **Pair selection method.** Same-sector correlation (simple, using existing sector tags)? Cointegration test (Engle-Granger / ADF on the spread, more rigorous but another "free parameter" to justify)? Fixed candidate list the user already has in mind (e.g. ONGC/IOC, ICICIBANK/AXISBANK, TATASTEEL/JSWSTEEL)?
2. **Spread definition.** Simple price ratio? Log-price difference? Regression-based hedge ratio (rolling OLS beta)? Needs to be picked with an eye toward what's cheap to compute and easy to walk-forward-validate without it becoming another source of overfitting (pair selection itself is a form of multiple-comparisons risk — picking "the best pair after the fact" is survivorship bias, same trap flagged for `range_filtered_trend`'s winning-stock concentration).
3. **Entry/exit signal.** Likely a z-score of the spread over a rolling lookback (enter at ±2, exit at reversion to 0, stop if the spread keeps diverging past e.g. ±3.5) — needs confirming, and needs a bounded max-holding-days exit like `mean_reversion` has, since spreads can fail to revert.
4. **Backtest engine shape.** Does this need a new `PairsBacktestEngine` (two simultaneous legs, combined P&L, one capital account), or can the existing single-symbol `BacktestEngine` be reused per-leg with manual P&L combination? Needs a concrete small design before writing code.
5. **What "proxy" actually means for the short leg's cost model**, even in research-first mode — do we just mirror the long leg's cost structure for the short leg (acknowledging it's not realistic), or zero out costs on the short leg specifically to be explicit it's a placeholder? Needs an explicit, documented choice so results aren't misread as deployment-realistic.
6. **Validation bar.** Same `plan.md`/`WalkForwardValidator` gates as the single-stock strategies, or does pairs trading need different thresholds (e.g. a different Nifty-hurdle benchmark, since a market-neutral pairs strategy shouldn't be benchmarked against absolute index CAGR the same way a long-only strategy is)?

## 4. Related in-flight work

- `configs/universe.yaml` was expanded from 20 to 53 symbols to fix a sample-size problem for the single-stock strategies; this universe (plus sector tags) is the natural starting point for pair candidates too.
- `make add-holdings` (`data/loaders/add_holdings_to_universe.py`) pulls the user's real Kite holdings and merges missing symbols into `universe.yaml` — run after `make auth` (session was expired as of this writing). Once run, the user's actual holdings become part of the candidate universe for both single-stock and future pairs testing.

## 5. Next step

Resume brainstorming on the 5 open questions in §3, then size the work (likely "a project with a written design" per the brainstorming skill, since it needs a new backtest engine component) before writing any code.

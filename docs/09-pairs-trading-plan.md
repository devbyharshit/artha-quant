# 09 — Pairs / Relative-Value Trading: Design

**Status:** Built and tested (2026-11-10). Result: no OOS edge found under either selection method — see `docs/10-strategy-validation-findings.md` §9 for the full result. This document remains the design record; it is not superseded, just concluded.

## 1. Why this exists

Four single-stock technical strategies were built and walk-forward validated against a 53-symbol NSE large-cap universe (`configs/universe.yaml`):

| Strategy | Premise | Result |
|---|---|---|
| `trend_breakout` | Donchian breakout + 200-EMA filter + ATR stop | OOS profit factor collapsed to ~0.2-0.3 |
| `range_filtered_trend` | Kalman filter + Supertrend regime (Pine port) | Looked strong in-sample (+8.6% avg), OOS profit factor 1.68→0.34 pooled; 0.09 on the best-sector subset |
| `gaussian_channel` | N-pole Gaussian filter + Stoch RSI (Pine port) | Net negative, OOS profit factor ~1.0 |
| `mean_reversion` | Bollinger lower-band dip + 200-EMA trend filter + ATR stop | Roughly flat/negative, OOS profit factor 0.23-0.26 |

All four failed decisively under `backtesting/walk_forward.py`'s 70/30 IS/OOS split, even when restricted to the single sector subset (Utilities/Telecom/Energy/Automobile) that looked best across all four. Conclusion reached: there is no demonstrated single-stock technical edge on this universe/timeframe with the approaches tried so far.

**The pivot:** test a structurally different hypothesis — relative value between two correlated/related stocks (pairs trading) — rather than another single-stock indicator variant, per `plan.md`'s own philosophy of changing the question rather than re-tuning the same one.

## 2. Scope

**Goals**
- Determine whether a market-neutral, relative-value signal has any real out-of-sample edge on this universe — a genuinely different question from the four rejected single-stock strategies, not a variant of them.
- Reuse the existing data layer, cost model, and walk-forward methodology rather than building a parallel system.
- Keep pair selection itself walk-forward-honest: selecting "the best pair after the fact" is the same survivorship/multiple-comparisons trap already flagged for `range_filtered_trend`'s winning-stock concentration, so selection must be frozen on in-sample data before OOS is ever touched.
- Compare two pair-selection methods (same-sector correlation vs. Engle-Granger cointegration) empirically, since that's the one open question genuinely worth testing rather than deciding by assumption.

**Non-goals (this phase)**
- Real execution. Retail Indian cash-equity rules don't allow holding an overnight short; a real implementation needs F&O (futures) with entirely different margin, cost, and tax treatment. This phase explicitly does not model that — see §6.
- Tax modeling for the short leg. `IndianTaxEngine`'s STCG/LTCG capital-gains model assumes long-only delivery; it has no defined meaning for a short leg that isn't actually executable as cash equity. Tax is left out of the pairs engine entirely (documented placeholder, not computed as zero-by-omission).
- Parameter optimization / grid search over spread definitions, z-score thresholds, or lookback windows. One sensible, literature-standard configuration is used; if the signal shows real edge, tuning becomes a legitimate next step — tuning a dead signal is not.
- Changes to the single-stock engine, strategies, or their walk-forward results.

**Anti-goal:** shipping a result that reads as "pairs trading works" when what actually happened is costs were favorably mis-modeled on the short leg, or the best-looking pair was picked with OOS hindsight. Every place where realism was knowingly sacrificed for research speed must be named in the output, not buried.

## 3. Design decisions (resolves the prior open questions)

### 3.1 Pair selection — test both methods, selected in-sample only

Two independent candidate-generation methods, both restricted to same-sector pairs (same economic rationale as the user's own example pairs — ONGC/IOC, ICICIBANK/AXISBANK, TATASTEEL/JSWSTEEL — and it bounds the combinatorial search):

- **Correlation method:** daily log-return correlation over the in-sample window; keep pairs above a threshold (default 0.60 — measured against this universe's actual same-sector in-sample correlations, which top out around 0.67 (`BPCL`/`IOC`); 0.75 was the original literature-default guess but left zero candidates, so it was lowered and is recorded here per §7's standing permission to adjust a threshold that leaves too few candidates).
- **Cointegration method:** Engle-Granger test (`statsmodels.tsa.stattools.coint`) on log prices over the in-sample window; keep pairs with p-value < 0.05.

Both methods compute their candidate list using **only the in-sample window** (same split point `WalkForwardValidator` already uses), then freeze that list before touching OOS data — exactly mirroring how the single-stock strategies' train/test split already works, so pair selection cannot see the data it will later be judged on. Each method's resulting pairs are walk-forward tested independently and reported side by side; whichever (if either) survives the gates in §5 is the finding.

Two refinements to keep the candidate universe honest:
- **No overlapping symbols.** Same-sector search can propose a symbol into more than one candidate pair (e.g. ICICIBANK with both AXISBANK and HDFCBANK). Selection is greedy: candidates are ranked by selection-metric strength (correlation, or cointegration p-value), and once a symbol is used in an accepted pair it's removed from the remaining pool. This guarantees every surviving pair is capital- and risk-independent, so pooled OOS Sharpe/drawdown isn't silently overstating diversification by double-capitalizing one stock's price risk across two "independent" sleeves.
- **`Personal Holding` is excluded as a sector.** In `configs/universe.yaml` this tag is a mixed bag (ETFs, gold, individually-added holdings) rather than a real economic sector like Banking or FMCG — the whole rationale for restricting to same-sector pairs is economic linkage, and pairing across that placeholder bucket would inject spurious candidates for the wrong reason. Symbols tagged `Personal Holding` are not considered for either selection method.

### 3.2 Spread definition

Log-price spread with a rolling-OLS hedge ratio: `spread_t = log(close_A_t) - beta_t * log(close_B_t)`, where `beta_t` is the OLS slope of `log(close_A)` on `log(close_B)` over a trailing 60-bar window (computed using data strictly before `t`, so it's causal — no lookahead). This is the standard approach in the pairs-trading literature; not treated as a variant worth A/B testing.

### 3.3 Entry/exit signal

Z-score of the spread over a trailing 60-bar window:
- Enter long A / short B when z <= -2.0; enter short A / long B when z >= +2.0.
- Exit (flat) when z crosses back through 0.
- Hard stop: exit if |z| >= 3.5 (the spread kept diverging instead of reverting).
- Bounded holding period: exit if held > 20 trading days regardless of z (mirrors `mean_reversion`'s `max_holding_days` safeguard — a spread that hasn't reverted in a bounded window is itself evidence the thesis failed for that trade).

### 3.4 Backtest engine — new `PairsBacktestEngine`

`BacktestEngine` is single-symbol, single-leg, long-only and is not reused directly; a new `backtesting/pairs_engine.py::PairsBacktestEngine` is built, but it reuses `IndianCostModel` and the existing data loading/cleaning path as-is. Shape:

- One capital account per pair (not per leg). On entry, capital is split into two **equal-notional** legs (dollar-neutral: $X long, $X short), sized at the fill price of each leg. Position sizing is *not* beta-weighted to match the signal's hedge ratio — that's a legitimate future refinement, left to the builder as a minor call, and must be named as a simplification in the output.
- Exit closes both legs on the same bar (signal exit, stop, or max-holding-days).
- Combined P&L = long-leg P&L + short-leg P&L, net of costs on both legs.

### 3.5 Short-leg cost proxy

The short leg's round trip (sell-to-open, buy-to-close) is charged through `IndianCostModel.calculate(buy_value, sell_value)` exactly as the long leg is — i.e., costs are mirrored onto the short leg rather than zeroed. This is explicitly a proxy (real short delivery isn't possible in cash equity; a real short leg would be an F&O position with different costs entirely) and must be labeled as such in every report this engine produces, not just this document.

### 3.6 Validation bar — new `PairsWalkForwardValidator`, different benchmark gate

Reuses `WalkForwardValidator`'s train/test split, fold structure, and most gates (sample size, profit factor, expectancy, max drawdown, walk-forward Sharpe retention, consistency across folds — see `backtesting/walk_forward.py`), but:

- Drops the "beats Nifty buy-and-hold" gate — a market-neutral strategy isn't fairly compared to a long-only index.
- Replaces it with **"OOS Sharpe > 0"** as an explicit gate (a market-neutral strategy with non-positive risk-adjusted return has no case for existing at all, independent of what the index did).
- Lowers `min_oos_trades` from the single-stock default of 100 to **30 pooled OOS trades** (across all surviving pairs of a method combined). With a same-sector-only candidate pool, no-overlap de-duplication (§3.1), and a 20-day max hold, pairs will generate far fewer round trips than a 53-symbol single-stock universe did; keeping the single-stock figure would make the gate measure sample size rather than the hypothesis. The remaining ratio-based gates (profit factor, expectancy, max drawdown, Sharpe retention) are left at `WalkForwardValidator`'s existing defaults — those aren't sample-size-sensitive in the same way.
- **Capital:** each surviving pair gets its own ₹100,000 account (same `capital_per_symbol` figure the single-stock validator uses, applied per pair rather than per symbol), split into two ₹50,000-notional legs at entry. This keeps the per-sleeve capital base comparable to the single-stock runs it's being read against, and (combined with the no-overlap rule in §3.1) means the pooled portfolio capital is just `₹100,000 × number of surviving pairs`, with no double-counting of any symbol's risk.

## 4. Architecture

New modules (all additive; nothing existing is modified):

```
backtesting/
├── pairs_selection.py   # correlation + cointegration candidate generation (in-sample only)
├── pairs_signals.py     # spread, rolling hedge ratio, z-score, entry/exit signal generation
├── pairs_engine.py      # PairsBacktestEngine (two-leg, combined P&L, reuses IndianCostModel)
├── pairs_walk_forward.py# PairsWalkForwardValidator (adapted gates, see §3.6)
└── run_pairs_walk_forward.py  # CLI: python -m backtesting.run_pairs_walk_forward --method {correlation,cointegration,both}
```

No changes to `strategies/base_strategy.py` or `STRATEGY_REGISTRY` — a pair's signal generator takes two symbols' data, which doesn't fit the single-DataFrame `BaseStrategy` contract, so it's its own small class rather than forced into that interface.

New dependency: `statsmodels>=0.14.0` (for `coint`/ADF), added to `requirements.txt`.

## 5. Output / reporting

`run_pairs_walk_forward.py` prints, per selection method:
- The frozen (in-sample-selected) candidate pair list with its selection metric.
- Per-pair and pooled OOS trade count, win rate, profit factor, expectancy, Sharpe, max drawdown — same shape as `run_walk_forward.py`'s existing tearsheet.
- Gate pass/fail table (§3.6).
- A fixed disclosure block, always printed, naming the two proxy simplifications: short-leg cost mirroring (§3.5) and non-beta-weighted equal-notional sizing (§3.4), so results can never be read as deployment-realistic by omission.

## 6. Related in-flight work / explicit follow-on

- `configs/universe.yaml`'s 53 symbols and sector tags are the candidate universe for both methods.
- If (and only if) a selection method clears the gates in §5, the next phase is F&O-aware cost/margin/tax modeling before any real-money consideration — not before. This mirrors how `range_filtered_trend`/`gaussian_channel` were tested with `full_equity` sizing as a faithful-to-source proxy before any deployment-realism work.

## 7. Left to the builder

- Exact default thresholds in §3.1/§3.3 (correlation 0.75, cointegration p<0.05, z-entry 2.0/exit 0/stop 3.5, lookback 60, max-hold 20) are reasonable literature defaults, not independently derived from this universe — fine to adjust slightly if a sane engineering reason comes up (e.g. too few candidate pairs survive at 0.75 correlation), but document the change.
- Whether `PairsWalkForwardValidator` subclasses `WalkForwardValidator` or is a standalone class with shared helper functions — pick whichever keeps the two gate sets clearly separate and readable; do not let pairs-specific logic leak into the single-stock validator.
- Test coverage shape: at minimum, unit tests for the spread/hedge-ratio/z-score math (causality — no lookahead) and for `PairsBacktestEngine`'s entry/exit/cost accounting on a small synthetic two-series fixture, following this repo's existing `tests/` conventions.

# 10 — Strategy Validation Findings

**Status:** Current as of 2026-11-10, after the demerger/bad-opening-price data fix (`data/cleaners/corporate_actions.py`). The original version of this document concluded all four strategies failed — that conclusion was reached on uncleaned price data (demerger discontinuities + bad opening prints the engine's fill model was trading on) and is superseded below. Read this before resuming any strategy work on this universe.

## 1. Why this system exists (the real motivation)

The user already trades two strategies live, manually, on TradingView:

- **"Range Filtered Trend Strategy || GPT"** (Kalman-style smoother + Supertrend regime filter)
- **"Gaussian Channel Strategy || GPT o1"** (N-pole Gaussian/Ehlers filter + Stochastic RSI)

TradingView's free tier doesn't support proper walk-forward backtesting or real alerting, so trades were being missed and the strategies' quality was never rigorously checked — just "looked fine" on TradingView's single-window backtest. Artha Quant was built to (a) port both strategies faithfully into Python, (b) backtest them properly with Indian costs/taxes and out-of-sample validation, and (c) send real Telegram alerts. The underlying goal was explicit: **grow real P&L from ~9% by maximizing profits and cutting losing trades.**

## 2. What was built and tested

| Strategy | Type | Source | Sizing mode |
|---|---|---|---|
| `trend_breakout` | 200-EMA + Donchian breakout + ATR stop | Original (pre-existing) | risk_based |
| `range_filtered_trend` | Kalman filter + Supertrend | 1:1 Pine port | full_equity (no stop, matches Pine's `percent_of_equity=100`) |
| `gaussian_channel` | N-pole Gaussian filter + Stoch RSI | 1:1 Pine port | full_equity (no stop) |
| `mean_reversion` | Bollinger lower-band dip + 200-EMA filter + ATR stop/target | Built from scratch | risk_based |

## 3. Timeline: why the verdict changed

1. **First pass (uncleaned data, naive per-symbol validator):** all four looked like they failed decisively — pooled OOS profit factor 0.09–0.34 depending on strategy, even after expanding the universe 20→53→73 symbols and restricting to the best-performing sector subset. This was real, but partly an artifact of two separate problems, found later:
   - The per-symbol aggregation averaged independent 100%-of-equity single-symbol returns as if that were a portfolio, which isn't how a real portfolio compounds, and let a handful of outlier single-stock trends dominate the average.
   - Kite's daily history is split/bonus-adjusted but **not demerger-adjusted**, and contains **bad opening-price prints** (gaps that don't match the day's own close, on days the broader market didn't gap) — the backtest's fill model was trading on both, producing fake crashes/spikes that corrupted results for affected symbols (GRASIM, TMPV, VEDL, ADANIENT, and others market-wide on bad-print days).
2. **Fix (`data/cleaners/corporate_actions.py`, `backtesting/market_data.py`):** demerger-adjusts price history before the ex-date, repairs isolated bad opening prints (market-wide gap days excluded so real volatility isn't touched), and excludes ETFs/gold/liquid funds from the strategy universe (NIFTYBEES kept only as the Nifty benchmark).
3. **Validator rewrite (`backtesting/walk_forward.py`):** replaced the single 70/30 split + naive per-symbol pooling with a proper **equal-capital-per-symbol portfolio sleeve** model, portfolio-level tax netted by financial year (gains and losses across symbols offset each other, as real Indian tax law allows), a real **Nifty buy-and-hold benchmark** comparison, and **rolling 6-month OOS folds** for consistency checking — judged against `plan.md`'s acceptance gates (≥100 pooled OOS trades, beats Nifty Sharpe, ≥60% Sharpe retention from in-sample, profit factor ≥1.30, expectancy ≥0.20%, max drawdown ≤20%, majority of folds positive).
4. **Re-run on the cleaned data (2026-11-10):** the picture changed materially for two of the four strategies.

## 4. Current result

| Strategy | OOS Sharpe | Nifty Sharpe (same window) | Profit Factor | Max DD | Fold consistency | Verdict |
|---|---|---|---|---|---|---|
| `trend_breakout` | 0.15 | 0.75 | 1.09 | 1.1% | 7/14 | **NOT QUALIFIED** |
| `range_filtered_trend` | **0.98** | 0.75 | **2.20** | 16.4% | 7/14 | **QUALIFIED** |
| `gaussian_channel` | **0.85** | 0.75 | **1.87** | 14.9% | 8/14 | **QUALIFIED** |
| `mean_reversion` | 0.16 | 0.75 | 1.10 | 0.4% | 9/14 | **NOT QUALIFIED** |

**Both of the user's actual TradingView strategies now pass.** `trend_breakout` and `mean_reversion` — the two built for/by this project, not from the user's own live trading — still don't (both barely trade at all under the cleaned data: 2% and 0% average exposure respectively, and both fail the Nifty-beating and profit-factor gates).

Tested against `range_filtered_trend` specifically: the user's actual TradingView input values (`kalman_period=87, kalman_beta=0.11, deviation=1.1`, vs. the Pine script's own published defaults of `77/0.1/1.2`) were checked side by side. **Both parameter sets pass, with near-identical numbers** (Sharpe 0.98 vs 0.94, PF 2.20 vs 2.11) — the qualification is driven by the data-cleaning and validator fix, not by which of these two parameter sets is used. No tuning search was run to arrive at either value; the user's parameters are their own pre-existing live TradingView settings, not something mined from this system's backtests.

## 5. Honest caveats — this is a pass, not a slam dunk

- **Fold consistency is right at the edge**: 7/14 and 8/14 folds positive — barely a majority, not a strong one. Half of the rolling 6-month windows were net losers.
- **Exposure is well under 100%** (51% for `range_filtered_trend`, 36% for `gaussian_channel`) — these strategies are flat much of the time; the Sharpe advantage over Nifty comes partly from being out of the market during its worse stretches, not from generating outsized absolute returns (CAGR is actually slightly *below* Nifty's for both: +10.0% vs +11.1%, +6.3% vs +11.1%).
- **One split, not many independent trials**: this is a single chronological 50/50 in-sample/out-of-sample split with rolling sub-folds inside the OOS period — more rigorous than the original single-split test, but still one specific cut of history, not a fully independent repeated experiment.
- **Still worth re-confirming periodically** as more data accumulates, rather than treating this as permanently settled.

## 6. Per-symbol drawdown is severe — and the fix is position sizing, not a stop-loss

The portfolio-level 16.4%/14.9% max drawdown in §4 is only that low because it's spread equal-capital across ~66 symbols simultaneously. **Individual symbols, run alone at the Pine script's own `percent_of_equity=100` sizing, have brutal drawdowns**: across the 66-symbol universe, median max drawdown is **46.7%**, mean **46.9%**, range 17.8%–77.6%, with 45/66 symbols over 40% and 27/66 over 50%. This matters directly for the user's actual plan: **trading a handful of names, not the full diversified universe.**

**What doesn't fix it — a stop-loss.** Added an opt-in static ATR-based stop (`atr_stop_multiplier` param on `RangeFilteredTrendStrategy`, default `None`/off, so the faithful Pine port is unchanged unless explicitly set) and tested it at 2x/3x ATR. Result: barely moves the drawdown distribution (median 46.7% → 47.0% at 2x) while cutting mean returns by a third (+252% → +169%). Cause: the entry logic only re-fires on the next *edge-triggered* green-state transition, so a stop-out during ordinary noise inside a longer uptrend locks in a small loss and then sits out while the trend continues without it — the strategy's own signal-exit was already functioning as the effective risk control in the trades that mattered for drawdown; a naive stop mostly just cuts good trades short.

**What does fix it — capping position size.** Drawdown scales roughly linearly with the fraction of capital committed per trade (not explosively, unlike returns):

| Allocation per position | Median DD | Symbols w/ DD > 50% | Mean return |
|---|---|---|---|
| 100% (Pine default) | 46.7% | 27/66 | +252.1% |
| 50% | 28.4% | 2/66 | +93.7% |
| 25% | 15.7% | 0/66 | +40.2% |
| 20% | 12.8% | 0/66 | +31.2% |

Walk-forward re-checked with a 2x ATR stop still qualifies (Sharpe 0.92 vs 0.98 without, PF 2.05 vs 2.20, portfolio DD 14.3% vs 16.4%, fold consistency 8/14 vs 7/14) — a real but minor tradeoff, not the main lever.

**Concrete illustration of trading "a handful of stocks":** 5 symbols picked for sector spread, not performance (NTPC/Utilities, MARUTI/Automobile, SUNPHARMA/Healthcare, HCLTECH/Technology, BRITANNIA/FMCG), each run on its own 20%-of-capital sleeve:

| Symbol | Net return (own 20% sleeve) | Max DD (own sleeve) |
|---|---|---|
| NTPC | -28.5% | 61.5% |
| MARUTI | +335.9% | 26.8% |
| SUNPHARMA | +36.7% | 64.6% |
| HCLTECH | +135.1% | 58.6% |
| BRITANNIA | +685.0% | 28.8% |

Blended (sum of the 5 sleeves): **net return +232.8%, max drawdown 17.8%, Sharpe 0.87.** None of the individual symbols' 26-65% drawdowns happened at the same time, so the blend survives comfortably even though every individual sleeve looks scary alone.

**Recommendation for live/concentrated deployment:** keep the strategy logic exactly as-is (no stop-loss — it doesn't help and costs return); size each position at **~20-25% of capital**, and pick the handful of names from genuinely different sectors, not a concentrated theme — the diversification benefit comes from sector/correlation spread, not from any one stock being "better." The 5-symbol example above is illustrative of the mechanism only, not a recommendation of those specific names (NTPC lost money on its own sleeve; this isn't survivorship-filtered).

## 7. Earlier findings that still stand

From the pre-fix investigation, these observations were about structural properties of the strategies/data and remain relevant context even though the headline verdict changed:

- No stop-loss + 100%-of-equity sizing (faithful to the Pine source) means individual large single-stock trends can dominate a symbol-level result — this is why the portfolio-level, equal-capital-sleeve validator (§3, point 3) is the trustworthy number, not any single symbol's headline return, and why position sizing (§6) rather than a stop-loss is the right lever for concentrated deployment.
- `ETERNAL` under `range_filtered_trend` beat its own buy-and-hold return across a small number of trades in the earlier (pre-fix) per-symbol analysis — not re-verified under the cleaned data; still just a lead, not a conclusion.
- The broader point that retail daily-bar technical rules on liquid large-caps often don't beat indexing net of costs is a reasonable prior in general — but the specific claim "none of these four strategies work" no longer holds for two of them, so that general prior shouldn't be over-applied here either.

## 8. Status of live infrastructure — decision still pending, now more clearly motivated

`main.py`'s EOD scan currently hardcodes `TrendBreakoutStrategy` and sends real Telegram trade alerts via `make scan` / `make daemon`. `trend_breakout` is one of the two strategies that still does **not** qualify, while the two strategies that do qualify (`range_filtered_trend`, `gaussian_channel` — the user's actual TradingView strategies) are not wired into live scanning at all. **This mismatch is now more clearly worth fixing than before** — not yet changed, no code modified.

## 9. Pairs trading — tested, no edge (2026-11-10)

Per the design in `docs/09-pairs-trading-plan.md`, built the pairs-trading research infrastructure (`backtesting/pairs_selection.py`, `pairs_signals.py`, `pairs_engine.py`, `pairs_walk_forward.py`) on the same cleaned 73-symbol universe, and ran both selection methods end to end.

**One real-data correction during the build:** the design doc's 0.75 correlation threshold left zero candidates — this universe's actual same-sector in-sample return correlations top out around 0.67 (`BPCL`/`IOC`). Lowered to 0.60 and documented in `docs/09-pairs-trading-plan.md` §3.1, exactly the kind of adjustment that doc's §7 anticipated.

**Candidates tested** (both selected on the in-sample window only, de-duplicated so no symbol appears twice, `Personal Holding` excluded):
- Correlation (3 pairs): BPCL/IOC, ICICIBANK/SBIN, HINDALCO/TATASTEEL
- Cointegration (8 pairs): SHREECEM/ULTRACEMCO, BAJAJ-AUTO/MARUTI, NTPC/POWERGRID, HINDUNILVR/NESTLEIND, APOLLOHOSP/CIPLA, INFY/TCS, HDFCLIFE/SBILIFE, HINDALCO/TATASTEEL

**Pooled OOS result:**

| Method | OOS Trades | OOS Sharpe | Profit Factor | Expectancy | Max DD | Fold consistency | Verdict |
|---|---|---|---|---|---|---|---|
| Correlation | 123 | -0.60 | 0.60 | -0.45% | 20.1% | 6/14 | **NOT QUALIFIED** |
| Cointegration | 330 | -0.97 | 0.61 | -0.49% | 17.2% | 3/14 | **NOT QUALIFIED** |

Both fail decisively on nearly every gate (profit factor ~0.6 against a 1.30 bar, negative OOS Sharpe, minority of folds positive) — this is not a borderline miss, it's the same shape of failure as `trend_breakout`/`mean_reversion`'s original (pre-data-fix) results.

**Individual pairs:** 2 of the 8 cointegration pairs were net positive OOS (APOLLOHOSP/CIPLA +9.3%, SHREECEM/ULTRACEMCO +3.5%). Picking these out after the fact would be exactly the multiple-comparisons trap `docs/09-pairs-trading-plan.md` §2 flagged in advance — 2 winners out of 8 candidates is roughly what noise alone would produce, not evidence of a real edge specific to those two pairs. Reporting the pooled result above, not the best individual pair, is the honest number.

**Conclusion:** no evidence of out-of-sample edge from relative-value pairs trading on this universe/timeframe, using the research-first defaults (log-price spread, rolling-OLS hedge ratio, z-score entry ±2.0 / exit 0 / stop ±3.5, 20-day max hold). The structurally-different-hypothesis pivot (§1-§3 of `docs/09`) was itself a reasonable move after the single-stock strategies' first-pass failure — but that first-pass failure later turned out to be partly a data-cleaning bug (§3 above), which pairs trading has no equivalent of: this result was produced on the same cleaned data and fixed validator that let `range_filtered_trend`/`gaussian_channel` pass. There's no known artifact here to go fix.

**Caveats:**
- No parameter tuning was done, by design (`docs/09` §2 declared this out of scope). These are literature-standard z-score/window defaults, not fitted to this universe. A dead signal under untuned defaults doesn't prove tuning couldn't help — but a profit factor of ~0.6 on both methods is a long way to climb back from, and tuning a signal this far underwater is a much better way to manufacture an overfit result than to find a real one.
- The proxy simplifications (`docs/09` §3.4-§3.5: short-leg costs mirrored from the long leg, equal-notional rather than hedge-ratio-weighted leg sizing, no tax modeled) could bias the result in either direction depending on the pair, and are not resolved here — but a sign-flip from "decisively failing" to "qualifies" purely from fixing those proxies would be a surprising amount of leverage for them to have, given the margin by which both methods miss.
- Same single-split caveat as §5 above applies here too: one chronological 50/50 in-sample/out-of-sample split with rolling sub-folds, not a fully independent repeated experiment.

## 10. What's genuinely open for a future session

- Switch `main.py`'s live scan to `range_filtered_trend` and/or `gaussian_channel` (§8) — now has real evidence behind it, where it didn't before. Live sizing should follow §6's ~20-25%-per-position guidance if trading a concentrated handful of names rather than the full universe.
- Re-verify the `ETERNAL` lead under the cleaned data and current validator.
- Consider whether the fold-consistency and below-Nifty-CAGR caveats (§5) warrant more data before increasing position size or capital commitment.
- Same position-sizing question is worth checking for `gaussian_channel` (§6 was only run against `range_filtered_trend`).
- `docs/09-pairs-trading-plan.md` — built and tested (§9 above); no OOS edge found under either selection method. Not recommended to resume without a specific new reason to (e.g. a different universe, or a genuinely different spread/entry construction — not a parameter retune of the same one, per `plan.md`'s own philosophy).
- Clarify what the "~9%" P&L baseline actually measures, if further comparison against this system's findings is wanted.

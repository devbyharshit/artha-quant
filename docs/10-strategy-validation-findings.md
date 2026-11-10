# 10 — Strategy Validation Findings (Concluded Negative Result)

**Status:** Concluded. This documents a finished validation effort, not an open task. Read this before resuming any daily-bar technical-strategy work on this universe — it explains what was tried and why it was dropped, so the same ground isn't re-covered from scratch in a future session.

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

All four were run through:
- Single-symbol backtests
- Full-universe backtests (20 → 53 → 73 symbols, see §4)
- `WalkForwardValidator`: chronological 70/30 In-Sample/Out-of-Sample split, checked against `plan.md`'s 6 acceptance gates (≥30 trades, OOS CAGR ≥12.5% Nifty hurdle, max drawdown ≤20%, expectancy ≥0.20%, profit factor ≥1.30, <50% degradation from IS CAGR)

## 3. Result: all four failed walk-forward, decisively and repeatedly

| Strategy | In-sample look | OOS result |
|---|---|---|
| `trend_breakout` | — | OOS profit factor collapsed to ~0.2–0.3 |
| `range_filtered_trend` | Looked strong (+8.6% avg) | OOS profit factor 1.68 → 0.34 pooled; 0.09 on the single best-performing sector subset |
| `gaussian_channel` | — | Net negative, OOS profit factor ~1.0 |
| `mean_reversion` | — | Roughly flat/negative, OOS profit factor 0.23–0.26 |

This held even after:
- Expanding the universe from 20 → 53 symbols (more sample size)
- Restricting to the single best-performing sector subset (14 symbols: Utilities/Telecom/Energy/Automobile)
- Pulling the user's actual 20 Kite holdings into the universe (53 → 73 symbols) and re-running, specifically to check performance on stocks the user actually owns

## 4. Why the strategies *looked* fine on TradingView but aren't

The Finance Tracker agent's deep-dive analysis (multiple passes) found the "good-looking" results were largely artifacts, not edge:

- **No stop-loss + 100%-of-equity sizing** (faithfully ported from the Pine source) means a handful of huge single-stock winners can carry an entire equity curve while actually being poor risk management.
- **Concentration:** 86% of `range_filtered_trend`'s aggregate return came from just 5 stocks. A single GABRIEL trade was 39% of `gaussian_channel`'s entire OOS net profit.
- **Buy-and-hold beat the strategy on almost every big winner** — e.g. HAL, BHARTIARTL — meaning the "strategy" was often just diluted, whipsawed participation in a stock's own trend, worse than doing nothing.
- **Correlation across strategies (0.66)** between unrelated strategies' returns implied both were just riding the same stock-specific trends, not detecting a real distinct signal.
- **Non-independent OOS windows:** 16 of the 20 real holdings share an identical 18-month calendar OOS window — meaning pooled "sample size" across symbols was statistically inflated; these are correlated draws, not independent trials.
- `mean_reversion`'s OOS "improvement" over `trend_breakout` was ~$638 total — economically meaningless at this capital scale.

**One possible exception, not fully run down:** `ETERNAL` under `range_filtered_trend` beat buy-and-hold across 7 trades. Flagged as a lead worth a closer, skeptical look if single-stock technical work is ever resumed — not yet investigated further.

## 5. Conclusion reached

**There is no demonstrated single-stock daily-bar technical edge on this universe, with any of the four approaches tried.** Per `plan.md`'s own stated philosophy — "a strategy validation system first... if the answer is no, stop" — this is being treated as a **concluded, legitimate negative result**, not a reason to keep re-tuning the same family of indicators. Hunting for more daily-bar technical edge on this universe is explicitly not being pursued further.

This is also a fairly standard, well-documented result in quant finance more broadly: retail daily-bar technical rules on liquid large-cap equities, net of real costs and taxes, usually don't beat simply holding an index.

## 6. So: "how do I invest now?" — guidance given

Given the goal was never "run a backtest system" for its own sake but to grow real P&L, the practical takeaways discussed:

1. **For the two live TradingView strategies specifically:** stop trusting them as-is. The data shows they looked good on TradingView mainly due to backtesting artifacts (no stop-loss, 100%-equity sizing, single fitted window, survivorship from a few huge winners) that this system's walk-forward testing was specifically built to catch — and did.
2. **If the goal is maximizing long-run return with reasonable effort:** the evidence here points toward a low-cost index fund/ETF core (Nifty 50 / Next 50) over active technical trading as the primary vehicle — not an exciting answer, but the one the data in this project supports.
3. **If discretionary/active trading continues:** use this system as a *filter* that flags setups for manual review, not as something to follow unsupervised — these specific rules aren't reliable enough to run as an autopilot.
4. **If still chasing systematic edge:** the only untested, structurally different idea on the table is pairs/relative-value trading (`docs/09-pairs-trading-plan.md`) — parked, not pursued, 5 open design questions unresolved. The `ETERNAL` lead (§4) is a smaller, cheaper thing to check first.
5. Clarifying question still open: what the "~9%" P&L figure actually represents (overall portfolio return over what period, vs. one strategy/trade) — matters for whether 9% is actually under- or out-performing anything, and wasn't resolved in this session.

## 7. Status of live infrastructure — decision still pending

`main.py`'s EOD scan currently hardcodes `TrendBreakoutStrategy` and sends real Telegram trade alerts on a daily schedule (via `make scan` / `make daemon`). This strategy also failed walk-forward (§3). **This is inconsistent with the "stop, no validated edge" conclusion above and has not been resolved** — the user has not yet decided whether to pause/disable the live scan, and no code change has been made to it. Revisit before relying on any further live alerts from `make daemon`.

## 8. Universe and infra left in place (reference, not required reading)

- `configs/universe.yaml` (v1.1, "nifty_largecap_extended") — 73 symbols: ~53 original NSE large-caps across 11 sectors + 20 of the user's real Kite holdings (tagged `Personal Holding`, added via `make add-holdings`).
- All four strategies, the full-universe backtest runner (`backtesting/run_universe.py`), and the walk-forward runner (`backtesting/run_walk_forward.py`) remain in the codebase as working, tested reference infrastructure (62/62 tests passing) — kept for any future re-validation, not removed.
- `docs/09-pairs-trading-plan.md` — parked brainstorming checkpoint for the relative-value idea; not built, not currently being pursued.

## 9. What's genuinely open for a future session

- Decide `main.py`'s live scan (§7).
- Optionally investigate the `ETERNAL` lead in isolation.
- Optionally resume `docs/09-pairs-trading-plan.md` if a structurally different hypothesis is wanted later.
- Clarify what the "~9%" P&L baseline actually measures, if further comparison against this system's findings is wanted.

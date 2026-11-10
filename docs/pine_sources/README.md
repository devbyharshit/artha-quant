# TradingView open-source strategy candidates

Pulled from the `/scripts/?script_type=strategies` listing (default sort — TradingView's
static HTML doesn't expose which sort is active, and its "most popular" / "open-source only"
controls are JS-only, so this is one unsorted page of candidates, not a ranked top-N). Each
file's badge was confirmed as `OPEN-SOURCE SCRIPT` on the script's own page, and the source was
extracted from the rendered Pine editor via browser automation (TradingView's static HTML hides
the source behind a JS-only tab; the listing page hides even the badge). Not yet ported to Python
or checked for correctness — this is a raw source pool for triage.

| File | Title | Author | License note in source | Lines | Notes |
|---|---|---|---|---|---|
| `ut_bot_risk_management.pine` | UT Bot Strategy with Risk Management [TradeIQ] | Tradeiqofficial | MPL-2.0 | 228 | Wraps QuantNomad's open-source UT Bot Alerts (ATR trailing-stop crossover) with filters, risk-based sizing, account guards, webhook alerts. Defaults tuned for 4H crypto; underlying logic is instrument/timeframe-agnostic. |
| `3_ema_rsi_filtered.pine` | 3 EMA + RSI Filtered | TomQuixote | MPL-2.0 (credits Free990's base) | 507 | Triple-EMA entries + RSI/trailing/fixed exits, plus session windows, daily loss lock, day-of-week filters. Defaults target 1 MNQ on 5-min — needs re-tuning for daily EOD equities. |
| `displaced_donchian_decycler.pine` | Displaced Donchian Decycler \| Trend Breakout with MFI | blitz_algo | MPL-2.0 | 141 | Donchian breakout (lagged window) + 2-pole decycler trend filter + MFI filter, ATR stop/target. Same family as our existing `trend_breakout.py` — good direct A/B candidate. Suggested timeframe 1H–Daily. |
| `atty_trend_pro.pine` | Atty Trend Pro Strategy | AttyLabs | none stated | 730 | Dual-MA "ribbon" with a 5-timeframe trend stack, multiple exit models (ribbon flip, R-multiple, ATR trail, MA cross), session gate. Largest/most complex of the batch — more free parameters than plan.md's 3-parameter cap allows as-is. |
| `algorover_volume_oscillator.pine` | AlgoRover Volume Oscillator Prior-Extreme Reversal v1.1.1 | AlgoRover | none stated | 156 | Volume-oscillator zero-crossing reversal, direction set by comparing stored prior extremes. Author's own example is 1H crypto; fixed 5% stop via market close, not a resting order. |
| `aurora_value_trend.pine` | Aurora Value Trend \| Value Area Retest Strategy | blitz_algo | none stated | 315 | Weekly volume-profile VAH/VAL/POC retest in the prevailing trend, ATR stop + R-multiple target. Needs a volume-profile implementation we don't have yet (`pandas_ta` has no equivalent) — highest porting effort here. |
| `sweep_fvg_reversal.pine` | Sweep + FVG Reversal [Strategy] | DoruGavrila | MPL-2.0 | 385 | Liquidity sweep + fair-value-gap reversal (ICT-style), confirmed pivots, limit entry/stop/target. Has real `strategy.entry`/`strategy.exit` calls despite the listing blurb reading like a setup-only alert script. |
| `xauusd_ny_open_ema_fakeout.pine` | XAUUSD NY Open 8 EMA Fakeout Strategy | acgoldgogo | none stated | 51 | Smallest script. Intraday 8-EMA fakeout/reclaim at the NY open with an HTF trend filter via `request.security`, fixed %-based stop/target, flattens at session end. Directly relevant if we pursue gold (XAUUSD / MCX gold) — but it's intraday-session logic, not a daily-bar fit. |

## How these were pulled

TradingView's listing page (`/scripts/...`) only renders card badges and the "Source code" tab
content client-side; a plain HTTP fetch of either the listing or a script's own page gets the
author's prose description but not the code or the open-source/protected badge. Script pages
*do* show the badge in static HTML (confirmed separately), but not the source itself.

Working recipe (browser automation, not a plain fetch):
1. Navigate to the script's page.
2. Click the tab whose text matches "Source code" (`[role=tab]`).
3. Poll for `.monaco-editor-tv-pine-light` to populate (TradingView's read-only syntax-highlighted
   render container — not Monaco itself, no textarea or `window.monaco` to read from directly).
4. Read `.innerText` of that element — it's the full script, not virtualized/paginated, for
   every size tried here (up to 731 lines).
5. Normalize `\xa0` (non-breaking spaces from the rendered `&nbsp;` markup) back to plain spaces.

## Next step

Pick 1–2 to port once the instrument direction is settled. `displaced_donchian_decycler.pine` is
the readiest (smallest, same family as an existing ported strategy, clean MPL license). The
others need either a timeframe/parameter rework (3 EMA+RSI, Atty Trend Pro, AlgoRover, XAUUSD —
all built for intraday futures/crypto) or new infrastructure (Aurora Value Trend's volume
profile) before they fit this system's daily-bar EOD design.

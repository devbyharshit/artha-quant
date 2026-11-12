# 13 — MCX Commodities: Time-Series Momentum Design

**Status:** PARKED (2026-11-12). Built and tested (308 tests passing, pipeline reviewed and correct — see §13), but never validated against live/fresh data: MCX's server blocks scripted historical-data access (§3.1, §13), and a direct Kite Connect test (§13.2) confirmed Kite cannot substitute — it only serves ~6 months of history per contract-month and has no catalog of expired contracts. User decision: stop investing further effort here and move to a different satellite-sleeve hypothesis (crypto) rather than continue pursuing a paid vendor or alternate network path. Revisit only if a working MCX data-access path appears later. Follows the pattern of `docs/09-pairs-trading-plan.md` and `docs/12-futures-trading-plan.md` — written before implementation, updated in place as the build surfaces real blockers (the same honesty convention those two docs used when things didn't go as planned).

## 1. Why this exists

This is the satellite-sleeve hypothesis for the user's long-horizon (7-10 year) capital-growth plan, discussed and sized in conversation rather than in a prior doc: a core index allocation handles the "patient compounding" leg, and a smaller, strictly tested satellite sleeve is allowed to chase something more ambitious — but only if it's a genuinely new, honestly-evidenced hypothesis, not a re-run of something already inconclusive.

Everything tested so far in this repo has been one of:
- Daily-bar technical indicators on single-stock cash equities (`docs/10`) — two now qualify, two don't.
- The same cash-equity signals re-run on NFO single-stock-futures execution (`docs/12`) — inconclusive, blocked on a margin-sizing parameter.
- Correlation/cointegration pairs trading on the same equity universe (`docs/10` §9) — tested, no edge.

**This is deliberately not another variation on those.** Time-series momentum (trend-following on an instrument's own trailing return, scaled to a constant volatility target) is the most replicated systematic strategy in commodities specifically — it's the actual basis of most live CTA/managed-futures track records, not just a backtested curiosity, and its documented edge (Moskowitz/Ooi/Pedersen, "Time Series Momentum," 2012, and the live CTA industry built on the same idea) is a different mechanism from anything tried here: trend persistence from hedging pressure and slow information diffusion in futures markets, not a technical indicator fitted to equity price charts.

## 2. Scope

**Goals**
- Build a time-series momentum strategy, trading both long and short (native to futures, and core to the hypothesis itself — not a bolt-on), on the most liquid MCX commodity futures.
- Validate it with the same walk-forward rigor as the rest of this system (gates modeled on `WalkForwardValidator`'s philosophy, adapted per §6 below).
- Keep it additive — no changes to any existing equity/pairs/NFO-futures code path.

**Non-goals (this phase)**
- Tax modeling. Same posture as `docs/12`: commodity F&O P&L is taxed as business income at the trader's personal slab rate, which this system has no model of. No tax applied, labeled as a proxy throughout, same as the futures and pairs engines.
- Options on commodities.
- Agricultural commodities (different CTT treatment per §5; liquidity unconfirmed; out of scope until the core 4 are validated).
- Intraday/MIS — EOD daily bars only, matching every other strategy in this system.
- Margin-efficiency optimization — same stated-approximation posture as `docs/12` §3.2.

## 3. Phase 0 — data-source investigation (do this first, before any strategy code)

This is the single biggest open risk, flagged explicitly rather than assumed away, because `docs/12` already burned a full build cycle on the equivalent mistake for NFO futures.

Pre-build research (web search only, not verified against the live source) found:
- MCX's own site has `mcxindia.com/market-data/bhavcopy` and `mcxindia.com/market-data/reports-on-historical-data` pages, referenced from a SEBI page, but their actual file format, history depth, and access terms were **not independently confirmed**.
- Third-party packages (`mcx-data`, `mcxlib` on PyPI) and tools (Marketcalls' downloaders) claim to pull MCX historical/bhavcopy data, but none were verified working.
- Kite's own instrument catalog almost certainly has the same near/next/far-month-only limitation documented in `docs/12` §5 for NFO — several Zerodha developer-forum threads independently report MCX-specific gaps too (missing SILVER/SILVERM data, wrong lot sizes in the instrument dump), which corroborates not relying on Kite for historical commodity prices.

**Builder's first task:** directly open `mcxindia.com/market-data/bhavcopy` (and the historical-data reports page), determine whether it provides free daily OHLCV + lot size + expiry per contract going back multiple years in a parseable format (CSV/ZIP, like NSE's bhavcopy), and report back clearly — including "it doesn't exist in a usable free form" as a legitimate, actionable answer, exactly like `docs/12` did when Kite's NFO catalog turned out to be too shallow. **Do not write `backtesting/commodity_engine.py` or any signal code until this is resolved** — per `docs/12`'s own lesson, a data-layer gap discovered after the engine is built just means redoing the integration twice.

If no usable free historical source exists, the fallback options (in order of preference) are: (a) a paid data vendor, flagged to the user as a real recurring cost before committing to it, (b) accumulating live-contract history going forward and validating once enough exists (as `docs/12` is currently doing for NFO), or (c) parking this hypothesis, same honest-negative-result posture as pairs trading.

## 4. Instrument universe

Based on current research (lot sizes reasonably confident; liquidity claims not independently verified — builder should sanity-check against actual recent volume data once the data layer exists):

| Commodity | Standard lot | Smaller variant | Notes |
|---|---|---|---|
| Gold | 1 kg | Mini 100g, Guinea 8g, Petal 1g | |
| Silver | 30 kg | Mini 5kg, Micro 1kg | |
| Crude Oil | 100 barrels | Mini 10 barrels | |
| Natural Gas | 1,250 mmBtu | Mini 250 mmBtu | |

**Correction from an earlier draft, against the real capital this sleeve has (₹5k-35k/month, per conversation):** the standard contracts are not viable at this scale. Illustrative, approximate, not live-verified math: standard Gold (1kg) and Silver (30kg) notional runs into the ₹30-100+ lakh range per lot — even at a 20-30% margin approximation, that's lakhs of margin for *one* lot, the same "every signal sizes to 0" failure `docs/12` hit with large-cap equity futures. The Mini/Micro variants bring this down to a plausible range for Crude Oil Mini and Silver Micro specifically (rough notional low lakhs or less, margin in the tens of thousands) — **default to Mini/Micro contracts throughout, not standard**, pending the real capital figure in the open question below.

Copper, Zinc, Aluminium were flagged in research as plausible candidates but their current lot sizes/liquidity weren't confirmed — **left to the builder** to add once the core 4 are working, not blocking Phase 0-3.

Agri commodities excluded (§2).

## 5. Cost model — new, commodity-specific, several rates flagged as unconfirmed

Reuses the shape of `IndianFuturesCostModel` (itemized breakdown, one `calculate()` entry point) with commodity-segment rates. Research found conflicting/unconfirmed figures for several of these — **the builder must verify each against `zerodha.com/charge-list` directly before trusting any backtest number**, not just carry the figures below forward silently:

- **CTT (Commodities Transaction Tax):** research suggests 0.01% sell-side for non-agri commodities (vs. 0.05% for agri) — this is the opposite of what might be assumed by analogy to equity STT, and was **not independently confirmed against a primary source**. Verify before coding.
- **Brokerage:** ₹20 flat or 0.03% of turnover, whichever is lower, per executed order (same shape as NFO futures) — reasonably confident.
- **Exchange transaction charges:** ~0.0021-0.0026% of turnover (Group A rate), lower above a broker-turnover slab — reasonably confident on the shape, verify the exact current number.
- **SEBI charges:** ₹10/crore (non-agri) — reasonably confident.
- **GST:** 18% on brokerage + transaction charges + SEBI charges combined — reasonably confident, standard rate.
- **Stamp duty:** applies buy-side; exact current rate **not confirmed** — verify, don't guess a number.

## 6. Signal design — time-series momentum

Adapted from the standard TSM formulation to this system's existing daily-bar/EOD-scan architecture (not monthly rebalancing, to stay consistent with how every other strategy here runs):

- **Directional signal:** sign of the trailing 252-trading-day (≈12-month) total return on the front-month-rolled continuous price. Positive → long bias; negative → short bias. This is the classic TSM lookback (Moskowitz/Ooi/Pedersen used 1/3/12-month variants; starting with the single most literature-standard one, not a multi-lookback ensemble, to keep this a clean first test rather than an immediately-tunable multi-parameter model).
- **Position sizing — volatility-scaled, not full-equity or fixed-lot:** size each position so its ex-ante annualized volatility (from trailing 60-day daily returns) matches a fixed target (default 35-40% annualized per instrument — a stated default, not independently derived, left to the builder to tune within the walk-forward framework, not by eyeballing results). This is the actual mechanism TSM's live track record is built on — it's what lets the same signal logic work sensibly across instruments with very different native volatility (gold vs. crude vs. natural gas), and skipping it in favor of fixed-lot sizing would be testing a meaningfully different, weaker strategy while calling it the same thing.
- **Rebalance/trade trigger:** daily directional check, but only trade when the sign flips or volatility-scaling pushes the required lot count across a rounding threshold — not a forced daily re-trade. Reduces needless turnover/costs, consistent with how every other strategy in this system treats signal-exit.
- **No technical stop.** Consistent with §6 of `docs/10`'s finding that stops mostly just cut good trends short without meaningfully improving drawdown for signal-exit strategies — the position-sizing/vol-targeting *is* the risk control here, same lesson, different mechanism. If early walk-forward results suggest otherwise for this instrument class, that's a finding to report, not an assumption to start from.

## 7. Contract handling — same pattern as `docs/12`

Two distinct series, not one — resolving an ambiguity the builder-check flagged in an earlier draft of this section:

- **Signal series:** a back-adjusted (ratio-spliced) continuous price history, used *only* to compute the 252-day trailing return and 60-day realized volatility in §6. This avoids phantom return jumps at roll dates corrupting the momentum/vol calculation — it is never used to generate a fill.
- **Execution series:** mirrors `docs/12` §3.1 exactly — real front-month contract prices, with any open position **forced closed on a stated buffer before expiry** (3 trading days, same default) and a fresh entry on the new front-month if the (continuous-series-computed) signal is still active. Each roll is a real close + reopen: realized P&L and a full second round of costs (§5), not a free synthetic splice. This is the same two-series pattern `docs/12` used for equity-signal-on-futures (cash history for the signal, futures overlay for fills), adapted here because commodities have no separate cash market to play that role — the back-adjusted series fills that slot instead.

**Known limitation (found in final review, deliberately not fixed): every rollover leaves the position flat for ~2 trading days.** The data layer keeps the old contract listed in the overlay through its own expiry (to avoid pricing exits on the wrong contract — the contract-switch bias documented in `backtesting/commodity_engine.py`), while the engine refuses to open a contract already inside its own rollover buffer (it would be force-closed the next bar). So after the forced ROLLOVER exit, the re-entry waits until the overlay switches to the next front-month, the day after the old contract expires. A reviewer's simulation found this on 24/24 rollovers. The cost is real days-out-of-market drag, material for the monthly-roll commodities (CRUDEOILM, NATGASMINI: ~12 rolls/year, on the order of 24 trading days/year flat that a real trader rolling straight into the next month would be holding). Fixing it needs per-contract quote plumbing through the engine (pricing the re-entry on the next contract while the overlay still lists the old one), an architecture change out of scope for this build; results should be read as understating exposure around rolls.

Margin approximated as a flat % of notional per lot — **treat as an explicit placeholder** (one data point found: Crude Oil NRML ≈31.25% of contract value from a single third-party source, not an official circular; gold/silver margin figures found were illustrative only) — same honesty posture as `docs/12` §3.2's 20% equity-futures placeholder, which that doc explicitly flagged as needing revisiting rather than trusting blindly.

## 8. Validation bar — same gate philosophy, different benchmark

Reuses `WalkForwardValidator`'s fold structure (rolling OOS folds, portfolio-level pooling), but **two deliberate deviations**, not one — the second added after the builder-check flagged that carrying the gate *thresholds* over unexamined would risk a false "fails on sample size" verdict indistinguishable from "no edge":

1. **Benchmark:** the cash-equity/futures validators benchmark against Nifty buy-and-hold, because those are long-only strategies on equities where "would I have done better just holding the index" is the right question. That comparison doesn't make sense here — commodities don't have an equivalent passive index benchmark, and the strategy itself goes both long and short. Benchmark instead against **each commodity's own buy-and-hold return** over the same OOS window (long-only, no leverage).
2. **Gate shape:** a 252-day-lookback, sign-flip-triggered signal on only 4 instruments will generate far fewer trades per year than the 50-70+-symbol daily-bar equity universe `docs/10`'s ≥100-pooled-OOS-trades floor was calibrated against — that floor does not carry over unchanged. Primary gates for this strategy should be **continuous-equity-curve metrics** (OOS Sharpe, max drawdown, Calmar ratio) computed from the daily portfolio-value series, not per-trade statistics — standard practice for low-turnover trend-following/CTA evaluation, where a handful of large, long-duration positions is the norm, not a flaw. Profit factor and expectancy are still computed and reported (cheap to get from the trade log) but are not gating. The trade-count floor is replaced with a **minimum backtest history length** instead (propose: at least 5 years of OOS data pooled across the 4 instruments, so the few trades that do occur aren't just 1-2 lucky/unlucky trends) — left to the builder to adjust with a documented reason, same as every other stated default in this doc.

## 9. Architecture (proposed, adjustable by the builder)

```
data/loaders/
├── mcx_instruments.py       # underlying commodity -> list of historical MCX contracts (lot size, expiry)
└── mcx_bhavcopy.py          # historical OHLCV ingester — ONLY after Phase 0 confirms a real source

backtesting/
├── commodity_cost_model.py  # IndianCommodityCostModel (§5)
├── commodity_signals.py     # time-series momentum signal + vol-target sizing (§6)
├── commodity_engine.py      # continuous-contract roll + execution (§7) — reuse FuturesBacktestEngine's
│                              lot-size/rollover logic via a shared base if that turns out clean; don't
│                              force it if the continuous-series handling differs enough to fight a shared base
├── commodity_walk_forward.py  # validator reusing WalkForwardValidator's gates, commodity benchmark (§8)
└── run_commodity_walk_forward.py  # CLI
```

## 10. Build plan — parallel subagent chunks, Phase 0 gates everything else

Following this repo's own precedent (`docs/12` §5: "built in three parallel chunks, then integrated"):

1. **Phase 0 (sequential, blocking):** data-source investigation (§3). Nothing else starts until this reports back with a real verdict.
2. **Phase 1 (parallel, once Phase 0 confirms a source):** data layer (`mcx_instruments.py`, `mcx_bhavcopy.py`), cost model (`commodity_cost_model.py`), signal+engine (`commodity_signals.py`, `commodity_engine.py`) as independent subagent tasks against synthetic fixtures, same division of labor as the futures build.
3. **Phase 2:** integration against real data, smoke test, one real run, same "report what actually happened" posture as `docs/12` §5 (if it fails, that's a finding — document it, don't quietly retune until it passes).
4. **Phase 3:** walk-forward validation against the gates in §8, written up as a new section of this doc (or a `docs/10`-style findings doc), honestly, whatever the result.

## 11. Backtest sizing capital — decided

₹1,00,000 notional, matching `docs/12`'s per-symbol convention — a clean placeholder for Phase 3's vol-target sizing, not the real deployment ladder (₹0 research / ₹5-10k starter / up to ₹35k scaled, from conversation). This is purely so the backtest produces a meaningful, affordable lot count under §4's Mini/Micro default; translating a passing result into real position sizes against the actual ladder figures is a separate step once/if this clears walk-forward validation.

## 12. Left to the builder

- Exact vol-target (35-40% suggested), lookback window beyond the single 252-day default, and rollover buffer (3 days suggested) are stated defaults, not independently derived.
- Whether `CommodityBacktestEngine` shares a base class with `FuturesBacktestEngine` — pick whichever keeps rollover/lot-size logic clean; not required to unify them.
- Exact CTT/stamp-duty rates (§5) — verify against zerodha.com/charge-list before the cost model is trusted, don't carry research estimates forward silently.
- Whether Copper/Zinc/Aluminium get added once the core 4 are working.
- Test coverage shape — unit tests for vol-target sizing, signal sign-flip logic, and cost calculation at minimum, following this repo's existing `tests/` conventions.

## 3.1 Phase 0 finding — MCX bhavcopy data-source investigation (2026-11-11)

**Verdict: GO**, with one explicit pre-condition flagged below that Task 2's builder must check before writing `mcx_bhavcopy.py` against a plain `requests.Session` (the NSE-style approach). The data itself is confirmed to exist, is free, has no login/API-key requirement, and has materially deeper history than NSE's own archives (back to the exchange's 2003 launch, vs. NSE F&O's 2011). The only open risk is an *access-method* question, not a *does-the-data-exist* question — see "Access-method caveat" below.

This finding was produced by live interaction with `www.mcxindia.com` using the built-in browser tool (a real browser session, not a scripted HTTP client) — both `mcxindia.com/market-data/bhavcopy` and `mcxindia.com/market-data/reports-on-historical-data` (now titled `/market-data/historical-data` on the current site) were opened, their pages read, and their underlying network calls inspected and exercised directly.

**The two pages are UI-rendered report builders, not direct file downloads** — same conclusion-shape as NSE's archives, but a different mechanism. Behind the "Bhavcopy" page's "Date Wise" / "Commodity Wise" tabs and the "Historical Data" page's "Year/Month/Date Wise" tabs, the page's own JS fetches a JSON payload from the server and renders it into an HTML table; there is no `.csv.zip` file sitting at a predictable URL the way NSE's archives work. Critically, **the page's own "Download CSV"/"Download XLS" buttons are 100% client-side** — `BhavCopy.js`'s `ExportManager.exportAsCSV`/`exportAsExcel` just reformat the JSON array already sitting in the browser's memory into a Blob and trigger a browser download; there is no server-side bhavcopy archive file to hit directly. An ingester must therefore call the JSON endpoints below and build its own CSV/parquet cache from the JSON, same end result as NSE but via a different plumbing shape.

**Confirmed JSON endpoints (same origin, `www.mcxindia.com`), one format, no "legacy vs current" split found:**

1. Date-wise, all commodities for one day:

       GET https://www.mcxindia.com/market-data/bhavcopy/GetDateWiseBhavCopy?InstrumentName={ALL|FUTCOM|FUTIDX|OPTCOM|OPTFUT|OPTIDX}&fromDate=DD%2FMM%2FYYYY

   Returns `{"IsSuccess":true,"Message":"...","Data":[{...}],"Metadata":null}` where each row of `Data` is one contract's bar for that day: `Date` (`MM/DD/YYYY`), `Symbol` (space-padded, e.g. `"GOLD         "` — must `.strip()`), `ExpiryDate` (`DDMMMYYYY`, e.g. `"05JUN2015"`), `Open`/`High`/`Low`/`Close`/`PreviousClose`, `Volume` (lots), `VolumeInThousands` (a string with a trailing unit suffix baked in, e.g. `"13724.000 GRMS "`, `"6177.500 BBL  "`, `"61346.500 mmBtu"`, `"35.700 BALES"` — unit varies by commodity and must be parsed out, not assumed), `Value` (turnover, lakhs), `OpenInterest`, `DateDisplay`, `InstrumentName`, `StrikePrice`, `OptionType`. Verified working with `InstrumentName=ALL` and `InstrumentName=FUTCOM` for 10 Nov 2003 (3-4 rows — ALUMINI only, consistent with the exchange ramping up), 20 Nov 2003, 1 Dec 2003, 15 Jun 2015, 15 Jan 2020, and 9-10 Nov 2026 (16,630-16,648 rows across all instrument types for the most recent two trading days; 11 Nov 2026 itself returns `"No data found."` since EOD data for "today" isn't published yet, as expected). **17 Nov 2003 returned `"No data found."`** (a day the exchange genuinely had no trading, not a format gap) — confirms the no-data response is `IsSuccess:false` + `"No data found."`, distinguishable from a real empty/holiday day, not a silent empty array.

2. Commodity-wise, full life of one specific contract:

       GET https://www.mcxindia.com/market-data/bhavcopy/GetCommoditywiseBhavCopy?InstrumentName=FUTCOM&Symbol={SYMBOL}&Expiry={DDMMMYYYY}&fromDate=&toDate=

   Same row shape as above. With `fromDate`/`toDate` left empty, this returns **every trading day across the contract's entire listed life in one call** — verified for `GOLD`/`05JUN2015`: 174 rows spanning 01 Oct 2014 (listing, zero volume/pre-trading rows present) through 05 Jun 2015 (expiry day). This is the more efficient endpoint for backfilling a specific contract's full history in one request rather than one request per day — recommended as the primary fetch shape for Task 2's `mcx_bhavcopy.py`, with the date-wise endpoint reserved for cross-sectional/discovery use (e.g., finding which contracts were live on a given day). The `fromDate`/`toDate` params were not exercised end-to-end with non-empty values in this investigation (the live page's custom JS datepicker didn't bind cleanly to a scripted field-set), but the endpoint signature accepts them; Task 2 should verify date-bounded calls directly against the endpoint rather than through the UI.

**Required headers — different and stricter than NSE's:** an `X-Requested-With: XMLHttpRequest` header is **required** on both endpoints. Without it, the server does not 403 or 404 in the usual sense — it returns **HTTP 200 with the site's full HTML "404" error page as the body**, for every URL and date tried. This is a real trap for an ingester: checking `status_code == 200` is not sufficient; the response must be checked for valid JSON / `Content-Type: application/json` (or the `IsSuccess` key must be probed) before trusting it, or every single request will silently appear to "work" while returning garbage HTML. With `X-Requested-With: XMLHttpRequest` plus `Accept: application/json, text/plain, */*` and same-origin cookies, requests succeed and return genuine JSON.

**Access-method caveat — the one real open risk, must be checked before Task 2 writes the ingester:** direct scripted HTTP access (`curl`/Python `requests`, with full browser-header mimicry matching the pattern `nse_bhavcopy.py` already uses successfully for NSE — User-Agent, Accept, Referer, even a same-origin cookie jar from a prior page load) was **blocked with a flat Akamai edge `403 Access Denied`** for every single MCX URL tried from this sandboxed dev environment's network egress — including the plain page load itself (`mcxindia.com/market-data/bhavcopy` 403'd over plain `curl`, before even reaching the JSON endpoint). The block happens at Akamai's edge, not MCX's application layer (the error page is Akamai's own "Access Denied" / "AkamaiGHost" response, not an MCX 404/403). By contrast, every MCX request made through the built-in browser tool (a real Chromium-based browser session) succeeded immediately and consistently, across roughly 15-20 separate JSON calls spanning dates from 2003 to 2026, with no failures.

This is **unresolved** and could go either way: (a) Akamai may be blocking this specific sandbox's cloud/datacenter IP range specifically, in which case a script run from the user's own home/office connection or an India-based host would likely work fine with the same request shape that worked in the browser (same JSON endpoints, same `X-Requested-With` header, no need for a full browser); or (b) Akamai's bot-detection on this domain may be fingerprinting at the TLS/JS-challenge level in a way that blocks any non-browser HTTP client regardless of source IP, in which case Task 2 would need a headless-browser-based fetch (e.g., Playwright/Selenium hitting the JSON endpoints the way this investigation's browser tool did) rather than NSE's lightweight `requests.Session` approach — a meaningfully heavier architecture. **Task 2's builder must run one real request (the date-wise endpoint for a recent date, with the headers above) from the actual intended runtime/deployment host before writing `mcx_bhavcopy.py`, and build around whichever of (a)/(b) turns out true.** This is a one-line/one-request check, not a research project — it just has to happen from the real host, not this sandbox.

**Lot size: not in the bhavcopy data, but not a blocker (unlike NSE's case).** Neither JSON endpoint includes a lot-size field (confirmed: the full field list on every row is listed above, no lot/quantity-per-lot column exists). `/market-data/contract-available-for-trading` (Current/History tabs) was also checked directly — it lists `Instrument Type, Symbol, Product Month, Option Type, Strike Price, Start Date, End Date, Tender/Staggered Start/End Date`, also no lot size. Unlike NSE single-stock-futures lot sizes (which change with corporate actions and genuinely need a per-date historical value — the gap `nse_bhavcopy.py`'s docstring flags as unresolved pre-UDiFF), MCX commodity lot sizes are fixed exchange product specifications that change only via occasional circulars, not daily/contract-level data. **This means `mcx_instruments.py` can carry a small static lot-size table per commodity** (current values per `docs/13` §4, to be verified by the Task 2 builder against MCX's published contract specifications/circulars rather than treated as a per-bar data field) — a materially simpler problem than the NSE case, not an open risk for Phase 0.

**History depth: confirmed ~23 years, deeper than NSE's own archive.** Earliest data with real trading seen: 10 Nov 2003 (ALUMINI). GOLD's expiry-date dropdown (populated from the exchange's own contract master, not bhavcopy) lists contracts back to `13FEB2004` and forward to `05OCT2027` (contracts not yet started trading). Spot-checked and confirmed present: `GOLD`, `GOLDM`, `SILVER`, `SILVERM`, `SILVERMIC`, `CRUDEOIL`, `CRUDEOILM`, `NATURALGAS`, `NATGASMINI` (the exact core-4-plus-mini/micro-variants universe from `docs/13` §4) on both 15 Jan 2020 and 10 Nov 2026. The full commodity list available via the "Commodity Wise" dropdown (158 symbols) also includes `ZINC`/`ZINCMINI`, `COPPER`/`COPPERM`, `ALUMINIUM`/`ALUMINI` and the agri names excluded by `docs/13` §2 scope.

**Rate limiting:** not exhaustively load-tested (out of scope for a one-time Phase 0 check), but roughly 15-20 sequential JSON requests across widely different dates (2003, 2004, 2015, 2020, 2026) all returned immediately with no 429s, no visible throttling, and no CAPTCHA/challenge pages. No official rate-limit documentation was found. Recommend the same conservative default posture as `nse_bhavcopy.py` (persistent session, aggressive disk caching keyed by date/contract, small delay between real network fetches, retry-with-backoff on transient failures) until proven unnecessary.

**Sample files saved** (`tests/fixtures/mcx_bhavcopy/`), all real, unmodified API responses captured during this investigation:
- `futcom_2015-06-15.json` — full `GetDateWiseBhavCopy` response, `InstrumentName=FUTCOM`, all commodities, 15 Jun 2015 (138 rows).
- `futcom_2020-01-15.json` — `GetDateWiseBhavCopy` response for 15 Jan 2020, trimmed to a representative subset of the 138 real rows (kept full rows for GOLD, SILVER, SILVERMIC, CRUDEOIL, NATURALGAS, ZINCMINI and others, dropped the rest for file size — not a format change).
- `futcom_2026-11-10_core4.json` — `GetDateWiseBhavCopy` response for 10 Nov 2026, filtered down to just the `docs/13` §4 core-4-plus-variants symbols (`GOLD`, `GOLDM`, `SILVER`, `SILVERM`, `SILVERMIC`, `CRUDEOIL`, `CRUDEOILM`, `NATURALGAS`, `NATGASMINI`) out of that day's 16,648 real rows.
- `gold_05jun2015_commoditywise_trimmed.json` — `GetCommoditywiseBhavCopy` response for `GOLD`/`05JUN2015`, trimmed to the first and last 15 of 174 real rows (full contract life, listing-to-expiry), with a `totalRows`/`Note` field added to the saved file documenting the trim — demonstrates the full-contract-life single-call shape.

**Why GO, not NO-GO:** the brief's NO-GO trigger is "no usable free historical source exists" — that is clearly not the case here. MCX publishes free, no-login, JSON-accessible daily OHLCV+volume+OI per contract back to the exchange's 2003 launch, for exactly the commodities this strategy needs. The Akamai access-method question is a real, non-trivial engineering risk that could change `mcx_bhavcopy.py`'s implementation shape (plain HTTP session vs. headless-browser-driven fetch), but it is not a "the data doesn't exist" finding, and `docs/12`'s own precedent (the NSE legacy/UDiFF header dance) shows that working around exchange-side access friction is normal, buildable work for this system, not a reason to abandon the hypothesis. Task 2 should proceed, with its very first step being the one-request access check from the real deployment host described above.

## 13. Results (2026-11-12) — Task 9, full walk-forward run against live MCX data

**Verdict: INCONCLUSIVE.** The walk-forward validator could not be run against live MCX data in this environment. Per-gate pass/fail, OOS Sharpe/Calmar/max-drawdown, and profit factor/expectancy cannot be reported for GOLDM, SILVERMIC, CRUDEOILM, or NATGASMINI, because no live bhavcopy data could be fetched for any of them. This is a data-access finding, not a strategy-quality finding — see below for what was and wasn't verified.

### What was run

```
.venv/bin/python -m backtesting.run_commodity_walk_forward --symbols GOLDM SILVERMIC CRUDEOILM NATGASMINI
```

from this repo's `mcx-commodities-momentum` worktree, `.venv/bin/python`, no Kite session configured (lot sizes resolved from the static table only, as expected without a broker session).

### What actually happened

The run reached the data layer and began fetching MCX bhavcopy history starting at the earliest backtest date (2015-11-12). Every single HTTP request returned **403 Access Denied** from MCX's Akamai edge — not an application-level MCX error, the same Akamai block Task 8's smoke test hit and documented. The loader retried each date 3 times with backoff (per Task 8's retry policy), confirmed 403 on every attempt, and did not cache the failure (also per Task 8's design, so a later real recovery isn't poisoned by a cached failure). After 5 consecutive failed dates (2015-11-12, 11-13, 11-16, 11-17, 11-18), the Task 8 circuit-breaker (`MCXAccessBlockedError`) tripped and aborted the fetch cleanly:

```
[!] Could not reach Kite (Kite session file missing ...); resolving lot sizes from the static table only.
2026-11-12 00:22:10 | WARNING | MCX fetch attempt 1/3 got status 403 for 2015-11-12
2026-11-12 00:22:12 | WARNING | MCX fetch attempt 2/3 got status 403 for 2015-11-12
2026-11-12 00:22:16 | WARNING | MCX fetch attempt 3/3 got status 403 for 2015-11-12
2026-11-12 00:22:22 | WARNING | Giving up on MCX bhavcopy for 2015-11-12 after 3 attempts: Unexpected status 403 for 2015-11-12
2026-11-12 00:22:22 | WARNING | Could not fetch MCX bhavcopy for 2015-11-12 (consecutive failures: 1); not caching, will retry on a later call.
... (same pattern for 2015-11-13, 2015-11-16, 2015-11-17, 2015-11-18) ...
2026-11-12 00:22:59 | WARNING | Giving up on MCX bhavcopy for 2015-11-18 after 3 attempts: Unexpected status 403 for 2015-11-18
2026-11-12 00:23:11 | WARNING | Could not fetch MCX bhavcopy for 2015-11-18 (consecutive failures: 5); not caching, will retry on a later call.

[!] MCX access appears blocked (5 consecutive failures) -- aborting fetch, see data/loaders/mcx_bhavcopy.py docstring for the known access-method risk.
```

This took about 70 seconds wall-clock and ended on its own — it is a clean, fast abort via the circuit-breaker Task 8 built specifically for this scenario, not a multi-hour grind and not a crash. No symbol produced any usable bars; nothing downstream of the fetch (signal generation, sizing, the engine, the validator's gates) ever ran.

### What was observed, and from where (corrected in final review)

What is actually established, stated as narrowly as the evidence allows:

- **Scripted `requests` calls got HTTP 403 from MCX's Akamai edge.** Task 9's run (above), and Task 8's smoke test before it, were started from this repo's worktree on the user's macOS machine, through Claude Code's Bash tool. One more scripted request was made by accident during the final-review fix wave, when a unit test that had not been updated reached the live endpoint for 2015-06-15. It also got 403 on all 3 attempts. That test is now fixed, and `tests/test_mcx_bhavcopy.py` has an autouse guard that refuses any real network call.
- **Browser calls succeeded.** Task 1's Phase 0 calls (§3.1) went through the built-in browser tool, a real Chromium session, and all of them worked.
- **Not established: whether both kinds of call left from the same network egress / public IP.** §3.1 describes its own scripted `curl` attempts as coming from "this sandboxed dev environment's network egress". Claude Code's Bash tool can run commands inside a sandbox that may route or filter network traffic differently from the desktop app's browser pane. No proxy environment variables were visible to the Bash tool during the fix wave, but that does not prove the two paths shared an egress IP, and nobody compared egress IPs. So "the browser works, scripted requests don't" has **not** been shown to be a clean same-host, same-IP comparison.

That leaves §3.1's two hypotheses **both open**, and neither should be treated as settled: (a) an IP/network-range block, which running the same `requests` code from a different host or network could get around; or (b) Akamai fingerprinting non-browser clients at the TLS/JS-challenge level whatever the source IP, which would need a browser-driven fetch. The discriminating test is still the one §3.1 asked for: one plain request, sent from a host whose egress is known to match a browser that succeeds (or from a different, ordinary residential/office connection), compared side by side.

### What this run does NOT mean

It does not mean the strategy is bad, or that MCX lacks usable data. §3.1 already confirmed MCX publishes free, no-login daily OHLCV+OI history back to 2003 for exactly this symbol set: the data exists, and this environment cannot currently reach it over a scripted HTTP client. **It also does not mean the pipeline's logic had been shown correct.** The original version of this section claimed that, and the claim was wrong (see the next two subsections).

### What WAS verified (independent of the live-fetch problem), as originally written and as corrected

- At the time of Task 9, all 247 unit/integration tests across Tasks 1-8 passed. Passing tests did **not** prove the pipeline correct, though: the final whole-branch review found a Critical money-correctness bug (next subsection) that none of those 247 tests caught, because none of them checked a rupee figure against real exchange data. After the final-review fixes the suite has 308 tests, all passing.
- The back-adjustment/roll math, cost model, signal generation, vol-target position sizing, backtest engine and walk-forward validator were built and reviewed against synthetic fixtures and the real captured MCX bhavcopy fixtures in `tests/fixtures/mcx_bhavcopy/` (listed in §3.1). ~~Correctness of the pipeline's logic is not in question.~~ **Retracted.** The review found that every GOLDM rupee figure was overstated 10x (next subsection). It also found that the roll-math and HTML-trap/retry code, where the earlier fixes landed, had no direct unit tests.
- The CLI runner (Task 8) wires the above together and handles the missing-Kite-session case by falling back to the static lot-size table. Its circuit-breaker aborts fast on persistent access failure and does not cache failed fetches. That still holds. In the original build, though, intermittent failed dates were indistinguishable from holidays (fixed below).
- ~~What remains unverified, specifically and only: a complete walk-forward run against live MCX data. Nothing else in the pipeline is implicated.~~ **Retracted.** The pricing bug below sat squarely in the pipeline, independent of data access.

### 13.1 Final-review corrections (2026-11-12) — a Critical pricing bug was found and fixed

**Critical: GOLDM's price quote unit was wrong, overstating every GOLDM rupee figure 10x.** MCX quotes Gold Mini in ₹ per **10 grams**, but its lot is **100 grams**. The pipeline computed rupees as `quantity (grams) × quoted price`, with no unit conversion, so one GOLDM lot was valued at 100× the quote instead of 10×. That error flowed into vol-target sizing, entry/exit value, the cost-model inputs, gross/net P&L, mark-to-market equity and exposure. It is proven from MCX's own data rather than asserted: each bhavcopy row reports traded `Value` (₹ lakh) and `Volume` (lots), so `Value × 1e5 / Volume / Close` is the rupees per lot per quoted point. In the real fixtures that comes out at ~10.0 for GOLDM (9.96 in 2015, 10.05 in 2026), i.e. lot 100 g ÷ quote unit 10 g; ~1.0 for SILVERMIC (1 kg lot, quoted per kg); ~10 for CRUDEOILM (10 bbl lot, quoted per bbl); and ~248 for NATGASMINI (250 mmBtu lot, quoted per mmBtu). **GOLDM is the only one of the four that needs a conversion.**

*Fix and convention.* `data/loaders/mcx_instruments.py` now has a per-symbol `price_quote_unit` (GOLDM 10, the others 1), defined as the lot-size units that one quoted price refers to. It is a **divisor**: `rupees = quantity_in_lot_units × quoted_price / price_quote_unit`. Prices stay raw exchange quotes end to end, so trade logs still match the bhavcopy. The value travels as a required overlay column into `CommodityBacktestEngine` (entry/exit value → cost model, P&L, mark-to-market, exposure). It is also a required keyword argument of `calculate_vol_target_position_size`. The column has no silent default of 1, because a silent 1 is exactly how the bug hid. `tests/test_mcx_instruments.py` derives the quote unit from the real fixtures' `Value/Volume/Close` and does not assert a hand-picked constant.

*Re-verified economics after the fix* (illustrative, at the default ₹1,00,000 capital, 35% vol target, 25% margin, using fixture closes and assumed realized vols of ~15% gold, ~25% silver, ~35% crude, ~55% natgas): a GOLDM lot is ~₹2.7 lakh notional at the 2015 quote and ~₹14 lakh at the 2026 quote, not ₹27 lakh / ₹1.4 crore. Even corrected, GOLDM sizes to **0 lots**: in 2015 its margin (~₹67k) fits, but one lot's risk exceeds the ₹35k vol budget; in 2026 neither fits. SILVERMIC and NATGASMINI also size to 0 lots at the 2026 quotes (vol-target-bound). That is why the zero-size counter below matters.

**Other final-review fixes made in the same wave** (details in the code docstrings):

- *Zero-size entries are visible.* `CommodityBacktestSummary` now counts entry attempts and the ones that sized to 0 lots, split into vol-target-bound and margin-bound. A warning is logged, and the CLI table shows an `OOS 0-lot` column. A symbol that never sized a single lot is labelled "NEVER TRADEABLE AT THIS CAPITAL" in the "Beats own buy-and-hold" gate detail. The gate's pass/fail logic and the default capital are unchanged.
- *Failed fetch dates are separated from holidays.* `get_historical_commodity_bars` classifies every weekday as data / genuine no-data / FAILED in a `BhavcopyFetchReport`, and always logs a summary. The CLI prints a "DATA HOLES" warning before the verdict whenever any date failed.
- *Kite's lot size is sanity-checked.* On any mismatch with the static table, the loader logs a warning and **keeps the static value**. §3 already warned that Kite's MCX lot sizes have been observed wrong, Kite expresses MCX quantity in lots, and the static table is cross-checked against bhavcopy turnover, so a mismatch is far more likely a Kite artefact than a real lot change. A genuine exchange lot change must be applied by deliberately updating the static table.
- *Direct tests* now cover `_active_contract_rows`, `_roll_ratio` and `_back_adjust` (synthetic two-roll scenario: roll-day returns, compounding), `_fetch_date_wise_json` (HTML-200 trap, missing `IsSuccess`, 403 backoff, exceptions, genuine no-data not retried), and the cost model (component sum, ₹20 per-leg brokerage cap, GST base).
- *Known limitation recorded, not fixed:* the ~2-trading-day flat gap at every rollover (§7).

### Honest next steps (per spec §3's own fallback options, not re-litigated here)

This is the same honesty convention `docs/10` §3 and `docs/12` §5 already set for this repo: write up a negative/inconclusive result plainly and stop, rather than keep re-tuning or re-running against a live blocker until something that looks like success appears. Continuing to fight the Akamai 403 from this sandbox (rotating headers, trying a headless browser, etc.) would be the same bot-check-circumvention problem already correctly declined earlier in this plan — it is out of scope here too. The spec's own cited fallback options, unchanged by this run, are:

1. **Paid vendor** — a commercial MCX historical-data provider that isn't gated by Akamai's bot detection on the free bhavcopy endpoint.
2. **Accumulate live-contract history some other way** — e.g. run the ingester from a non-sandboxed host (the user's own machine/network, or an India-based host) where §3.1's browser-based access already succeeded, and build a local cache over time instead of requiring instant deep history.
3. **Park this hypothesis** — treat the MCX commodities momentum strategy as built and tested but not yet validated against live data, and revisit if/when a working access path appears, without further investment in circumventing the block in the meantime.

### 13.2 Kite Connect tested directly (2026-11-12) — confirmed not a substitute

Tested live, with a real authenticated Kite session (not the free bhavcopy endpoint): `kite.instruments("MCX")` lists only currently-tradable contracts (748 GOLDM-prefixed rows, all forward-dated, nothing expired) — there is no catalog path to an expired contract's `instrument_token`, so no way to look up history for anything already rolled off. `kite.historical_data()` on a currently-active contract (CRUDEOILM expiring 2026-11-19) returned only 128 daily candles starting 2026-05-19 — exactly the contract's own listed lifetime, nothing before. Kite's per-row `lot_size` was `1` for every MCX instrument checked, confirming §3's warning that Kite's MCX lot sizes are unreliable. **Conclusion: Kite cannot provide multi-year MCX history under any circumstance — this isn't a credentials or rate-limit problem, it's that Kite simply doesn't retain pre-contract-listing or post-expiry data.** This closes off Kite as a workaround for §3's access problem; the three fallback options above stand unchanged.

**User decision (2026-11-12): park this hypothesis (option 3) and move to a crypto-based satellite-sleeve hypothesis instead.** No further MCX work is planned unless a working data-access path appears later.

~~Tasks 1-8's code is complete, tested, and ready to run the moment any of the three paths above produces a reachable data source; no code changes are implied by this result.~~ **Corrected:** code changes *were* needed. The final whole-branch review found the Critical GOLDM pricing bug and the visibility gaps described in §13.1, and they were fixed before this plan closed. With those fixes, the pipeline is ready to run once any of the three paths above produces a reachable data source. That readiness rests on the expanded test suite, which now includes a check of rupee values against real exchange turnover, and the remaining known limitation (§7's rollover flat gap) still applies. No live walk-forward result exists yet.

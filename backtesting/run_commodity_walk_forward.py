"""Runs universe-level walk-forward validation (CommodityWalkForwardValidator)
for the MCX long/short time-series momentum strategy across the default
Mini/Micro commodity universe (docs/13-mcx-commodities-momentum-design.md
§4), printing the same style of gate-by-gate report
`run_futures_walk_forward.py` prints for NSE single-stock futures (Task 7's
`FuturesWalkForwardValidator`), swapped to `CommodityUniverseValidationReport`'s
own fields (per-symbol own buy-and-hold instead of a shared Nifty benchmark,
Calmar + history-length gates instead of a trade-count floor).

Usage:
    python -m backtesting.run_commodity_walk_forward --symbols GOLDM
    python -m backtesting.run_commodity_walk_forward --symbols GOLDM SILVERMIC CRUDEOILM NATGASMINI

**Data assembly (this module's own job, not a separate sync module -- see
task brief):** `data.loaders.mcx_instruments.get_commodity_contracts` and
`data.loaders.mcx_bhavcopy.get_historical_commodity_bars` each give a piece
of the picture -- an approximated contract calendar, and raw per-live-
contract-per-day OHLC bars -- but neither alone produces the two series
`CommodityBacktestEngine`/`CommodityWalkForwardValidator` actually consume
(docs/13 §7): a back-adjusted *signal* series (`continuous_df`, columns
open/high/low/close) and a real front-month *execution* series (`overlay_df`,
columns fut_open/fut_high/fut_low/fut_close/lot_size/expiry/tradingsymbol).
`_assemble_symbol` below builds both from `get_historical_commodity_bars`'s
raw rows, mirroring `data/loaders/sync_futures_history.py`'s role in the NFO
futures pipeline:

1. **Active-contract selection.** For each date, the active contract is the
   one with the nearest expiry that has not yet passed (`expiry >= date`)
   among that date's live contracts. Unlike `sync_futures_history.
   select_active_contract` (which switches to the *next* contract once
   inside the rollover buffer, for NFO futures), this keeps the held
   contract listed through its own real expiry date and only switches the
   day after. This is a deliberate difference, required by
   `backtesting/commodity_engine.py`'s own docstring ("Task 8 ... must keep
   the held contract listed in the overlay through the buffer-exit date"):
   the engine's forced-rollover exit is *decided* days before expiry (on
   the `rollover_buffer_days` countdown) but needs the overlay to still
   list the originally-held contract on the date that exit actually fills,
   to avoid pricing the exit off a different contract's quote (the
   "contract-switch pricing bias" that module's docstring documents).
2. **Overlay** is this active-contract OHLC, verbatim (raw exchange
   quotes, never unit-converted), under the `fut_*`/`lot_size`/
   `price_quote_unit`/`expiry`/`tradingsymbol` column names the engine
   expects. `price_quote_unit` (lot-size units per quoted price, 10 for
   GOLDM) is what turns a quote into rupees inside the engine and sizer --
   see `data.loaders.mcx_instruments` PRICE QUOTE UNIT.
3. **Continuous (signal) series** is the same active-contract sequence,
   back-adjusted with the standard ratio ("Panama canal") method: at every
   roll, every earlier bar's OHLC is scaled by the ratio of the new
   contract's close to the old contract's close *on the same date* -- the
   old contract's last active day, on which the new (next-month) contract
   is also quoted in the raw bhavcopy rows. So the series has no artificial
   jump at the roll, the roll day's return equals the new contract's own
   day-over-day return (not a forced 0%), and the most recent contract's
   absolute price level is preserved (percentage returns are exact; only
   the pre-roll *levels* are rescaled). If the new contract has no quote on
   that date, the nearest earlier common date is used, with a warning.

**Data-quality surfacing.** `assemble_commodity_data` passes a
`BhavcopyFetchReport` into `get_historical_commodity_bars`, so weekdays whose
fetch FAILED (no genuine MCX response) are counted separately from genuine
no-data holidays. `build_universe_data` can return these per symbol, and the
CLI prints a data-quality warning ahead of the verdict whenever any date
failed -- a verdict on data with holes is flagged, not silently printed.

A symbol with no fetchable history at all -- an unsupported `--symbols`
value (`get_commodity_contracts`/`get_historical_commodity_bars` both raise
`ValueError` for anything outside the core-4 GOLDM/SILVERMIC/CRUDEOILM/
NATGASMINI lookup table), a live-data fetch that comes back empty, or a
built continuous/overlay pair with no rows -- is skipped with a logged
warning, never a crash, mirroring `FuturesWalkForwardValidator.
_tradable_symbols`'s tolerance for a symbol with no overlay data at all.

**Default history window.** `--start-date`/`--end-date` are not exposed as
flags (not in this task's brief); the module defaults to the trailing ~11
years, matching docs/13 §8's "at least 5 years OOS" gate once
`CommodityWalkForwardValidator`'s default 50/50 train/test split is applied
(and the synthetic fixture `tests/test_commodity_walk_forward.py` uses the
same span). This means a real run fetches roughly 11 years x ~260 trading
days of MCX bhavcopy, one HTTP request per calendar day
(`get_historical_commodity_bars` has no bulk-range endpoint wired in yet --
see `data/loaders/mcx_bhavcopy.py`'s own docstring on
`GetCommoditywiseBhavCopy` being the more efficient, not-yet-used,
alternative) -- expect a slow first run before the on-disk cache warms up.
"""
import argparse
from typing import Dict, List, Optional, Tuple

import pandas as pd

from backtesting.commodity_walk_forward import CommodityUniverseValidationReport, CommodityWalkForwardValidator
from backtesting.engine import colorize_signed
from configs.logger import logger
from data.loaders.kite_client import get_kite_client
from data.loaders.mcx_bhavcopy import BhavcopyFetchReport, MCXAccessBlockedError, get_historical_commodity_bars
from data.loaders.mcx_instruments import get_commodity_contracts

DEFAULT_SYMBOLS = ["GOLDM", "SILVERMIC", "CRUDEOILM", "NATGASMINI"]

CONTINUOUS_COLUMNS = ["open", "high", "low", "close"]
OVERLAY_COLUMNS = [
    "fut_open", "fut_high", "fut_low", "fut_close", "lot_size", "price_quote_unit", "expiry", "tradingsymbol",
]


def _active_contract_rows(raw: pd.DataFrame, lot_size_override: Optional[int]) -> pd.DataFrame:
    """Reduces `get_historical_commodity_bars`'s per-live-contract-per-day rows
    (several rows can share a date) to one row per date: the nearest-expiry
    contract that has not yet expired as of that date (see module docstring
    for why this differs from `sync_futures_history.select_active_contract`'s
    early, buffer-based switch). Returns a DataFrame indexed by date with
    columns tradingsymbol/expiry/open/high/low/close/lot_size/price_quote_unit,
    sorted ascending; empty (same columns) if no date has an unexpired
    contract. `price_quote_unit` is carried through from `raw` unchanged and
    is required there (no silent default)."""
    columns = ["tradingsymbol", "expiry", "open", "high", "low", "close", "lot_size", "price_quote_unit"]
    if raw.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for ts, group in raw.groupby(raw.index):
        unexpired = group[group["expiry"] >= ts]
        if unexpired.empty:
            continue
        # .iloc, not .loc: every row in `group`/`unexpired` shares the same
        # index label (the date), so a label-based lookup would return every
        # tied row instead of the one nearest-expiry contract.
        active = unexpired.iloc[unexpired["expiry"].reset_index(drop=True).idxmin()]
        lot_size = int(lot_size_override) if lot_size_override else int(active["lot_size"])
        rows.append({
            "date": pd.Timestamp(ts), "tradingsymbol": active["tradingsymbol"],
            "expiry": pd.Timestamp(active["expiry"]), "open": float(active["open"]),
            "high": float(active["high"]), "low": float(active["low"]), "close": float(active["close"]),
            "lot_size": lot_size, "price_quote_unit": int(active["price_quote_unit"]),
        })

    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows).set_index("date").sort_index()


def _roll_ratio(raw_closes: Dict[Tuple[pd.Timestamp, str], float], dates: List[pd.Timestamp],
                old_sym: str, new_sym: str, old_last_date: pd.Timestamp,
                fallback: float) -> float:
    """new/old close ratio for a roll, using BOTH contracts' closes on the SAME
    date: the old contract's last active day if the new contract quoted then
    (the normal case), else the nearest earlier date both quoted (warning).
    If they never co-quoted, returns `fallback` (new-close-on-roll-day /
    old-close-day-before, i.e. a zeroed roll-day return) with a warning."""
    old_close = raw_closes.get((old_last_date, old_sym))
    new_close = raw_closes.get((old_last_date, new_sym))
    if old_close and new_close:
        return new_close / old_close

    for d in reversed([d for d in dates if d < old_last_date]):
        old_close = raw_closes.get((d, old_sym))
        new_close = raw_closes.get((d, new_sym))
        if old_close and new_close:
            logger.warning(
                f"Roll {old_sym}->{new_sym}: {new_sym} has no quote on {old_last_date.date()}; "
                f"using nearest common date {d.date()} for the roll ratio."
            )
            return new_close / old_close

    logger.warning(
        f"Roll {old_sym}->{new_sym}: no common quoted date found; falling back to a "
        f"cross-date ratio (the roll-day return on the day after {old_last_date.date()} will read as 0%)."
    )
    return fallback


def _back_adjust(active: pd.DataFrame, raw: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Builds the back-adjusted (ratio/"Panama canal") continuous close
    series from `_active_contract_rows`'s one-row-per-date active-contract
    sequence. See module docstring point 3 for the method.

    `raw` is `get_historical_commodity_bars`'s every-live-contract-per-day
    frame, needed to read the *new* contract's close on the *old* contract's
    last active day, so each roll ratio compares the two contracts on the
    same date and the roll day's adjusted return equals the new contract's
    own day-over-day return (not an artificial 0%)."""
    if active.empty:
        return pd.DataFrame(columns=CONTINUOUS_COLUMNS)

    raw_closes: Dict[Tuple[pd.Timestamp, str], float] = {}
    if raw is not None and not raw.empty:
        for ts, sym, close in zip(raw.index, raw["tradingsymbol"], raw["close"]):
            raw_closes[(pd.Timestamp(ts), sym)] = float(close)
    all_dates = sorted({k[0] for k in raw_closes})

    n = len(active)
    dates = [pd.Timestamp(d) for d in active.index]
    symbols = active["tradingsymbol"].to_numpy()
    raw_open = active["open"].to_numpy(dtype=float)
    raw_high = active["high"].to_numpy(dtype=float)
    raw_low = active["low"].to_numpy(dtype=float)
    raw_close = active["close"].to_numpy(dtype=float)

    adj_open = raw_open.copy()
    adj_high = raw_high.copy()
    adj_low = raw_low.copy()
    adj_close = raw_close.copy()

    cum = 1.0
    for i in range(n - 1, 0, -1):
        if symbols[i] != symbols[i - 1] and raw_close[i - 1] != 0:
            # Roll between day i-1 (old contract's last active day) and day
            # i (new contract's first). Both ratios are raw/raw on the same
            # date, so the earlier segment's factor *compounds* onto the
            # later segment's: cum_earlier = cum_later * new/old.
            fallback = raw_close[i] / raw_close[i - 1]
            ratio = _roll_ratio(raw_closes, all_dates, symbols[i - 1], symbols[i], dates[i - 1], fallback)
            cum = cum * ratio
        adj_open[i - 1] *= cum
        adj_high[i - 1] *= cum
        adj_low[i - 1] *= cum
        adj_close[i - 1] *= cum

    return pd.DataFrame(
        {"open": adj_open, "high": adj_high, "low": adj_low, "close": adj_close},
        index=active.index,
    )


def assemble_commodity_data(
    symbol: str, start_date, end_date, kite=None,
    fetch_report: Optional[BhavcopyFetchReport] = None,
) -> Optional[Tuple[pd.DataFrame, pd.DataFrame]]:
    """Builds (continuous_df, overlay_df) for one commodity `symbol` by
    calling Task 2's `get_commodity_contracts` and Task 3's
    `get_historical_commodity_bars`, then assembling the signal/execution
    pair per the module docstring. Returns None -- logging a warning, never
    raising -- for a symbol with no fetchable history at all: an unsupported
    symbol (both calls raise ValueError for the same reason), an empty live
    fetch, or a built table with no rows.

    `fetch_report`, if given, is filled in by `get_historical_commodity_bars`
    with the failed-vs-genuine-no-data date accounting (a failed fetch date
    must never pass silently as a holiday)."""
    symbol = symbol.upper()

    try:
        contracts = get_commodity_contracts(symbol, start_date, end_date, kite=kite)
    except ValueError as exc:
        logger.warning(f"Skipping {symbol}: {exc}")
        return None

    # A live-resolved current lot size (Kite's MCX catalog), when reachable,
    # overrides the static table both get_commodity_contracts and
    # get_historical_commodity_bars otherwise fall back to -- same posture as
    # sync_futures_history._resolve_current_lot_size for NFO futures. Kite's
    # value is NOT trusted blindly: get_commodity_contracts has already run it
    # through mcx_instruments._validated_live_lot_size, which warns and keeps
    # the static (bhavcopy-cross-checked) value on any mismatch (docs/13 §3
    # warns Kite's MCX lot sizes have been observed wrong).
    live_lot_size = None
    for contract in contracts:
        if contract.instrument_token is not None:
            live_lot_size = contract.lot_size
            break

    try:
        raw = get_historical_commodity_bars(symbol, start_date, end_date, fetch_report=fetch_report)
    except ValueError as exc:
        logger.warning(f"Skipping {symbol}: {exc}")
        return None

    if raw.empty:
        logger.warning(f"Skipping {symbol}: no MCX bhavcopy history fetched for {start_date}..{end_date}.")
        return None

    active = _active_contract_rows(raw, live_lot_size)
    if active.empty:
        logger.warning(f"Skipping {symbol}: fetched bhavcopy rows but none had an unexpired contract.")
        return None

    continuous_df = _back_adjust(active, raw)
    overlay_df = pd.DataFrame(
        {
            "fut_open": active["open"], "fut_high": active["high"], "fut_low": active["low"],
            "fut_close": active["close"], "lot_size": active["lot_size"],
            "price_quote_unit": active["price_quote_unit"], "expiry": active["expiry"],
            "tradingsymbol": active["tradingsymbol"],
        },
        index=active.index,
    )
    return continuous_df, overlay_df


def build_universe_data(
    symbols: List[str], start_date, end_date, kite=None,
    fetch_reports: Optional[Dict[str, BhavcopyFetchReport]] = None,
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame]]:
    """Calls `assemble_commodity_data` per symbol, skipping (with a logged
    warning, not a crash) any symbol with no fetchable history -- the same
    tolerance `FuturesWalkForwardValidator._tradable_symbols` has for a
    symbol missing futures data entirely.

    If `fetch_reports` (a dict) is given, it receives one
    `BhavcopyFetchReport` per requested symbol (filled in as far as the fetch
    got, even if it later raised `MCXAccessBlockedError`)."""
    continuous_data: Dict[str, pd.DataFrame] = {}
    overlay_data: Dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        report = BhavcopyFetchReport()
        if fetch_reports is not None:
            fetch_reports[symbol.upper()] = report
        result = assemble_commodity_data(symbol, start_date, end_date, kite=kite, fetch_report=report)
        if result is None:
            continue
        continuous_df, overlay_df = result
        continuous_data[symbol.upper()] = continuous_df
        overlay_data[symbol.upper()] = overlay_df
    return continuous_data, overlay_data


def data_quality_lines(fetch_reports: Dict[str, BhavcopyFetchReport]) -> List[str]:
    """Human-readable data-quality warnings: one line per symbol whose range
    had FAILED fetches (data missing, not holidays). Empty when clean."""
    lines = []
    for symbol, fr in sorted(fetch_reports.items()):
        if fr.has_failures:
            lines.append(
                f"[!] DATA HOLES {symbol}: {fr.failed_count} of {fr.weekdays_requested} weekday(s) had "
                f"FETCH FAILURES (missing data, not holidays) -- treat this symbol's results with caution."
            )
    return lines


def print_report(report: CommodityUniverseValidationReport,
                 fetch_reports: Optional[Dict[str, BhavcopyFetchReport]] = None) -> None:
    is_s, oos = report.in_sample, report.out_of_sample
    width = 104
    print("\n" + "=" * width)
    print("  ARTHA QUANT — MCX COMMODITY WALK-FORWARD VALIDATION (long/short momentum)")
    print(f"  In-sample {is_s.start.date()} -> {is_s.end.date()}  |  Out-of-sample {oos.start.date()} -> {oos.end.date()}")
    print("=" * width)
    print(f"{'Symbol':<12}{'IS Trd':>7}{'OOS Trd':>8}{'OOS Ret%':>10}{'B&H Ret%':>10}{'OOS PF':>8}{'Exposure%':>11}"
          f"{'OOS 0-lot':>11}")
    print("-" * width)
    for symbol, (is_sum, oos_sum) in sorted(report.per_symbol.items(), key=lambda kv: -kv[1][1].net_return_pct):
        is_trades = is_sum.total_trades if is_sum else 0
        bh = report.benchmarks_oos.get(symbol)
        bh_ret = bh.net_return_pct if bh else float("nan")
        print(f"{symbol:<12}{is_trades:>7}{oos_sum.total_trades:>8}"
              f"{colorize_signed(f'{oos_sum.net_return_pct:>10.2f}', oos_sum.net_return_pct)}"
              f"{bh_ret:>10.2f}{oos_sum.profit_factor:>8.2f}{oos_sum.exposure_pct:>11.1f}"
              f"{f'{oos_sum.zero_size_entry_count}/{oos_sum.entry_attempt_count}':>11}")
    print("-" * width)
    print("OOS 0-lot = entry attempts that sized to 0 lots / all entry attempts (vol-target budget or margin")
    print("could not afford one lot at this capital). A symbol at n/n never traded: see the gate notes below.")
    print("Per-symbol rows are pre-tax and informational only (see backtesting/commodity_engine.py: no tax")
    print("is modeled anywhere in this engine). Gates below are judged on the pooled portfolio.\n")

    print(f"{'Portfolio':<22}{'Trades':>7}{'Win%':>7}{'PF':>6}{'Exp%':>7}{'CAGR%':>8}{'Sharpe':>8}{'Calmar':>8}{'MaxDD%':>8}")
    for label, s in (("In-sample", is_s), ("Out-of-sample", oos)):
        print(f"{label:<22}{s.total_trades:>7}{s.win_rate_pct:>7.1f}{s.profit_factor:>6.2f}{s.expectancy_pct:>+7.2f}"
              f"{s.cagr_pct:>+8.2f}{s.sharpe:>8.2f}{s.calmar:>8.2f}{s.max_drawdown_pct:>8.1f}")

    print(f"\nFolds ({len(report.folds)}):")
    for f in report.folds:
        bench_str = f", basket B&H {f.benchmark_return_pct:+.2f}%" if f.benchmark_return_pct is not None else ""
        print(f"  {f.start.date()} -> {f.end.date()}: return {f.return_pct:+.2f}%, "
              f"Sharpe {f.sharpe:.2f}, {f.trades} trades{bench_str}")

    print("\nGates:")
    for g in report.gates:
        status = "PASS" if g.passed else "FAIL"
        print(f"  [{status}] {g.name}: {g.detail}")

    quality = data_quality_lines(fetch_reports or {})
    if quality:
        print("\nData quality:")
        for line in quality:
            print(f"  {line}")

    verdict = "QUALIFIED" if report.is_qualified else "NOT QUALIFIED"
    print(colorize_signed(f"\n>>> MCX commodity momentum: {verdict}", 1.0 if report.is_qualified else -1.0))
    print(
        "\nReminder (docs/13-mcx-commodities-momentum-design.md §7-8): the signal series is a back-adjusted\n"
        "continuous price built by this CLI from MCX's own bhavcopy, the execution series is the real "
        "front-month\ncontract; neither is a modelled cash-market proxy. No tax is applied to the strategy "
        "side. Treat a QUALIFIED\nverdict on a thin 4-instrument universe as a first read, not a final one."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Artha Quant MCX Commodity Walk-Forward Validation")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS, help="MCX commodities to validate (default: the four Mini/Micro instruments)")
    parser.add_argument("--capital-per-symbol", type=float, default=100000.0)
    parser.add_argument("--vol-target", type=float, default=35.0)
    args = parser.parse_args()

    end_date = pd.Timestamp.today().normalize()
    start_date = end_date - pd.DateOffset(years=11)

    kite = None
    try:
        kite = get_kite_client(verify_remote=True)
    except Exception as exc:
        print(f"[!] Could not reach Kite ({exc}); resolving lot sizes from the static table only.")

    fetch_reports: Dict[str, BhavcopyFetchReport] = {}
    try:
        continuous_data, overlay_data = build_universe_data(
            [s.upper() for s in args.symbols], start_date, end_date, kite=kite,
            fetch_reports=fetch_reports,
        )
    except MCXAccessBlockedError as exc:
        # Persistent fetch failure (likely the Akamai block) -- abort the whole
        # run rather than retrying every remaining date for every symbol.
        print(f"\n[!] {exc}")
        raise SystemExit(1)

    for line in data_quality_lines(fetch_reports):
        print(line)

    if not continuous_data:
        print("\n[!] No MCX commodity history available for any requested symbol. Nothing to validate.")
    else:
        validator = CommodityWalkForwardValidator(
            vol_target_pct=args.vol_target,
            capital_per_symbol=args.capital_per_symbol,
        )
        report = validator.run(continuous_data, overlay_data)
        print_report(report, fetch_reports)

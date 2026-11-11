"""Time-series momentum signal and volatility-target position sizing for MCX commodities."""
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


def compute_momentum_signal(
    continuous_close: pd.Series, lookback_days: int = 252,
) -> pd.Series:
    """Returns -1/0/1 per bar (short/no-signal/long), from the sign of the
    trailing `lookback_days` return. 0 wherever fewer than `lookback_days`
    prior bars exist — never NaN, never a guessed direction from partial
    history (Review Focus: insufficient warm-up)."""
    shifted = continuous_close.shift(lookback_days)
    trailing_return = continuous_close / shifted - 1
    signal = np.sign(trailing_return)
    signal = signal.fillna(0.0)

    warmup_mask = pd.Series(np.arange(len(continuous_close)), index=continuous_close.index) < lookback_days
    signal = signal.where(~warmup_mask, 0.0)

    return signal.astype(int)


def compute_realized_vol(
    continuous_close: pd.Series, vol_lookback_days: int = 60,
) -> pd.Series:
    """Annualized realized volatility (%) per bar, from the trailing
    `vol_lookback_days` daily log-return standard deviation. NaN wherever
    fewer than `vol_lookback_days` prior bars exist (Review Focus:
    deliberately distinct warm-up convention from compute_momentum_signal,
    which returns 0 — this returns NaN instead)."""
    log_returns = np.log(continuous_close / continuous_close.shift(1))
    rolling_std = log_returns.rolling(window=vol_lookback_days, min_periods=vol_lookback_days).std()
    annualized_vol_pct = rolling_std * math.sqrt(252) * 100

    return annualized_vol_pct


@dataclass
class CommoditySizingResult:
    """Result of vol-target position sizing for one MCX commodity position."""
    quantity: int
    num_lots: int
    lot_size: int
    entry_price: float
    realized_vol_pct: float
    vol_target_pct: float
    margin_per_lot: float
    capital_required: float
    capital_allocation_pct: float
    # Lot-size units per quoted price (see data.loaders.mcx_instruments PRICE
    # QUOTE UNIT): rupee notional per lot = lot_size * entry_price / this.
    price_quote_unit: int
    # The two candidate lot counts num_lots is the minimum of, so a caller can
    # tell WHY a size came out 0 (vol budget vs. margin affordability).
    vol_target_lots: int = 0
    max_lots_by_margin: int = 0


def calculate_vol_target_position_size(
    portfolio_capital: float, vol_target_pct: float, entry_price: float,
    lot_size: int, realized_vol_pct: float, margin_pct: float = 25.0,
    *, price_quote_unit: int,
) -> CommoditySizingResult:
    """Scales lot count so the position's notional, times realized_vol_pct,
    approximates vol_target_pct of portfolio_capital. Returns a zero-quantity
    result (never inf/NaN) when realized_vol_pct <= 0 (Review Focus: zero/
    negative realized vol) or any other input is non-positive.

    The vol-target notional is then capped by what the stated margin_pct can
    actually afford: num_lots is the minimum of the vol-target lot count and
    the number of lots whose margin requirement fits within
    portfolio_capital. This guarantees capital_required never exceeds
    portfolio_capital, which bounds leverage (at low realized volatility the
    uncapped vol-target formula alone can imply triple-digit leverage).
    This cap is not a stop-loss or a margin call: it only bounds the size
    taken at entry. An adverse move beyond what margin_pct covers within one
    holding period can still push backtest equity below zero. Trading then
    stops naturally, since this sizer returns 0 lots once capital is
    non-positive.

    `entry_price` is the RAW exchange quote and `lot_size` is in lot-size
    units; `price_quote_unit` (keyword-only, deliberately no default) is how
    many lot-size units that quote refers to, so the rupee notional of one lot
    is `lot_size * entry_price / price_quote_unit` (GOLDM: 100 g lot quoted
    per 10 g -> price_quote_unit=10). See data.loaders.mcx_instruments for
    the convention and its fixture-derived ground truth."""
    if (
        portfolio_capital <= 0
        or entry_price <= 0
        or lot_size <= 0
        or not (price_quote_unit > 0)
        or not (realized_vol_pct > 0)  # catches <= 0 and NaN (NaN > 0 is False)
    ):
        return CommoditySizingResult(
            quantity=0,
            num_lots=0,
            lot_size=lot_size,
            entry_price=entry_price,
            realized_vol_pct=realized_vol_pct,
            vol_target_pct=vol_target_pct,
            margin_per_lot=0.0,
            capital_required=0.0,
            capital_allocation_pct=0.0,
            price_quote_unit=price_quote_unit,
        )

    lot_notional = lot_size * entry_price / price_quote_unit
    target_capital_at_risk = portfolio_capital * (vol_target_pct / 100.0)
    risk_per_lot = lot_notional * (realized_vol_pct / 100.0)

    vol_target_num_lots = max(0, math.floor(target_capital_at_risk / risk_per_lot)) if risk_per_lot > 0 else 0

    margin_per_lot = lot_notional * (margin_pct / 100.0)
    max_lots_by_margin = math.floor(portfolio_capital / margin_per_lot) if margin_per_lot > 0 else vol_target_num_lots

    num_lots = min(vol_target_num_lots, max_lots_by_margin)
    quantity = num_lots * lot_size

    capital_required = round(num_lots * margin_per_lot, 2)
    capital_allocation_pct = round((capital_required / portfolio_capital) * 100.0, 2) if portfolio_capital > 0 else 0.0

    return CommoditySizingResult(
        quantity=quantity,
        num_lots=num_lots,
        lot_size=lot_size,
        entry_price=entry_price,
        realized_vol_pct=realized_vol_pct,
        vol_target_pct=vol_target_pct,
        margin_per_lot=round(margin_per_lot, 2),
        capital_required=capital_required,
        capital_allocation_pct=capital_allocation_pct,
        price_quote_unit=price_quote_unit,
        vol_target_lots=vol_target_num_lots,
        max_lots_by_margin=max_lots_by_margin,
    )

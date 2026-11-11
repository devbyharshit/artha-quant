"""Dual-constraint position sizing engine preventing catastrophic concentration."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class SizingResult:
    """Detailed position sizing breakdown."""
    quantity: int
    qty_by_risk: int
    qty_by_cap: int
    limiting_factor: str          # 'RISK_BUDGET' or 'CAPITAL_CAP'
    entry_price: float
    stop_loss_price: float
    stop_distance: float
    capital_required: float
    capital_allocation_pct: float
    risk_amount: float
    risk_budget_pct: float


def calculate_position_size(
    portfolio_capital: float,
    risk_pct: float,            # e.g. 1.0%
    max_position_pct: float,    # e.g. 10.0%
    entry_price: float,
    stop_loss_price: float
) -> SizingResult:
    """
    Computes position size constrained by two independent ceilings:
    1. Risk Budget: (Capital * Risk%) / Stop Distance
    2. Capital Allocation: (Capital * Max Position%) / Entry Price
    
    The final quantity is strictly min(qty_by_risk, qty_by_cap).
    """
    if portfolio_capital <= 0 or entry_price <= 0:
        return SizingResult(0, 0, 0, "INVALID_INPUT", entry_price, stop_loss_price, 0.0, 0.0, 0.0, 0.0, 0.0)

    stop_distance = abs(entry_price - stop_loss_price)
    if stop_distance <= 0:
        return SizingResult(0, 0, 0, "ZERO_STOP_DISTANCE", entry_price, stop_loss_price, 0.0, 0.0, 0.0, 0.0, 0.0)

    # 1. Quantity by Risk Budget
    risk_budget = portfolio_capital * (risk_pct / 100.0)
    qty_by_risk = int(risk_budget / stop_distance)

    # 2. Quantity by Capital Cap
    capital_cap = portfolio_capital * (max_position_pct / 100.0)
    qty_by_cap = int(capital_cap / entry_price)

    if qty_by_risk <= qty_by_cap:
        final_qty = max(0, qty_by_risk)
        limiting_factor = "RISK_BUDGET"
    else:
        final_qty = max(0, qty_by_cap)
        limiting_factor = "CAPITAL_CAP"

    capital_req = round(final_qty * entry_price, 2)
    alloc_pct = round((capital_req / portfolio_capital) * 100.0, 2) if portfolio_capital > 0 else 0.0
    actual_risk = round(final_qty * stop_distance, 2)
    actual_risk_pct = round((actual_risk / portfolio_capital) * 100.0, 2) if portfolio_capital > 0 else 0.0

    return SizingResult(
        quantity=final_qty,
        qty_by_risk=qty_by_risk,
        qty_by_cap=qty_by_cap,
        limiting_factor=limiting_factor,
        entry_price=entry_price,
        stop_loss_price=stop_loss_price,
        stop_distance=stop_distance,
        capital_required=capital_req,
        capital_allocation_pct=alloc_pct,
        risk_amount=actual_risk,
        risk_budget_pct=actual_risk_pct
    )


def calculate_full_equity_position_size(
    portfolio_capital: float,
    allocation_pct: float,      # e.g. 20.0%
    entry_price: float
) -> SizingResult:
    """
    Fixed-percent-of-equity sizing for signal-exit-only strategies with no
    real technical stop (e.g. `range_filtered_trend`, `gaussian_channel`):
    mirrors the `full_equity` backtest sizing mode these strategies were
    validated under. Per docs/10-strategy-validation-findings.md §6, sizing
    (not a stop-loss) is what controls drawdown for these strategies.
    """
    if portfolio_capital <= 0 or entry_price <= 0:
        return SizingResult(0, 0, 0, "INVALID_INPUT", entry_price, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    capital_cap = portfolio_capital * (allocation_pct / 100.0)
    final_qty = max(0, int(capital_cap / entry_price))

    capital_req = round(final_qty * entry_price, 2)
    alloc_pct = round((capital_req / portfolio_capital) * 100.0, 2) if portfolio_capital > 0 else 0.0

    return SizingResult(
        quantity=final_qty,
        qty_by_risk=0,
        qty_by_cap=final_qty,
        limiting_factor="FIXED_EQUITY_ALLOCATION",
        entry_price=entry_price,
        stop_loss_price=0.0,
        stop_distance=0.0,
        capital_required=capital_req,
        capital_allocation_pct=alloc_pct,
        risk_amount=0.0,
        risk_budget_pct=0.0
    )

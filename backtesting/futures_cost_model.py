"""Zerodha NSE equity-derivatives (F&O futures) cost calculator and lot-size position sizing."""
import math
from dataclasses import dataclass
from typing import Dict


@dataclass
class FuturesTradeCostBreakdown:
    """Itemized breakdown of NSE F&O futures statutory charges and broker fees."""
    brokerage: float
    stt: float
    exchange_charges: float
    sebi_charges: float
    stamp_duty: float
    gst: float
    total_friction: float

    def to_dict(self) -> Dict[str, float]:
        return {
            "brokerage": round(self.brokerage, 2),
            "stt": round(self.stt, 2),
            "exchange_charges": round(self.exchange_charges, 2),
            "sebi_charges": round(self.sebi_charges, 4),
            "stamp_duty": round(self.stamp_duty, 2),
            "gst": round(self.gst, 2),
            "total_friction": round(self.total_friction, 2)
        }


class IndianFuturesCostModel:
    """
    Computes exact regulatory and broker charges for NSE equity-derivatives (F&O futures).

    Rates differ from cash-equity delivery (`IndianCostModel`): STT is far lower and
    sell-side-only, brokerage is charged per executed leg (not combined turnover), and
    stamp duty is lower and buy-side-only. No DP charges — F&O isn't held in a demat
    holding.
    """

    STT_RATE = 0.0005               # 0.05% on sell side only — Finance Act 2026 raised this
                                     # from 0.02% effective 2026-04-01 (NSE circular FATAX73524)
    BROKERAGE_CAP = 20.0           # ₹20 flat or 0.03% per executed leg, whichever is lower
    BROKERAGE_RATE = 0.0003
    EXCHANGE_RATE = 0.000019       # ~0.0019% NSE F&O futures turnover
    SEBI_RATE = 0.000001           # ₹10 per crore (0.0001%), same as cash equity
    STAMP_DUTY_BUY = 0.00002       # 0.002% on buy turnover only
    GST_RATE = 0.18                # 18% on (brokerage + exchange + sebi)

    @classmethod
    def calculate(cls, buy_value: float, sell_value: float) -> FuturesTradeCostBreakdown:
        """
        Calculates all roundtrip transaction costs for an F&O futures trade.

        Args:
            buy_value: Total rupee buy turnover (qty * buy_price)
            sell_value: Total rupee sell turnover (qty * sell_price)
        """
        if buy_value <= 0 and sell_value <= 0:
            return FuturesTradeCostBreakdown(0, 0, 0, 0, 0, 0, 0)

        total_turnover = buy_value + sell_value

        # Brokerage is capped per leg, not on combined turnover (asymmetric unlike
        # cash-equity MIS, since buy_value and sell_value can differ in backtests
        # with partial fills or pyramiding).
        brokerage = min(cls.BROKERAGE_CAP, cls.BROKERAGE_RATE * buy_value) + \
            min(cls.BROKERAGE_CAP, cls.BROKERAGE_RATE * sell_value)

        stt = sell_value * cls.STT_RATE
        stamp_duty = buy_value * cls.STAMP_DUTY_BUY

        exchange = total_turnover * cls.EXCHANGE_RATE
        sebi = total_turnover * cls.SEBI_RATE

        gst = (brokerage + exchange + sebi) * cls.GST_RATE

        total = brokerage + stt + exchange + sebi + stamp_duty + gst

        return FuturesTradeCostBreakdown(
            brokerage=brokerage,
            stt=stt,
            exchange_charges=exchange,
            sebi_charges=sebi,
            stamp_duty=stamp_duty,
            gst=gst,
            total_friction=total
        )


@dataclass
class FuturesSizingResult:
    """Lot-size-rounded position sizing breakdown for a flat-margin-approximation sizer."""
    quantity: int
    num_lots: int
    lot_size: int
    entry_price: float
    margin_pct: float
    margin_per_lot: float
    capital_required: float
    capital_allocation_pct: float


def calculate_futures_position_size(
    portfolio_capital: float,
    allocation_pct: float,      # e.g. 20.0% of capital allocated to this one position
    entry_price: float,
    lot_size: int,
    margin_pct: float = 20.0,
) -> FuturesSizingResult:
    """
    Fixed-percent-of-equity sizing for futures, rounded down to whole lots, against a
    flat margin approximation (margin per lot = margin_pct% of lot notional). Real
    SPAN + exposure margin isn't available for historical backtesting — see
    docs/12-futures-trading-plan.md §3.2 for why this approximation is used instead of
    sizing against unleveraged full notional.
    """
    if portfolio_capital <= 0 or entry_price <= 0 or lot_size <= 0:
        return FuturesSizingResult(0, 0, lot_size, entry_price, margin_pct, 0.0, 0.0, 0.0)

    capital_cap = portfolio_capital * (allocation_pct / 100.0)
    margin_per_lot = lot_size * entry_price * (margin_pct / 100.0)

    num_lots = max(0, math.floor(capital_cap / margin_per_lot)) if margin_per_lot > 0 else 0
    quantity = num_lots * lot_size

    capital_required = round(num_lots * margin_per_lot, 2)
    alloc_pct = round((capital_required / portfolio_capital) * 100.0, 2) if portfolio_capital > 0 else 0.0

    return FuturesSizingResult(
        quantity=quantity,
        num_lots=num_lots,
        lot_size=lot_size,
        entry_price=entry_price,
        margin_pct=margin_pct,
        margin_per_lot=round(margin_per_lot, 2),
        capital_required=capital_required,
        capital_allocation_pct=alloc_pct
    )

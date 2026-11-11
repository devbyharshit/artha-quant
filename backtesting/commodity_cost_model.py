"""MCX commodity futures cost calculator (Indian commodity derivatives, non-agri)."""
from dataclasses import dataclass
from typing import Dict


@dataclass
class CommodityTradeCostBreakdown:
    """Itemized breakdown of MCX commodity futures statutory charges and broker fees."""
    brokerage: float
    ctt: float
    exchange_charges: float
    sebi_charges: float
    stamp_duty: float
    gst: float
    total_friction: float

    def to_dict(self) -> Dict[str, float]:
        return {
            "brokerage": round(self.brokerage, 2),
            "ctt": round(self.ctt, 2),
            "exchange_charges": round(self.exchange_charges, 2),
            "sebi_charges": round(self.sebi_charges, 4),
            "stamp_duty": round(self.stamp_duty, 2),
            "gst": round(self.gst, 2),
            "total_friction": round(self.total_friction, 2)
        }


class IndianCommodityCostModel:
    """
    Computes charges for MCX commodity futures trades (non-agri commodities).

    Direction-agnostic by design: `sell_value` is whichever leg was the sell, regardless
    of whether it opened the trade (short) or closed it (long). CTT is charged on that
    sell leg, so short trades need no separate code path.
    """

    # 0.01% sell-side, non-agri — FLAGGED UNVERIFIED against a primary source (spec §5);
    # taken from web research during the design phase, not a confirmed regulatory document.
    CTT_RATE = 0.0001
    BROKERAGE_CAP = 20.0           # ₹20 flat or 0.03% per executed leg, whichever is lower
    BROKERAGE_RATE = 0.0003
    EXCHANGE_RATE = 0.000023       # midpoint of 0.0021-0.0026% MCX turnover (spec §5)
    SEBI_RATE = 0.000001           # ₹10 per crore (0.0001%), same as cash equity
    # 0.002% on buy turnover only — FLAGGED UNVERIFIED against a primary source (spec §5);
    # taken from web research during the design phase, not a confirmed regulatory document.
    STAMP_DUTY_BUY = 0.00002
    GST_RATE = 0.18                # 18% on (brokerage + exchange + sebi)

    @classmethod
    def calculate(cls, buy_value: float, sell_value: float) -> CommodityTradeCostBreakdown:
        """
        Calculates all roundtrip transaction costs for an MCX commodity futures trade.

        Args:
            buy_value: Total rupee buy turnover (qty * buy_price)
            sell_value: Total rupee sell turnover (qty * sell_price). For a short trade
                this is the opening leg; for a long trade it is the closing leg.
        """
        if buy_value <= 0 and sell_value <= 0:
            return CommodityTradeCostBreakdown(0, 0, 0, 0, 0, 0, 0)

        total_turnover = buy_value + sell_value

        # Brokerage is capped per leg, not on combined turnover, since buy_value and
        # sell_value can differ in backtests with partial fills or pyramiding.
        brokerage = min(cls.BROKERAGE_CAP, cls.BROKERAGE_RATE * buy_value) + \
            min(cls.BROKERAGE_CAP, cls.BROKERAGE_RATE * sell_value)

        ctt = sell_value * cls.CTT_RATE
        stamp_duty = buy_value * cls.STAMP_DUTY_BUY

        exchange = total_turnover * cls.EXCHANGE_RATE
        sebi = total_turnover * cls.SEBI_RATE

        gst = (brokerage + exchange + sebi) * cls.GST_RATE

        total = brokerage + ctt + exchange + sebi + stamp_duty + gst

        return CommodityTradeCostBreakdown(
            brokerage=brokerage,
            ctt=ctt,
            exchange_charges=exchange,
            sebi_charges=sebi,
            stamp_duty=stamp_duty,
            gst=gst,
            total_friction=total
        )

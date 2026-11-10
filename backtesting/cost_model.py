"""Institutional-grade Zerodha equity delivery cost calculator."""
from dataclasses import dataclass
from typing import Dict, Any


@dataclass
class TradeCostBreakdown:
    """Itemized breakdown of Indian statutory charges and broker fees."""
    brokerage: float
    stt: float
    exchange_charges: float
    sebi_charges: float
    stamp_duty: float
    dp_charges: float
    gst: float
    total_friction: float

    def to_dict(self) -> Dict[str, float]:
        return {
            "brokerage": round(self.brokerage, 2),
            "stt": round(self.stt, 2),
            "exchange_charges": round(self.exchange_charges, 2),
            "sebi_charges": round(self.sebi_charges, 4),
            "stamp_duty": round(self.stamp_duty, 2),
            "dp_charges": round(self.dp_charges, 2),
            "gst": round(self.gst, 2),
            "total_friction": round(self.total_friction, 2)
        }


class IndianCostModel:
    """
    Computes exact regulatory and depository charges for NSE Equity Delivery (CNC).
    """

    # Rates
    STT_RATE = 0.0010              # 0.1% on buy and sell
    EXCHANGE_RATE = 0.0000297      # 0.00297% NSE cash turnover
    SEBI_RATE = 0.000001           # ₹10 per crore (0.0001%)
    STAMP_DUTY_BUY = 0.00015       # 0.015% on buy turnover
    DP_CHARGE_PER_SELL = 15.93     # ₹13.50 + 18% GST per scrip sell
    GST_RATE = 0.18                # 18% on (brokerage + exchange + sebi)

    @classmethod
    def calculate(cls, buy_value: float, sell_value: float, is_delivery: bool = True) -> TradeCostBreakdown:
        """
        Calculates all roundtrip transaction costs.
        
        Args:
            buy_value: Total rupee purchase value (qty * buy_price)
            sell_value: Total rupee sale value (qty * sell_price)
            is_delivery: True for CNC (delivery), False for MIS (intraday)
        """
        if buy_value <= 0 and sell_value <= 0:
            return TradeCostBreakdown(0, 0, 0, 0, 0, 0, 0, 0)

        total_turnover = buy_value + sell_value

        if is_delivery:
            # 1. Brokerage: Free on Zerodha delivery
            brokerage = 0.0
            # 2. STT: 0.1% on buy and sell
            stt = total_turnover * cls.STT_RATE
            # 3. Stamp duty: 0.015% on buy only
            stamp_duty = buy_value * cls.STAMP_DUTY_BUY
            # 4. DP Charges: ₹15.93 flat per scrip sold
            dp_charges = cls.DP_CHARGE_PER_SELL if sell_value > 0 else 0.0
        else:
            # Intraday (MIS)
            brokerage = min(40.0, 0.0003 * total_turnover) # ₹20 per leg or 0.03%
            stt = sell_value * 0.00025                      # 0.025% on sell only
            stamp_duty = buy_value * 0.00003                # 0.003% on buy
            dp_charges = 0.0                                # No DP charge for intraday

        # Common charges
        exchange = total_turnover * cls.EXCHANGE_RATE
        sebi = total_turnover * cls.SEBI_RATE

        # GST applies to: Brokerage + Exchange charges + SEBI charges
        gst = (brokerage + exchange + sebi) * cls.GST_RATE

        total = brokerage + stt + exchange + sebi + stamp_duty + dp_charges + gst

        return TradeCostBreakdown(
            brokerage=brokerage,
            stt=stt,
            exchange_charges=exchange,
            sebi_charges=sebi,
            stamp_duty=stamp_duty,
            dp_charges=dp_charges,
            gst=gst,
            total_friction=total
        )

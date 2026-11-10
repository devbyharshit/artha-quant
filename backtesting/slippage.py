"""Slippage and market impact models by liquidity tier."""
from typing import Optional


class SlippageModel:
    """Computes realistic trade slippage and market impact."""

    DEFAULT_BASE_SLIPPAGE = {
        "LARGE_CAP": 0.0005,    # 0.05% (5 bps) for Nifty 50
        "MID_CAP": 0.0025,      # 0.25% (25 bps)
        "SMALL_CAP": 0.0100     # 1.00% (100 bps)
    }

    @classmethod
    def calculate_slippage_pct(
        cls,
        tier: str = "LARGE_CAP",
        order_value: float = 0.0,
        adv_20d: Optional[float] = None,
        impact_factor: float = 0.05
    ) -> float:
        """
        Calculates total slippage percentage including market impact.
        
        Args:
            tier: 'LARGE_CAP', 'MID_CAP', or 'SMALL_CAP'
            order_value: Rupee size of trade (price * qty)
            adv_20d: 20-day Average Daily Rupee Volume
            impact_factor: Elasticity coefficient for market impact
        """
        base_slippage = cls.DEFAULT_BASE_SLIPPAGE.get(tier.upper(), 0.0005)

        # Market impact component: order size relative to daily liquidity
        market_impact = 0.0
        if adv_20d and adv_20d > 0 and order_value > 0:
            participation_rate = order_value / adv_20d
            market_impact = participation_rate * impact_factor

        return base_slippage + market_impact

    @classmethod
    def apply_slippage(cls, price: float, is_buy: bool, slippage_pct: float = 0.0005) -> float:
        """Applies slippage to produce realistic executed fill price."""
        if is_buy:
            return round(price * (1.0 + slippage_pct), 2)
        else:
            return round(price * (1.0 - slippage_pct), 2)

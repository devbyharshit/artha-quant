"""Indian Capital Gains Tax engine for backtesting."""
from dataclasses import dataclass
from typing import List, Dict, Any


@dataclass
class TradeTaxResult:
    """Tax details for a closed trade."""
    holding_days: int
    gross_pnl: float
    is_stcg: bool
    tax_rate: float
    tax_payable: float
    net_pnl_after_tax: float


class IndianTaxEngine:
    """
    Simulates Indian Income Tax Act capital gains taxation:
    - STCG (< 365 days): 20% flat on net profit
    - LTCG (>= 365 days): 12.5% on net profit
    """

    STCG_RATE = 0.20  # 20% Short Term Capital Gains
    LTCG_RATE = 0.125 # 12.5% Long Term Capital Gains

    @classmethod
    def evaluate_trade(cls, gross_pnl: float, holding_days: int) -> TradeTaxResult:
        """
        Calculates tax liability on a single realized trade.
        Tax applies strictly to net positive realized gains.
        """
        if gross_pnl <= 0:
            return TradeTaxResult(
                holding_days=holding_days,
                gross_pnl=gross_pnl,
                is_stcg=(holding_days < 365),
                tax_rate=0.0,
                tax_payable=0.0,
                net_pnl_after_tax=gross_pnl
            )

        is_stcg = (holding_days < 365)
        rate = cls.STCG_RATE if is_stcg else cls.LTCG_RATE
        tax_due = round(gross_pnl * rate, 2)
        net_pnl = round(gross_pnl - tax_due, 2)

        return TradeTaxResult(
            holding_days=holding_days,
            gross_pnl=gross_pnl,
            is_stcg=is_stcg,
            tax_rate=rate,
            tax_payable=tax_due,
            net_pnl_after_tax=net_pnl
        )

    LTCG_HOLDING_DAYS = 365        # listed equity is long-term if held > 12 months
    LTCG_ANNUAL_EXEMPTION = 125000.0

    @classmethod
    def calculate_portfolio_tax_drag(
        cls,
        gross_cagr: float,
        net_after_tax_cagr: float
    ) -> float:
        """Returns the annual percentage points of return consumed by tax drag."""
        return max(0.0, gross_cagr - net_after_tax_cagr)


def financial_year(ts) -> int:
    """Indian FY label by its ending year: Apr 2025 - Mar 2026 -> 2026."""
    return ts.year + 1 if ts.month >= 4 else ts.year


class AnnualTaxLedger:
    """Assesses capital gains per financial year instead of per trade.

    Within a year all short-term results net against each other, as do all
    long-term ones. Then: short-term losses offset short-term gains, then
    long-term gains; long-term losses offset only long-term gains. Unused
    losses carry forward (the 8-year limit is ignored; backtests here are
    shorter). The LTCG exemption applies after set-off.
    """

    def __init__(self):
        self.open_fy = None
        self._st = 0.0
        self._lt = 0.0
        self.st_loss_cf = 0.0
        self.lt_loss_cf = 0.0

    def record(self, pnl: float, holding_days: int, exit_time) -> None:
        fy = financial_year(exit_time)
        if self.open_fy is None:
            self.open_fy = fy
        if holding_days > IndianTaxEngine.LTCG_HOLDING_DAYS:
            self._lt += pnl
        else:
            self._st += pnl

    def due_on_rollover(self, ts) -> float:
        """Settles the open FY if `ts` falls in a later one; returns tax due."""
        if self.open_fy is not None and financial_year(ts) != self.open_fy:
            return self.settle()
        return 0.0

    def settle(self) -> float:
        stg, stl = max(self._st, 0.0), max(-self._st, 0.0)
        ltg, ltl = max(self._lt, 0.0), max(-self._lt, 0.0)

        def offset(loss, gain):
            used = min(loss, gain)
            return loss - used, gain - used

        stl, stg = offset(stl, stg)
        stl, ltg = offset(stl, ltg)
        ltl, ltg = offset(ltl, ltg)
        self.st_loss_cf, stg = offset(self.st_loss_cf, stg)
        self.st_loss_cf, ltg = offset(self.st_loss_cf, ltg)
        self.lt_loss_cf, ltg = offset(self.lt_loss_cf, ltg)
        self.st_loss_cf += stl
        self.lt_loss_cf += ltl

        taxable_lt = max(0.0, ltg - IndianTaxEngine.LTCG_ANNUAL_EXEMPTION)
        tax = round(stg * IndianTaxEngine.STCG_RATE + taxable_lt * IndianTaxEngine.LTCG_RATE, 2)

        self._st = self._lt = 0.0
        self.open_fy = None
        return tax

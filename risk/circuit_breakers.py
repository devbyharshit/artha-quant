"""Portfolio-level circuit breakers, correlation filters, and exposure limits."""
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Any, Tuple, Union
import pandas as pd
import numpy as np

from configs.logger import logger


@dataclass
class RiskEvaluation:
    """Outcome of risk governor audit on a candidate signal."""
    approved: bool
    veto_reasons: List[str] = field(default_factory=list)


class PortfolioRiskGovernor:
    """
    Adversarial risk governor with authority to veto candidate trades.
    """

    def __init__(
        self,
        max_drawdown_limit_pct: float = 15.0,
        max_sector_positions: int = 3,
        max_pairwise_correlation: float = 0.75,
        min_cash_buffer_pct: float = 10.0,
        consecutive_losses_limit: int = 3,
        loss_cooldown_days: int = 3
    ):
        self.max_drawdown_limit_pct = max_drawdown_limit_pct
        self.max_sector_positions = max_sector_positions
        self.max_pairwise_correlation = max_pairwise_correlation
        self.min_cash_buffer_pct = min_cash_buffer_pct
        self.consecutive_losses_limit = consecutive_losses_limit
        self.loss_cooldown_days = loss_cooldown_days

    @staticmethod
    def calculate_consecutive_losses(
        closed_trades: List[Dict[str, Any]]
    ) -> Tuple[int, Optional[str]]:
        """
        Calculates consecutive loss streak from closed trades ordered by recency.
        Returns: (streak_count, last_loss_exit_date_iso_string)
        """
        streak = 0
        last_exit_date = None
        for trade in closed_trades:
            net_pnl = trade.get("net_pnl")
            gross_pnl = trade.get("gross_pnl")
            exit_reason = trade.get("exit_reason")

            is_loss = False
            if net_pnl is not None:
                is_loss = (net_pnl < 0)
            elif gross_pnl is not None:
                is_loss = (gross_pnl < 0)
            elif exit_reason == "STOP_LOSS":
                is_loss = True

            if is_loss:
                streak += 1
                if last_exit_date is None:
                    last_exit_date = str(trade.get("exit_date", ""))
            else:
                # Streak broken by win or breakeven
                break

        return streak, last_exit_date

    def evaluate_candidate(
        self,
        symbol: str,
        sector: str,
        capital_required: float,
        portfolio_peak_capital: float,
        portfolio_current_capital: float,
        available_cash: float,
        active_positions: List[Dict[str, Any]],  # list of dicts with 'symbol', 'sector', etc.
        symbol_returns_60d: Optional[pd.Series] = None,
        active_returns_60d: Optional[Dict[str, pd.Series]] = None,
        candidate_entry_price: Optional[float] = None,
        consecutive_losses: Optional[int] = None,
        days_since_last_loss: Optional[int] = None,
        recent_closed_trades: Optional[List[Dict[str, Any]]] = None,
        eval_date: Optional[Union[date, str]] = None
    ) -> RiskEvaluation:
        """
        Executes all defensive gates against candidate trade.
        """
        reasons = []

        # Gate 1: Portfolio Drawdown Breaker
        if portfolio_peak_capital > 0:
            current_dd = (portfolio_peak_capital - portfolio_current_capital) / portfolio_peak_capital * 100.0
            if current_dd >= self.max_drawdown_limit_pct:
                reasons.append(
                    f"Portfolio drawdown ({current_dd:.1f}%) exceeds circuit breaker limit "
                    f"({self.max_drawdown_limit_pct:.1f}%). All new entries halted."
                )

        # Gate 2: Sector Concentration Cap
        sector_active_count = sum(1 for p in active_positions if p.get("sector", "").lower() == sector.lower())
        if sector_active_count >= self.max_sector_positions:
            reasons.append(
                f"Sector '{sector}' already has {sector_active_count} active positions "
                f"(max permitted is {self.max_sector_positions})."
            )

        # Gate 3: Correlation Filter (> 0.75 with any open holding)
        if symbol_returns_60d is not None and active_returns_60d:
            for active_sym, ret_series in active_returns_60d.items():
                if len(symbol_returns_60d) >= 30 and len(ret_series) >= 30:
                    combined = pd.concat([symbol_returns_60d, ret_series], axis=1).dropna()
                    if len(combined) >= 30:
                        corr = combined.iloc[:, 0].corr(combined.iloc[:, 1])
                        if corr > self.max_pairwise_correlation:
                            reasons.append(
                                f"High price correlation ({corr:.2f}) with active holding '{active_sym}' "
                                f"exceeds limit ({self.max_pairwise_correlation:.2f})."
                            )
                            break

        # Gate 4: Cash Buffer & Margin Verification
        cash_buffer_needed = portfolio_current_capital * (self.min_cash_buffer_pct / 100.0)
        usable_cash = available_cash - cash_buffer_needed
        if capital_required > usable_cash:
            reasons.append(
                f"Insufficient usable cash (Required: ₹{capital_required:,.2f}, "
                f"Usable after {self.min_cash_buffer_pct}% buffer: ₹{max(0.0, usable_cash):,.2f})."
            )

        # Gate 5: Never add to a losing trade
        matching_positions = [
            p for p in active_positions
            if p.get("symbol", "").upper() == symbol.upper()
        ]
        for pos in matching_positions:
            is_losing = False
            if "unrealized_pnl" in pos and pos["unrealized_pnl"] < 0:
                is_losing = True
            elif pos.get("is_losing") is True:
                is_losing = True
            elif (
                candidate_entry_price is not None
                and "entry_price" in pos
                and candidate_entry_price < pos["entry_price"]
            ):
                is_losing = True

            if is_losing:
                reasons.append(
                    f"Active position in '{symbol}' is currently at a loss. "
                    f"Vetoed by risk rule: Never add to a losing trade."
                )
                break

        # Gate 6: Consecutive Loss Cooldown Breaker (e.g. 3 losses in a row = 3 days off)
        streak = consecutive_losses
        days_passed = days_since_last_loss

        if recent_closed_trades is not None and streak is None:
            streak, last_exit_date = self.calculate_consecutive_losses(recent_closed_trades)
            if last_exit_date and eval_date is not None:
                try:
                    eval_d = eval_date if isinstance(eval_date, date) else pd.to_datetime(eval_date).date()
                    exit_d = pd.to_datetime(last_exit_date).date()
                    days_passed = (eval_d - exit_d).days
                except Exception as exc:
                    logger.debug(f"Could not parse dates for loss cooldown: {exc}")

        if streak is not None and streak >= self.consecutive_losses_limit:
            if days_passed is None or days_passed < self.loss_cooldown_days:
                remaining = (
                    self.loss_cooldown_days - days_passed
                    if days_passed is not None and days_passed >= 0
                    else self.loss_cooldown_days
                )
                reasons.append(
                    f"Consecutive loss circuit breaker active ({streak} losses in a row >= limit of "
                    f"{self.consecutive_losses_limit}). Trading halted for {remaining} more day(s) "
                    f"({self.loss_cooldown_days} days off cooldown rule)."
                )

        approved = (len(reasons) == 0)
        return RiskEvaluation(approved=approved, veto_reasons=reasons)

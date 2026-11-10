"""Forward paper trading tracker and live signal reconciler."""
import sqlite3
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

from configs.logger import logger
from data.store.parquet_store import ParquetStore, get_parquet_store
from signals.ledger import get_default_db_path
from backtesting.cost_model import IndianCostModel
from backtesting.tax_model import IndianTaxEngine
from backtesting.slippage import SlippageModel


class PaperTradeReconciler:
    """
    Simulates forward execution and reconciles live paper trades
    against daily settled market bars.
    """

    def __init__(self, db_path: Optional[Path] = None, store: Optional[ParquetStore] = None):
        self.db_path = db_path or get_default_db_path()
        self.store = store or get_parquet_store()
        self.init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        """Initializes paper trades tracking table."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_hash TEXT UNIQUE NOT NULL,
                    symbol TEXT NOT NULL,
                    entry_date TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    stop_loss REAL NOT NULL,
                    target REAL NOT NULL,
                    exit_date TEXT,
                    exit_price REAL,
                    gross_pnl REAL,
                    friction_costs REAL,
                    tax_paid REAL,
                    net_pnl REAL,
                    exit_reason TEXT,
                    status TEXT DEFAULT 'OPEN',
                    created_at TEXT NOT NULL
                );
            """)
            conn.commit()

    def create_paper_trade_from_signal(self, signal: Dict[str, Any], slippage_pct: float = 0.0005) -> bool:
        """Opens a simulated paper trade from an emitted signal."""
        signal_hash = signal["signal_hash"]
        symbol = signal["symbol"].upper()
        entry_ref = float(signal["entry_ref"])
        qty = int(signal["calculated_qty"])
        stop_loss = float(signal["stop_loss"])
        target = float(signal["target"])

        # Model fill at next open / close + slippage
        fill_price = SlippageModel.apply_slippage(entry_ref, is_buy=True, slippage_pct=slippage_pct)
        created_at = datetime.now(timezone.utc).isoformat()
        today_str = date.today().isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    INSERT INTO paper_trades (
                        signal_hash, symbol, entry_date, entry_price, quantity,
                        stop_loss, target, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
                """, (
                    signal_hash, symbol, today_str, fill_price, qty,
                    stop_loss, target, created_at
                ))
                conn.commit()
                logger.info(f"Opened forward paper trade for {symbol} ({qty} shares @ ₹{fill_price:,.2f})")
                return True
            except sqlite3.IntegrityError:
                return False

    def reconcile_open_positions(self) -> List[Dict[str, Any]]:
        """
        Evaluates open paper trades against settled market bars
        to detect stop-loss or take-profit hits.
        """
        closed_trades = []

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM paper_trades WHERE status = 'OPEN'")
            open_trades = [dict(r) for r in cursor.fetchall()]

            for trade in open_trades:
                trade_id = trade["id"]
                symbol = trade["symbol"]
                stop_loss = trade["stop_loss"]
                target = trade["target"]
                entry_price = trade["entry_price"]
                qty = trade["quantity"]
                entry_date = trade["entry_date"]

                # Load bars after entry date
                bars_df = self.store.load_bars(symbol, timeframe="1D", start_date=entry_date)
                if bars_df.empty or len(bars_df) <= 1:
                    continue

                # Check bars after the entry bar
                eval_bars = bars_df.iloc[1:]

                for bar_time, bar in eval_bars.iterrows():
                    bar_low = bar["low"]
                    bar_high = bar["high"]
                    bar_close = bar["close"]

                    stop_hit = (bar_low <= stop_loss)
                    target_hit = (bar_high >= target)

                    if stop_hit or target_hit:
                        if stop_hit:
                            raw_exit = stop_loss
                            reason = "STOP_LOSS"
                        else:
                            raw_exit = target
                            reason = "TARGET"

                        exit_price = SlippageModel.apply_slippage(raw_exit, is_buy=False)
                        exit_date = str(bar_time.date())

                        entry_val = qty * entry_price
                        exit_val = qty * exit_price
                        gross_pnl = exit_val - entry_val

                        # Cost and tax calculation
                        costs = IndianCostModel.calculate(entry_val, exit_val, is_delivery=True)
                        friction = costs.total_friction
                        pnl_after_friction = gross_pnl - friction

                        tax_res = IndianTaxEngine.evaluate_trade(pnl_after_friction, holding_days=10)
                        tax_paid = tax_res.tax_payable
                        net_pnl = tax_res.net_pnl_after_tax

                        cursor.execute("""
                            UPDATE paper_trades
                            SET status = 'CLOSED', exit_date = ?, exit_price = ?,
                                gross_pnl = ?, friction_costs = ?, tax_paid = ?,
                                net_pnl = ?, exit_reason = ?
                            WHERE id = ?
                        """, (exit_date, exit_price, gross_pnl, friction, tax_paid, net_pnl, reason, trade_id))
                        conn.commit()

                        closed_summary = {
                            "symbol": symbol,
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "quantity": qty,
                            "net_pnl": net_pnl,
                            "exit_reason": reason
                        }
                        closed_trades.append(closed_summary)
                        logger.info(
                            f"Paper trade CLOSED: {symbol} hit {reason} "
                            f"@ ₹{exit_price:,.2f} (Net P&L: ₹{net_pnl:+,.2f})"
                        )
                        break

        return closed_trades

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """Returns all currently active paper trades."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM paper_trades WHERE status = 'OPEN'")
            return [dict(r) for r in cursor.fetchall()]

    def get_closed_trades(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Returns settled paper trades ordered by most recent exit date."""
        query = "SELECT * FROM paper_trades WHERE status = 'CLOSED' ORDER BY exit_date DESC, id DESC"
        if limit is not None and limit > 0:
            query += f" LIMIT {int(limit)}"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query)
            return [dict(r) for r in cursor.fetchall()]

    def get_consecutive_losses(self) -> int:
        """Returns current streak of consecutive loss trades from closed trades."""
        from risk.circuit_breakers import PortfolioRiskGovernor
        trades = self.get_closed_trades()
        streak, _ = PortfolioRiskGovernor.calculate_consecutive_losses(trades)
        return streak

    def get_days_since_last_loss(self, eval_date: Optional[Any] = None) -> Optional[int]:
        """Returns calendar days since the most recent loss trade in the current loss streak."""
        from risk.circuit_breakers import PortfolioRiskGovernor
        import pandas as pd
        trades = self.get_closed_trades()
        streak, last_exit_date = PortfolioRiskGovernor.calculate_consecutive_losses(trades)
        if streak == 0 or not last_exit_date:
            return None

        today = eval_date or date.today()
        eval_d = today if isinstance(today, date) else pd.to_datetime(today).date()
        exit_d = pd.to_datetime(last_exit_date).date()
        return (eval_d - exit_d).days

"""SQLite signal ledger with deterministic SHA-256 deduplication."""
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

from configs.config import get_settings
from configs.logger import logger


def get_default_db_path() -> Path:
    settings = get_settings()
    settings.SIGNALS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return settings.SIGNALS_DB_PATH


def generate_signal_hash(symbol: str, strategy: str, bar_timestamp: str) -> str:
    """Generates deterministic unique SHA-256 hash for deduplication."""
    payload = f"{symbol.upper()}_{strategy}_{bar_timestamp}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SignalLedger:
    """Manages transactional recording and deduplication of signals in SQLite."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or get_default_db_path()
        self.init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        """Creates tables if they do not exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_hash TEXT UNIQUE NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    bar_timestamp TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entry_ref REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    target REAL NOT NULL,
                    risk_reward_ratio REAL NOT NULL,
                    calculated_qty INTEGER NOT NULL,
                    capital_required REAL NOT NULL,
                    risk_budget REAL NOT NULL,
                    status TEXT DEFAULT 'PENDING',
                    veto_reason TEXT,
                    telegram_sent_at TEXT,
                    created_at TEXT NOT NULL
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_hash ON signals (signal_hash);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_date ON signals (bar_timestamp);")
            conn.commit()

    def record_if_unique(self, signal: Dict[str, Any]) -> bool:
        """
        Inserts candidate signal.
        Returns:
            True if newly inserted.
            False if duplicate signal already exists.
        """
        signal_hash = signal.get("signal_hash")
        if not signal_hash:
            signal_hash = generate_signal_hash(
                signal["symbol"], signal["strategy"], str(signal["bar_timestamp"])
            )
            signal["signal_hash"] = signal_hash

        created_at = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    INSERT INTO signals (
                        signal_hash, symbol, strategy, timeframe, bar_timestamp,
                        action, entry_ref, stop_loss, target, risk_reward_ratio,
                        calculated_qty, capital_required, risk_budget, status,
                        veto_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    signal_hash,
                    signal["symbol"],
                    signal["strategy"],
                    signal.get("timeframe", "1D"),
                    str(signal["bar_timestamp"]),
                    signal["action"],
                    float(signal["entry_ref"]),
                    float(signal["stop_loss"]),
                    float(signal["target"]),
                    float(signal.get("risk_reward_ratio", 0.0)),
                    int(signal["calculated_qty"]),
                    float(signal["capital_required"]),
                    float(signal["risk_budget"]),
                    signal.get("status", "PENDING"),
                    signal.get("veto_reason"),
                    created_at
                ))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                # Unique constraint violated -> duplicate signal
                return False

    def mark_notified(self, signal_hash: str) -> None:
        """Marks signal as successfully dispatched to Telegram."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE signals
                SET status = 'NOTIFIED', telegram_sent_at = ?
                WHERE signal_hash = ?
            """, (now_str, signal_hash))
            conn.commit()

    def mark_vetoed(self, signal_hash: str, reason: str) -> None:
        """Marks candidate signal as vetoed by risk governor."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE signals
                SET status = 'VETOED', veto_reason = ?
                WHERE signal_hash = ?
            """, (reason, signal_hash))
            conn.commit()

    def get_signal(self, signal_hash: str) -> Optional[dict]:
        """Fetch signal by hash."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM signals WHERE signal_hash = ?", (signal_hash,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_signals_by_status(self, status: str = "PENDING") -> List[dict]:
        """Retrieve signals matching status."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM signals WHERE status = ? ORDER BY id DESC", (status,))
            return [dict(r) for r in cursor.fetchall()]

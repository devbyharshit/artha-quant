"""Abstract Base Class for all quantitative strategies in Artha Quant."""
from abc import ABC, abstractmethod
from typing import Dict, Any, List
import pandas as pd
import numpy as np

from configs.logger import logger


class BaseStrategy(ABC):
    """
    Abstract strategy contract.
    Strategies are pure, stateless functions operating on historical OHLCV data.
    """

    def __init__(self, name: str, params: Dict[str, Any]):
        self.name = name
        self.params = params

    @abstractmethod
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Computes technical indicators and appends them to the DataFrame.
        Must NOT drop existing OHLCV columns.
        """
        pass

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Evaluates confirmed closed bars and produces trade signals.
        
        Returns DataFrame containing at minimum:
        - 'signal': 1 (BUY), -1 (SELL/EXIT), 0 (HOLD)
        - 'stop_loss': float price level
        - 'target': float price level
        """
        pass

    def validate_signals_df(self, df: pd.DataFrame) -> None:
        """Validates that generate_signals returns properly formatted signal columns."""
        required_cols = {"signal", "stop_loss", "target"}
        missing = required_cols - set(df.columns)
        if missing:
            raise ValueError(f"Strategy {self.name} failed to return required columns: {missing}")

        valid_signals = {-1, 0, 1}
        unique_signals = set(df["signal"].dropna().unique())
        invalid_signals = unique_signals - valid_signals
        if invalid_signals:
            raise ValueError(
                f"Strategy {self.name} generated invalid signal values: {invalid_signals}. "
                f"Must be strictly in {valid_signals}."
            )

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Executes full pipeline:
        1. Calculate indicators
        2. Generate signals
        3. Validate output integrity
        """
        if df.empty:
            return df

        df_with_indicators = self.calculate_indicators(df.copy())
        signals_df = self.generate_signals(df_with_indicators)
        self.validate_signals_df(signals_df)

        # Standardize helper columns
        signals_df["entry_ref"] = signals_df["close"]
        signals_df["risk_distance"] = (signals_df["entry_ref"] - signals_df["stop_loss"]).abs()

        # Compute risk reward ratio safely (avoid division by zero)
        reward_distance = (signals_df["target"] - signals_df["entry_ref"]).abs()
        signals_df["risk_reward_ratio"] = np.where(
            signals_df["risk_distance"] > 0,
            reward_distance / signals_df["risk_distance"],
            0.0
        )

        return signals_df

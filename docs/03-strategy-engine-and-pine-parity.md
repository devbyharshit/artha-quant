# 03 — Strategy Engine & Pine Parity

## 1. Strategy Interface Design

Every strategy implemented in Artha Quant inherits from `BaseStrategy`. The contract enforces pure, stateless execution over historical market data:

```python
from abc import ABC, abstractmethod
from typing import Dict, Any
import pandas as pd

class BaseStrategy(ABC):
    def __init__(self, name: str, params: Dict[str, Any]):
        self.name = name
        self.params = params

    @abstractmethod
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Takes raw OHLCV DataFrame and appends technical indicators.
        Must NOT drop existing columns.
        """
        pass

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Takes DataFrame with indicators and appends:
        - 'signal': 1 (BUY), -1 (SELL), or 0 (HOLD)
        - 'stop_loss': float price
        - 'target': float price
        Must calculate on confirmed closed bars only.
        """
        pass
```

---

## 2. Pine Script to Python Pitfalls & Mitigations

Translating TradingView Pine Script into Python is the most common point of quantitative failure. Subtle mathematical and architectural mismatches will produce completely different trade results.

### A. Lookahead Bias & Historical Referencing (`[1]`)
* **In Pine Script:** `close[1]` refers to the close of 1 bar ago.
* **In Python (`pandas`):** Must explicitly call `.shift(1)`.
* **Danger:** If your Python strategy tests `df['close'] > df['upper_band']` at bar $t$ and assumes entry at bar $t$ without shifting, you are buying at the close after the event happened, or peeking into the future.
* **Rule:** If the signal is calculated on bar $t$'s close, the entry reference price is that bar's close, and the actual trade execution is modeled at **Bar $t+1$ Open** (or end-of-day close at 15:29 with slippage).

### B. Wilder's Smoothing (RMA) vs Standard EMA/SMA
* In TradingView, `ta.rsi()` and `ta.atr()` use **RMA (Running Moving Average / Wilder's Smoothing)**, which is an exponential moving average with $\alpha = 1/\text{length}$.
* Standard Python libraries sometimes default to EMA ($\alpha = 2/(\text{length} + 1)$).
* **Fix:** In `pandas-ta`, explicitly configure:
  ```python
  import pandas_ta as ta
  df.ta.rsi(length=14, append=True) # Uses Wilder's RMA by default
  df.ta.atr(length=14, append=True) # Uses RMA by default
  ```

### C. Bar State & Unconfirmed Intraday Signals
* Pine Script strategies frequently re-paint or fluctuate if `calc_on_every_tick=true` or if referencing `barstate.isconfirmed` incorrectly.
* **Rule in Artha Quant:** The EOD scanner **only runs after 15:45 IST** when the daily bar is 100% frozen and settled by the exchange. Intraday bar repainting is impossible on fully finalized daily bars.

### D. Indicator Warm-Up Periods & `NaN` Handling
* Indicators like a 200-period EMA or 14-period RSI require sufficient history to stabilize mathematically. An exponential moving average started on bar 0 differs slightly depending on how many bars precede it.
* **Rule:** Always feed at least **250 to 300 bars of historical data** into `calculate_indicators()`, even if you only need the signal for today's bar.

---

## 3. Step-by-Step Pine Parity Verification Suite

Before accepting any Python strategy implementation, it must pass an automated parity audit against TradingView:

```
┌────────────────────────────────────────────────────────────┐
│ Step 1: Export TradingView Benchmark CSV                   │
│ - Add strategy/indicator to TradingView (e.g. RELIANCE 1D) │
│ - Export OHLCV + Indicator Values + Signals to CSV         │
└─────────────────────────────┬──────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────┐
│ Step 2: Run Python Strategy over Same OHLCV                │
│ - Feed identical raw OHLCV into Python strategy            │
│ - Compute indicators and signals                           │
└─────────────────────────────┬──────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────┐
│ Step 3: Run Automated Pytest Comparison                    │
│ - Assert indicator difference: abs(py - tv) / tv < 0.05%   │
│ - Assert signal match rate: 100% match on BUY/SELL bars    │
└────────────────────────────────────────────────────────────┘
```

### Pytest Verification Spec (`tests/test_pine_parity.py`)
```python
import numpy as np
import pandas as pd
import pytest

def test_strategy_matches_tradingview_export():
    tv_df = pd.read_csv("tests/fixtures/tradingview_export_reliance_1d.csv", parse_dates=["time"])
    
    # Run python strategy
    py_strategy = MyStrategy(name="breakout", params={"length": 20})
    computed_df = py_strategy.calculate_indicators(tv_df[["open", "high", "low", "close", "volume"]].copy())
    signals_df = py_strategy.generate_signals(computed_df)
    
    # 1. Indicator Parity (< 0.05% delta)
    indicator_delta = np.abs(computed_df["indicator_val"] - tv_df["tv_indicator_val"]) / tv_df["tv_indicator_val"]
    assert (indicator_delta.dropna() < 0.0005).all(), "Indicator delta exceeds 0.05% tolerance"
    
    # 2. Signal Parity (100% match on closed bars)
    mismatched_signals = (signals_df["signal"] != tv_df["tv_signal"]).sum()
    assert mismatched_signals == 0, f"Found {mismatched_signals} mismatched signals between Python and TradingView!"
```

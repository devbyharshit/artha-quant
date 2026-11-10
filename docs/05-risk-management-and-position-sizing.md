# 05 — Risk Management & Position Sizing

## 1. Core Risk Philosophy

In quantitative trading, **edge determines the rate of return, but position sizing determines survival.** 

The Artha Quant risk engine operates as an independent, adversarial governor:
* It executes **after** the strategy engine has generated a candidate signal.
* It has full authority to veto, resize, or drop any signal.
* It assumes every entry can experience an overnight gap-down past its stop-loss level.

---

## 2. Dual-Constraint Position Sizing

A fatal error in naive position sizing systems is relying solely on fixed-fractional risk per trade:

$$\text{Naive Qty} = \frac{\text{Capital} \times \text{Risk \%}}{\text{Entry} - \text{Stop Loss}}$$

### The Flaw of Naive Sizing:
Suppose you have a ₹1,00,000 portfolio and risk 1.5% (₹1,500) per trade. You identify an entry at ₹2,000 with a very tight stop-loss at ₹1,975 (stop distance = ₹25).
$$\text{Naive Qty} = \frac{₹1,500}{₹25} = 60 \text{ shares}$$
$$\text{Capital Required} = 60 \times ₹2,000 = ₹1,20,000 \quad (120\% \text{ of your total capital!})$$
If that stock opens the next day with a 5% gap-down at ₹1,900, you don't lose ₹1,500—you lose ₹6,000 (6% of your entire portfolio on a single trade).

---

### The Dual-Constraint Solution

Artha Quant enforces **two independent caps**, taking the smaller of the two:

```python
# risk/position_sizer.py

def calculate_position_size(
    portfolio_capital: float,
    risk_pct: float,            # e.g., 1.0% to 1.5%
    max_position_pct: float,    # e.g., 10.0% to 15.0%
    entry_price: float,
    stop_loss_price: float
) -> int:
    stop_distance = abs(entry_price - stop_loss_price)
    if stop_distance <= 0:
        return 0
    
    # 1. Quantity permitted by Risk Budget
    risk_budget = portfolio_capital * (risk_pct / 100.0)
    qty_by_risk = int(risk_budget / stop_distance)
    
    # 2. Quantity permitted by Capital Allocation Cap
    capital_cap = portfolio_capital * (max_position_pct / 100.0)
    qty_by_cap = int(capital_cap / entry_price)
    
    # Final quantity is strictly the lower of both
    final_qty = min(qty_by_risk, qty_by_cap)
    return max(0, final_qty)
```

With the dual constraint applied to the previous example:
* `qty_by_risk` = 60 shares
* `qty_by_cap` = $(₹1,00,000 \times 12\%) / ₹2,000 = 6$ shares
* **Result:** Final quantity is **6 shares** (₹12,000 allocated, maximum possible loss on overnight catastrophe is strictly bounded).

---

## 3. Portfolio-Level Circuit Breakers & Filters

Before any signal is approved for dispatch, it must pass 6 adversarial portfolio defense gates:

```
Candidate Signal
       │
       ├── 1. Peak Portfolio Drawdown Check
       │      └── If Portfolio Drawdown >= 15% from high: HALT ALL NEW SIGNALS.
       │
       ├── 2. Sector Concentration Filter
       │      └── Max 3 active positions per sector (e.g., Banking, Auto, IT).
       │      └── If sector cap reached: REJECT SIGNAL.
       │
       ├── 3. Correlation Filter
       │      └── Compute 60-day price correlation with existing open holdings.
       │      └── If correlation > 0.75: REJECT SIGNAL (avoids double-counting same beta).
       │
       ├── 4. Margin & Liquidity Check
       │      └── Query Kite Margins API (`kite.margins("equity")`).
       │      └── Verify required capital + 10% cash buffer is available.
       │      └── If insufficient cash: REJECT SIGNAL.
       │
       ├── 5. Anti-Averaging-Down Rule ("Never Add to a Loser")
       │      └── If an active position for the symbol exists and is at an unrealized loss,
       │          or candidate entry is lower than current entry price: VETO SIGNAL.
       │
       └── 6. Consecutive Loss Cooldown Breaker ("Streak Tilt / Regime Gate")
              └── If the last 3 closed trades were consecutive losses:
                  HALT ALL NEW SIGNALS for a mandatory 3-day cooldown period.
```

---

## 4. Gap-Down & GTT Limitations

Zerodha's **Good Till Triggered (GTT)** orders are widely misunderstood:
1. **Sell GTT requires held shares:** You cannot place a Sell GTT until the Buy order has settled and shares exist in your demat account or T1 holding.
2. **Delivery (CNC) Only:** GTT orders apply to delivery trades, not intraday MIS.
3. **Execution is a Limit Order:** When a GTT stop triggers, it places a limit order at your trigger/limit price. If the stock **gaps down below your limit price at the 09:15 open**, the trigger fires, but the order remains unfilled while the stock continues to drop.
4. **API Order Regulation:** Automating GTT creation via API triggers SEBI algorithmic trading static IP requirements. In Artha Quant, **you place GTT orders manually** via the official Kite UI.

**System Defense:** Never rely on GTT or stop orders to save an oversized position. The only genuine protection against an overnight black swan gap-down is the **10–12% hard capital cap per position**.

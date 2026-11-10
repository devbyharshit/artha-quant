# 04 — Backtesting, Costs & Tax Engine

## 1. The Cost Reality

Most algorithmic retail strategies that appear profitable on TradingView or paper fail immediately in production because they assume zero transaction friction and zero tax. In the Indian equity market, statutory charges, depository fees, and taxes create significant hurdle rates.

Artha Quant embeds a **deterministic Indian cost model** directly into the `vectorbt` backtesting engine.

---

## 2. Granular Zerodha Cost Model (Equity Delivery / CNC)

The following schedule reflects current regulatory and Zerodha charges for equity delivery trades:

| Charge Type | Rate / Basis | Applicable On | Notes |
|---|---|---|---|
| **Brokerage** | **₹0** (Zero) | Buy & Sell | Free for equity delivery on Zerodha |
| **STT (Securities Transaction Tax)** | **0.10%** (10 bps) | Both Buy & Sell | ₹100 per ₹1,00,000 traded |
| **Exchange Turnover Charges** | **0.00297%** (~3 bps) | Both Buy & Sell | NSE equity cash turnover fee |
| **SEBI Turnover Charges** | **₹10 per Crore** (0.0001%) | Both Buy & Sell | Regulatory levy |
| **Stamp Duty** | **0.015%** (1.5 bps) | **Buy Only** | State revenue levy |
| **DP Charges (Depository Participant)** | **₹15.93 flat** (₹13.50 + 18% GST) | **Sell Only** | Charged per scrip per day on delivery sell |
| **GST** | **18%** | Brokerage + Exchange + SEBI | STT and Stamp Duty are exempt from GST |

### Mathematical Formula for Total Roundtrip Cost ($C$)

Given an equity delivery trade with Buy Value ($V_b = Q \times P_b$) and Sell Value ($V_s = Q \times P_s$):

$$\text{STT} = 0.0010 \times (V_b + V_s)$$
$$\text{Exchange Fee} = 0.0000297 \times (V_b + V_s)$$
$$\text{SEBI Fee} = 0.000001 \times (V_b + V_s)$$
$$\text{Stamp Duty} = 0.00015 \times V_b$$
$$\text{DP Charge} = ₹15.93 \quad (\text{if } Q > 0)$$
$$\text{GST} = 0.18 \times (\text{Exchange Fee} + \text{SEBI Fee})$$

$$\text{Total Transaction Frictions} = \text{STT} + \text{Exchange Fee} + \text{SEBI Fee} + \text{Stamp Duty} + \text{DP Charge} + \text{GST}$$

*Notice: For smaller trade sizes (e.g., ₹10,000), the flat ₹15.93 DP charge alone consumes 0.16% of capital.*

---

## 3. Slippage Modeling by Liquidity Tier

Market orders or gap opens incur slippage between the signal price (bar close) and execution price:

| Universe Tier | Base Slippage | ADV Minimum | Execution Assumption |
|---|---|---|---|
| **Tier 1 (Nifty 50 Large-Cap)** | **0.05% to 0.10%** | > ₹100 Crore / day | Next bar Open $\pm$ 0.05% |
| **Tier 2 (Nifty Next 50 / Midcap)** | **0.20% to 0.50%** | > ₹25 Crore / day | Next bar Open $\pm$ 0.25% |
| **Tier 3 (Small-Cap / Illiquid)** | **Exempt / Excluded** | < ₹25 Crore / day | Do not trade; market impact destroys alpha |

### Dynamic Slippage Impact Formula
$$\text{Slippage bps} = \text{Base bps} + \left( \frac{\text{Order Size (₹)}}{\text{20-day Average Daily Volume (₹)}} \right) \times 100$$

---

## 4. Indian Capital Gains Tax Engine

Trading profits cannot be compounded tax-free. Under Indian tax law:
* **Short-Term Capital Gains (STCG):** Equity delivery held for $< 365$ days is taxed at **20% flat** on net realized gains (applicable from Budget 2024 revisions).
* **Long-Term Capital Gains (LTCG):** Delivery held $\ge 365$ days is taxed at **12.5%** on gains exceeding ₹1.25 Lakh per fiscal year.

### Tax Deduction in Backtesting
In `backtesting/cost_model.py`:
1. Calculate Gross Realized Profit on each closed trade: $P_{\text{gross}} = V_s - V_b - C$.
2. If $P_{\text{gross}} > 0$:
   * Deduct STCG liability: $\text{Tax} = 0.20 \times P_{\text{gross}}$.
   * Net Realized Profit: $P_{\text{net}} = 0.80 \times P_{\text{gross}}$.
3. Track accumulated tax liabilities and report **Net-of-Tax CAGR**.

---

## 5. Benchmark Comparison: Nifty 50 TRI

Every strategy must be benchmarked against **Nifty 50 Total Return Index (TRI)** over the identical time horizon. 

### Why TRI?
Nifty 50 TRI includes dividend reinvestment (~1.2%–1.5% additional annual return over price index). Beating a plain price index while underperforming TRI means passive index investing was superior with zero effort and lower risk.

---

## 6. Quantitative Acceptance Gates

Before any strategy qualifies for live paper trading, it must satisfy all 8 gates:

```
[Strategy Backtest Results]
       │
       ├── 1. Total Closed Trades >= 100
       │      └── FAILS: "Insufficient statistical sample size."
       │
       ├── 2. Outperforms Nifty 50 TRI net of all costs & 20% tax
       │      └── FAILS: "Negative alpha over passive index."
       │
       ├── 3. Max Drawdown <= 20% (and less than Nifty Drawdown)
       │      └── FAILS: "Unacceptable tail risk."
       │
       ├── 4. Expectancy per Trade >= 0.50% after costs
       │      └── FAILS: "Edge too thin; will vanish in live execution."
       │
       ├── 5. Free Parameters <= 3
       │      └── FAILS: "High risk of curve-fitting / data snooping."
       │
       ├── 6. Out-of-Sample (OOS) Sharpe >= 60% of In-Sample Sharpe
       │      └── FAILS: "Degrades out-of-sample; overfitted."
       │
       ├── 7. Profit Factor >= 1.50
       │      └── FAILS: "Gross gains do not sufficiently cushion gross losses."
       │
       └── 8. Positive Expectancy across Bull, Bear, and Sideways Regimes
              └── PASS: QUALIFIED FOR PHASE 3 (PAPER TRADING)
```

# Artha Quant — Project Documentation & Master Roadmap

Welcome to the **Artha Quant** project documentation. This system is a personal, quantitative trading validation and alert platform for the Indian equity market (NSE/Zerodha Kite).

The system's core premise: **Capital preservation through rigorous falsification.** It validates trading strategies net of all real Indian transaction frictions, slippage, and capital gains tax before dispatching actionable Telegram alerts on closed daily bars.

---

## Documentation Index

| Document | Purpose |
|---|---|
| [01. Architecture & System Design](./01-architecture-and-system-design.md) | High-level system architecture, module boundaries, data flows, and design principles. |
| [02. Data Layer & Kite Integration](./02-data-layer-and-kite-integration.md) | Kite Connect API setup, token lifecycle, rate limiting, corporate action adjustments, and Parquet storage. |
| [03. Strategy Engine & Pine Parity](./03-strategy-engine-and-pine-parity.md) | Strategy interface, Pine Script-to-Python translation, lookahead bias prevention, and bar-by-bar verification. |
| [04. Backtesting, Costs & Tax Engine](./04-backtesting-costs-and-tax-engine.md) | Exact Zerodha fee structure, slippage models, Indian STCG tax modeling, and benchmark comparison. |
| [05. Risk Management & Position Sizing](./05-risk-management-and-position-sizing.md) | Dual-constraint sizing formulas, portfolio circuit breakers, correlation filters, and exposure caps. |
| [06. Signal Engine & Telegram Alerts](./06-signal-engine-and-telegram-bot.md) | SQLite deduplication, Telegram bot configuration, alert templates, and dispatch logic. |
| [07. Scheduler & Production Operations](./07-scheduler-and-operations.md) | Cron/APScheduler orchestration, daily operational timeline, watchdog health checks, and hosting. |
| [08. Progressive Implementation WBS](./08-progressive-implementation-wbs.md) | Sprint-by-sprint Work Breakdown Structure (Sprints 0–7) with acceptance criteria and definition of done. |

---

## Phased Progression Gateways

No stage begins until the prior stage's quantitative gate has been satisfied:

```
┌─────────────────────────────────────────────────────────────┐
│ Stage 1: MVP (Daily Scanner & Telegram Bot)                 │
│ Gate: Runs unattended for 14 consecutive trading days       │
│       with zero silent failures or duplicate alerts.        │
└──────────────────────────────┬──────────────────────────────┘
                               │ PASS
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ Stage 2: Strategy Validation (Net of Cost & Tax)            │
│ Gate: Outperforms Nifty 50 TRI net of all fees and 20% STCG │
│       over 100+ trades and survives holdout out-of-sample.  │
└──────────────────────────────┬──────────────────────────────┘
                               │ PASS
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ Stage 3: Forward Paper Trading (3 Months Live)              │
│ Gate: Live signals match backtest signals 1:1;              │
│       execution metrics align within expected slippage bands│
└──────────────────────────────┬──────────────────────────────┘
                               │ PASS
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ Stage 4: Micro-Capital Live Deployment                      │
│ Gate: Manual order placement with strict position caps.     │
└─────────────────────────────────────────────────────────────┘
```

---

## Core Regulatory & Operational Constraints

1. **Read-Only System:** Data In $\rightarrow$ Logic $\rightarrow$ Telegram Out. All order executions are executed manually by hand on Zerodha Kite. This eliminates SEBI algorithmic trading regulatory compliance hurdles (effective April 2026).
2. **Personal Use Only:** Signals are strictly private. Sharing signals publicly or in closed groups incurs SEBI Investment Adviser (IA) / Research Analyst (RA) registration liabilities.
3. **Daily Auth Reality:** Kite Connect access tokens expire daily around 06:00 IST. A daily authentication workflow is a hard operational prerequisite.

# 11 — AI Agents & LLM Trading Systems: Landscape Evaluation & Integration Plan

**Status:** Research & Architecture Plan  
**Source Repository Evaluated:** [`georgezouq/awesome-ai-in-finance`](https://github.com/georgezouq/awesome-ai-in-finance)  
**Target System:** Artha Quant (NSE/BSE Indian Equities, Zerodha Kite, Daily/Swing, `vectorbt`, Fail-Closed Risk Engine)

---

## 1. Executive Summary & Objective

This document evaluates the multi-agent and LLM-driven trading frameworks cataloged in `awesome-ai-in-finance`, with the specific goal of answering:
> **"Can we use any of these agent repositories to enhance Artha Quant?"**

### The Core Filtering Lens
Artha Quant operates under strict architectural and regulatory constraints:
1. **Target Market:** Indian equities (NSE/BSE cash segment).
2. **Execution & Regulatory Boundary:** Read-only alerts via Telegram. Personal use only. Manual order entry on Zerodha Kite to avoid SEBI retail algo-trading constraints (effective April 2026).
3. **Core Tenet:** **Deterministic compute, zero hallucination.** Quantitative signals, price checks, stop losses, and position sizing must be pure mathematical functions over verified data. An LLM must never invent a price, fill, or trade sizing.
4. **Timeframe:** Daily confirmed bars (EOD scan at 16:00 IST).

### Verdict Summary
- **Directly Usable / High Value:** **`Vibe-Trading`** (Alpha Zoo with 478 factors, Zerodha connector, Pine script exporter) and **`TradingAgents`** (multi-agent debate framework for qualitative news/sentiment filtering).
- **Specialized Value:** **`FinRobot`** (deterministic valuation + LLM IC memos), **`TraceArena`** (auditable benchmark runtime), and **`FinceptTerminal`** (open-source Bloomberg-style analytics/data terminal — useful as a research/data reference, not a trading agent).
- **Architectural Reference (Borrow ideas, don't import):** **`ATLAS`** (Darwinian prompt tuning and market regime cohorts).
- **Irrelevant / Incompatible:** **`nofx`** (crypto perpetuals only), **`oracle3` / `ProfitPlay`** (prediction markets), **`Nova-TradingAgent`** (China A-shares only), **`AutoHedge`** (autonomous agent that *places real trades itself* on Solana/crypto — directly conflicts with Artha Quant's manual-execution/SEBI boundary and has no Indian-equity support), and closed-source SaaS products (**`Pineify`**, **`Cod3x`**).

---

## 2. Comprehensive Repository Breakdown

### A. Tier 1: High Practical Value for Artha Quant

#### 1. [Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) (`HKUDS/Vibe-Trading`)
* **Role in Ecosystem:** All-in-one personal quantitative agent platform (ranked #1 on GitHub in backtesting, #3 in algorithmic trading).
* **Tech Stack:** Python 3.11+, FastAPI backend, React 19 UI, PyPI package (`vibe-trading-ai`), MCP server.
* **Why it fits Artha Quant:**
  - **Native Zerodha integration:** Already provides an out-of-the-box Zerodha connector profile alongside Dhan and Shoonya.
  - **Indian market awareness:** Built-in rules for Indian market calibers, currency handling, and Indian stamp duty on short covers.
  - **Alpha Zoo (478 Factors):** Implements factor libraries from Qlib, WorldQuant Alpha101, GTJA 191, and academic finance literature.
  - **Pine Script Tooling:** Built-in `/pine` exporter to convert strategy signals into Pine Script code, matching Artha Quant’s Pine parity goals.
  - **Shadow Account:** Replays historical trade journals against rule-based baselines to identify behavioral leakages and transaction fee drag.
* **How to use it:**
  - Run as an independent research sandbox (`pip install vibe-trading-ai`) to discover and screen new candidate factors.
  - Lift the Python factor implementations from its Alpha Zoo into Artha Quant's `strategies/` directory for validation under `backtesting/walk_forward.py`.

---

#### 2. [TradingAgents](https://github.com/TauricResearch/TradingAgents) (`TauricResearch/TradingAgents`)
* **Role in Ecosystem:** Multi-agent LLM financial trading framework built on LangGraph (Tauric Research).
* **Tech Stack:** Python 3.11+, LangGraph, multi-provider LLM support (Claude, OpenAI, DeepSeek, Gemini, local Ollama).
* **Architecture:**
  - **Analyst Team:** Fundamentals, Sentiment (StockTwits/Reddit), News, and Technical indicators.
  - **Researcher Debate:** Bullish vs. Bearish researcher debate to eliminate confirmation bias.
  - **Trader & Risk / Portfolio Manager:** Synthesizes consensus, evaluates portfolio risk, and decides action.
* **Why it fits Artha Quant:**
  - **Indian ticker support:** Resolves NSE/BSE tickers automatically via Yahoo Finance (`RELIANCE.NS`, `TCS.NS`).
  - **Point-in-time integrity:** Strict separation of historical data snapshots to prevent look-ahead bias on filings and news.
  - **Deterministic price grounding:** Analyst claims are anchored against verified OHLCV data rather than hallucinated LLM memory.
  - **Memory Log & Reflection:** Persists past decisions and settles them against realized performance to learn what worked.
* **How to use it:**
  - **As an EOD "Second-Opinion Filter":** When Artha Quant's deterministic engine generates a BUY/SELL signal at 16:00 IST, pass the symbol to `TradingAgents` to run a 60-second background debate on recent news, earnings sentiment, and macro risks.
  - The synthesized debate summary (Bull case, Bear case, Red flags) is appended to the Telegram alert card.

---

### B. Tier 2: Specialized Use Cases (Valuation & Strategy Auditing)

#### 3. [FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) (`AI4Finance-Foundation/FinRobot`)
* **Role in Ecosystem:** Financial decision intelligence platform by AI4Finance Foundation.
* **Tech Stack:** Python 3.10+, PydanticAI, FastAPI, React/Tauri desktop cockpit.
* **Core Principle:** *"Models reason, software computes, agents orchestrate, and systems verify."*
  - Valuation models (DCF, DDM, LBO, comps, WACC) run on pure Python deterministic operators.
  - LLMs only write the qualitative narrative and Investment Committee (IC) memo.
* **Utility for Artha Quant:**
  - While Artha Quant is focused on daily technical/statistical swing trades, FinRobot can serve as an on-demand fundamental valuation generator for stocks that pass technical screening.

---

#### 4. [TraceArena](https://github.com/tonyhyworld/TraceArena) (`tonyhyworld/TraceArena`)
* **Role in Ecosystem:** Open-source runtime for auditable multi-agent investment evaluation.
* **Tech Stack:** Python, Vue.js, deterministic simulation engine.
* **Core Philosophy:** Strictly no brokerage connection. All agents submit typed actions that face identical simulated settlement rules, slippage, and commission models.
* **Utility for Artha Quant:**
  - Useful as a scientific test harness to answer: *"Does adding an LLM agent improve risk-adjusted returns over our deterministic vectorbt baseline, or does it add noise and cost?"*

---

#### 5. [FinceptTerminal](https://github.com/Fincept-Corporation/FinceptTerminal) (`Fincept-Corporation/FinceptTerminal`)
* **Role in Ecosystem:** Open-source, Bloomberg-terminal-style financial intelligence desktop app (C++20/Qt6 + embedded Python, formerly a Tauri/Bun build), aimed at market analytics, investment research, and economic data — not a trading agent or execution engine.
* **Tech Stack:** C++20, Qt6, embedded Python; Docker build scripts; AGPL/mixed licensing depending on version — **check the LICENSE file before vendoring any code**, since the project trademarks its name/logo and restricts commercial forks.
* **Why it's relevant (narrowly):**
  - It's a data/analytics surface, not a signal or execution layer, so it doesn't compete with or threaten Artha Quant's deterministic core.
  - Could be a candidate **data source or MCP** for macro/economic indicators and CFA-style fundamental analytics to enrich Telegram alerts (same slot as `FinRobot`/`Tapetide` in Phase C below), *if* its data providers cover NSE/BSE adequately — not yet verified.
* **Utility for Artha Quant:** Low priority, optional — evaluate only if/when fundamental-data enrichment (Phase C) is actually pursued. Don't adopt the desktop app itself; at most reuse a data-fetching module.

---

#### 6. [AI Hedge Fund](https://github.com/virattt/ai-hedge-fund) (`virattt/ai-hedge-fund`)
* **Role in Ecosystem:** Persona-based investment analysis simulation (Buffett, Munger, Graham, Ackman, Wood).
* **Limitations:** Coupled to US equity data via `financialdatasets.ai` (requires credit card or Base USDC micro-payments).
* **Utility for Artha Quant:** Minimal direct integration value, but clean reference code for prompt engineering persona-based debates.

---

### C. Tier 3: Conceptual Inspiration (Patterns to Borrow)

#### 7. [ATLAS](https://github.com/chrisworsey55/atlas-gic) (`chrisworsey55/atlas-gic`)
* **Role in Ecosystem:** Self-improving AI trading framework by General Intelligence Capital.
* **Key Mechanisms:**
  - **Autoresearch Prompt Optimization:** Inspired by Karpathy’s autoresearch. Prompts act as model weights; rolling Sharpe is the loss function. The worst-performing agent's prompt is modified and tested for 5 trading days; changes are kept (git commit) or reverted (git reset).
  - **Darwinian Weighting:** Top quartile agents get their weight boosted by 1.05x; bottom quartile reduced by 0.95x.
  - **All Seasons (PRISM) Cohorts:** Separate agent cohorts trained on distinct historical regimes (Bull, Euphoria, Crisis, Rate Tightening).
* **Status:** Core trained prompts and weights are proprietary.
* **Key Takeaway for Artha Quant:** The concept of **regime-specific strategy weighting** and **Darwinian capital allocation** across quantitative sleeves can be implemented deterministically in `risk/position_sizer.py`.

---

### D. Tier 4: Incompatible / Not Recommended

| Repository | Focus | Reason for Exclusion |
|---|---|---|
| **`nofx`** | Crypto Perpetuals (Binance, OKX, Hyperliquid) | Go/React stack, crypto perps only, requires Base USDC wallet. Zero Indian market support. |
| **`oracle3` / `ProfitPlay` / `AgentFund`** | Prediction markets (Kalshi, Polymarket, Solana) | Irrelevant to Indian cash equity swing trading. |
| **`Nova-TradingAgent` / `stock-analysis`** | China A-shares & Hong Kong stocks | Hardcoded to Chinese data providers (Tushare, AkShare, Eastmoney). |
| **`Cod3x` / `Pineify`** | Commercial SaaS / Closed-source | Paid web subscriptions; not extensible Python code. |
| **[`AutoHedge`](https://github.com/The-Swarm-Corporation/AutoHedge)** | Autonomous "agent hedge fund" that **places live trades itself** (Solana wallet key; Coinbase planned) | Violates Artha Quant's core, non-negotiable boundary: this system is alert-only so the human stays the executing party and it stays outside SEBI's algo-trading registration rules. `AutoHedge` is architected to self-execute — adopting it, even partially, would require giving an agent direct order-placement authority, which is explicitly out of scope. Also crypto-only (Solana/Coinbase), zero NSE/BSE or Zerodha support. **Note:** its 4-role pipeline (Director → Quant → Risk Manager → Execution) is a clean separation-of-concerns pattern worth noting alongside `ATLAS` in Tier 3 as a conceptual reference — but only up to the Risk Manager step; the Execution role must never be ported. |

---

## 3. Notable Ecosystem Skills & MCP Servers

Beyond full agents, the list includes modular tools that integrate directly into agent workflows:

1. **`Jimmy7892/quant-research-skill`:** Validates backtests against parameter stability, selection bias, and walk-forward degradation.
2. **`Tapetide`:** Indian market data API and MCP server for NSE/BSE stocks, corporate financials, and institutional flows.
3. **`atilaahmettaner/tradingview-mcp`:** MCP server providing 30+ tools for real-time TradingView technical screeners and indicator calculations.

---

## 4. Integration Blueprint: How to Safely Use AI Agents in Artha Quant

To preserve Artha Quant's core principles (deterministic execution, SEBI read-only safety, zero hallucination), LLMs and agents must **never** be given authority to place orders or compute technical indicators directly.

Instead, they should be deployed in two isolated, high-leverage stages:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                    STAGE 1: RESEARCH                                   │
│                                                                                        │
│   Vibe-Trading Alpha Zoo (478 Alphas)  ──►  Port candidate factors to strategies/      │
│                                            ──►  Validate in vectorbt + walk_forward    │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │ Survives acceptance gates
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                              STAGE 2: PRODUCTION EOD SCAN                              │
│                                                                                        │
│   Cron (16:00 IST)                                                                     │
│        │                                                                               │
│        ▼                                                                               │
│   Kite Historical Daily Bars                                                           │
│        │                                                                               │
│        ▼                                                                               │
│   Deterministic Strategy Engine (vectorbt) ──► SignalEvent (e.g., BUY RELIANCE)        │
│        │                                                                               │
│        ▼                                                                               │
│   Deterministic Risk & Sizer (Capital caps, drawdown breaker)                          │
│        │                                                                               │
│        ▼ (Signal Validated)                                                            │
│   ┌────────────────────────────────────────────────────────────────────────────────┐   │
│   │ OPTIONAL: TradingAgents / LLM Reasoning Filter (60s Async Call)                │   │
│   │ • News & Earnings Sentiment Scan (Yahoo Finance / NSE filings)                 │   │
│   │ • Red Flag Check (Corporate governance, impending earnings release)            │   │
│   │ • Bull / Bear Debate Summary                                                   │   │
│   └───────────────────────────────────────┬────────────────────────────────────────┘   │
│                                           │                                            │
│                                           ▼                                            │
│   Telegram Notifier (Actionable Trade Card + LLM Reasoning Summary)                    │
│        │                                                                               │
│        ▼                                                                               │
│   Manual Trader Execution on Zerodha Kite Mobile/Web (100% SEBI Compliant)             │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Phased Integration Plan

### Phase A: Factor Library Expansion (Week 1–2)
- **Tool:** `Vibe-Trading` factor codebase.
- **Action:** Inspect the top-performing cross-sectional factors from `Alpha101` and `GTJA191` in `Vibe-Trading`.
- **Target:** Port 3–5 uncorrelated factors into Artha Quant's `strategies/` directory.
- **Evaluation:** Run `backtesting/run_walk_forward.py` on the 73-symbol NSE universe using the cleaned corporate action dataset.

### Phase B: EOD Signal Enrichment & Qualitative Reasoning (Week 3–4)
- **Tool:** Lightweight variant of `TradingAgents` reasoning graph.
- **Action:** Build an optional enrichment step in `alerts/formatter.py`. When a signal is emitted:
  1. Fetch latest news and earnings date for the symbol.
  2. Prompt an LLM with structured guidelines: *"Given this BUY signal on {SYMBOL} at {PRICE}, evaluate any immediate earnings announcements, litigation, or regulatory red flags in the past 7 days."*
  3. Include a concise 3-bullet point rationale inside the Telegram alert.

### Phase C: Valuation & Deep Research on Demand (Optional / Later)
- **Tool:** `FinRobot` or `Tapetide` MCP.
- **Action:** Add a Telegram bot command (e.g., `/research RELIANCE`) that triggers a background task to generate a one-page Markdown fundamental tearsheet.

---

## 6. Key References & Source Links

- Awesome AI in Finance: https://github.com/georgezouq/awesome-ai-in-finance
- Vibe-Trading: https://github.com/HKUDS/Vibe-Trading
- TradingAgents: https://github.com/TauricResearch/TradingAgents
- FinRobot: https://github.com/AI4Finance-Foundation/FinRobot
- ATLAS (Autoresearch Trading): https://github.com/chrisworsey55/atlas-gic
- TraceArena: https://github.com/tonyhyworld/TraceArena
- Tapetide (NSE/BSE MCP): https://tapetide.com
- FinceptTerminal: https://github.com/Fincept-Corporation/FinceptTerminal
- AutoHedge (evaluated, not adopted — see Tier 4): https://github.com/The-Swarm-Corporation/AutoHedge

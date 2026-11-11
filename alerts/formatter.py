"""Markdown card formatters for Telegram alerts and daily summaries."""
from typing import Dict, Any


def format_trade_alert_card(signal: Dict[str, Any], available_cash: float = 0.0) -> str:
    """
    Formats a clean, actionable Markdown trade card for Telegram.
    """
    symbol = signal["symbol"].upper()
    action = signal["action"].upper()
    strategy = signal["strategy"]
    timeframe = signal.get("timeframe", "1D")
    bar_ts = signal.get("bar_timestamp", "Recent Close")

    entry_ref = float(signal["entry_ref"])
    stop_loss = float(signal["stop_loss"])
    target = float(signal["target"])
    rr_ratio = float(signal.get("risk_reward_ratio", 0.0))
    has_technical_stop = stop_loss > 0

    stop_pct = ((stop_loss - entry_ref) / entry_ref) * 100.0 if entry_ref > 0 else 0.0
    target_pct = ((target - entry_ref) / entry_ref) * 100.0 if entry_ref > 0 else 0.0

    qty = int(signal["calculated_qty"])
    capital_req = float(signal["capital_required"])
    risk_budget = float(signal["risk_budget"])
    alloc_pct = float(signal.get("capital_allocation_pct", 0.0))
    limiting_factor = signal.get("limiting_factor", "DUAL_CAP")

    post_cash = max(0.0, available_cash - capital_req)
    icon = "🟢" if action == "BUY" else "🔴"

    if has_technical_stop:
        trade_params = (
            f"Entry Ref : `₹{entry_ref:,.2f}`\n"
            f"Stop Loss : `₹{stop_loss:,.2f}` ({stop_pct:+.2f}%)\n"
            f"Target    : `₹{target:,.2f}` ({target_pct:+.2f}%)\n"
            f"R:R Ratio : `1 : {rr_ratio:.1f}`\n"
        )
        sizing_header = "*Position Sizing (Dual-Constraint)*"
        sizing_lines = (
            f"Suggested Qty : `{qty} shares` ({limiting_factor})\n"
            f"Capital Req   : `₹{capital_req:,.2f}` ({alloc_pct:.1f}% cap)\n"
            f"Risk Budget   : `₹{risk_budget:,.2f}`\n"
        )
    else:
        trade_params = (
            f"Entry Ref : `₹{entry_ref:,.2f}`\n"
            f"Exit Rule : *Signal-exit only — no technical stop.* Exit on the next SELL signal for this symbol.\n"
        )
        sizing_header = "*Position Sizing (Fixed Equity Allocation)*"
        sizing_lines = (
            f"Suggested Qty : `{qty} shares` ({alloc_pct:.1f}% of capital)\n"
            f"Capital Req   : `₹{capital_req:,.2f}`\n"
        )

    card = (
        f"{icon} *{action} SIGNAL — {symbol} (NSE)*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Strategy  : `{strategy}`\n"
        f"Timeframe : `{timeframe}` (Closed Bar)\n"
        f"Action    : *Actionable for Next Open*\n\n"
        f"*Trade Parameters*\n"
        f"────────────────────────────\n"
        f"{trade_params}\n"
        f"{sizing_header}\n"
        f"────────────────────────────\n"
        f"{sizing_lines}\n"
        f"*Account Snapshot*\n"
        f"────────────────────────────\n"
        f"Available Cash : `₹{available_cash:,.2f}`\n"
        f"Post-Trade Cash: `₹{post_cash:,.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ *Manual Execution:* Place CNC order manually on Kite. Verify market depth."
    )
    return card


def format_eod_summary_card(
    symbols_scanned: int,
    signals_emitted: int,
    signals_vetoed: int,
    elapsed_seconds: float
) -> str:
    """Formats end-of-day scanner execution heartbeat."""
    return (
        f"📊 *ARTHA QUANT — EOD SCAN COMPLETE*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Symbols Evaluated : `{symbols_scanned}`\n"
        f"Actionable Alerts : `{signals_emitted}`\n"
        f"Risk Vetoes       : `{signals_vetoed}`\n"
        f"Execution Time    : `{elapsed_seconds:.2f}s`\n"
        f"System Status     : *NOMINAL (All Gates Passed)*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

# Artha Quant — Operations Makefile

.PHONY: help test auth check-auth sync scan dry-run backtest backtest-rft backtest-gc backtest-mr backtest-ev backtest-universe backtest-universe-tb backtest-universe-rft backtest-universe-gc backtest-universe-mr backtest-universe-ev backtest-universe-all walk-forward walk-forward-tb walk-forward-rft walk-forward-gc walk-forward-mr walk-forward-ev walk-forward-all sync-backfill add-holdings daemon clean

help:
	@echo "Artha Quant Commands:"
	@echo "  make test        - Run the full test suite"
	@echo "  make auth        - Run interactive browser Kite token refresh"
	@echo "  make check-auth  - Run 08:30 IST morning auth health check"
	@echo "  make sync        - Ingest 5-year historical data for the universe (new symbols) and add new bars"
	@echo "  make sync-backfill - Extend every universe symbol's daily history back 15 years"
	@echo "  make add-holdings - Pull your real Kite holdings and merge any missing symbols into configs/universe.yaml"
	@echo "  make scan        - Run 16:00 IST live End-of-Day market scanner"
	@echo "  make dry-run     - Run scanner on local Parquet cache without Kite API"
	@echo "  make backtest    - Run backtester CLI with Indian costs and 20% tax"
	@echo "  make backtest-rft - Backtest the Range Filtered Trend strategy (full-equity sizing)"
	@echo "  make backtest-gc - Backtest the Gaussian Channel strategy (full-equity sizing)"
	@echo "  make backtest-mr - Backtest the Mean Reversion strategy (risk-based sizing)"
	@echo "  make backtest-ev - Backtest the EMA/VWAP Cross strategy (full-equity sizing)"
	@echo "  make backtest-universe-tb  - Backtest Trend Breakout across the full universe (risk-based sizing)"
	@echo "  make backtest-universe-rft - Backtest Range Filtered Trend across the full universe (full-equity sizing)"
	@echo "  make backtest-universe-gc  - Backtest Gaussian Channel across the full universe (full-equity sizing)"
	@echo "  make backtest-universe-mr  - Backtest Mean Reversion across the full universe (risk-based sizing)"
	@echo "  make backtest-universe-ev  - Backtest EMA/VWAP Cross across the full universe (full-equity sizing)"
	@echo "  make backtest-universe-all - Run all universe backtests above, one after another"
	@echo "  make backtest-universe - Alias for backtest-universe-rft (kept for backward compatibility)"
	@echo "  make walk-forward-tb  - Walk-forward validation (rolling OOS folds, portfolio-level) of Trend Breakout"
	@echo "  make walk-forward-rft - Walk-forward validation of Range Filtered Trend"
	@echo "  make walk-forward-gc  - Walk-forward validation of Gaussian Channel"
	@echo "  make walk-forward-mr  - Walk-forward validation of Mean Reversion"
	@echo "  make walk-forward-ev  - Walk-forward validation of EMA/VWAP Cross"
	@echo "  make walk-forward-all - Run every walk-forward validation above, one after another"
	@echo "  make walk-forward     - Alias for walk-forward-rft (kept for backward compatibility)"
	@echo "  make daemon      - Start automated APScheduler daemon (Asia/Kolkata)"
	@echo "  make clean       - Clear python cache files"

test:
	.venv/bin/pytest -v

auth:
	.venv/bin/python -m scheduler.token_refresh

check-auth:
	.venv/bin/python -m scheduler.auth_check

sync:
	.venv/bin/python -m data.loaders.sync_history

sync-backfill:
	.venv/bin/python -m data.loaders.sync_history --years 15 --backfill

add-holdings:
	.venv/bin/python -m data.loaders.add_holdings_to_universe

scan:
	.venv/bin/python main.py

dry-run:
	.venv/bin/python main.py --dry-run

backtest:
	.venv/bin/python -m backtesting.engine --symbol RELIANCE

backtest-rft:
	.venv/bin/python -m backtesting.engine --symbol RELIANCE --strategy range_filtered_trend

backtest-gc:
	.venv/bin/python -m backtesting.engine --symbol RELIANCE --strategy gaussian_channel

backtest-mr:
	.venv/bin/python -m backtesting.engine --symbol RELIANCE --strategy mean_reversion

backtest-ev:
	.venv/bin/python -m backtesting.engine --symbol RELIANCE --strategy ema_vwap_cross

backtest-universe-tb:
	.venv/bin/python -m backtesting.run_universe --strategy trend_breakout

backtest-universe-rft:
	.venv/bin/python -m backtesting.run_universe --strategy range_filtered_trend

backtest-universe-gc:
	.venv/bin/python -m backtesting.run_universe --strategy gaussian_channel

backtest-universe-mr:
	.venv/bin/python -m backtesting.run_universe --strategy mean_reversion

backtest-universe-ev:
	.venv/bin/python -m backtesting.run_universe --strategy ema_vwap_cross

backtest-universe-all: backtest-universe-tb backtest-universe-rft backtest-universe-gc backtest-universe-mr backtest-universe-ev

backtest-universe: backtest-universe-rft

walk-forward-tb:
	.venv/bin/python -m backtesting.run_walk_forward --strategy trend_breakout

walk-forward-rft:
	.venv/bin/python -m backtesting.run_walk_forward --strategy range_filtered_trend

walk-forward-gc:
	.venv/bin/python -m backtesting.run_walk_forward --strategy gaussian_channel

walk-forward-mr:
	.venv/bin/python -m backtesting.run_walk_forward --strategy mean_reversion

walk-forward-ev:
	.venv/bin/python -m backtesting.run_walk_forward --strategy ema_vwap_cross

walk-forward-all: walk-forward-tb walk-forward-rft walk-forward-gc walk-forward-mr walk-forward-ev

walk-forward: walk-forward-rft

daemon:
	.venv/bin/python -m scheduler.jobs

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +

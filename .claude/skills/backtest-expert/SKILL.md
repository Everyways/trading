---
name: backtest-expert
description: Run structured, reproducible backtests on this project's strategies using Alpaca historical data. Use when the user asks to "backtest", "test strategy X on data", "evaluate a new idea", or "compare parameters".
---

# Backtest Expert

Turn vague strategy ideas into reproducible backtest reports using the tooling already present in this repository. No EODHD / paid feeds — Alpaca's free paper-account history is the data source, same as live trading.

## Recipe — follow this order every time

1. **Confirm the setup**. Before running anything, restate back to the user:
   - Which strategy (one of: `adx_ema_trend`, `bollinger_bands`, `breakout`, `ma_crossover`, `macd_crossover`, `rsi_mean_reversion`, or a new one)
   - Which symbol(s) and timeframe
   - The date range (require ≥ 2 years of in-sample data or explicitly call out the overfitting risk)
   - The parameter grid (if sweeping) or the exact values being tested

2. **Never run the backtest silently**. Invoke `scripts/run_backtest.py` — do not reimplement backtest loops.
   ```bash
   python scripts/run_backtest.py \
       --strategy rsi_mean_reversion \
       --symbol AAPL \
       --timeframe 15m \
       --start 2023-01-01 \
       --end 2024-12-31
   ```
   Read the script before calling it to pick up any new CLI flags.

3. **Indicators must come from `app/strategies/`**. If the strategy isn't there yet, add it there first (see the `signal-generation` skill). Do not inline pandas/numpy indicator logic into one-off scripts — that bypasses the existing test coverage.

4. **Report the canonical metrics**. Always show:
   - Net return (%), annualised Sharpe, max drawdown (%), win rate (%), trade count
   - Number of trades per month (flag if < 4/month or > 60/month)
   - In-sample window length — if < 2 years, add a bold **Overfitting risk** note

5. **Walk-forward before declaring victory**. If the user is about to deploy a change to `config/strategies/*.yaml`, run:
   ```bash
   python scripts/run_walk_forward.py --strategy <name> --symbol <sym>
   ```
   Out-of-sample results must stay within 30% of in-sample Sharpe, otherwise flag it.

6. **Persist the result**. Backtest reports go to `data/backtests/<strategy>_<symbol>_<YYYYMMDD>.json` — do not leave the numbers only in chat. Commit the JSON with the related config change.

## Known traps in this repo

- **Candle source is Alpaca, not EODHD**. Symbols are bare tickers (`AAPL`, not `AAPL.US`).
- **`lookback` in the YAML config** determines how many bars the runner fetches at tick time — it is NOT the backtest window. Don't conflate them.
- **Fractional shares are allowed on Alpaca** — the backtest must not round to integer shares.
- **Paper account has NO historical crypto from an EU IP** — backtests on crypto symbols fail silently with empty bars. Skip them.
- **`stop_loss_pct` and `take_profit_pct` are bracket legs** (see `app/execution/runner.py::_submit_buy`). Backtests must simulate both: the stop hits first if price touches it before the TP in the same bar.

## Output

Deliver a short summary (≤ 15 lines) and the path to the JSON report. If comparing multiple variants, show a Markdown table sorted by Sharpe descending.

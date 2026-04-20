---
name: signal-generation
description: Translate a strategy idea (English/Pine Script/research note) into a new Strategy subclass that follows this project's pattern. Use when the user says "add a new strategy", "implement this idea", "port this TradingView script", or similar.
---

# Signal Generation

Convert strategy rules into a clean, vectorised `Strategy` subclass that plugs into the existing trading runner. Keep everything vectorised (pandas / numpy) — no per-row Python loops, no look-ahead bias.

## Recipe — follow this order every time

1. **Parse the rules into explicit conditions.**
   - List each entry condition as a Boolean pandas Series (aligned to the `df` index).
   - Do the same for exits. If the strategy is long-only with a bracket SL/TP, exits can come from the bracket legs instead of a separate signal.
   - Write the conditions in the PR description / commit message before touching code, so a reviewer can verify them.

2. **Create the Strategy file at `app/strategies/<snake_name>.py`**. Mirror the shape of `app/strategies/rsi_mean_reversion.py`:
   ```python
   from app.core.registry import strategy_registry
   from app.strategies.base import Strategy

   @strategy_registry.register("my_new_strategy")
   class MyNewStrategy(Strategy):
       required_params = {"period": int, "threshold": float}
       default_params = {"period": 14, "threshold": 30.0}

       def generate_signal(self, df: pd.DataFrame, ctx: StrategyContext) -> Signal | None:
           # vectorised computation on df, then inspect df.iloc[-1]
           ...
   ```
   Export it from `app/strategies/__init__.py` so the registry decorator runs.

3. **Shift before you compare.** Every indicator the signal depends on must be produced from data that existed at bar close. If you use `df["close"]`, the signal is allowed to compare against it directly (that bar IS closed by the time `generate_signal` runs — see `app/execution/runner.py::_candles_to_df`). But anything involving a future value (`df["close"].shift(-1)`) is a bug.

4. **Add the YAML config at `config/strategies/<snake_name>.yaml`** with:
   ```yaml
   name: my_new_strategy
   enabled: false          # start disabled — never auto-deploy
   version: "1.0.0"
   mode: paper
   timeframe: 15m
   lookback: 120           # enough bars for all indicators
   universe:
     - symbol: AAPL
       asset_class: equity
   params:
     period: 14
     threshold: 30.0
   risk:
     max_risk_per_trade_pct: 1.0
     max_concurrent_positions: 2
     max_daily_loss_pct: 2.0
     max_orders_per_minute: 3
   ```

5. **Prove no look-ahead bias.** Add a unit test in `tests/unit/strategies/test_<snake_name>.py`:
   - Fixture: a synthetic OHLCV DataFrame where the expected signal timing is known.
   - Assert: the strategy produces the signal on the expected bar, and NO signal on the bar before.

6. **Backtest before enabling.** Run the `backtest-expert` skill on ≥ 2 years of data. Never flip `enabled: true` without a committed backtest JSON.

## What NOT to do

- **Don't** use TA-Lib or pandas_ta — the project computes indicators from scratch for auditability (see `_rsi` in `rsi_mean_reversion.py`).
- **Don't** call the broker from inside `generate_signal` — the method is pure and synchronous. All I/O happens in `app/execution/runner.py`.
- **Don't** mutate `df` in place past what's needed for the signal. Return a `Signal` or `None`.
- **Don't** hardcode thresholds — put them in `default_params` so YAML can override them.

## Output

After implementing, report:
1. File paths created/modified.
2. The exact entry/exit conditions as a bullet list.
3. A pointer to the backtest command the user should run next.

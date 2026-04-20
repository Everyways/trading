---
name: risk-analysis
description: Size positions, compute stop distances, and audit the risk layer for this trading bot. Use when the user asks about "position sizing", "stop loss placement", "risk per trade", "portfolio heat", or "can I add another strategy".
---

# Risk Analysis

All sizing and safety checks go through `app/risk/manager.py::RiskManager` and `app/risk/position_sizer.py::size_position`. This skill keeps every analysis rooted in those functions — never reinvent them inline.

## Recipe — follow this order every time

1. **Start from the configured limits.** Read `config/risk_global.yaml` and the strategy's own `risk:` block. The precedence (first failure wins) is documented in `app/risk/manager.py`:
   1. Global kill switch (file + DB)
   2. Strategy kill switch
   3. Monthly hard stop (−50 €, EUR after USD→EUR conversion)
   4. Global daily loss
   5. Strategy daily loss
   6. Max concurrent positions per strategy
   7. PDT compliance (3 day-trades per 5 rolling days)
   8. Order rate limit per strategy per minute

2. **Size with `size_position`, never with hand-rolled math.**
   ```python
   from app.risk.position_sizer import size_position

   qty = size_position(
       account_equity=Decimal("500"),
       entry_price=Decimal("175.32"),
       stop_loss_pct=2.0,
       risk_pct=1.0,      # 1% of equity at risk
   )
   ```
   If a new use case needs a different sizing rule, add it to `position_sizer.py` with a unit test — do not duplicate the formula elsewhere.

3. **ATR-based stops are fine — convert to % first.** The bracket legs in `OrderRequest` use absolute prices, but the strategy YAML uses `stop_loss_pct`. Convert:
   ```python
   stop_loss_pct = (atr_value * atr_multiplier / entry_price) * 100
   ```
   Do the conversion inside the strategy, not inside the runner.

4. **Audit portfolio heat before adding a new strategy.**
   Sum `max_risk_per_trade_pct × max_concurrent_positions` across every `enabled: true` strategy. If the total crosses 5 % of equity, push back — the global daily-loss limit would trigger before most positions hit their stops.

5. **Never bypass `RiskManager.check_order()`.** Every order path in `app/execution/runner.py` must go through it. If a new order path is added (liquidation, rebalancing, bracket watchdog), wire it through the gate or explicitly document why it's bypassing — the bracket watchdog at `runner.py::_check_bracket_health` is the one accepted exception and has a dedicated `strategy_name="bracket_watchdog"`.

6. **Kill switch is multi-layer.** Three reset paths exist, in order of scope:
   - `KILL` file (sentinel) — engages on every tick
   - `RESUME` file (sentinel) — resets in-memory flag on every tick (dashboard button writes this)
   - Telegram `/stop` / `/resume`
   - `POST /api/stop` / `POST /api/resume`

## What NOT to do

- **Don't** raise `max_monthly_loss_eur` silently. The permanent −50 € stop is the entire point of the bot. Only the `TRADING_BOT_ACCEPT_MONTHLY_STOP_OVERRIDE` env var unlocks it and must be logged CRITICAL.
- **Don't** short on Alpaca paper if the strategy has `asset_class: crypto` — FR regulation restricts EU retail.
- **Don't** skip the PDT check for equities — 3 day-trades in 5 days locks the account.
- **Don't** hardcode EUR/USD. Use the `eur_per_usd` constructor arg in `RiskManager`.

## Output

For any risk question, deliver:
1. The specific limit(s) that apply (with YAML path).
2. The computed number (qty, % risk, stop distance — whichever was asked).
3. A sentence on which `RiskManager` check would fire first if things go wrong.

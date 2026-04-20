---
name: log-diagnose
description: Diagnose "why is the bot not trading?" from log files. Use when the user says "no positions taken", "bot is idle", "nothing in the last X hours", or asks to parse the trading logs.
---

# Log Diagnose

When paper-trading looks dead, the answer is almost always in the logs — not in the strategy code. This skill walks the log in a fixed order to pinpoint which of the 8 possible causes is firing.

## Recipe — follow this order every time

Run each check against `logs/bot.log` (or whatever log file the user names). Stop at the first one that matches — later causes won't trigger if an earlier cause is blocking.

### 1. Is the process alive at all?

```bash
tail -n 50 logs/bot.log
```

No entries in the last ~15 minutes during ET market hours → the process died or the scheduler is frozen. Check `systemctl status` / `docker logs`.

### 2. Kill switch engaged?

```bash
grep -Ei "(kill switch|KILL file detected)" logs/bot.log | tail -20
```

If yes, read `KillSwitch` in the DB (`sqlite3 data/trading.db "SELECT * FROM killswitch"`) for `reason`. Reset via the dashboard button, Telegram `/resume`, or `POST /api/resume`.

### 3. Market closed?

```bash
grep -c "Market closed" logs/bot.log
```

Dominant → bad timing (weekend, pre-market, post-market). Not a bug. US equities trade 9:30–16:00 ET on weekdays only.

### 4. Insufficient historical candles?

```bash
grep -E "only [0-9]+ candles" logs/bot.log | tail -10
```

Count close to zero available → Alpaca connectivity issue or new account. Fix by lowering `lookback` in the strategy YAML (`config/strategies/<name>.yaml`), e.g. 250 → 120, so the runner only needs ~2.5 days of history.

### 5. Universe resolution failure?

```bash
grep -iE "no tradable universe|universe_resolved" logs/bot.log | tail -10
```

If a strategy has no resolvable symbols, it is silently skipped. Check `app/execution/strategy_loader.py` and the `universe:` block in the YAML.

### 6. Signal generator returning None every bar?

```bash
grep -E "no signal this bar" logs/bot.log | wc -l
```

Very high count → thresholds too strict. Common culprits:
- `adx_ema_trend`: `adx_threshold` too high (25 is strict in range-bound markets; 20 is more permissive)
- `rsi_mean_reversion`: window too narrow for the symbol's volatility
- `breakout`: `lookback_bars` covering a trending-only period

Backtest with a wider grid to confirm.

### 7. Signal produced but blocked by risk?

```bash
grep "Blocked:" logs/bot.log | tail -20
```

Common blocking reasons and their fix:
- `at max positions` → raise `max_concurrent_positions` or wait for an exit
- `PDT: N day trades in 5 days` → wait or enable `overnight_hold` mode
- `order rate limit` → genuine burst; raise `max_orders_per_minute` cautiously
- `daily loss limit` → strategy paused for today, resets at 09:25 ET
- `monthly hard stop` → permanent until next month or explicit override

### 8. Orders rejected by the broker?

```bash
grep -iE "failed to submit|rejected" logs/bot.log | tail -20
```

Typical: insufficient buying power, after-hours without `extended_hours=True`, symbol not tradable, shorting restricted.

## Output

Deliver a short diagnosis (≤ 10 lines):
1. The primary cause (which numbered check fired)
2. A concrete fix (YAML line, command, or config change — not a vague suggestion)
3. Which log line supports the diagnosis (quote one representative line)

Never conclude "nothing is wrong" without walking all 8 checks. The whole point of this skill is to catch the silent failures.

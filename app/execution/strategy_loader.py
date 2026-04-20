"""Strategy configuration loader.

Parses YAML files from config/strategies/ and returns typed StrategyConfig
objects. Only enabled strategies are returned.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


@dataclass
class UniverseEntry:
    symbol: str
    asset_class: str  # "equity" | "crypto"


@dataclass
class StrategyConfig:
    """Parsed strategy YAML configuration."""

    name: str
    version: str
    enabled: bool
    mode: str                              # "paper" | "live"
    provider: str
    timeframe: str
    lookback: int
    universe: list[UniverseEntry]
    params: dict[str, Any]
    risk: dict[str, Any]
    execution: dict[str, Any] = field(default_factory=dict)
    favourable_regimes: list[str] = field(default_factory=list)
    # Empty list → gate disabled (runs in all regimes, backward-compatible default)


def _resolve_universe(raw: dict[str, Any]) -> list[UniverseEntry]:
    """Extract universe entries from a strategy YAML dict.

    Handles both simple `universe:` lists and `universe_resolution:` profiles.
    For universe_resolution, always selects the first profile whose name ends
    with `_fallback` or the equity profile (FR residents: no crypto).
    """
    # Simple universe list
    if "universe" in raw:
        return [
            UniverseEntry(
                symbol=e["symbol"],
                asset_class=e.get("asset_class", "equity"),
            )
            for e in raw["universe"]
        ]

    # universe_resolution block — pick equity/fallback profile
    res = raw.get("universe_resolution", {})
    profiles = res.get("profiles", [])
    chosen = None

    # Prefer equity profile (safe default for FR residents — no crypto)
    for profile in profiles:
        if "equity" in profile.get("name", "").lower() or "fallback" in profile.get("name", ""):
            chosen = profile
            break

    if chosen is None and profiles:
        chosen = profiles[0]   # last resort: first profile

    if chosen is None:
        log.warning("No universe profiles found — returning empty universe")
        return []

    log.info("Universe resolution: selected profile '%s'", chosen.get("name"))
    return [
        UniverseEntry(
            symbol=e["symbol"],
            asset_class=e.get("asset_class", "equity"),
        )
        for e in chosen.get("universe", [])
    ]


def load_strategy_configs(
    config_dir: str = "config/strategies",
    *,
    mode_filter: str | None = None,
) -> list[StrategyConfig]:
    """Load all strategy YAML files from *config_dir*.

    Args:
        config_dir:   Path to the directory containing strategy YAML files.
        mode_filter:  If set ("paper" or "live"), only configs with matching
                      mode are returned. Pass None to return all enabled ones.

    Returns:
        List of StrategyConfig for every enabled strategy matching the filter.
    """
    configs: list[StrategyConfig] = []
    config_path = Path(config_dir)

    if not config_path.exists():
        log.warning("Strategy config directory not found: %s", config_dir)
        return configs

    for yaml_file in sorted(config_path.glob("*.yaml")):
        try:
            raw = yaml.safe_load(yaml_file.read_text())
        except Exception:
            log.exception("Failed to parse %s — skipping", yaml_file)
            continue

        if not raw.get("enabled", True):
            log.debug("Strategy %s is disabled — skipping", yaml_file.stem)
            continue

        cfg_mode = raw.get("mode", "paper")
        if mode_filter and cfg_mode != mode_filter:
            log.debug(
                "Strategy %s mode=%s ≠ filter=%s — skipping",
                yaml_file.stem, cfg_mode, mode_filter,
            )
            continue

        universe = _resolve_universe(raw)
        if not universe:
            on_no_match = raw.get("on_no_profile_match", "disable_strategy")
            if on_no_match == "fail_boot":
                raise RuntimeError(
                    f"Strategy {raw.get('name')} has no tradable universe and "
                    "on_no_profile_match=fail_boot"
                )
            log.warning("Strategy %s has no tradable universe — skipping", raw.get("name"))
            continue

        configs.append(
            StrategyConfig(
                name=raw["name"],
                version=str(raw.get("version", "1.0.0")),
                enabled=raw.get("enabled", True),
                mode=cfg_mode,
                provider=raw.get("provider", "alpaca"),
                timeframe=raw.get("timeframe", "15m"),
                lookback=int(raw.get("lookback", 250)),
                universe=universe,
                params=raw.get("params", {}),
                risk=raw.get("risk", {}),
                execution=raw.get("execution", {}),
                favourable_regimes=[
                    str(r).lower() for r in raw.get("favourable_regimes", [])
                ],
            )
        )
        log.info(
            "Loaded strategy: %s v%s [%s] universe=%s",
            configs[-1].name,
            configs[-1].version,
            configs[-1].mode,
            [u.symbol for u in configs[-1].universe],
        )

    return configs

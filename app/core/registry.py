"""Generic plugin registry — used for both brokers and strategies.

Usage:
    from app.core.registry import broker_registry, strategy_registry

    @broker_registry.register("alpaca")
    class AlpacaProvider(BrokerProvider): ...

    @strategy_registry.register("rsi_mean_reversion")
    class RSIMeanReversion(Strategy): ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Generic, TypeVar

if TYPE_CHECKING:
    pass

T = TypeVar("T")


class Registry(Generic[T]):
    """Thread-safe plugin registry backed by a simple dict.

    Keys are string identifiers. Values are the registered classes (not instances).
    Registration happens at import time via the @register decorator.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._items: dict[str, type[T]] = {}

    def register(self, key: str) -> Callable[[type[T]], type[T]]:
        """Decorator that registers a class under the given key."""

        def decorator(cls: type[T]) -> type[T]:
            if key in self._items:
                raise ValueError(
                    f"{self.name}: '{key}' is already registered. "
                    f"Registered keys: {list(self._items)}"
                )
            self._items[key] = cls
            return cls

        return decorator

    def get(self, key: str) -> type[T]:
        """Return the class registered under key, or raise KeyError."""
        if key not in self._items:
            raise KeyError(
                f"{self.name}: '{key}' not found. "
                f"Available: {list(self._items)}"
            )
        return self._items[key]

    def all(self) -> dict[str, type[T]]:
        """Return a copy of all registered items."""
        return dict(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        return key in self._items


# Singleton registries — imported and used throughout the codebase.
# Type annotations are strings to avoid circular imports.
broker_registry: Registry[object] = Registry("broker")
strategy_registry: Registry[object] = Registry("strategy")

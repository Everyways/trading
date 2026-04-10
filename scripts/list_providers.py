#!/usr/bin/env python3
"""List all registered broker providers.

Usage:
    python scripts/list_providers.py
"""

import app.providers  # noqa: F401 — triggers all provider registrations
from app.core.registry import broker_registry


def main() -> None:
    providers = broker_registry.all()
    if not providers:
        print("No providers registered.")
        return
    print(f"Registered broker providers ({len(providers)}):")
    for name, cls in sorted(providers.items()):
        print(f"  {name:20s}  {cls.__module__}.{cls.__qualname__}")


if __name__ == "__main__":
    main()

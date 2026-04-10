"""Provider package — import all broker providers here to trigger registration.

When adding a new provider, add its import below. The @broker_registry.register
decorator runs at import time.
"""

# Providers are imported lazily to avoid errors when dependencies are missing
# in test environments. Add new providers here.
from app.providers.alpaca import provider as _alpaca  # noqa: F401

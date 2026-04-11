"""Strategy package — import all strategies here to trigger registration.

When adding a new strategy, add its import below.
"""

from app.strategies.adx_ema_trend import ADXEMATrend  # noqa: F401
from app.strategies.bollinger_bands import BollingerBandsMR  # noqa: F401
from app.strategies.breakout import Breakout  # noqa: F401
from app.strategies.ma_crossover import MACrossover  # noqa: F401
from app.strategies.macd_crossover import MACDCrossover  # noqa: F401
from app.strategies.rsi_mean_reversion import RSIMeanReversion  # noqa: F401

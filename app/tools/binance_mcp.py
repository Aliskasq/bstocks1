"""Market data utilities — mock candles for development.

Since baw CLI doesn't provide market data endpoints, we use deterministic
synthetic candles for development. In production, replace with public
Binance REST API or websocket feed.
"""
from __future__ import annotations

import math
import random
import time
from typing import Any


# Dev-only clock: advancing this makes mock series evolve so monitor
# conditions (pullbacks, breakouts) can actually fire during a demo run.
_MOCK_TICK = 0


def advance_mock_clock(steps: int = 1) -> int:
    global _MOCK_TICK
    _MOCK_TICK += steps
    return _MOCK_TICK


def mock_clock() -> int:
    return _MOCK_TICK


def _mock_candles(symbol: str, n: int = 120) -> list[dict]:
    """Deterministic-ish synthetic series for offline development."""
    rnd = random.Random(hash(symbol) & 0xFFFF)
    price = 100 + (hash(symbol) % 120)
    out = []
    # Generate extra bars so the mock clock can slide a window forward.
    total = n + _MOCK_TICK
    for i in range(total):
        drift = math.sin(i / 14) * 0.6 + rnd.uniform(-0.7, 0.8)
        price = max(1.0, price * (1 + drift / 100))
        high = price * (1 + abs(rnd.uniform(0, 0.006)))
        low = price * (1 - abs(rnd.uniform(0, 0.006)))
        vol = rnd.uniform(8e5, 1.6e6) * (3.2 if i == n - 1 else 1.0)
        out.append({
            "open": round(price * (1 - rnd.uniform(0, 0.003)), 4),
            "high": round(high, 4),
            "low": round(low, 4),
            "close": round(price, 4),
            "volume": round(vol, 2),
            "ts": int(time.time()) - (total - i) * 3600,
        })
    # Slide the window: later ticks reveal later bars (price can pull back).
    return out[_MOCK_TICK:_MOCK_TICK + n]


def get_mock_market_data(symbol: str, interval: str = "1h", limit: int = 120) -> dict:
    """Return normalized mock candles for a bStock symbol."""
    candles = _mock_candles(symbol, limit)
    return {
        "symbol": symbol,
        "interval": interval,
        "source": "mock (baw doesn't provide market data)",
        "candles": candles,
        "last_price": candles[-1]["close"],
    }
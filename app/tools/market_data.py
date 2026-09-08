"""
Real Binance market data via public REST API.
No auth required for public endpoints.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
import httpx

BINANCE_REST = "https://api.binance.com/api/v3"

# Map baw format (NVDAB-USDT) -> Binance format (NVDABUSDT)
def baw_to_binance(symbol: str) -> str:
    """Convert baw symbol format to Binance symbol format."""
    # Auto-append -USDT if only base symbol given (e.g., "NVDAB" -> "NVDAB-USDT")
    if not symbol.endswith("BUSDT") and not symbol.endswith("-USDT"):
        symbol = symbol + "-USDT"
    return symbol.replace("-", "").replace("BUSDT", "BUSDT")  # NVDAB-USDT -> NVDABUSDT

def binance_to_baw(symbol: str) -> str:
    """Convert Binance symbol format to baw symbol format."""
    # NVDABUSDT -> NVDAB-USDT
    if symbol.endswith("BUSDT"):
        return symbol[:-5] + "-BUSDT"
    return symbol


class BinanceMarketData:
    """Async client for Binance public REST API."""

    def __init__(self, timeout: float = 10.0):
        self.client = httpx.AsyncClient(timeout=timeout, base_url=BINANCE_REST)
        self._cache: dict[str, tuple[float, list[dict]]] = {}  # symbol -> (ts, candles)
        self._cache_ttl = 30.0  # seconds

    async def close(self):
        await self.client.aclose()

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 120,
    ) -> list[dict]:
        """Fetch klines from Binance and normalize to internal candle format."""
        bn_symbol = baw_to_binance(symbol)
        cache_key = f"{bn_symbol}:{interval}:{limit}"
        now = time.time()

        # Check cache
        if cache_key in self._cache:
            cached_ts, cached_data = self._cache[cache_key]
            if now - cached_ts < self._cache_ttl:
                return cached_data

        try:
            resp = await self.client.get(
                "/klines",
                params={"symbol": bn_symbol, "interval": interval, "limit": limit},
            )
            resp.raise_for_status()
            raw = resp.json()

            candles = []
            for k in raw:
                candles.append({
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "ts": int(k[0] / 1000),  # ms -> seconds
                })

            self._cache[cache_key] = (now, candles)
            return candles

        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Binance API error {e.response.status_code}: {e.response.text}")
        except Exception as e:
            raise RuntimeError(f"Failed to fetch market data: {e}")

    async def get_ticker_24h(self, symbol: str) -> dict:
        """Get 24h ticker stats."""
        bn_symbol = baw_to_binance(symbol)
        resp = await self.client.get("/ticker/24hr", params={"symbol": bn_symbol})
        resp.raise_for_status()
        return resp.json()

    async def get_exchange_info(self) -> dict:
        """Get exchange info (symbols, filters, etc.)."""
        resp = await self.client.get("/exchangeInfo")
        resp.raise_for_status()
        return resp.json()

    def invalidate_cache(self, symbol: str | None = None):
        """Invalidate cache for a symbol or all."""
        if symbol:
            prefix = f"{baw_to_binance(symbol)}:"
            keys = [k for k in self._cache if k.startswith(prefix)]
            for k in keys:
                del self._cache[k]
        else:
            self._cache.clear()


# Singleton instance
_market_data: BinanceMarketData | None = None


def get_market_data() -> BinanceMarketData:
    global _market_data
    if _market_data is None:
        _market_data = BinanceMarketData()
    return _market_data


# Sync wrapper for non-async contexts
import functools

def sync_get_klines(symbol: str, interval: str = "1h", limit: int = 120) -> list[dict]:
    """Synchronous wrapper for simple scripts."""
    import httpx as sync_httpx
    bn_symbol = baw_to_binance(symbol)
    resp = sync_httpx.get(f"{BINANCE_REST}/klines",
                          params={"symbol": bn_symbol, "interval": interval, "limit": limit},
                          timeout=10.0)
    resp.raise_for_status()
    raw = resp.json()
    return [{
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "volume": float(k[5]),
        "ts": int(k[0] / 1000),
    } for k in raw]
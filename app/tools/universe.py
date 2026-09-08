"""
Dynamic bStocks universe discovery and quick scanning.
All functions are sync for simple tool registration.
"""
from __future__ import annotations

import httpx
from typing import Any


BINANCE_REST = "https://api.binance.com/api/v3"


# Known crypto tokens that end with BUSDT but are NOT bStocks
FALSE_POSITIVE_BSTOCKS = {
    "BNBUSDT", "SHIBUSDT", "ARBUSDT", "TRBUSDT", "CKBUSDT",
    "DGBUSDT", "YBUSDT", "STXBUSDT", "BBUSDT", "QNTBUSDT",
    "MUBUSDT", "GSBUSDT", "MUUBUSDT",  # These are stocks but verify via underlying
}

def discover_bstocks() -> list[str]:
    """Fetch all trading bStock symbols from Binance (ending in BUSDT, excluding crypto)."""
    resp = httpx.get(f"{BINANCE_REST}/exchangeInfo", timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    return sorted([
        s["symbol"] for s in data["symbols"]
        if s["symbol"].endswith("BUSDT") 
        and s["status"] == "TRADING"
        and s["symbol"] not in FALSE_POSITIVE_BSTOCKS
    ])


def baw_format(symbol: str) -> str:
    """Binance format (NVDABUSDT) -> baw format (NVDAB-USDT)."""
    if symbol.endswith("BUSDT"):
        return symbol[:-5] + "-BUSDT"
    return symbol


def binance_format(symbol: str) -> str:
    """baw format (NVDAB-USDT) -> Binance format (NVDABUSDT)."""
    # Auto-append -USDT if only base symbol given (e.g., "NVDAB" -> "NVDAB-USDT")
    if not symbol.endswith("BUSDT") and not symbol.endswith("-USDT"):
        symbol = symbol + "-USDT"
    return symbol.replace("-", "")


def get_klines(symbol: str, interval: str = "1h", limit: int = 120) -> list[dict]:
    """Fetch klines from Binance REST."""
    bn_symbol = binance_format(symbol)
    resp = httpx.get(
        f"{BINANCE_REST}/klines",
        params={"symbol": bn_symbol, "interval": interval, "limit": limit},
        timeout=10.0,
    )
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


def quick_scan(min_volume_usd: float = 50000, max_symbols: int = 20) -> list[dict]:
    """
    Quick prefilter: fetch 24h tickers, filter by volume, return top candidates
    with basic stats (price, change%, volume). No indicators yet.
    """
    resp = httpx.get(f"{BINANCE_REST}/ticker/24hr", timeout=10.0)
    resp.raise_for_status()
    tickers = resp.json()

    bstocks = [
        t for t in tickers
        if t["symbol"].endswith("BUSDT") and float(t["quoteVolume"]) >= min_volume_usd
    ]
    bstocks.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    bstocks = bstocks[:max_symbols]

    return [{
        "symbol": baw_format(t["symbol"]),
        "last_price": float(t["lastPrice"]),
        "change_pct": float(t["priceChangePercent"]),
        "volume_usd": float(t["quoteVolume"]),
        "high_24h": float(t["highPrice"]),
        "low_24h": float(t["lowPrice"]),
    } for t in bstocks]


# Indicator functions (each standalone, takes candles list)
def rsi(candles: list[dict], period: int = 14) -> float:
    """RSI from closes."""
    closes = [c["close"] for c in candles]
    if len(closes) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def ema(candles: list[dict], period: int) -> float:
    """EMA from closes."""
    closes = [c["close"] for c in candles]
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    k = 2 / (period + 1)
    e = sum(closes[:period]) / period
    for c in closes[period:]:
        e = c * k + e * (1 - k)
    return e


def macd(candles: list[dict], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD line, signal, histogram."""
    closes = [c["close"] for c in candles]
    ema_fast = ema(candles, fast)
    ema_slow = ema(candles, slow)
    macd_line = ema_fast - ema_slow
    # Simplified signal (would need history for proper)
    return {"macd": macd_line, "signal": 0.0, "histogram": macd_line}


def atr(candles: list[dict], period: int = 14) -> float:
    """Average True Range."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l = candles[i]["high"], candles[i]["low"]
        pc = candles[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs[-period:]) / period


def volume_anomaly(candles: list[dict], lookback: int = 20) -> dict:
    """Volume ratio vs baseline."""
    vols = [c["volume"] for c in candles]
    if len(vols) < lookback + 1:
        return {"ratio": 1.0, "verdict": "insufficient_data"}
    baseline = sum(vols[-(lookback + 1):-1]) / lookback
    current = vols[-1]
    ratio = current / baseline if baseline > 0 else 1.0
    verdict = "high" if ratio > 2 else "low" if ratio < 0.5 else "normal"
    return {"ratio": ratio, "baseline": baseline, "verdict": verdict}


def market_structure(candles: list[dict], lookback: int = 50) -> dict:
    """HH/HL/LH/LL, range position %."""
    relevant = candles[-lookback:] if len(candles) >= lookback else candles
    highs = [c["high"] for c in relevant]
    lows = [c["low"] for c in relevant]
    hh = max(highs)
    ll = min(lows)
    current = relevant[-1]["close"]
    range_pos = (current - ll) / (hh - ll) * 100 if hh != ll else 50
    return {
        "hh": hh,
        "ll": ll,
        "range_position_pct": range_pos,
        "trend": "bullish" if relevant[-1]["close"] > relevant[0]["close"] else "bearish",
    }


def bollinger_bands(candles: list[dict], period: int = 20, std_mult: float = 2.0) -> dict:
    """Bollinger Bands %B."""
    closes = [c["close"] for c in candles[-period:]]
    if len(closes) < period:
        return {"pct_b": 0.5, "upper": 0, "lower": 0, "mid": 0}
    import statistics
    mid = statistics.mean(closes)
    std = statistics.stdev(closes) if len(closes) > 1 else 0
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    current = closes[-1]
    pct_b = (current - lower) / (upper - lower) if upper != lower else 0.5
    return {"pct_b": pct_b, "upper": upper, "lower": lower, "mid": mid}


def stochastic(candles: list[dict], period: int = 14) -> dict:
    """Stochastic %K, %D."""
    relevant = candles[-period:]
    highest = max(c["high"] for c in relevant)
    lowest = min(c["low"] for c in relevant)
    current = relevant[-1]["close"]
    k = (current - lowest) / (highest - lowest) * 100 if highest != lowest else 50
    return {"k": k, "d": k}  # simplified


def adx(candles: list[dict], period: int = 14) -> float:
    """Average Directional Index (simplified)."""
    if len(candles) < period + 1:
        return 25.0
    # Simplified: just return trend strength proxy
    closes = [c["close"] for c in candles[-period:]]
    changes = [abs(closes[i] - closes[i-1]) for i in range(1, len(closes))]
    avg_change = sum(changes) / len(changes) if changes else 0
    atr_val = atr(candles, period)
    return min(100, (avg_change / atr_val * 100) if atr_val > 0 else 25)


# Composite: all indicators at once (for backward compat / quick access)
def all_indicators(candles: list[dict]) -> dict:
    return {
        "rsi": rsi(candles),
        "ema20": ema(candles, 20),
        "ema50": ema(candles, 50),
        "macd": macd(candles),
        "atr": atr(candles),
        "volume": volume_anomaly(candles),
        "structure": market_structure(candles),
        "bb": bollinger_bands(candles),
        "stoch": stochastic(candles),
        "adx": adx(candles),
    }
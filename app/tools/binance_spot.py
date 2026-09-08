#!/usr/bin/env python3
"""
Binance Spot Trading Client — for bStocks trading on Binance CEX.
Uses REST API with HMAC-SHA256 signatures.
"""
import asyncio
import hashlib
import hmac
import time
import json
import shutil
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from urllib.parse import urlencode

import aiohttp

from ..config import BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_BASE_URL


@dataclass
class BinanceOrderResult:
    success: bool
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    status: str = "UNKNOWN"
    symbol: Optional[str] = None
    side: Optional[str] = None
    type: Optional[str] = None
    orig_qty: float = 0.0
    executed_qty: float = 0.0
    cummulative_quote_qty: float = 0.0
    avg_price: float = 0.0
    fees_usd: float = 0.0
    error: Optional[str] = None
    raw_response: Optional[Dict] = None


@dataclass
class BinanceAccountInfo:
    equity: float
    available_balance: float
    positions: List[Dict]


class BinanceSpotClient:
    """
    Async Binance Spot REST API client.
    Supports market/limit orders, account info, positions.
    """
    
    def __init__(self, api_key: str = "", api_secret: str = "", base_url: str = BINANCE_BASE_URL, timeout: int = 30):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None
        self._has_creds = bool(api_key and api_secret)
    
    @property
    def available(self) -> bool:
        return self._has_creds
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"X-MBX-APIKEY": self.api_key} if self._has_creds else {}
            )
        return self._session
    
    def _sign_params(self, params: Dict) -> str:
        """Create HMAC-SHA256 signature for Binance API."""
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return f"{query_string}&signature={signature}"
    
    async def _request(self, method: str, endpoint: str, params: Dict = None, signed: bool = False) -> Dict:
        """Make HTTP request to Binance API."""
        if not self._has_creds and signed:
            return {"error": "API credentials not configured"}
        
        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"
        
        if params is None:
            params = {}
        
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            query_string = self._sign_params(params)
            if method == "GET":
                url = f"{url}?{query_string}"
                headers = {}
            else:
                headers = {"Content-Type": "application/x-www-form-urlencoded"}
        else:
            query_string = urlencode(params)
            if method == "GET" and query_string:
                url = f"{url}?{query_string}"
            headers = {}
        
        try:
            if method == "GET":
                async with session.get(url, headers=headers) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        return {"error": data, "status": resp.status}
                    return data
            elif method == "POST":
                async with session.post(url, data=query_string if signed else params, headers=headers) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        return {"error": data, "status": resp.status}
                    return data
            elif method == "DELETE":
                async with session.delete(url, data=query_string if signed else params, headers=headers) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        return {"error": data, "status": resp.status}
                    return data
        except asyncio.TimeoutError:
            return {"error": f"Request timed out after {self.timeout}s"}
        except Exception as e:
            return {"error": f"Request failed: {e}"}
    
    # ==================== PUBLIC MARKET DATA ====================
    
    async def get_exchange_info(self) -> Dict:
        """Get exchange trading rules and symbol info."""
        return await self._request("GET", "/api/v3/exchangeInfo")
    
    async def get_ticker_24h(self, symbol: str = None) -> Dict:
        """Get 24hr ticker price change statistics."""
        params = {"symbol": symbol} if symbol else {}
        return await self._request("GET", "/api/v3/ticker/24hr", params)
    
    async def get_klines(self, symbol: str, interval: str = "1h", limit: int = 120) -> List[Dict]:
        """Get kline/candlestick data."""
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        data = await self._request("GET", "/api/v3/klines", params)
        if "error" in data:
            return []
        return [
            {
                "open_time": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "close_time": k[6],
                "quote_volume": float(k[7]),
                "trades": int(k[8]),
            }
            for k in data
        ]
    
    # ==================== ACCOUNT (SIGNED) ====================
    
    async def get_account(self) -> Optional[BinanceAccountInfo]:
        """Get account information (balances, permissions)."""
        result = await self._request("GET", "/api/v3/account", signed=True)
        
        if "error" in result:
            return None
        
        balances = {}
        for b in result.get("balances", []):
            free = float(b["free"])
            locked = float(b["locked"])
            if free > 0 or locked > 0:
                balances[b["asset"]] = {"free": free, "locked": locked, "total": free + locked}
        
        # Calculate USDT equity (approximate - would need prices for all assets)
        usdt_balance = balances.get("USDT", {}).get("total", 0)
        
        return BinanceAccountInfo(
            equity=usdt_balance,  # Simplified - only USDT
            available_balance=balances.get("USDT", {}).get("free", 0),
            positions=[],  # Spot positions = non-USDT balances
        )
    
    async def get_open_orders(self, symbol: str = None) -> List[Dict]:
        """Get all open orders."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        result = await self._request("GET", "/api/v3/openOrders", params, signed=True)
        if "error" in result:
            return []
        return result if isinstance(result, list) else []
    
    # ==================== TRADING (SIGNED) ====================
    
    async def place_order(
        self,
        symbol: str,
        side: str,  # BUY | SELL
        order_type: str,  # MARKET | LIMIT
        quantity: float = None,  # base asset quantity
        quote_order_qty: float = None,  # quote asset quantity (for MARKET BUY)
        price: float = None,  # for LIMIT
        time_in_force: str = "GTC",
        new_client_order_id: str = None,
    ) -> BinanceOrderResult:
        """Place a new order."""
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "timeInForce": time_in_force,
        }
        
        if new_client_order_id:
            params["newClientOrderId"] = new_client_order_id
        
        if order_type == "MARKET":
            if side == "BUY" and quote_order_qty:
                params["quoteOrderQty"] = f"{quote_order_qty:.2f}"
            elif quantity:
                params["quantity"] = f"{quantity:.8f}"
            else:
                return BinanceOrderResult(success=False, error="MARKET order needs quantity or quoteOrderQty")
        else:  # LIMIT
            if not quantity or not price:
                return BinanceOrderResult(success=False, error="LIMIT order needs quantity and price")
            params["quantity"] = f"{quantity:.8f}"
            params["price"] = f"{price:.8f}"
        
        result = await self._request("POST", "/api/v3/order", params, signed=True)
        return self._parse_order_result(result)
    
    async def cancel_order(self, symbol: str, order_id: str = None, client_order_id: str = None) -> Dict:
        """Cancel an active order."""
        params = {"symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        return await self._request("DELETE", "/api/v3/order", params, signed=True)
    
    async def get_order(self, symbol: str, order_id: str = None, client_order_id: str = None) -> Optional[BinanceOrderResult]:
        """Query order status."""
        params = {"symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        result = await self._request("GET", "/api/v3/order", params, signed=True)
        if "error" in result:
            return None
        return self._parse_order_result(result)
    
    # ==================== HELPERS ====================
    
    def _parse_order_result(self, result: Dict) -> BinanceOrderResult:
        if "error" in result:
            return BinanceOrderResult(success=False, error=str(result["error"]), raw_response=result)
        
        status = result.get("status", "UNKNOWN")
        filled = float(result.get("executedQty", 0))
        quote_filled = float(result.get("cummulativeQuoteQty", 0))
        avg_price = quote_filled / filled if filled > 0 else 0
        
        return BinanceOrderResult(
            success=status in ("FILLED", "PARTIALLY_FILLED", "NEW"),
            order_id=str(result.get("orderId")) if result.get("orderId") else None,
            client_order_id=result.get("clientOrderId"),
            status=status,
            symbol=result.get("symbol"),
            side=result.get("side"),
            type=result.get("type"),
            orig_qty=float(result.get("origQty", 0)),
            executed_qty=filled,
            cummulative_quote_qty=quote_filled,
            avg_price=avg_price,
            fees_usd=0.0,  # Would need to fetch from trade history
            raw_response=result,
        )
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ==================== SYNC WRAPPER ====================

import requests

class SyncBinanceSpotClient:
    """Synchronous version for simple scripts."""
    
    def __init__(self, api_key: str = "", api_secret: str = "", base_url: str = BINANCE_BASE_URL):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self._has_creds = bool(api_key and api_secret)
    
    def _sign_params(self, params: Dict) -> str:
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return f"{query_string}&signature={signature}"
    
    def _request(self, method: str, endpoint: str, params: Dict = None, signed: bool = False) -> Dict:
        if params is None:
            params = {}
        
        url = f"{self.base_url}{endpoint}"
        headers = {"X-MBX-APIKEY": self.api_key} if self._has_creds else {}
        
        if signed:
            if not self._has_creds:
                return {"error": "API credentials not configured"}
            params["timestamp"] = int(time.time() * 1000)
            query_string = self._sign_params(params)
            if method == "GET":
                url = f"{url}?{query_string}"
            else:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            query_string = urlencode(params)
            if method == "GET" and query_string:
                url = f"{url}?{query_string}"
        
        try:
            if method == "GET":
                resp = requests.get(url, headers=headers, timeout=30)
            elif method == "POST":
                resp = requests.post(url, data=query_string if signed else params, headers=headers, timeout=30)
            elif method == "DELETE":
                resp = requests.delete(url, data=query_string if signed else params, headers=headers, timeout=30)
            else:
                return {"error": f"Unsupported method: {method}"}
            
            data = resp.json()
            if resp.status_code != 200:
                return {"error": data, "status": resp.status_code}
            return data
        except Exception as e:
            return {"error": f"Request failed: {e}"}
    
    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 120) -> List[Dict]:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        data = self._request("GET", "/api/v3/klines", params)
        if "error" in data:
            return []
        return [
            {
                "open_time": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "close_time": k[6],
                "quote_volume": float(k[7]),
                "trades": int(k[8]),
            }
            for k in data
        ]
    
    def get_account(self) -> Optional[Dict]:
        result = self._request("GET", "/api/v3/account", signed=True)
        if "error" in result:
            return None
        return result
    
    def place_market_buy(self, symbol: str, quote_qty: float) -> BinanceOrderResult:
        params = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": f"{quote_qty:.2f}",
            "timestamp": int(time.time() * 1000),
        }
        query_string = self._sign_params(params)
        headers = {"X-MBX-APIKEY": self.api_key, "Content-Type": "application/x-www-form-urlencoded"}
        resp = requests.post(f"{self.base_url}/api/v3/order", data=query_string, headers=headers, timeout=30)
        data = resp.json()
        if resp.status_code != 200:
            return BinanceOrderResult(success=False, error=str(data), raw_response=data)
        return self._parse_order_result(data)
    
    def _parse_order_result(self, result: Dict) -> BinanceOrderResult:
        if "error" in result:
            return BinanceOrderResult(success=False, error=str(result["error"]), raw_response=result)
        
        status = result.get("status", "UNKNOWN")
        filled = float(result.get("executedQty", 0))
        quote_filled = float(result.get("cummulativeQuoteQty", 0))
        avg_price = quote_filled / filled if filled > 0 else 0
        
        return BinanceOrderResult(
            success=status in ("FILLED", "PARTIALLY_FILLED", "NEW"),
            order_id=str(result.get("orderId")) if result.get("orderId") else None,
            client_order_id=result.get("clientOrderId"),
            status=status,
            symbol=result.get("symbol"),
            side=result.get("side"),
            type=result.get("type"),
            orig_qty=float(result.get("origQty", 0)),
            executed_qty=filled,
            cummulative_quote_qty=quote_filled,
            avg_price=avg_price,
            fees_usd=0.0,
            raw_response=result,
        )


# ==================== CLI TEST ====================

if __name__ == "__main__":
    import sys
    
    def test():
        client = SyncBinanceSpotClient(
            api_key=BINANCE_API_KEY,
            api_secret=BINANCE_API_SECRET,
        )
        print("Testing Binance Spot API...")
        print(f"Has credentials: {client._has_creds}")
        
        # Test public endpoint
        klines = client.get_klines("NVDABUSDT", "1h", 10)
        print(f"Klines fetched: {len(klines)}")
        if klines:
            print(f"  Last close: ${klines[-1]['close']:.4f}")
        
        if client._has_creds:
            account = client.get_account()
            print(f"Account: {account}")
        else:
            print("No API credentials - skipping private endpoints")
    
    test()

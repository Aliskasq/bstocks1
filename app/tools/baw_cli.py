#!/usr/bin/env python3
"""
baw CLI Wrapper — Binance Agentic Wallet integration via subprocess.
Works with baw CLI v1.9.0+ for DEX swaps on BSC (PancakeSwap) for bStocks.

Usage:
    from app.tools.baw_cli import BawClient
    client = BawClient()
    await client.check_auth()
    quote = await client.get_quote("NVDAB", 50.0)  # $50 USDT quote for NVDAB
    result = await client.swap("NVDAB", 50.0)      # $50 market swap
"""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime


@dataclass
class BawAuthStatus:
    authenticated: bool
    address: Optional[str] = None
    chain_id: Optional[int] = None
    expires_at: Optional[int] = None
    error: Optional[str] = None


@dataclass
class BawQuoteResult:
    success: bool
    from_token: str
    to_token: str
    from_qty: float
    to_qty: float
    price_usd: float
    price_impact_pct: float
    error: Optional[str] = None
    raw_response: Optional[Dict] = None


@dataclass
class BawSwapResult:
    success: bool
    tx_hash: Optional[str] = None
    order_id: Optional[str] = None
    status: str = "UNKNOWN"
    from_qty: float = 0.0
    to_qty: float = 0.0
    fees_usd: float = 0.0
    error: Optional[str] = None
    raw_response: Optional[Dict] = None


@dataclass
class BawPosition:
    symbol: str
    side: str  # LONG | SHORT (for spot, always LONG)
    size: float
    entry_price: float
    current_price: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    position_usd: float


@dataclass
class BawAccountInfo:
    equity: float
    available_balance: float
    unrealized_pnl: float
    positions: List[BawPosition]


# bStock token addresses on BSC (chain 56)
BSTOCKS_TOKENS = {
    "NVDAB": "0x02Fca66C1D1aFB4E2A7884261eB00F63598a7436",
    "TSLAB": "0x5b1910eAaD6450E50f816082Aa078C41F10C292f",
    # Add more as discovered
}

# USDT on BSC
USDT_BSC = "0x55d398326f99059fF775485246999027B3197955"

BSC_CHAIN_ID = 56


class BawClient:
    """
    Async wrapper around baw CLI binary.
    All calls are subprocess invocations with JSON I/O.
    """
    
    def __init__(self, baw_path: str = "baw", timeout: int = 60):
        self.baw_path = baw_path
        self.timeout = timeout
        self._available = shutil.which(self.baw_path) is not None
        if self._available:
            self.baw_path = shutil.which(self.baw_path)
    
    @property
    def available(self) -> bool:
        return self._available
    
    async def _run_cmd(self, args: List[str]) -> Dict:
        """Run baw command and parse JSON output."""
        if not self._available:
            return {"error": "baw binary not available. Install baw CLI for live trading."}
        
        cmd = [self.baw_path] + args
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
            
            if proc.returncode != 0:
                err_msg = stderr.decode().strip() if stderr else "Unknown error"
                return {"error": err_msg, "returncode": proc.returncode}
            
            output = stdout.decode().strip()
            if not output:
                return {"error": "Empty output"}
            
            return json.loads(output)
            
        except asyncio.TimeoutError:
            return {"error": f"Command timed out after {self.timeout}s"}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON: {e}", "raw": output[:500] if 'output' in locals() else ""}
        except Exception as e:
            return {"error": f"Execution failed: {e}"}
    
    # ==================== AUTH ====================
    
    async def check_auth(self) -> BawAuthStatus:
        """Check if baw has valid authenticated session."""
        result = await self._run_cmd(["wallet", "status", "--json"])
        
        if "error" in result:
            return BawAuthStatus(authenticated=False, error=result["error"])
        
        # baw returns: {"success": true, "data": {"status": "CONNECTED"}}
        authenticated = result.get("success", False) and result.get("data", {}).get("status") == "CONNECTED"
        
        # Get primary address (EVM/BSC)
        addr_result = await self._run_cmd(["wallet", "address", "--json"])
        address = None
        if "success" in addr_result:
            for a in addr_result.get("data", {}).get("addresses", []):
                if a.get("binanceChainId") in ("56", "1", "137", "42161", "4663", "8453"):
                    address = a.get("address")
                    break
        
        return BawAuthStatus(
            authenticated=authenticated,
            address=address,
            chain_id=BSC_CHAIN_ID,
        )
    
    # ==================== DEX SWAPS (bStocks on BSC) ====================
    
    def _resolve_token(self, symbol: str) -> Optional[str]:
        """Resolve bStock symbol to token address."""
        return BSTOCKS_TOKENS.get(symbol.upper())
    
    async def get_quote(self, symbol: str, usdt_amount: float) -> BawQuoteResult:
        """Get swap quote: USDT -> bStock."""
        from_token = USDT_BSC
        to_token = self._resolve_token(symbol)
        
        if not to_token:
            return BawQuoteResult(
                success=False, error=f"Unknown bStock symbol: {symbol}",
                from_token="", to_token="", from_qty=0, to_qty=0, price_usd=0, price_impact_pct=0
            )
        
        args = ["market-order", "quote", "--json"]
        args.extend(["--binanceChainId", str(BSC_CHAIN_ID)])
        args.extend(["--fromToken", from_token])
        args.extend(["--toToken", to_token])
        args.extend(["--fromTokenQty", str(usdt_amount)])
        
        result = await self._run_cmd(args)
        
        if "error" in result:
            return BawQuoteResult(
                success=False, error=result["error"],
                from_token=from_token, to_token=to_token,
                from_qty=usdt_amount, to_qty=0, price_usd=0, price_impact_pct=0,
                raw_response=result
            )
        
        data = result.get("data", result)
        # baw quote response structure
        to_qty = float(data.get("toTokenQty", data.get("toQty", 0)))
        price_impact = float(data.get("priceImpact", data.get("priceImpactPct", 0)))
        price_usd = usdt_amount / to_qty if to_qty > 0 else 0
        
        return BawQuoteResult(
            success=True,
            from_token=from_token,
            to_token=to_token,
            from_qty=usdt_amount,
            to_qty=to_qty,
            price_usd=price_usd,
            price_impact_pct=price_impact,
            raw_response=result,
        )
    
    async def swap(self, symbol: str, usdt_amount: float, slippage: float = 1.0) -> BawSwapResult:
        """Execute market swap: USDT -> bStock."""
        from_token = USDT_BSC
        to_token = self._resolve_token(symbol)
        
        if not to_token:
            return BawSwapResult(
                success=False, error=f"Unknown bStock symbol: {symbol}",
                from_qty=usdt_amount, to_qty=0
            )
        
        args = ["market-order", "swap", "--json"]
        args.extend(["--binanceChainId", str(BSC_CHAIN_ID)])
        args.extend(["--fromToken", from_token])
        args.extend(["--toToken", to_token])
        args.extend(["--fromTokenQty", str(usdt_amount)])
        args.extend(["--slippage", str(slippage)])
        
        result = await self._run_cmd(args)
        
        if "error" in result:
            return BawSwapResult(
                success=False, error=result["error"],
                from_qty=usdt_amount, to_qty=0,
                raw_response=result
            )
        
        data = result.get("data", result)
        tx_hash = data.get("txHash", data.get("transactionHash"))
        order_id = data.get("orderId", data.get("order_id"))
        to_qty = float(data.get("toTokenQty", data.get("toQty", 0)))
        status = data.get("status", "PENDING")
        fees = float(data.get("fees", data.get("gasFee", 0)))
        
        return BawSwapResult(
            success=status in ("SUCCESS", "FILLED", "PARTIAL"),
            tx_hash=tx_hash,
            order_id=order_id,
            status=status,
            from_qty=usdt_amount,
            to_qty=to_qty,
            fees_usd=fees,
            raw_response=result,
        )
    
    # ==================== ACCOUNT / POSITIONS ====================
    
    async def get_balances(self) -> Dict[str, float]:
        """Get token balances (only non-zero)."""
        result = await self._run_cmd(["wallet", "balance", "--json"])
        
        if "error" in result:
            return {}
        
        balances = {}
        data = result.get("data", result)
        for item in data.get("balances", []):
            token = item.get("token", {})
            symbol = token.get("symbol", "UNKNOWN")
            amount = float(item.get("amount", 0))
            if amount > 0:
                balances[symbol] = amount
        
        return balances
    
    async def get_equity(self) -> float:
        """Get total USDT balance (approximate equity)."""
        balances = await self.get_balances()
        return balances.get("USDT", 0.0)
    
    # ==================== HELPERS ====================
    
    async def get_bstock_balances(self) -> Dict[str, float]:
        """Get balances of known bStocks."""
        balances = await self.get_balances()
        bstock_balances = {}
        for symbol, address in BSTOCKS_TOKENS.items():
            # Balance response uses token symbols, need to match
            # For now return raw balances
            pass
        return balances


# ==================== SYNC WRAPPERS (for non-async contexts) ====================

def run_baw_sync(args: List[str], baw_path: str = "baw", timeout: int = 60) -> Dict:
    """Synchronous version for simple scripts."""
    resolved = shutil.which(baw_path)
    if not resolved:
        return {"error": "baw binary not found"}
    
    cmd = [resolved] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            return {"error": proc.stderr.strip(), "returncode": proc.returncode}
        if not proc.stdout.strip():
            return {"error": "Empty output"}
        return json.loads(proc.stdout.strip())
    except subprocess.TimeoutExpired:
        return {"error": f"Timed out after {timeout}s"}
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}", "raw": proc.stdout[:500]}


# ==================== CLI TEST ====================

if __name__ == "__main__":
    import sys
    
    async def test():
        client = BawClient()
        print("Testing baw CLI...")
        
        # Check auth
        auth = await client.check_auth()
        print(f"Auth: {auth}")
        
        if auth.authenticated:
            # Get balances
            balances = await client.get_balances()
            print(f"Balances: {balances}")
            
            # Test quote for NVDAB
            quote = await client.get_quote("NVDAB", 50.0)
            print(f"Quote: {quote}")
        else:
            print("Not authenticated. Run 'baw auth signin' first.")
    
    asyncio.run(test())
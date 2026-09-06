#!/usr/bin/env python3
"""
baw CLI Wrapper — Binance Agentic Wallet integration via subprocess.
Replaces MCP for spot trading operations (bStocks).

Usage:
    from app.tools.baw_cli import BawClient
    client = BawClient()
    await client.check_auth()
    result = await client.place_spot_order("BUY", "NVDAB-USDT", 50.0)  # $50 market buy
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
class BawOrderResult:
    success: bool
    tx_hash: Optional[str] = None
    order_id: Optional[str] = None
    status: str = "UNKNOWN"  # SUCCESS, PARTIAL, REJECTED, PENDING, FILLED
    filled_qty: float = 0.0
    avg_price: float = 0.0
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
        result = await self._run_cmd(["auth", "status", "--json"])
        
        if "error" in result:
            return BawAuthStatus(authenticated=False, error=result["error"])
        
        return BawAuthStatus(
            authenticated=result.get("authenticated", False),
            address=result.get("address"),
            chain_id=result.get("chainId"),
            expires_at=result.get("expiresAt"),
        )
    
    # ==================== SPOT TRADING (bStocks) ====================
    
    async def place_spot_order(
        self,
        side: str,  # "BUY" | "SELL"
        symbol: str,  # e.g., "NVDAB-USDT"
        quote_qty: float,  # quote asset amount (USDT) for market orders
        price: Optional[float] = None,  # limit price
        order_type: str = "MARKET",  # MARKET | LIMIT
        time_in_force: str = "GTC",
    ) -> BawOrderResult:
        """
        Place spot order via baw CLI for bStocks.
        """
        args = ["spot", "order", "--json"]
        args.extend(["--side", side.upper()])
        args.extend(["--symbol", symbol.upper()])
        
        if order_type == "MARKET":
            args.extend(["--type", "MARKET"])
            args.extend(["--quote-qty", str(quote_qty)])
        else:
            args.extend(["--type", "LIMIT"])
            args.extend(["--price", str(price)])
            # For limit, need base qty
            args.extend(["--quantity", str(quote_qty / price if price else 0)])
        
        args.extend(["--tif", time_in_force])
        
        result = await self._run_cmd(args)
        return self._parse_order_result(result)
    
    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel open order."""
        result = await self._run_cmd(["spot", "cancel", "--order-id", order_id, "--symbol", symbol.upper(), "--json"])
        return result.get("success", False)
    
    async def get_order_status(self, order_id: str, symbol: str) -> Optional[BawOrderResult]:
        """Get order status."""
        result = await self._run_cmd(["spot", "status", "--order-id", order_id, "--symbol", symbol.upper(), "--json"])
        return self._parse_order_result(result) if "error" not in result else None
    
    # ==================== ACCOUNT / POSITIONS ====================
    
    async def get_account(self) -> Optional[BawAccountInfo]:
        """Get account equity and positions."""
        result = await self._run_cmd(["account", "info", "--json"])
        
        if "error" in result:
            return None
        
        positions = []
        for p in result.get("positions", []):
            positions.append(BawPosition(
                symbol=p["symbol"],
                side=p.get("side", "LONG"),
                size=float(p["size"]),
                entry_price=float(p["entryPrice"]),
                current_price=float(p["markPrice"]),
                unrealized_pnl=float(p.get("unrealizedPnl", 0)),
                unrealized_pnl_pct=float(p.get("unrealizedPnlPct", 0)),
                position_usd=float(p["positionUsd"]),
            ))
        
        return BawAccountInfo(
            equity=float(result.get("totalWalletBalance", 0)),
            available_balance=float(result.get("availableBalance", 0)),
            unrealized_pnl=float(result.get("totalUnrealizedPnl", 0)),
            positions=positions,
        )
    
    async def get_positions(self) -> Dict[str, BawPosition]:
        """Get open positions as dict keyed by symbol."""
        account = await self.get_account()
        if not account:
            return {}
        return {p.symbol: p for p in account.positions}
    
    async def get_equity(self) -> float:
        """Get total account equity."""
        account = await self.get_account()
        return account.equity if account else 0.0
    
    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """Get open orders."""
        args = ["spot", "open-orders", "--json"]
        if symbol:
            args.extend(["--symbol", symbol.upper()])
        result = await self._run_cmd(args)
        if "error" in result:
            return []
        return result.get("orders", [])
    
    # ==================== HELPERS ====================
    
    def _parse_order_result(self, result: Dict) -> BawOrderResult:
        """Parse baw order response into BawOrderResult."""
        if "error" in result:
            return BawOrderResult(success=False, error=result["error"], raw_response=result)
        
        status = result.get("status", "UNKNOWN")
        return BawOrderResult(
            success=status in ("SUCCESS", "FILLED", "PARTIAL"),
            tx_hash=result.get("txHash") or result.get("transactionHash"),
            order_id=result.get("orderId") or result.get("order_id"),
            status=status,
            filled_qty=float(result.get("filledQty", result.get("executedQty", 0))),
            avg_price=float(result.get("avgPrice", result.get("price", 0))),
            fees_usd=float(result.get("fees", result.get("commission", 0))),
            raw_response=result,
        )


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
            # Get account
            account = await client.get_account()
            print(f"Equity: ${account.equity:.2f}")
            print(f"Available: ${account.available_balance:.2f}")
            print(f"Positions: {len(account.positions)}")
            for p in account.positions:
                print(f"  {p.symbol}: {p.side} {p.size:.4f} @ ${p.entry_price:.2f} = ${p.position_usd:.2f} (PnL: ${p.unrealized_pnl:.2f})")
        else:
            print("Not authenticated. Run 'baw auth signin' first.")
    
    asyncio.run(test())
#!/usr/bin/env python3
"""
baw CLI Wrapper — Binance Agentic Wallet integration via subprocess.
Self-custody wallet for DEX/DeFi operations.

Usage:
    from app.tools.baw_cli import BawClient
    client = BawClient()
    await client.check_auth()
    result = await client.swap("56", "0x...", "0x...", 10.0)  # swap on BSC
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
    status: str = "UNKNOWN"
    filled_qty: float = 0.0
    avg_price: float = 0.0
    fees_usd: float = 0.0
    error: Optional[str] = None
    raw_response: Optional[Dict] = None


@dataclass
class BawPosition:
    symbol: str
    side: str
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
    chain_balances: Dict[str, float]


@dataclass
class BawWalletSettings:
    daily_limit: float
    quota_used: float
    quota_left: float
    defi_daily_limit: float
    defi_quota_used: float
    defi_quota_left: float
    session_expire_time: str


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
        
        connected = result.get("data", {}).get("status") == "CONNECTED"
        
        # Get address
        addr_result = await self._run_cmd(["wallet", "address", "--json"])
        address = None
        if "data" in addr_result and addr_result["data"].get("addresses"):
            address = addr_result["data"]["addresses"][0].get("address")
        
        return BawAuthStatus(
            authenticated=connected,
            address=address,
        )
    
    # ==================== BALANCE / ACCOUNT ====================
    
    async def get_balances(self) -> Dict[str, float]:
        """Get token balances across all chains."""
        result = await self._run_cmd(["wallet", "balance", "--json"])
        
        if "error" in result or "data" not in result:
            return {}
        
        balances = {}
        for item in result["data"]:
            # item: {chain, token, symbol, balance, usdValue, ...}
            sym = item.get("symbol", "UNKNOWN")
            usd = float(item.get("usdValue", 0))
            if usd > 0:
                balances[sym] = balances.get(sym, 0) + usd
        return balances
    
    async def get_account(self) -> Optional[BawAccountInfo]:
        """Get account equity and DeFi positions."""
        balances = await self.get_balances()
        equity = sum(balances.values())
        
        # Get DeFi positions
        pos_result = await self._run_cmd(["defi", "position", "--json"])
        positions = []
        unrealized_pnl = 0.0
        
        if "data" in pos_result:
            for p in pos_result["data"].get("deFiProtocolVOList", []):
                # Parse protocol positions
                pos = BawPosition(
                    symbol=p.get("protocolName", "UNKNOWN"),
                    side="LONG",
                    size=float(p.get("totalValue", 0)),
                    entry_price=0.0,
                    current_price=0.0,
                    unrealized_pnl=float(p.get("profit", 0)),
                    unrealized_pnl_pct=0.0,
                    position_usd=float(p.get("totalValue", 0)),
                )
                positions.append(pos)
                unrealized_pnl += pos.unrealized_pnl
        
        return BawAccountInfo(
            equity=equity,
            available_balance=equity,  # self-custody = all available
            unrealized_pnl=unrealized_pnl,
            positions=positions,
            chain_balances=balances,
        )
    
    async def get_positions(self) -> Dict[str, BawPosition]:
        """Get DeFi positions as dict keyed by protocol."""
        account = await self.get_account()
        if not account:
            return {}
        return {p.symbol: p for p in account.positions}
    
    async def get_equity(self) -> float:
        """Get total account equity (wallet balance)."""
        balances = await self.get_balances()
        return sum(balances.values())
    
    async def get_settings(self) -> Optional[BawWalletSettings]:
        """Get wallet settings and limits."""
        result = await self._run_cmd(["wallet", "settings", "--json"])
        
        if "error" in result or "data" not in result:
            return None
        
        d = result["data"]
        return BawWalletSettings(
            daily_limit=float(d.get("dailyLimit", 0)),
            quota_used=float(d.get("quotaUsed", 0)),
            quota_left=float(d.get("quotaLeft", 0)),
            defi_daily_limit=float(d.get("defiDailyLimit", 0)),
            defi_quota_used=float(d.get("defiQuotaUsed", 0)),
            defi_quota_left=float(d.get("defiQuotaLeft", 0)),
            session_expire_time=d.get("sessionExpireTime", ""),
        )
    
    # ==================== TRADING (DEX/DeFi) ====================
    
    async def swap(
        self,
        chain_id: str,
        from_token: str,
        to_token: str,
        from_token_qty: float,
        slippage: str = "auto",
        mev: bool = True,
        gas_level: str = "HIGH",
    ) -> BawOrderResult:
        """Execute DEX swap via market order."""
        args = [
            "market-order", "swap", "--json",
            "--binanceChainId", chain_id,
            "--fromToken", from_token,
            "--toToken", to_token,
            "--fromTokenQty", str(from_token_qty),
            "--slippage", slippage,
            "--mev", "true" if mev else "false",
            "--gasLevel", gas_level,
        ]
        
        result = await self._run_cmd(args)
        return self._parse_order_result(result)
    
    async def limit_buy(
        self,
        chain_id: str,
        from_token: str,
        to_token: str,
        from_token_qty: float,
        trigger_price: float,
        slippage: str = "auto",
        gas_level: str = "HIGH",
    ) -> BawOrderResult:
        """Create limit buy order."""
        args = [
            "limit-order", "buy", "--json",
            "--binanceChainId", chain_id,
            "--fromToken", from_token,
            "--toToken", to_token,
            "--fromTokenQty", str(from_token_qty),
            "--triggerPrice", str(trigger_price),
            "--slippage", slippage,
            "--gasLevel", gas_level,
        ]
        
        result = await self._run_cmd(args)
        return self._parse_order_result(result)
    
    async def limit_sell(
        self,
        chain_id: str,
        from_token: str,
        to_token: str,
        from_token_qty: float,
        trigger_price: float,
        slippage: str = "auto",
        gas_level: str = "HIGH",
    ) -> BawOrderResult:
        """Create limit sell order."""
        args = [
            "limit-order", "sell", "--json",
            "--binanceChainId", chain_id,
            "--fromToken", from_token,
            "--toToken", to_token,
            "--fromTokenQty", str(from_token_qty),
            "--triggerPrice", str(trigger_price),
            "--slippage", slippage,
            "--gasLevel", gas_level,
        ]
        
        result = await self._run_cmd(args)
        return self._parse_order_result(result)
    
    async def get_open_orders(self) -> Dict[str, List[Dict]]:
        """Get open market and limit orders."""
        market = await self._run_cmd(["market-order", "list", "--json"])
        limit = await self._run_cmd(["limit-order", "list", "--json"])
        
        return {
            "market": market.get("data", {}).get("list", []) if "data" in market else [],
            "limit": limit.get("data", {}).get("list", []) if "data" in limit else [],
        }
    
    async def cancel_limit_order(self, strategy_id: str) -> bool:
        """Cancel limit order by strategy ID."""
        result = await self._run_cmd(["limit-order", "cancel", "--json", "--strategyId", strategy_id])
        return result.get("success", False)
    
    # ==================== HELPERS ====================
    
    def _parse_order_result(self, result: Dict) -> BawOrderResult:
        """Parse baw order response into BawOrderResult."""
        if "error" in result:
            return BawOrderResult(success=False, error=result["error"], raw_response=result)
        
        data = result.get("data", {})
        status = data.get("status", "UNKNOWN")
        
        return BawOrderResult(
            success=status in ("SUCCESS", "FILLED", "PARTIAL", "PENDING"),
            tx_hash=data.get("txHash") or data.get("transactionHash"),
            order_id=data.get("orderId") or data.get("strategyId"),
            status=status,
            filled_qty=float(data.get("filledQty", data.get("executedQty", 0))),
            avg_price=float(data.get("avgPrice", data.get("price", 0))),
            fees_usd=float(data.get("fees", data.get("commission", 0))),
            raw_response=result,
        )


# ==================== SYNC WRAPPERS ====================

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
        print(f"Available: {client.available}")
        print(f"Path: {client.baw_path}")
        
        auth = await client.check_auth()
        print(f"Auth: {auth}")
        
        if auth.authenticated:
            account = await client.get_account()
            if account:
                print(f"Equity: ${account.equity:.2f}")
                print(f"Available: ${account.available_balance:.2f}")
                print(f"Chain balances: {account.chain_balances}")
                print(f"DeFi Positions: {len(account.positions)}")
                for p in account.positions:
                    print(f"  {p.symbol}: ${p.position_usd:.2f} (PnL: ${p.unrealized_pnl:.2f})")
            
            settings = await client.get_settings()
            if settings:
                print(f"Daily limit: ${settings.daily_limit:,.0f}")
                print(f"DeFi daily limit: ${settings.defi_daily_limit:,.0f}")
                print(f"Session expires: {settings.session_expire_time}")
            
            orders = await client.get_open_orders()
            print(f"Open market orders: {len(orders['market'])}")
            print(f"Open limit orders: {len(orders['limit'])}")
        else:
            print("Not authenticated. Run 'baw auth signin' first.")
    
    asyncio.run(test())

#!/usr/bin/env python3
"""
baw DEX Trading for bStocks on BSC.
Trades bStock tokens (NVDAB, TSLAB, etc.) via PancakeSwap/Uniswap on BSC using baw CLI.
"""
import asyncio
import json
import os
import re
import shutil
import httpx
from typing import Dict, List, Optional, Any
from dataclasses import dataclass


# bStock token addresses on BSC (chainId 56)
BSTOCK_TOKENS = {
    "NVDAB": "0x02Fca66C1D1aFB4E2A7884261eB00F63598a7436",
    "TSLAB": "0x5b1910eAaD6450E50f816082Aa078C41F10C292f",
    "AAPLB": "0x...",  # TODO: find addresses
    "AMZNB": "0x...",
    "MSFTB": "0x...",
    "METAB": "0x...",
}

# Quote tokens on BSC
USDT_BSC = "0x55d398326f99059fF775485246999027B3197955"
WBNB_BSC = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
BSC_CHAIN_ID = "56"
BSC_RPC_URL = "https://bsc-dataseed1.binance.org/"


# Security: Eth call simulation for honeypot check
async def simulate_swap(
    from_token: str,
    to_token: str,
    from_amount: float,
    router_address: str = "0x10ED43C718714eb63d5aA57B78B54704E256024E",  # PancakeSwap V2 Router
    chain_id: str = BSC_CHAIN_ID,
) -> Dict:
    """
    Simulate a swap via eth_call to check for honeypots / revert / excessive taxes.
    Returns dict with success, estimated_output, tax_pct, error.
    """
    # ERC20 approve + swap calldata construction
    # For simplicity, use baw's internal simulation if available, else skip
    # This is a placeholder - full implementation requires ABI encoding
    return {"success": True, "note": "Simulation stub - implement with web3.py for production"}


# Security: Verify contract source on BSCScan
async def verify_contract_source(token_address: str) -> Dict:
    """
    Check if contract has verified source code on BSCScan.
    """
    api_key = os.environ.get("BSCSCAN_API_KEY", "")
    if not api_key:
        return {"verified": False, "reason": "No BSCSCAN_API_KEY"}
    
    url = "https://api.bscscan.com/api"
    params = {
        "module": "contract",
        "action": "getsourcecode",
        "address": token_address,
        "apikey": api_key,
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            data = resp.json()
        
        if data.get("status") == "1" and data.get("result"):
            source = data["result"][0].get("SourceCode", "")
            return {"verified": bool(source and source != ""), "source_length": len(source)}
    except Exception as e:
        return {"verified": False, "error": str(e)}
    
    return {"verified": False, "reason": "No source code"}


# Security: Cross-reference with CoinGecko
async def cross_reference_coingecko(symbol: str, token_address: str) -> Dict:
    """
    Check if token address matches CoinGecko's listing for the symbol.
    """
    try:
        url = f"https://api.coingecko.com/api/v3/coins/binance-smart-chain/contract/{token_address.lower()}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
        
        if resp.status_code == 200:
            data = resp.json()
            cg_symbol = data.get("symbol", "").upper()
            return {"matches": cg_symbol == symbol.upper(), "coingecko_symbol": cg_symbol}
    except Exception:
        pass
    
    return {"matches": False, "reason": "Not found on CoinGecko"}


# Security: Human confirmation for new symbols
NEW_SYMBOL_CONFIRMATIONS_FILE = "new_symbol_confirmations.json"

def load_confirmations() -> Dict:
    """Load confirmed symbols from file."""
    if os.path.exists(NEW_SYMBOL_CONFIRMATIONS_FILE):
        with open(NEW_SYMBOL_CONFIRMATIONS_FILE) as f:
            return json.load(f)
    return {}

def save_confirmation(symbol: str, confirmed_by: str = "human") -> None:
    """Save that a symbol was confirmed."""
    confirmations = load_confirmations()
    confirmations[symbol] = {"confirmed": True, "by": confirmed_by, "timestamp": asyncio.get_event_loop().time()}
    with open(NEW_SYMBOL_CONFIRMATIONS_FILE, "w") as f:
        json.dump(confirmations, f, indent=2)

def is_symbol_confirmed(symbol: str) -> bool:
    """Check if symbol was previously confirmed."""
    return symbol in load_confirmations()


@dataclass
class DexOrderResult:
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
class DexQuote:
    from_token: str
    to_token: str
    from_amount: float
    to_amount: float
    price_impact_pct: float
    gas_estimate_usd: float


class BawDexClient:
    """
    DEX trading via baw CLI for bStocks on BSC.
    """
    
    def __init__(self, baw_path: str = "baw", timeout: int = 120):
        self.baw_path = baw_path
        self.timeout = timeout
        self._available = shutil.which(self.baw_path) is not None
        if self._available:
            self.baw_path = shutil.which(self.baw_path)
    
    @property
    def available(self) -> bool:
        return self._available
    
    async def _run_cmd(self, args: List[str]) -> Dict:
        if not self._available:
            return {"error": "baw binary not available"}
        
        cmd = [self.baw_path] + args
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            
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
            return {"error": f"Invalid JSON: {e}", "raw": output[:500]}
        except Exception as e:
            return {"error": f"Execution failed: {e}"}
    
    # ==================== QUOTES ====================
    
    async def get_quote(
        self,
        from_token: str,
        to_token: str,
        from_amount: float,
        chain_id: str = BSC_CHAIN_ID,
        slippage: str = "auto",
    ) -> Optional[DexQuote]:
        """Get swap quote without executing."""
        args = [
            "market-order", "quote", "--json",
            "--binanceChainId", chain_id,
            "--fromToken", from_token,
            "--toToken", to_token,
            "--fromTokenQty", str(from_amount),
            "--slippage", slippage,
        ]
        result = await self._run_cmd(args)
        
        if "error" in result or "data" not in result:
            return None
        
        d = result["data"]
        return DexQuote(
            from_token=from_token,
            to_token=to_token,
            from_amount=from_amount,
            to_amount=float(d.get("toCoinAmount", 0)),
            price_impact_pct=float(d.get("slippage", 0)) * 100,
            gas_estimate_usd=0.0,  # not in quote response
        )
    
    # ==================== SWAPS ====================
    
    async def swap_usdt_to_bstock(
        self,
        bstock_symbol: str,
        usdt_amount: float,
        slippage: str = "1",  # 1% default
        gas_level: str = "HIGH",
        skip_security: bool = False,
    ) -> DexOrderResult:
        """Buy bStock with USDT on BSC with security checks."""
        if bstock_symbol not in BSTOCK_TOKENS:
            return DexOrderResult(success=False, error=f"Unknown bStock: {bstock_symbol}")
        
        bstock_addr = BSTOCK_TOKENS[bstock_symbol]
        
        # Security checks
        if not skip_security:
            # 1. Verify contract source
            verify = await verify_contract_source(bstock_addr)
            if not verify.get("verified"):
                return DexOrderResult(success=False, error=f"Contract not verified on BSCScan: {verify}")
            
            # 2. Cross-reference CoinGecko
            cg = await cross_reference_coingecko(bstock_symbol, bstock_addr)
            if not cg.get("matches"):
                return DexOrderResult(success=False, error=f"CoinGecko mismatch: {cg}")
            
            # 3. Human confirmation for new symbols
            if not is_symbol_confirmed(bstock_symbol):
                return DexOrderResult(success=False, error=f"New symbol {bstock_symbol} requires human confirmation. Run with skip_security=True after manual verification.")
            
            # 4. Simulate swap (honeypot check) - stub for now
            sim = await simulate_swap(USDT_BSC, bstock_addr, usdt_amount)
            if not sim.get("success"):
                return DexOrderResult(success=False, error=f"Simulation failed: {sim}")
        
        args = [
            "market-order", "swap", "--json",
            "--binanceChainId", BSC_CHAIN_ID,
            "--fromToken", USDT_BSC,
            "--toToken", bstock_addr,
            "--fromTokenQty", str(usdt_amount),
            "--slippage", slippage,
            "--gasLevel", gas_level,
        ]
        result = await self._run_cmd(args)
        return self._parse_result(result)
    
    async def swap_bstock_to_usdt(
        self,
        bstock_symbol: str,
        bstock_amount: float,
        slippage: str = "1",
        gas_level: str = "HIGH",
        skip_security: bool = False,
    ) -> DexOrderResult:
        """Sell bStock for USDT on BSC with security checks."""
        if bstock_symbol not in BSTOCK_TOKENS:
            return DexOrderResult(success=False, error=f"Unknown bStock: {bstock_symbol}")
        
        bstock_addr = BSTOCK_TOKENS[bstock_symbol]
        
        # Security checks (same as buy)
        if not skip_security:
            verify = await verify_contract_source(bstock_addr)
            if not verify.get("verified"):
                return DexOrderResult(success=False, error=f"Contract not verified: {verify}")
            
            cg = await cross_reference_coingecko(bstock_symbol, bstock_addr)
            if not cg.get("matches"):
                return DexOrderResult(success=False, error=f"CoinGecko mismatch: {cg}")
            
            if not is_symbol_confirmed(bstock_symbol):
                return DexOrderResult(success=False, error=f"New symbol {bstock_symbol} requires human confirmation")
            
            sim = await simulate_swap(bstock_addr, USDT_BSC, bstock_amount)
            if not sim.get("success"):
                return DexOrderResult(success=False, error=f"Simulation failed: {sim}")
        
        args = [
            "market-order", "swap", "--json",
            "--binanceChainId", BSC_CHAIN_ID,
            "--fromToken", bstock_addr,
            "--toToken", USDT_BSC,
            "--fromTokenQty", str(bstock_amount),
            "--slippage", slippage,
            "--gasLevel", gas_level,
        ]
        result = await self._run_cmd(args)
        return self._parse_result(result)
    
    # ==================== LIMIT ORDERS ====================
    
    async def limit_buy_bstock(
        self,
        bstock_symbol: str,
        usdt_amount: float,
        trigger_price_usd: float,  # price in USD per bStock
        slippage: str = "1",
        gas_level: str = "HIGH",
    ) -> DexOrderResult:
        """Place limit buy order for bStock."""
        if bstock_symbol not in BSTOCK_TOKENS:
            return DexOrderResult(success=False, error=f"Unknown bStock: {bstock_symbol}")
        
        bstock_addr = BSTOCK_TOKENS[bstock_symbol]
        # Calculate how many bStock tokens for the USDT amount at trigger price
        bstock_qty = usdt_amount / trigger_price_usd
        
        args = [
            "limit-order", "buy", "--json",
            "--binanceChainId", BSC_CHAIN_ID,
            "--fromToken", USDT_BSC,
            "--toToken", bstock_addr,
            "--fromTokenQty", str(usdt_amount),
            "--triggerPrice", str(trigger_price_usd),
            "--slippage", slippage,
            "--gasLevel", gas_level,
        ]
        result = await self._run_cmd(args)
        return self._parse_result(result)
    
    async def limit_sell_bstock(
        self,
        bstock_symbol: str,
        bstock_amount: float,
        trigger_price_usd: float,
        slippage: str = "1",
        gas_level: str = "HIGH",
    ) -> DexOrderResult:
        """Place limit sell order for bStock."""
        if bstock_symbol not in BSTOCK_TOKENS:
            return DexOrderResult(success=False, error=f"Unknown bStock: {bstock_symbol}")
        
        bstock_addr = BSTOCK_TOKENS[bstock_symbol]
        args = [
            "limit-order", "sell", "--json",
            "--binanceChainId", BSC_CHAIN_ID,
            "--fromToken", bstock_addr,
            "--toToken", USDT_BSC,
            "--fromTokenQty", str(bstock_amount),
            "--triggerPrice", str(trigger_price_usd),
            "--slippage", slippage,
            "--gasLevel", gas_level,
        ]
        result = await self._run_cmd(args)
        return self._parse_result(result)
    
    # ==================== OPEN ORDERS ====================
    
    async def get_open_orders(self) -> Dict[str, List[Dict]]:
        market = await self._run_cmd(["market-order", "list", "--json"])
        limit = await self._run_cmd(["limit-order", "list", "--json"])
        return {
            "market": market.get("data", {}).get("list", []) if "data" in market else [],
            "limit": limit.get("data", {}).get("list", []) if "data" in limit else [],
        }
    
    async def cancel_limit_order(self, strategy_id: str) -> bool:
        result = await self._run_cmd(["limit-order", "cancel", "--json", "--strategyId", strategy_id])
        return result.get("success", False)
    
    # ==================== HELPERS ====================
    
    def _parse_result(self, result: Dict) -> DexOrderResult:
        if "error" in result:
            return DexOrderResult(success=False, error=result["error"], raw_response=result)
        
        data = result.get("data", {})
        status = data.get("status", "UNKNOWN")
        
        return DexOrderResult(
            success=status in ("SUCCESS", "FILLED", "PARTIAL", "PENDING", "SUBMITTED"),
            tx_hash=data.get("txHash") or data.get("transactionHash"),
            order_id=data.get("orderId") or data.get("strategyId"),
            status=status,
            filled_qty=float(data.get("filledQty", data.get("executedQty", 0))),
            avg_price=float(data.get("avgPrice", data.get("price", 0))),
            fees_usd=float(data.get("fees", data.get("gasFeeUSD", 0))),
            raw_response=result,
        )
    
    # ==================== BALANCE CHECK ====================
    
    async def check_usdt_balance(self) -> float:
        """Check USDT balance on BSC."""
        result = await self._run_cmd(["wallet", "balance", "--json"])
        if "error" in result or "data" not in result:
            return 0.0
        
        for item in result["data"]:
            if item.get("symbol") == "USDT" and item.get("binanceChainId") == "56":
                return float(item.get("balance", 0))
        return 0.0
    
    async def check_bstock_balance(self, bstock_symbol: str) -> float:
        """Check bStock balance on BSC."""
        if bstock_symbol not in BSTOCK_TOKENS:
            return 0.0
        
        result = await self._run_cmd(["wallet", "balance", "--json"])
        if "error" in result or "data" not in result:
            return 0.0
        
        for item in result["data"]:
            if item.get("symbol") == bstock_symbol and item.get("binanceChainId") == "56":
                return float(item.get("balance", 0))
        return 0.0
    
    # Security: Confirm a new symbol after manual verification
    def confirm_new_symbol(self, symbol: str) -> None:
        """Mark a symbol as confirmed after manual verification (address, source, etc.)."""
        save_confirmation(symbol, "manual")


# ==================== CLI TEST ====================

if __name__ == "__main__":
    async def test():
        client = BawDexClient()
        print(f"baw available: {client.available}")
        
        if not client.available:
            return
        
        # Check USDT balance
        usdt = await client.check_usdt_balance()
        print(f"USDT balance on BSC: {usdt}")
        
        # Check NVDAB balance
        nvdab = await client.check_bstock_balance("NVDAB")
        print(f"NVDAB balance: {nvdab}")
        
        # Get quote for $50 USDT -> NVDAB
        quote = await client.get_quote(USDT_BSC, BSTOCK_TOKENS["NVDAB"], 50)
        if quote:
            print(f"Quote: ${quote.from_amount} USDT -> {quote.to_amount:.6f} NVDAB")
            print(f"  Price impact: {quote.price_impact_pct:.2f}%")
            print(f"  Gas est: ${quote.gas_estimate_usd:.4f}")
    
    asyncio.run(test())

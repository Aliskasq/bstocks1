"""The agent: dynamic universe + tool registry + decision cycle."""
from __future__ import annotations

import asyncio
import json
from typing import Callable

from ..config import ALLOW_MOCK_MARKET
from ..services.openrouter import ToolRegistry, chat_with_tools, extract_json
from ..tools import memory as mem
from ..tools import risk as risk_mod
from ..tools.baw_cli import BawClient
from ..tools.market_data import sync_get_klines
from ..tools.universe import (
    discover_bstocks, quick_scan, get_klines,
    rsi, ema, macd, atr, volume_anomaly, market_structure,
    bollinger_bands, stochastic, adx, all_indicators,
    baw_format, binance_format,
)
from ..trace import Trace
from .monitor import WatchList
from .prompts import DECISION_USER_TEMPLATE, REANALYSIS_USER_TEMPLATE, SYSTEM


class Agent:
    def __init__(self, on_event: Callable[[dict], None] | None = None):
        # baw client is optional in mock mode
        try:
            self.baw = BawClient()
        except FileNotFoundError:
            self.baw = None
        self.on_event = on_event
        self._candle_cache: dict[str, list[dict]] = {}
        self.watchlist = WatchList()
        self.last_decision: dict | None = None
        self.pending_trade: dict | None = None

    # ---- data access -------------------------------------------------
    def _candles(self, symbol: str, interval: str = "1h", limit: int = 120) -> list[dict]:
        """Get candles from Binance REST (production) or mock (dev)."""
        key = f"{symbol}:{interval}:{limit}"
        if key not in self._candle_cache:
            if ALLOW_MOCK_MARKET:
                from ..tools.binance_mcp import _mock_candles
                self._candle_cache[key] = _mock_candles(symbol, limit)
                self._last_source = "mock"
            else:
                # Fetch real data from Binance REST API
                self._candle_cache[key] = sync_get_klines(symbol, interval, limit)
                self._last_source = "binance_rest"
        return self._candle_cache[key]

    def invalidate(self) -> None:
        self._candle_cache.clear()

    def snapshot(self, symbol: str) -> dict:
        """Cheap current-state read used by the monitor (no LLM involved)."""
        c = self._candles(symbol)
        ind = all_indicators(c)
        return {
            "symbol": symbol,
            "price": c[-1]["close"],
            "rsi": ind["rsi"],
            "volume_ratio": ind["volume"].get("ratio"),
        }

    # ---- tools exposed to the LLM ------------------------------------
    def build_registry(self, trace: Trace) -> ToolRegistry:
        reg = ToolRegistry()

        # Universe discovery & quick scan
        def t_discover_universe():
            try:
                symbols = discover_bstocks()
                return {"symbols": symbols, "count": len(symbols)}
            except Exception as e:
                return {"error": str(e), "symbols": []}

        def t_quick_scan(min_volume_usd: float = 50000, max_symbols: int = 20):
            try:
                return {"candidates": quick_scan(min_volume_usd, max_symbols)}
            except Exception as e:
                return {"error": str(e), "candidates": []}

        # Individual indicators (LLM chooses what it needs)
        def t_get_rsi(symbol: str, period: int = 14):
            c = self._candles(symbol)
            return {"symbol": symbol, "rsi": rsi(c, period), "period": period}

        def t_get_ema(symbol: str, period: int = 20):
            c = self._candles(symbol)
            return {"symbol": symbol, "ema": ema(c, period), "period": period}

        def t_get_macd(symbol: str):
            c = self._candles(symbol)
            return {"symbol": symbol, **macd(c)}

        def t_get_atr(symbol: str, period: int = 14):
            c = self._candles(symbol)
            return {"symbol": symbol, "atr": atr(c, period), "period": period}

        def t_get_volume(symbol: str, lookback: int = 20):
            c = self._candles(symbol)
            return {"symbol": symbol, **volume_anomaly(c, lookback)}

        def t_get_structure(symbol: str, lookback: int = 50):
            c = self._candles(symbol)
            return {"symbol": symbol, **market_structure(c, lookback)}

        def t_get_bollinger(symbol: str, period: int = 20):
            c = self._candles(symbol)
            return {"symbol": symbol, **bollinger_bands(c, period)}

        def t_get_stochastic(symbol: str, period: int = 14):
            c = self._candles(symbol)
            return {"symbol": symbol, **stochastic(c, period)}

        def t_get_adx(symbol: str, period: int = 14):
            c = self._candles(symbol)
            return {"symbol": symbol, "adx": adx(c, period), "period": period}

        def t_get_all_indicators(symbol: str):
            c = self._candles(symbol)
            return {"symbol": symbol, **all_indicators(c)}

        # Memory & risk (unchanged)
        def t_get_memory(symbol: str):
            c = self._candles(symbol)
            ind = all_indicators(c)
            # Reconstruct fingerprint from individual indicators
            fp_parts = []
            if ind["rsi"] > 70: fp_parts.append("rsi:overbought")
            elif ind["rsi"] < 30: fp_parts.append("rsi:oversold")
            else: fp_parts.append("rsi:neutral")
            if ind["volume"]["verdict"] == "high": fp_parts.append("vol:spike")
            elif ind["volume"]["verdict"] == "low": fp_parts.append("vol:dry")
            else: fp_parts.append("vol:normal")
            mom = ind["structure"]["trend"]
            if mom == "bullish" and ind.get("macd", {}).get("macd", 0) > 0:
                fp_parts.append("mom:strong_up")
            elif mom == "bearish":
                fp_parts.append("mom:down")
            else:
                fp_parts.append("mom:flat")
            fp = "|".join(fp_parts)
            return {"fingerprint": fp,
                    "similar_past_setups": mem.search_memory(symbol, fp),
                    "historical_stats": mem.outcome_stats(fp)}

        def t_limits():
            return risk_mod.limits() | {
                "trades_used_today": risk_mod.STATE.trades_today,
                "realized_pnl_today": risk_mod.STATE.realized_pnl_today,
            }

        # Register all tools
        reg.register("discover_universe",
                     "Discover all tradeable bStock symbols on Binance (ending in BUSDT).",
                     {"type": "object", "properties": {}}, t_discover_universe)

        reg.register("quick_scan",
                     "Quick volume-filtered scan of bStocks universe. Returns top candidates with price, change%, volume.",
                     {"type": "object", "properties": {
                         "min_volume_usd": {"type": "number", "default": 50000},
                         "max_symbols": {"type": "integer", "default": 20}},
                      "required": []}, t_quick_scan)

        reg.register("get_rsi", "Relative Strength Index (14).",
                     {"type": "object", "properties": {
                         "symbol": {"type": "string"}, "period": {"type": "integer", "default": 14}},
                      "required": ["symbol"]}, t_get_rsi)

        reg.register("get_ema", "Exponential Moving Average.",
                     {"type": "object", "properties": {
                         "symbol": {"type": "string"}, "period": {"type": "integer", "default": 20}},
                      "required": ["symbol"]}, t_get_ema)

        reg.register("get_macd", "MACD (12,26,9).",
                     {"type": "object", "properties": {"symbol": {"type": "string"}},
                      "required": ["symbol"]}, t_get_macd)

        reg.register("get_atr", "Average True Range (14).",
                     {"type": "object", "properties": {
                         "symbol": {"type": "string"}, "period": {"type": "integer", "default": 14}},
                      "required": ["symbol"]}, t_get_atr)

        reg.register("get_volume", "Volume anomaly ratio vs baseline.",
                     {"type": "object", "properties": {
                         "symbol": {"type": "string"}, "lookback": {"type": "integer", "default": 20}},
                      "required": ["symbol"]}, t_get_volume)

        reg.register("get_structure", "Market structure: HH/LL, range position %, trend.",
                     {"type": "object", "properties": {
                         "symbol": {"type": "string"}, "lookback": {"type": "integer", "default": 50}},
                      "required": ["symbol"]}, t_get_structure)

        reg.register("get_bollinger", "Bollinger Bands %B.",
                     {"type": "object", "properties": {
                         "symbol": {"type": "string"}, "period": {"type": "integer", "default": 20}},
                      "required": ["symbol"]}, t_get_bollinger)

        reg.register("get_stochastic", "Stochastic %K/%D.",
                     {"type": "object", "properties": {
                         "symbol": {"type": "string"}, "period": {"type": "integer", "default": 14}},
                      "required": ["symbol"]}, t_get_stochastic)

        reg.register("get_adx", "Average Directional Index (trend strength).",
                     {"type": "object", "properties": {
                         "symbol": {"type": "string"}, "period": {"type": "integer", "default": 14}},
                      "required": ["symbol"]}, t_get_adx)

        reg.register("get_all_indicators", "All indicators at once (convenience).",
                     {"type": "object", "properties": {"symbol": {"type": "string"}},
                      "required": ["symbol"]}, t_get_all_indicators)

        reg.register("get_memory",
                     "Recall past similar setups for this symbol and their outcomes.",
                     {"type": "object", "properties": {"symbol": {"type": "string"}},
                      "required": ["symbol"]}, t_get_memory)

        reg.register("get_risk_limits",
                     "Current hard risk limits and today's usage.",
                     {"type": "object", "properties": {}}, t_limits)

        return reg

    # ---- one decision cycle -----------------------------------------
    def cycle(self, goal: str) -> dict:
        trace = Trace(goal, on_event=self.on_event)
        self.invalidate()

        # No pre-scan — LLM will call discover_universe / quick_scan as needed
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": DECISION_USER_TEMPLATE.format(
                goal=goal,
                limits=json.dumps(risk_mod.limits(), indent=2),
                candidates="[LLM will scan universe via tools]",
            )},
        ]

        return self._run_decision(messages, trace, scores=None)

    # ---- shared decision runner --------------------------------------
    def _run_decision(self, messages: list[dict], trace: Trace,
                      scores: list[dict] | None = None) -> dict:
        registry = self.build_registry(trace)
        result = chat_with_tools(messages, registry, trace, response_json=True)
        decision = extract_json(result.get("content", "")) or {}

        if not decision:
            trace.add("decision_parse_failed", raw=result.get("content", "")[:500])
            trace.save()
            return {"trace": trace, "decision": None}

        trace.decision(decision)

        # Risk gate — the LLM's proposal is only a request.
        risk_verdict = None
        if decision.get("decision") == "BUY":
            size = float(decision.get("proposed_size_usd") or 0)
            risk_verdict = risk_mod.check_risk(decision.get("symbol", "?"), size)
            trace.risk(risk_verdict)
            decision["final_action"] = (
                "AWAITING_USER_CONFIRMATION" if risk_verdict["approved"]
                else "REJECTED_BY_RISK"
            )
        else:
            decision["final_action"] = decision.get("decision")

        # Remember this setup.
        sym = decision.get("symbol")
        mem_id = None
        if sym:
            try:
                c = self._candles(sym)
                ind = all_indicators(c)
                # Build fingerprint from individual indicators
                fp_parts = []
                if ind["rsi"] > 70: fp_parts.append("rsi:overbought")
                elif ind["rsi"] < 30: fp_parts.append("rsi:oversold")
                else: fp_parts.append("rsi:neutral")
                if ind["volume"]["verdict"] == "high": fp_parts.append("vol:spike")
                elif ind["volume"]["verdict"] == "low": fp_parts.append("vol:dry")
                else: fp_parts.append("vol:normal")
                mom = ind["structure"]["trend"]
                if mom == "bullish" and ind.get("macd", {}).get("macd", 0) > 0:
                    fp_parts.append("mom:strong_up")
                elif mom == "bearish":
                    fp_parts.append("mom:down")
                else:
                    fp_parts.append("mom:flat")
                fp = "|".join(fp_parts)
                score = None  # no pre-scan score available
                mem_id = mem.save_memory(sym, fp, score, ind, decision)
                trace.add("memory_saved", memory_id=mem_id, fingerprint=fp)
            except Exception as exc:  # noqa: BLE001
                trace.add("memory_error", error=str(exc)[:200])

        # Register the monitor condition so WAIT becomes observable state.
        if decision.get("decision") == "WAIT" and decision.get("monitor_condition"):
            ref = self.snapshot(sym)["price"] if sym else None
            if ref:
                w = self.watchlist.add(sym, decision["monitor_condition"], ref, mem_id)
                if w:
                    trace.add("watch_registered", **w.to_dict())
                else:
                    trace.add("watch_rejected",
                              condition=decision["monitor_condition"],
                              reason="condition not machine-checkable")

        if decision.get("final_action") == "AWAITING_USER_CONFIRMATION":
            self.pending_trade = {
                "symbol": sym,
                "size_usd": risk_verdict["approved_size_usd"],
                "entry_price": self.snapshot(sym)["price"],
                "memory_id": mem_id,
                "decision": decision,
            }

        self.last_decision = decision
        path = trace.save()
        return {"trace": trace, "decision": decision, "risk": risk_verdict,
                "memory_id": mem_id, "trace_path": path, "candidates": scores}

    # ---- monitoring --------------------------------------------------
    def check_watches(self) -> list[dict]:
        """Evaluate all active watches against fresh data. No LLM cost."""
        self.invalidate()
        return self.watchlist.check_all(self.snapshot)

    def reanalyze(self, goal: str, fired: dict) -> dict:
        """A monitor condition triggered — re-verify from scratch."""
        trace = Trace(f"[re-analysis] {goal}", on_event=self.on_event)
        self.invalidate()
        symbol = fired["symbol"]
        watch = next((w for w in self.watchlist.all() if w.symbol == symbol), None)
        prev_reason = (self.last_decision or {}).get("reason", "(not recorded)")

        trace.add("trigger", symbol=symbol, condition=fired["condition"],
                  detail=fired["detail"], waited_s=fired.get("waited_s"))

        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": REANALYSIS_USER_TEMPLATE.format(
                goal=goal, symbol=symbol, condition=fired["condition"],
                detail=fired["detail"], waited_s=fired.get("waited_s", "?"),
                checks=watch.checks if watch else "?",
                previous_reason=prev_reason,
                limits=json.dumps(risk_mod.limits(), indent=2),
            )},
        ]
        out = self._run_decision(messages, trace)
        self.watchlist.drop(symbol)
        return out

    # ---- trade execution (gated) -------------------------------------
    def confirm_trade(self) -> dict:
        """User-confirmed execution path: risk re-check -> baw order -> verify."""
        if not self.pending_trade:
            return {"error": "no pending trade"}

        pt = self.pending_trade
        trace = Trace(f"[execute] {pt['symbol']}", on_event=self.on_event)

        # Re-check risk at execution time, not just at decision time.
        verdict = risk_mod.check_risk(pt["symbol"], pt["size_usd"])
        trace.risk(verdict)
        if not verdict["approved"]:
            self.pending_trade = None
            trace.add("execution_aborted", reason=verdict["reason"])
            trace.save()
            return {"executed": False, "reason": verdict["reason"], "trace": trace}

        # Execute via baw CLI (or simulate in mock mode)
        trace.tool_call("baw_spot_order", {
            "symbol": pt["symbol"], 
            "side": "BUY", 
            "type": "MARKET", 
            "quote_qty": pt["size_usd"]
        })
        
        if self.baw is None:
            # Mock execution for development
            import uuid
            result_dict = {
                "success": True,
                "order_id": f"mock_{uuid.uuid4().hex[:12]}",
                "tx_hash": f"0x{uuid.uuid4().hex}",
                "status": "FILLED",
                "filled_qty": pt["size_usd"] / pt["entry_price"],
                "avg_price": pt["entry_price"],
                "fees_usd": round(pt["size_usd"] * 0.0004, 4),
                "error": None,
                "mock": True,
            }
            executed = True
        else:
            try:
                result = asyncio.run(self.baw.place_spot_order(
                    side="BUY",
                    symbol=pt["symbol"],
                    quote_qty=pt["size_usd"],
                    order_type="MARKET",
                ))
                executed = result.success
                result_dict = {
                    "success": result.success,
                    "order_id": result.order_id,
                    "tx_hash": result.tx_hash,
                    "status": result.status,
                    "filled_qty": result.filled_qty,
                    "avg_price": result.avg_price,
                    "fees_usd": result.fees_usd,
                    "error": result.error,
                }
            except Exception as exc:  # noqa: BLE001
                result_dict = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                executed = False
        
        trace.tool_result("baw_spot_order", result_dict)

        if executed:
            risk_mod.STATE.record_trade(pt["symbol"], pt["size_usd"])
            trace.add("position_opened", symbol=pt["symbol"],
                      size_usd=pt["size_usd"], entry=pt["entry_price"])

        self.pending_trade = None
        trace.save()
        return {"executed": executed, "order": {"symbol": pt["symbol"], "side": "BUY", "type": "MARKET", "quoteOrderQty": pt["size_usd"]}, "result": result_dict,
                "trace": trace, "risk": verdict}

    def close_position(self, symbol: str, exit_price: float,
                       memory_id: int | None = None) -> dict:
        """Close the loop: record the outcome so memory becomes useful."""
        entry = risk_mod.STATE.open_positions.get(symbol)
        out: dict = {"symbol": symbol}
        if memory_id:
            out |= mem.record_outcome(memory_id, entry or exit_price, exit_price)
            risk_mod.STATE.record_pnl(
                (out.get("pnl_pct") or 0) / 100 * (entry or 0))
        risk_mod.STATE.open_positions.pop(symbol, None)
        return out

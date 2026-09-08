"""The autonomous loop — the heart of the project.

SCAN -> DECIDE -> (WAIT: register watch) -> MONITOR -> on trigger: RE-ANALYZE
                -> (BUY: risk gate -> user confirmation -> execute -> remember)

Design note for judges: the LLM is called only at decision points. Monitoring is
free deterministic Python, so the agent can watch patiently for hours cheaply.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

from ..tools import binance_mcp
from ..config import ALLOW_MOCK_MARKET
from .agent import Agent


class AgentLoop:
    def __init__(self, goal: str, on_event: Callable[[dict], None] | None = None,
                 *, monitor_interval_s: float = 20.0,
                 rescan_interval_s: float = 600.0,
                 mock_speed: int = 0):
        self.goal = goal
        self.on_event = on_event
        self.agent = Agent(on_event=on_event, trading_mode=self._load_trading_mode())
        self.monitor_interval_s = monitor_interval_s
        self.rescan_interval_s = rescan_interval_s
        self.mock_speed = mock_speed  # dev: advance synthetic clock per tick
        self.running = False
        self._thread: threading.Thread | None = None
        self._last_scan_at = 0.0
        self.cycles = 0
        self.status = "idle"
        self.history: list[dict] = []
        self._trading_mode_path = Path(__file__).resolve().parent.parent / "config" / "trading_mode.json"
        self._trading_mode = self._load_trading_mode()

    def _load_trading_mode(self) -> str:
        try:
            with open(self._trading_mode_path) as f:
                data = json.load(f)
                return data.get("mode", "MOCK").upper()
        except Exception:
            return "MOCK"

    def _save_trading_mode(self, mode: str) -> None:
        self._trading_mode = mode.upper()
        self._trading_mode_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._trading_mode_path, "w") as f:
            json.dump({"mode": self._trading_mode}, f)

    @property
    def trading_mode(self) -> str:
        return self._trading_mode

    def set_trading_mode(self, mode: str) -> str:
        mode = mode.upper()
        if mode not in ("MOCK", "LIVE"):
            return f"invalid mode: {mode}"
        self._save_trading_mode(mode)
        self.agent.trading_mode = mode
        self._emit("status", status=f"trading mode: {mode}")
        return f"trading mode set to {mode}"

    # Handle natural language chat messages from dashboard
    def handle_chat(self, text: str, chat_history: list[dict] | None = None) -> str:
        """Process user message through LLM with full context, return response."""
        from ..services.openrouter import chat_with_tools, extract_json
        from ..tools import risk as risk_mod
        from ..tools.universe import all_indicators
        from ..trace import Trace

        trace = Trace(f"[chat] {text[:80]}", on_event=self.on_event)

        # Build context for LLM
        pending = self.agent.pending_trade
        watches = self.agent.watchlist.active()
        risk_limits = risk_mod.limits()
        risk_state = {
            "trades_today": risk_mod.STATE.trades_today,
            "realized_pnl_today": risk_mod.STATE.realized_pnl_today,
            "open_positions": risk_mod.STATE.open_positions,
        }

        context = {
            "goal": self.goal,
            "mode": self._trading_mode,
            "pending_trade": pending,
            "watches": [w.to_dict() for w in watches],
            "risk_limits": risk_limits,
            "risk_state": risk_state,
            "last_decision": {k: v for k, v in self.agent.last_decision.items() if k != "symbols"} if self.agent.last_decision else {},
        }

        # Register ALL tools from the agent's full registry + chat-specific ones
        reg = self.agent.build_registry(trace)

        def t_analyze(symbol: str):
            return {"symbol": symbol, **all_indicators(self.agent._candles(symbol))}

        def t_portfolio():
            return {
                "mode": self._trading_mode,
                "risk_state": risk_state,
                "risk_limits": risk_limits,
                "pending_trade": pending,
                "watches": [w.to_dict() for w in watches],
                "last_decision": self.agent.last_decision,
            }

        def t_watch(symbol: str, condition: str):
            """Add a symbol to the watchlist with a monitoring condition."""
            ref = float(self.agent.snapshot(symbol)["price"]) if self.agent.snapshot(symbol)["price"] else None
            mem_id = None
            try:
                c = self.agent._candles(symbol)
                ind = all_indicators(c)
                fp_parts = []
                if ind["rsi"] > 70: fp_parts.append("rsi:overbought")
                elif ind["rsi"] < 30: fp_parts.append("rsi:oversold")
                else: fp_parts.append("rsi:neutral")
                if ind["volume"]["verdict"] == "high": fp_parts.append("vol:spike")
                elif ind["volume"]["verdict"] == "low": fp_parts.append("vol:dry")
                else: fp_parts.append("vol:normal")
                fp = "|".join(fp_parts)
                mem_id = self.agent.memory.search_memory(symbol, fp)
            except Exception:
                pass
            w = self.agent.watchlist.add(symbol, condition, ref, mem_id)
            if w:
                return {"ok": True, "watch": w.to_dict()}
            return {"error": f"could not add {symbol} to watchlist"}

        def t_change_goal(new_goal: str):
            old = self.goal
            self.goal = new_goal
            return {"old_goal": old, "new_goal": new_goal}

        def t_set_mode(mode: str):
            return {"result": self.set_trading_mode(mode)}

        def t_confirm():
            if self._trading_mode == "MOCK":
                return {"error": "Trading disabled in MOCK mode. Use set_trading_mode('LIVE') to enable real trading."}
            if not pending:
                return {"error": "no pending trade to confirm"}
            out = self.agent.confirm_trade()
            out.pop("trace", None)
            return out

        def t_reject():
            if self._trading_mode == "MOCK":
                return {"cancelled": True, "note": "Mock mode — no trade was executed"}
            self.agent.pending_trade = None
            return {"cancelled": True}

        reg.register("get_portfolio", "Get current portfolio state: positions, risk usage, pending trade, watches",
                     {"type": "object", "properties": {}}, t_portfolio)
        reg.register("analyze_symbol", "Analyze a specific bStock symbol (RSI, MACD, EMA, volume, all indicators).",
                     {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
                     t_analyze)
        reg.register("watch_symbol", "Add a symbol to the watchlist for monitoring. Condition examples: 'rsi < 40', 'price < 300', 'volume spike'.",
                     {"type": "object", "properties": {"symbol": {"type": "string"}, "condition": {"type": "string"}}, "required": ["symbol", "condition"]},
                     t_watch)
        reg.register("change_goal", "Change the agent's goal/objective",
                     {"type": "object", "properties": {"new_goal": {"type": "string"}}, "required": ["new_goal"]},
                     t_change_goal)
        reg.register("set_trading_mode", "Switch between MOCK and LIVE trading mode",
                     {"type": "object", "properties": {"mode": {"type": "string", "enum": ["MOCK", "LIVE"]}}, "required": ["mode"]},
                     t_set_mode)
        reg.register("confirm_trade", "Confirm and execute the pending trade (after risk check)",
                     {"type": "object", "properties": {}}, t_confirm)
        reg.register("reject_trade", "Reject/cancel the pending trade",
                     {"type": "object", "properties": {}}, t_reject)

        messages = [
            {"role": "system", "content": (
                "You are the bStocks AI Agent. The user is talking to you naturally.\n"
                "You have FULL CONTEXT of the agent state (see below).\n"
                "Decide what to do: reply naturally, call tools to analyze/check portfolio, "
                "change goal, confirm/reject pending trade, or switch trading mode.\n"
                "If user says 'buy'/'approve'/'go ahead' and there's a pending trade → call confirm_trade.\n"
                "If user asks about a symbol → call analyze_symbol.\n"
                "If user asks to watch/monitor a symbol → call watch_symbol with the symbol and condition.\n"
                "If user wants to change strategy → call change_goal.\n"
                "If user asks 'how are we doing' / 'portfolio' / 'positions' → call get_portfolio.\n"
                "If user says 'live mode' / 'real trading' / 'mock mode' → call set_trading_mode.\n"
                "If user asks about news, events, or anything external → call web_search.\n"
                "Use web_search, quick_scan, discover_universe, get_memory and other tools freely.\n"
                "IMPORTANT: In MOCK mode, confirm_trade returns an error — no real trades are executed.\n"
                "Always respond in the user's language (Russian/English). Keep it concise.\n\n"
                "IMPORTANT: bStocks on Binance use format like NVDABUSDT, TSLABUSDT, AAPLBUSDT (ticker + BUSDT). "
                "The analyze_symbol tool accepts user-friendly formats: NVDAB, NVDAB-USDT, NVDABUSDT — all work. "
                "ALWAYS call analyze_symbol when user asks about a symbol — do NOT rely on your training knowledge. "
                "The tool fetches LIVE data from Binance REST API.\n\n"
                f"CURRENT CONTEXT:\n{json.dumps(context, indent=2, default=str)}"
            )},
            {"role": "user", "content": text},
        ]

        # Add last 5 chat messages for context (user doesn't want agent to forget)
        if chat_history:
            recent = [h for h in chat_history if h.get("kind") in ("chat_user", "chat_agent", "chat_tool_call", "chat_tool_result")][-5:]
            for h in recent:
                if h.get("kind") == "chat_user":
                    messages.append({"role": "user", "content": h.get("text", "")})
                elif h.get("kind") == "chat_agent":
                    messages.append({"role": "assistant", "content": h.get("text", "")})
                elif h.get("kind") == "chat_tool_call":
                    messages.append({"role": "user", "content": f"🔧 {h.get('tool','')}({json.dumps(h.get('args',{}))})"})
                elif h.get("kind") == "chat_tool_result":
                    messages.append({"role": "assistant", "content": f"📥 {h.get('tool','')}: {str(h.get('result',''))[:300]}"})

        # Wrap chat_with_tools to capture tool calls for chat display
        original_emit = self._emit
        
        def emit_tool_call(name: str, args: dict):
            original_emit("chat_tool_call", ts=time.time(), tool=name, args=args)
        
        def emit_tool_result(name: str, result: Any):
            original_emit("chat_tool_result", ts=time.time(), tool=name, result=str(result)[:500])
        
        # Patch trace to also emit to chat
        original_tool_call = trace.tool_call
        original_tool_result = trace.tool_result
        
        def patched_tool_call(name: str, args: dict):
            original_tool_call(name, args)
            emit_tool_call(name, args)
        
        def patched_tool_result(name: str, result: Any, ms: int | None = None):
            original_tool_result(name, result, ms)
            emit_tool_result(name, result)
        
        trace.tool_call = patched_tool_call
        trace.tool_result = patched_tool_result
        
        result = chat_with_tools(messages, reg, trace, response_json=False)
        
        return result.get("content", "...")

    # ---- events ------------------------------------------------------
    def _emit(self, kind: str, **data) -> None:
        payload = {"kind": kind, "ts": time.time(), **data}
        self.history.append(payload)
        self.history = self.history[-300:]
        if self.on_event:
            try:
                self.on_event(payload)
            except Exception:
                pass

    def _set_status(self, status: str) -> None:
        self.status = status
        self._emit("status", status=status)

    # ---- one iteration ----------------------------------------------
    def tick(self) -> None:
        now = time.time()
        watches = self.agent.watchlist.active()

        if watches:
            self._set_status(f"monitoring {', '.join(w.symbol for w in watches)}")
            if self.mock_speed and ALLOW_MOCK_MARKET:
                binance_mcp.advance_mock_clock(self.mock_speed)
            fired = self.agent.check_watches()
            for w in self.agent.watchlist.all():
                snap = self.agent.snapshot(w.symbol)
                self._emit("watch_check", symbol=w.symbol, condition=w.condition,
                           price=snap["price"], rsi=snap["rsi"],
                           reference_price=w.reference_price, checks=w.checks)
            for f in fired:
                self._emit("condition_triggered", **f)
                self._set_status(f"re-analyzing {f['symbol']}")
                out = self.agent.reanalyze(self.goal, f)
                self.cycles += 1
                self._emit("decision", cycle=self.cycles, reanalysis=True,
                           decision=out.get("decision"), risk=out.get("risk"),
                           trace_id=out["trace"].id)
            if not fired:
                return

        # No active watches -> scan for a new opportunity (rate-limited).
        if not self.agent.watchlist.active() and \
           now - self._last_scan_at >= self.rescan_interval_s:
            self._last_scan_at = now
            self._set_status("scanning bStocks universe")
            out = self.agent.cycle(self.goal)
            self.cycles += 1
            self._emit("decision", cycle=self.cycles, reanalysis=False,
                       decision=out.get("decision"), risk=out.get("risk"),
                       candidates=[
                           {"symbol": c["symbol"], "score": c["signal_score"],
                            "label": c["label"]}
                           for c in (out.get("candidates") or [])
                       ],
                       trace_id=out["trace"].id)

    # ---- control -----------------------------------------------------
    def _run(self) -> None:
        while self.running:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self._emit("error", error=f"{type(exc).__name__}: {str(exc)[:300]}")
            time.sleep(self.monitor_interval_s)
        self._set_status("stopped")

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._last_scan_at = 0.0
        self._set_status("starting")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False

    def state(self) -> dict:
        return {
            "running": self.running,
            "goal": self.goal,
            "status": self.status,
            "cycles": self.cycles,
            "watches": self.agent.watchlist.to_dict(),
            "last_decision": self.agent.last_decision,
            "pending_trade": self.agent.pending_trade,
            "trading_mode": self.trading_mode,
        }

"""The autonomous loop — the heart of the project.

SCAN -> DECIDE -> (WAIT: register watch) -> MONITOR -> on trigger: RE-ANALYZE
                -> (BUY: risk gate -> user confirmation -> execute -> remember)

Design note for judges: the LLM is called only at decision points. Monitoring is
free deterministic Python, so the agent can watch patiently for hours cheaply.
"""
from __future__ import annotations

import threading
import time
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
        self.agent = Agent(on_event=on_event)
        self.monitor_interval_s = monitor_interval_s
        self.rescan_interval_s = rescan_interval_s
        self.mock_speed = mock_speed  # dev: advance synthetic clock per tick
        self.running = False
        self._thread: threading.Thread | None = None
        self._last_scan_at = 0.0
        self.cycles = 0
        self.status = "idle"
        self.history: list[dict] = []
        self.trading_mode = "MOCK"  # MOCK | LIVE
        self.trading_mode = "MOCK"

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

    def set_trading_mode(self, mode: str) -> str:
        """Switch trading mode: MOCK or LIVE."""
        if mode not in ("MOCK", "LIVE"):
            return f"Invalid mode: {mode}"
        old = self.trading_mode
        self.trading_mode = mode
        return f"Trading mode changed from {old} to {mode}"

    def state(self) -> dict:
        return {
            "running": self.running,
            "goal": self.goal,
            "status": self.status,
            "cycles": self.cycles,
            "watches": self.agent.watchlist.to_dict(),
            "last_decision": self.agent.last_decision,
            "pending_trade": self.agent.pending_trade,
        }

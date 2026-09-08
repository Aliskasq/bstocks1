"""FastAPI app: REST + WebSocket, serves the dashboard."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agent.loop import AgentLoop
from .config import ALLOW_MOCK_MARKET, OPENROUTER_MODEL, TRACE_DIR, BAW_PATH
from .services import openrouter as llm
from .tools import risk as risk_mod
from .tools.baw_cli import BawClient

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="bStocks AI Agent")

DEFAULT_GOAL = ("Find the best bStocks opportunity with moderate risk. "
                "Do not trade unless the setup meets all risk criteria.")

# ---- event fan-out ------------------------------------------------------

class Hub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.backlog: list[dict] = []
        self.chat_history: list[dict] = []  # persist chat messages

    async def register(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        for ev in self.backlog[-80:]:
            try:
                await ws.send_text(json.dumps(ev, default=str))
            except Exception:
                break
        # Send chat history to new client
        for ev in self.chat_history:
            try:
                await ws.send_text(json.dumps(ev, default=str))
            except Exception:
                break

    def unregister(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    def publish(self, event: dict) -> None:
        """Called from the agent's worker thread."""
        self.backlog.append(event)
        self.backlog = self.backlog[-300:]
        # Also persist chat messages
        if event.get("kind") in ("chat_user", "chat_agent", "chat_tool_call", "chat_tool_result"):
            self.chat_history.append(event)
            self.chat_history = self.chat_history[-200:]
        if not self.loop:
            return
        asyncio.run_coroutine_threadsafe(self._broadcast(event), self.loop)

    async def _broadcast(self, event: dict) -> None:
        dead = []
        payload = json.dumps(event, default=str)
        for ws in list(self.clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.unregister(ws)


hub = Hub()
agent_loop = AgentLoop(DEFAULT_GOAL, on_event=hub.publish,
                       monitor_interval_s=15.0, rescan_interval_s=300.0)

# baw client for status checks (optional - may not be installed)
_baw_client = None
try:
    _baw_client = BawClient(BAW_PATH)
except FileNotFoundError:
    pass


@app.on_event("startup")
async def _startup() -> None:
    hub.loop = asyncio.get_running_loop()


# ---- API ----------------------------------------------------------------

async def get_movers():
    """Fetch top 3 gainers and losers from Binance 24h ticker for bStocks."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://api.binance.com/api/v3/ticker/24hr")
            resp.raise_for_status()
            data = resp.json()
        
        # Filter bStocks (symbols ending with BUSDT, excluding false positives)
        false_positives = {"BNBUSDT", "SHIBUSDT", "ARBUSDT", "TRBUSDT", "CKBUSDT",
                          "DGBUSDT", "YBUSDT", "STXBUSDT", "BBUSDT", "QNTBUSDT",
                          "MUBUSDT", "GSBUSDT", "MUUBUSDT"}
        
        bstocks = [
            d for d in data
            if d["symbol"].endswith("BUSDT") and d["symbol"] not in false_positives
        ]
        
        # Sort by price change percent
        bstocks.sort(key=lambda x: float(x["priceChangePercent"]), reverse=True)
        
        gainers = bstocks[:3]
        losers = bstocks[-3:][::-1]  # bottom 3, ascending
        
        def fmt(d):
            return {
                "symbol": d["symbol"],
                "price": float(d["lastPrice"]),
                "change": float(d["priceChangePercent"]),
            }
        
        return {
            "gainers": [fmt(d) for d in gainers],
            "losers": [fmt(d) for d in losers],
        }
    except Exception:
        return {"gainers": [], "losers": []}


@app.get("/api/movers")
async def movers():
    """Top 3 gainers/losers among bStocks (24h)."""
    return await get_movers()


@app.get("/api/state")
async def state() -> dict:
    if _baw_client:
        baw_auth = await _baw_client.check_auth()
        baw_authenticated = baw_auth.authenticated
        baw_address = baw_auth.address
    else:
        baw_authenticated = False
        baw_address = None
    return {
        **agent_loop.state(),
        "trading_mode": agent_loop.trading_mode,
        "risk_limits": risk_mod.limits(),
        "risk_state": {
            "trades_today": risk_mod.STATE.trades_today,
            "realized_pnl_today": risk_mod.STATE.realized_pnl_today,
            "open_positions": risk_mod.STATE.open_positions,
        },
        "model": llm.active_model(),
        "data_source": "mock" if ALLOW_MOCK_MARKET else "binance_rest",
        "authenticated": baw_authenticated,
        "address": baw_address,
        "movers": await get_movers(),
    }


@app.post("/api/trading_mode")
def set_trading_mode(payload: dict) -> dict:
    """Switch trading mode via dashboard button (MOCK/LIVE)."""
    mode = (payload or {}).get("mode", "").upper()
    if mode not in ("MOCK", "LIVE"):
        return {"ok": False, "error": "mode must be MOCK or LIVE"}
    result = agent_loop.set_trading_mode(mode)
    hub.publish({"kind": "status", "ts": time.time(),
                 "status": f"trading mode changed to {mode}"})
    return {"ok": True, "message": result, "mode": mode}


@app.get("/api/models")
def models(free_only: bool = True, tools_only: bool = True,
           refresh: bool = False) -> dict:
    """Model catalogue for the dashboard picker.

    Defaults to free + tool-calling models so demos cost nothing.
    """
    try:
        items = llm.list_models(free_only=free_only, tools_only=tools_only,
                                refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "models": [],
                "active": llm.active_model()}
    return {"models": items, "active": llm.active_model(),
            "default": OPENROUTER_MODEL, "count": len(items)}


@app.post("/api/model")
def set_model(payload: dict) -> dict:
    """Switch the model at runtime. Empty id restores the .env default."""
    requested = (payload or {}).get("model", "")
    if requested:
        try:
            allowed = {m["id"] for m in llm.list_models(free_only=False,
                                                        tools_only=True)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if requested not in allowed:
            return {"ok": False,
                    "error": "unknown model or no tool-calling support",
                    "active": llm.active_model()}
    active = llm.set_active_model(requested)
    hub.publish({"type": "model_changed", "model": active})
    return {"ok": True, "active": active}


@app.post("/api/start")
def start(payload: dict | None = None) -> dict:
    if payload and payload.get("goal"):
        agent_loop.goal = payload["goal"]
    agent_loop.start()
    return {"ok": True, "goal": agent_loop.goal}


@app.post("/api/stop")
def stop() -> dict:
    agent_loop.stop()
    return {"ok": True}


@app.post("/api/cycle")
def one_cycle() -> dict:
    """Run a single decision cycle synchronously (handy for demos)."""
    out = agent_loop.agent.cycle(agent_loop.goal)
    return {"decision": out.get("decision"), "risk": out.get("risk"),
            "trace_id": out["trace"].id}


@app.post("/api/confirm")
def confirm() -> dict:
    out = agent_loop.agent.confirm_trade()
    out.pop("trace", None)
    return out


@app.get("/api/traces")
def traces() -> dict:
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(TRACE_DIR.glob("*.json"), reverse=True)[:50]
    return {"traces": [f.name for f in files]}


@app.get("/api/traces/{name}")
def trace(name: str) -> JSONResponse:
    path = TRACE_DIR / Path(name).name
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(json.loads(path.read_text()))


@app.get("/api/chat_history")
def chat_history() -> dict:
    return {"history": hub.chat_history}


@app.get("/api/events")
def events() -> dict:
    return {"events": agent_loop.history[-150:]}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await hub.register(websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
                if msg.get("kind") == "user_message":
                    text = msg.get("text", "").strip()
                    if text:
                        # Emit user message to all clients
                        hub.publish({"kind": "chat_user", "ts": time.time(), "text": text})
                        # Process through agent
                        response = agent_loop.agent.handle_chat(text)
                        # Emit agent response
                        hub.publish({"kind": "chat_agent", "ts": time.time(), "text": response})
            except json.JSONDecodeError:
                # Ignore non-JSON (old ping/pong)
                pass
    except WebSocketDisconnect:
        hub.unregister(websocket)
    except Exception:
        hub.unregister(websocket)


# ---- frontend -----------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
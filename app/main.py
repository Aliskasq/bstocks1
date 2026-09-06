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

    async def register(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        for ev in self.backlog[-80:]:
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
        "risk_limits": risk_mod.limits(),
        "risk_state": {
            "trades_today": risk_mod.STATE.trades_today,
            "realized_pnl_today": risk_mod.STATE.realized_pnl_today,
            "open_positions": risk_mod.STATE.open_positions,
        },
        "model": llm.active_model(),
        "data_source": "mock" if ALLOW_MOCK_MARKET else "binance_rest",
        "baw_authenticated": baw_authenticated,
        "baw_address": baw_address,
    }


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


@app.get("/api/events")
def events() -> dict:
    return {"events": agent_loop.history[-150:]}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await hub.register(websocket)
    try:
        while True:
            await websocket.receive_text()
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
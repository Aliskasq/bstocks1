# bStocks AI Agent

**Autonomous market research & trading agent for Binance bStocks — Binance Agent OS Track A.**

You don't tell it what to buy. You give it a **goal** and **risk limits** — it decides
what to research, which tools to call, when to wait, and when to refuse.

---

## Why this is an agent, not a bot

A trading bot is a rule evaluated on a tick:

```
price crosses EMA  →  BUY
```

This agent runs a loop with judgement in it:

```
GOAL
 ↓
SCAN universe            (deterministic Python pre-filter, no LLM cost)
 ↓
CHOOSE what to inspect   (the model picks its own tools from 17 available)
 ↓
CALCULATE indicators     (Python does the math, the model never invents numbers)
 ↓
RECALL memory            (past similar setups + how they resolved)
 ↓
REASON  →  BUY / WAIT / AVOID
 ↓
RISK GATE                (deterministic Python code, can veto the model)
 ↓
USER CONFIRMATION
 ↓
EXECUTE via baw CLI (DEX on BSC)  →  VERIFY  →  REMEMBER OUTCOME
 ↓
MONITOR condition        (patience as observable state)
 ↓
on trigger: RE-ANALYZE from scratch
```

The interesting behaviour is **refusal**. In a real run the agent scored SOXL at 65,
then declined to buy:

> "RSI 68, price near Bollinger upper band (0.85), volume spike but momentum exhausting.
> Memory shows 3/5 similar overbought setups pulled back 3-5% before continuing.
> Waiting for pullback."
>
> → `WAIT`, monitoring for `pullback_3_percent`

A bot would have bought: the condition it was told to wait for was satisfied.
The agent understood that a satisfied condition is not the same as a valid setup.

Full machine-readable evidence for every cycle lives in [`traces/`](traces/).

---

## Architecture

```
                    ┌──────────────────────────┐
   Browser  ◄──WS───┤  FastAPI  (app/main.py)  │
   dashboard        └────────────┬─────────────┘
                                 │
                    ┌────────────▼─────────────┐
                    │   AgentLoop (loop.py)    │
                    │  scan · monitor · react  │
                    └────────────┬─────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
       Scanner/Signals       Memory            Risk Manager
       (universe.py)       (SQLite)             (risk.py)
              │                  │                  │
              └──────────────────┼──────────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │  OpenRouter (Nemotron)   │  ← tool-calling only
                    └────────────┬─────────────┘
                                 │ requests tools
              ┌──────────────────┴──────────────────┐
              ▼                                     ▼
     Local Python tools                    Binance Agent OS (baw CLI)
   indicators · volume · memory          baw (Agentic Wallet)
   signal score · risk limits                       │
                                                    ▼
                                          DEX on BSC (PancakeSwap)
                                          bStocks: NVDAB, TSLAB, SOXL...
```

**Design rule: the model reasons, Python decides what is permitted.** The LLM can
request a trade; it cannot set position size, bypass the daily loss limit, or execute.

---

## Binance Agent OS integration

This agent uses **baw CLI (Binance Agentic Wallet)** — the official Agent OS wallet
for DeFi/DEX operations. It connects to Binance via OAuth 2.1 + PKCE (allowlisted
by Binance) and executes **spot swaps on BSC (PancakeSwap)** for bStock tokens.

| bStock | BSC Contract Address |
|---|---|
| NVDAB | `0x02Fca66C1D1aFB4E2A7884261eB00F63598a7436` |
| TSLAB | `0x5b1910eAaD6450E50f816082Aa078C41F10C292f` |
| SOXL-B | `0x...` (discovered at runtime) |

**Why baw, not MCP?**
- MCP endpoint `https://agent.binance.com/mcp/agentic` requires OAuth with a
  Client ID Metadata Document at a public HTTPS URL
- Dynamic Client Registration is NOT supported (POST /register → 404)
- Custom `client_id` not in Binance allowlist → MCP access denied
- **baw CLI is the only allowlisted client** — it works out of the box

See [`docs/AGENT_OS_AUTH.md`](docs/AGENT_OS_AUTH.md) for full OAuth discovery details.

---

## The five things that make it agentic

| Capability | Where | Why it matters |
|---|---|---|
| Goal-driven, not ticker-driven | `agent/prompts.py` | user never names a symbol |
| Own tool selection (17 tools) | `agent/agent.py` | model picks what it needs |
| Memory that is *used* | `tools/memory.py` | past setups injected, cited in reasoning |
| Patience as state | `agent/monitor.py` | WAIT becomes a checkable condition |
| Refusal | `tools/risk.py` | deterministic veto outside the model |

### Memory

Setups get a coarse **fingerprint** (`ema_cross|rsi:bucket|vol:bucket|mom:bucket`)
so similar past situations can be retrieved cheaply, along with their realized
outcomes and win rate. The agent references retrieved rows by id in its reasoning.

### Monitor conditions

The model may only emit machine-checkable conditions:

`pullback_<N>_percent` · `breakout_above_<price>` · `rsi_below_<N>` · `volume_normalizes`

These are parsed and evaluated in pure Python every tick — so the agent can watch
patiently for hours at **zero token cost**. The LLM is invoked only at decision points.

### Risk manager

Six deterministic checks, each with a human-readable reason surfaced in the UI:
max position, max leverage, trades per day, daily loss floor, no pyramiding,
positive size. Re-checked **again at execution time**, not just at decision time.

---

## Dashboard

Single page, live over WebSocket:

- goal input, agent status, data source, auth state
- ranked opportunities from the deterministic pre-filter
- current decision with confidence, reasoning and recalled memory
- risk panel with per-check PASS/FAIL and a loud `REJECTED BY RISK MANAGER` state
- monitoring panel showing what the agent is waiting for and how long
- activity log
- **decision trace viewer** — every tool call, argument and result per cycle
- **natural language chat** — ask about symbols, confirm trades, change goal, check portfolio

---

## Quick Start

```bash
git clone https://github.com/Aliskasq/bstocks1.git
cd bstocks1
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env → add OPENROUTER_API_KEY

# Optional: for LIVE trading
# curl -fsSL https://raw.githubusercontent.com/binance-agentic-wallet/baw/main/install.sh | bash
# baw auth signin

uvicorn app.main:app --host 0.0.0.0 --port 8080
# Dashboard: http://localhost:8080
```

**Public URL (free):** `cloudflared tunnel --url http://localhost:8080`

Full install guide: [`INSTALL.md`](INSTALL.md)

---

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `OPENROUTER_API_KEY` | OpenRouter API key | **required** |
| `OPENROUTER_MODEL` | LLM model (free + tools) | `nvidia/nemotron-3-ultra-550b-a55b:free` |
| `BAW_PATH` | Path to baw binary | `baw` |
| `MAX_POSITION_USD` | Hard position cap | 50 |
| `MAX_DAILY_LOSS_USD` | Daily realized loss floor | 10 |
| `MAX_TRADES_PER_DAY` | Trade count cap | 3 |
| `MAX_LEVERAGE` | Leverage cap | 2 |
| `ALLOW_MOCK_MARKET` | `1` = synthetic candles (dev only) | 0 |

---

## 17 Tools available to the LLM

| Tool | Purpose |
|---|---|
| `discover_universe` | All bStocks on Binance (BUSDT pairs) |
| `quick_scan` | Top candidates by volume |
| `get_rsi` / `get_ema` / `get_macd` / `get_atr` / `get_volume` / `get_structure` / `get_bollinger` / `get_stochastic` / `get_adx` / `get_all_indicators` | Technical indicators |
| `get_memory` | Recall similar past setups + outcomes |
| `get_risk_limits` | Current risk limits + today's usage |
| `get_dex_quote` | DEX quote: USDT → bStock on BSC |
| `get_dex_balances` | USDT + bStock balances in baw wallet |

---

## Layout

```
app/
├── agent/       loop.py · agent.py · monitor.py · prompts.py
├── tools/       baw_cli.py · baw_dex.py · market_data.py
│               universe.py · indicators.py · risk.py · memory.py
├── services/    openrouter.py
├── main.py      FastAPI + WebSocket
├── trace.py     Decision trace recorder
├── config.py    .env loader
└── run_loop.py  CLI loop runner
frontend/        Single-page dashboard + chat
docs/            AGENT_OS_AUTH.md
traces/          JSON traces per cycle (evidence for judges)
```

**Dependencies:** `fastapi`, `uvicorn`, `httpx`, `python-dotenv` — installs in seconds.

---

## Safety

- Risk limits enforced in Python, never by the model.
- Trades require explicit user confirmation before execution.
- Risk re-validated at execution time.
- Trading via baw CLI → Agentic sub-account, minimal OAuth scopes.
- Secrets never in repo (`.env`, `~/.baw/` git-ignored, 0600 perms).

**Hackathon prototype. Not financial advice.**

## License

MIT
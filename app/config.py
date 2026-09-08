"""Central config loaded from environment (.env)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-opus-5")
OPENROUTER_URL = os.environ.get("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
MOCK_LLM = os.environ.get("MOCK_LLM", "") == "1"  # Mock LLM for demo when API is rate-limited

# Binance CEX API (for bStocks spot trading)
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
BINANCE_BASE_URL = "https://api.binance.com"

# baw CLI settings (for DEX/DeFi, wallet)
BAW_PATH = os.environ.get("BAW_PATH", "baw")  # path to baw binary

# Risk limits: $6 per trade, max 3 trades/day, max $10 daily loss
MAX_POSITION_USD = _num("MAX_POSITION_USD", 6)
MAX_DAILY_LOSS_USD = _num("MAX_DAILY_LOSS_USD", 10)
MAX_TRADES_PER_DAY = int(_num("MAX_TRADES_PER_DAY", 3))
MAX_LEVERAGE = _num("MAX_LEVERAGE", 2)

ALLOW_MOCK_MARKET = os.environ.get("ALLOW_MOCK_MARKET", "1") == "1"  # Default to mock for dev

# Trading mode: MOCK or LIVE (via baw DEX on BSC)
def get_trading_mode() -> str:
    import json
    path = ROOT / "app" / "config" / "trading_mode.json"
    if path.exists():
        try:
            return json.loads(path.read_text()).get("mode", "MOCK")
        except Exception:
            pass
    return "MOCK"

def set_trading_mode(mode: str) -> None:
    import json
    path = ROOT / "app" / "config" / "trading_mode.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mode": mode.upper()}))

TRACE_DIR = ROOT / "traces"
DB_PATH = ROOT / "agent.db"

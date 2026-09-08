# bStocks AI Agent — Полная инструкция по запуску (для чайников)

> **Проект:** bstocks1 — автономный агент для bStocks на Binance Agent OS (Track A хакатона)
> **Репозиторий:** https://github.com/Aliskasq/bstocks1
> **Дедлайн:** 2026-09-08 23:59 UTC

---

## 🎯 Что делает этот агент

- **Сканирует** все bStocks на Binance (80+ символов: NVDABUSDT, TSLABUSDT, SOXL-BUSDT...)
- **Анализирует** технические индикаторы (RSI, MACD, EMA, Bollinger, Volume, ADX...)
- **Помнит** прошлые сетапы (SQLite) и цитирует их в рассуждениях
- **Оценивает риски** (хард-лимиты: макс. позиция $50, макс. убыток $10/день, макс. 3 сделки/день)
- **Отказывается** от плохих сделок (WAIT/AVOID с обоснованием через память)
- **Торгует** через **baw CLI** (DEX на BSC — PancakeSwap) — официальный способ в Agent OS
- **Дашборд + Чат** в браузере (WebSocket, real-time)

---

## 📋 Требования

| Что | Версия | Зачем |
|---|---|---|
| **Python** | 3.10+ | Основной рантайм |
| **Git** | любой | Клонирование репо |
| **OpenRouter API Key** | — | LLM (Nemotron-3-Ultra free) |
| **baw CLI** | v1.9+ | Кошелёк Binance Agent OS (DEX на BSC) |
| **Cloudflared** | опционально | Публичный туннель для дашборда |

---

## 🚀 Быстрый старт (5 минут)

```bash
# 1. Клонируем репо
git clone https://github.com/Aliskasq/bstocks1.git
cd bstocks1

# 2. Создаём виртуальное окружение
python3 -m venv venv
source venv/bin/activate  # Linux/macOS
# venv\Scripts\activate   # Windows

# 3. Ставим зависимости
pip install -r requirements.txt

# 4. Настраиваем .env
cp .env.example .env
# Отредактируй .env — вставь свой OPENROUTER_API_KEY

# 5. Запускаем дашборд
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Открываешь в браузере: **http://localhost:8080** — дашборд работает! 🎉

---

## 🔧 Подробная настройка

### 1. OpenRouter API Key (обязательно)

1. Зарегистрируйся на https://openrouter.ai
2. Создай API Key: https://openrouter.ai/keys
3. Вставь в `.env`:
```bash
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxx
OPENROUTER_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free
```

> **Важно:** Используем бесплатную модель `nvidia/nemotron-3-ultra-550b-a55b:free` — у неё есть tool-calling, демо стоит $0.

### 2. baw CLI — Binance Agentic Wallet (обязательно для LIVE торговли)

**Установка (Linux/macOS/WSL):**
```bash
curl -fsSL https://raw.githubusercontent.com/binance-agentic-wallet/baw/main/install.sh | bash
```

**Проверка:**
```bash
baw --version
# должен вывести: baw version 1.9.x
```

**Авторизация (один раз, браузер откроется):**
```bash
baw auth signin
```
1. Откроется браузер → Binance login → разрешить Agentic Wallet
2. Вернёшься в терминал → `Logged in!`
3. Ключи сохранятся в `~/.baw/` (зашифрованные)

**Проверка кошелька:**
```bash
baw wallet status
baw wallet address --json
```
Должен показать EVM адрес (0x...) и Solana адрес.

> **Баланс:** Для LIVE торговли нужен USDT на BSC (адрес EVM из `baw wallet address`).
> Для демо/хакатона — **не обязательно**, работает MOCK режим.

### 3. Конфигурация .env (важно)

```bash
# .env — НИКОГДА не коммить в git!
OPENROUTER_API_KEY=sk-or-v1-...        # твой ключ OpenRouter
OPENROUTER_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free
BAW_PATH=baw                            # путь к baw (обычно в PATH)
MAX_POSITION_USD=50                     # макс. размер позиции
MAX_DAILY_LOSS_USD=10                   # стоп-лосс по дневному PnL
MAX_TRADES_PER_DAY=3                    # макс. сделок в день
MAX_LEVERAGE=2                          # макс. плечо
ALLOW_MOCK_MARKET=0                     # 0 = живые данные Binance REST, 1 = мок
```

**Режимы торговли (переключаются в дашборде кнопками MOCK/LIVE):**
- **MOCK** (по умолчанию) — симуляция, деньги не тратятся, идеально для демо
- **LIVE** — реальные DEX свапы на BSC через baw (нужен USDT баланс)

---

## 🌐 Дашборд — локально и публично

### Локально (после `uvicorn app.main:app --host 0.0.0.0 --port 8080`)

| URL | Что там |
|---|---|
| http://localhost:8080 | Главный дашборд |
| http://localhost:8080/api/state | JSON состояние агента |
| http://localhost:8080/api/models | Список моделей OpenRouter |
| http://localhost:8080/api/traces | Список трейзов решений |
| ws://localhost:8080/ws | WebSocket для чата/логов |

**На дашборде:**
- **Goal** — цель агента (редактируется, кнопка Start/Stop)
- **Model** — выбор LLM (free + tool-calling)
- **Candidates** — топ возможностей по объёму
- **Risk Manager** — 6 гейтов PASS/FAIL
- **Decision** — текущее решение с confidence, reason, memory
- **Monitoring** — активные watches (pullback, rsi_below, breakout...)
- **Activity Log** — лог циклов
- **Trace Viewer** — развернуть любой трейз (все tool calls + LLM + risk)
- **Chat** — естественный язык: «анализ NVDAB», «подтверди сделку», «портфель», «смени цель на...»

---

### Публичный доступ через Cloudflare Tunnel (бесплатно, без домена)

**Установка cloudflared:**
```bash
# Linux
wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared-linux-amd64.deb

# macOS
brew install cloudflared

# Windows
# https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation
```

**Запуск туннеля (в отдельном терминале):**
```bash
cloudflared tunnel --url http://localhost:8080 --no-autoupdate
```

**Вывод будет примерно таким:**
```text
2026-09-07T12:34:56Z INF Your quick Tunnel has been created! Visit it at:
https://random-words-123.trycloudflare.com
```

**Открывай эту ссылку в браузере** — дашборд доступен из интернета! 🌍

> Туннель работает, пока запущен cloudflared. Для постоянного — настрой systemd service.

---

## 🤖 Запуск автономного Loop (для накопления трейзов)

**Вариант 1: Через API дашборда (рекомендуется)**
1. Открой дашборд
- Введи Goal (или оставь дефолтный)
- Нажми **Start agent**
- Агент начнёт циклы: Scan → Decide → Monitor → Re-analyze

**Вариант 2: CLI (фоновый процесс)**
```bash
# В папке проекта с активированным venv
nohup python3 -c "
from app.agent.loop import AgentLoop
loop = AgentLoop('Find the best bStocks opportunity with moderate risk.')
loop.start()
import time
while True: time.sleep(3600)
" > agent.log 2>&1 &
```

**Проверка что работает:**
```bash
curl http://localhost:8080/api/state | jq .running
# должен вернуть: true

curl http://localhost:8080/api/state | jq .cycles
# будет расти: 1, 2, 3...
```

**Посмотреть логи:**
```bash
tail -f agent.log
# или через дашборд — Activity Log + Trace Viewer
```

---

## 💬 Работа с Чатом (WebSocket)

В дашборде внизу — **Chat with Agent**. Пиши естественно:

| Что написать | Что сделает агент |
|---|---|
| `анализ NVDAB` | Вызовет `analyze_symbol("NVDAB")` — вернёт RSI, MACD, тренд, объём |
| `что думаешь про TSLAB?` | То же для TSLAB |
| `портфель` | `get_portfolio()` — позиции, риск, watches |
| `подтверди сделку` / `approve` | `confirm_trade()` — исполнит pending BUY |
| `отмени сделку` / `reject` | `reject_trade()` — отменит pending |
| `смени цель на найди oversold bStocks` | `change_goal()` |
| `включи live mode` | `set_trading_mode("LIVE")` |
| `выключи live mode` | `set_trading_mode("MOCK")` |

> Агент получает **полный контекст** (goal, mode, pending_trade, watches, risk_state, last_decision) и сам решает, какой tool вызвать.

---

## 📁 Структура проекта (что где)

```
bstocks1/
├── .env                    # СЕКРЕТЫ (не в git!)
├── .env.example            # Шаблон
├── requirements.txt        # fastapi, uvicorn, httpx, python-dotenv
├── run_loop.py             # Упрощённый запуск loop
├── README.md               # Описание проекта
├── PLAN.md                 # План хакатона
├── INSTALL.md              # Этот файл
├── oauth-client.json       # Client ID Metadata Document (для MCP, не используется сейчас)
├── agent.db                # SQLite память (создаётся автоматически)
├── traces/                 # JSON трейзы каждого цикла (для демо/жюри)
├── uvicorn.log             # Лог сервера
├── app/
│   ├── main.py             # FastAPI + WS endpoints
│   ├── config.py           # Загрузка .env, константы
│   ├── trace.py            # Trace recorder
│   ├── agent/
│   │   ├── loop.py         # AgentLoop: SCAN→DECIDE→MONITOR→RE-ANALYZE
│   │   ├── agent.py        # LLM tool-calling, 17 tools, memory, risk, DEX
│   │   ├── prompts.py      # SYSTEM + DECISION/REANALYSIS templates
│   │   └── monitor.py      # WatchList: pullback_N%, rsi_below_N, breakout...
│   ├── tools/
│   │   ├── baw_cli.py      # Wrapper над baw CLI (wallet, balance, swap, limit)
│   │   ├── baw_dex.py      # DEX bStocks на BSC: quotes, swaps, balances
│   │   ├── market_data.py  # Binance REST API (public, no auth)
│   │   ├── universe.py     # discover_bstocks, quick_scan, indicators
│   │   ├── indicators.py   # RSI, EMA, MACD, ATR, BB, Stoch, ADX, volume
│   │   ├── risk.py         # 6 hard gates (deterministic, outside LLM)
│   │   └── memory.py       # SQLite: save/search/outcome_stats
│   └── services/
│       └── openrouter.py   # Tool-calling loop, model registry
└── frontend/
    └── index.html          # Single-page dashboard + chat
```

---

## ✅ Чек-лист перед сабмитом хакатона

- [ ] Репо публичный на GitHub: `https://github.com/Aliskasq/bstocks1`
- [ ] `README.md` с архитектурой, скриншотами, "why not a bot" таблицей
- [ ] `INSTALL.md` (этот файл) — полная инструкция
- [ ] Дашборд доступен публично через Cloudflare Tunnel
- [ ] Агент отработал 10+ циклов (есть трейзы в `traces/`)
- [ ] Есть моменты **WAIT** и **AVOID** с обоснованием через память
- [ ] Риск-панель показывает PASS/FAIL
- [ ] Чат отвечает на естественный язык
- [ ] 90-секундное демо-видео: дашборд → чат → WAIT → память → риск → BUY/WAIT
- [ ] Сабмит до **2026-09-08 23:59 UTC**

---

## 🐛 Типичные проблемы и решения

| Проблема | Решение |
|---|---|
| `ModuleNotFoundError: fastapi` | `pip install -r requirements.txt` внутри venv |
| `OPENROUTER_API_KEY not set` | Проверь `.env` — ключ должен быть без кавычек |
| `baw: command not found` | Установи baw CLI, проверь `which baw` |
| `401 Unauthorized` от OpenRouter | Ключ невалиден или кончился лимит — смени ключ в `.env` |
| Дашборд не открывается | Проверь `uvicorn` логи, порт 8080 свободен? |
| Cloudflare не работает | `cloudflared tunnel --url http://localhost:8080` в отдельном терминале |
| Агент не стартует | Нажми кнопку **Start agent** на дашборде или POST `/api/start` |
| Чат не отвечает | Проверь WebSocket (`ws://localhost:8080/ws`), консоль браузера |

---

## 🔐 Безопасность

- **`.env` в `.gitignore`** — секреты никогда не попадают в репо
- **baw ключи в `~/.baw/`** — зашифрованные, права 0600
- **Risk Manager на Python** — LLM не может обойти лимиты
- **User confirmation** — каждый BUY требует явного одобрения
- **Agentic sub-account** — изолирован от основного аккаунта Binance

---

## 📞 Поддержка

- **Issues:** https://github.com/Aliskasq/bstocks1/issues
- **Binance Agent OS Docs:** https://agent.binance.com
- **baw CLI:** https://github.com/binance-agentic-wallet/baw
- **OpenRouter:** https://openrouter.ai/docs

---

**Удачи на хакатоне! 🏆** 

> "Give it a goal and risk limits, not a ticker — it decides what to research, which tools to call, when to wait, and when to refuse."
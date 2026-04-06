# BITCOINSONT15

**Paper trading bot for Polymarket BTC Up/Down 15-minute markets.**

Trades automatically on every 15-minute BTC window using three independent signal strategies, Kelly Criterion position sizing, and a circuit breaker for loss protection. All trades are simulated (paper mode) — no real funds are ever at risk.

---

## What it does

- Connects to Binance WebSocket for real-time BTC/USD tick data
- Scans Polymarket for the active BTC Up/Down 15-minute market each window
- Generates signals using three strategies (Momentum, Mean Reversion, MACD Cross)
- Sizes positions via Kelly Criterion (25% fractional, capped at 5% of bankroll)
- Simulates trades with realistic orderbook prices and 1.5% taker fee
- Displays a live terminal dashboard with ASCII price chart, signals, and trade history
- Sends Telegram alerts on trade execution, resolution, and daily summaries

---

## Setup

### 1. Clone and install

```bash
cd ~/Desktop/BITCOINSONT15
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your values:

```env
# Optional: Telegram alerts
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

# Trading config (defaults work fine)
PAPER_MODE=true
INITIAL_BANKROLL=100.0
MIN_CONFIDENCE=0.60
MAX_POSITION_PCT=0.05
```

**Telegram is optional.** The bot runs fully without it.

To get a Telegram bot token: message [@BotFather](https://t.me/BotFather) on Telegram.
To get your chat ID: message [@userinfobot](https://t.me/userinfobot).

### 3. Run locally

```bash
python main.py
```

---

## Deploy to Railway

1. Push this folder to a GitHub repository
2. Create a new project on [Railway](https://railway.app)
3. Connect your GitHub repo
4. Add environment variables from `.env` in the Railway dashboard
5. Deploy — Railway uses the `Dockerfile` automatically

The bot will restart automatically on crash (`restartPolicyType = "always"`).

---

## Module descriptions

| File | Description |
|------|-------------|
| `main.py` | Entry point. Orchestrates the async event loop, spawns all tasks |
| `config.py` | Loads all environment variables from `.env` |
| `database.py` | SQLite helpers for `trades.db` — save, update, query trades |
| `market_scanner.py` | Builds deterministic Polymarket slugs, fetches YES/NO token IDs, handles window rollover |
| `market_data.py` | Binance WebSocket consumer. Buffers candles, calculates RSI/MACD/VWAP, maintains tick history |
| `signal_engine.py` | Three strategies (Momentum, Mean Reversion, MACD Cross) fused by majority vote |
| `risk_manager.py` | Kelly Criterion sizing, position caps, circuit breaker (5 losses → 45 min pause) |
| `paper_trader.py` | Simulates trade entry/exit with realistic prices, fees, and BTC close price resolution |
| `dashboard.py` | Rich terminal UI: live BTC price, ASCII chart, signal panel, trade history |
| `telegram_alerts.py` | Sends Telegram messages on trades, losses, circuit breaker, and daily summaries |

---

## Reading the dashboard

```
┌─────────────────────────── HEADER ───────────────────────────────┐
│  ₿ BITCOINSONT15   [PAPER TRADING]   Bankroll: $102.45 (+$2.45) │
└───────────────────────────────────────────────────────────────────┘
┌── BTC/USD ──────┬──── Price Chart (15min) ──────────┬── Signals ──┐
│  $83,412.50     │  $84k ┤                     ╱─●   │ Window: ... │
│  ▲ +$82 (+0.1%) │       │              ╱─────╱       │ Time: 12:34 │
│  ▲ +0.02% 1min  │  $83k ┤─────╲──────╱               │ ⬆ UP        │
│  HIGH $83,500   │       │·····╲·····················  │ Conf: 67%   │
│  LOW  $83,200   │  $82k ┤      ╲──────               │ Mom ✓ MACD ✓│
│  VOL  0.421 BTC │       └────────────────────────    │ CB: OK      │
└─────────────────┴──────────────────────────────────  └─────────────┘
┌─────────────────────── Recent Trades ───────────────────────────────┐
│ Trades: 12  WinRate: 58.3%  P&L: +$4.21  Best: +$2.10  Worst: -$1.50│
│ Window  Dir   Conf  Result  P&L    Bankroll                          │
│ 14:00   UP    67%   WIN ✓   +$2.10 $102.10                          │
│ 13:45   DOWN  67%   LOSS ✗  -$1.50 $100.00                          │
└──────────────────────────────────────────────────────────────────────┘
```

**Price panel**: Current BTC price, delta from window open, 1-minute delta, window high/low, volume, RSI, VWAP.

**Chart panel**: ASCII line chart of BTC price for the current 15-minute window. The dotted horizontal line marks the opening price (reference for UP/DOWN resolution). Green = price above open, red = below.

**Signals panel**: Current window slug, time remaining with progress bar, active signal direction, confidence bar, per-strategy vote status, circuit breaker state.

**Footer**: Cumulative stats + last 8 trades table with color-coded results.

---

## Strategy details

| Strategy | Trigger | Direction |
|----------|---------|-----------|
| **Momentum** | `|delta| > 0.3%` and RSI 25–75 | Continuation |
| **Mean Reversion** | `|delta in first 5min| > 1.2%` | Reversal |
| **MACD Cross** | MACD line crosses signal line in last 3 candles | Cross direction |

All three vote. A trade fires only when **≥2 agree** and **confidence ≥ 60%**.

---

## Risk controls

- **Kelly Criterion** (25% fractional) — bet size scales with edge estimate
- **Hard cap**: never more than 5% of bankroll per trade
- **Minimum bet**: $2.50 (skip if bankroll too low)
- **Circuit breaker**: 5 consecutive losses → pause 3 windows (45 minutes)

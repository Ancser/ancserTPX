# ancserTPX

English | [繁體中文](README_ZH.md)

ancserTPX is an NQ (Nasdaq 100 E-mini) futures auto-trading system built on **TopstepX (ProjectX API)**.

The system is centered around **1-minute candles** and supports both backtesting and live monitoring / trading.

---

## Strategy Overview

The main strategy is **Session Trend Follow**. It builds Volume Profile zones (VAH / VAL / POC) for each market session, then looks for entries after a breakout.

| Session | Time (ET) | UTC |
|---------|-----------|-----|
| ASIA | 6:00 PM - 3:00 AM | 22:00 - 07:00 |
| EURO | 3:00 AM - 7:00 AM | 07:00 - 11:00 |
| PRE | 7:00 AM - 9:30 AM | 11:00 - 13:30 |
| RTH | 9:30 AM - 4:00 PM | 13:30 - 20:00 |
| AH | 4:00 PM - 6:00 PM | 20:00 - 22:00 |

Current default trading settings:

- **Entry**: after 5 consecutive 1-minute closes outside VAH / VAL, place a limit order at the 50% retracement
- **Stop Loss**: 50 ticks
- **Take Profit**: 150 ticks
- **Trail SL**: 5 ticks
- **Flatten**: all positions are closed daily at 12:45 PM PT

---

## Before You Start

### 1. ProjectX API Setup

1. Go to https://dashboard.projectx.com/dashboard and enable ProjectX API Access
2. Link your TopstepX account
3. Go to https://topstepx.com/settings
4. Open the **API** page
5. Copy your API key

### 2. Configure `.env`

Create a `.env` file in the project root:

- `TOPSTEPX_USERNAME` — your TopstepX account email
- `TOPSTEPX_API_KEY` — your ProjectX API key

```env
TOPSTEPX_USERNAME=your_email@example.com
TOPSTEPX_API_KEY=your_api_key_here
```

---

## Install & Start

Run the matching files for your operating system:

### Windows

- First-time install: `install.bat`
- Start the app: `start.bat`

### macOS

- First-time install: `install.sh`
- Start the app: `start.sh`

---

## Usage

### Backtest

1. Choose a strategy or preset
2. Click **EXECUTE BACKTEST**

### Live Trading

1. Select a trading account
2. Click **GO LIVE**
3. Use **STOP** or **FLATTEN** when you want to stop trading or close positions manually

---

## License

See [LICENSE](LICENSE).

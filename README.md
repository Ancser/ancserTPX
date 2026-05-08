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

### 2. Credentials

Enter your **email** and **API key** in the Web UI's top-right **CONNECT** panel. Credentials are saved to `.env` automatically on first connect — no manual file editing needed.

### 3. TopstepX Auto OCO Preset

Live trading relies on a TopstepX **Auto OCO Bracket** preset for protective orders. The bot does not send API bracket fields with the entry order.

Recommended setup in TopstepX:

- Open [TopstepX Risk Settings](https://topstepx.com/settings?tab=risk-settings)
- Enable **Auto OCO Brackets**
- Create a preset for this bot
- **Stop Loss Order Type**: `Stop Market`
- **Take Profit Order Type**: `Limit`
- Use large enough default tick distances so the account is protected immediately after fill
- Do not use `Trailing Stop Market` for the preset SL; trailing is handled by the bot by modifying the existing Auto OCO stop order

Runtime behavior:

1. The bot sends a plain entry order.
2. After the entry fills, TopstepX creates the Auto OCO SL/TP child orders.
3. The bot waits for those child orders, selects the correct exit direction, and modifies SL/TP to the strategy-calculated prices.
4. When Trail SL triggers, the bot modifies the existing Auto OCO SL instead of placing a new stop order.

If the Auto OCO child orders are not created, live logs will show an `[AUTO OCO]` warning and the bot will not fall back to manual bracket orders. If SL/TP are still missing 5 minutes after fill, the bot flattens the position, pauses the engine, and logs the Risk Settings link above.

---

## Install & Start

Run the matching files for your operating system:

### Windows 11

- First-time install: double-click `install-Win11.bat`
- Start the app: double-click `start-Win11.bat`

> If Windows shows a SmartScreen warning, click **More info → Run anyway**.

### macOS

- First-time install: double-click `install-Mac.command`
- Start the app: double-click `start-Mac.command`

> **First launch only:** macOS will block the file with "cannot be opened because it is from an unidentified developer".
> Fix: **right-click** the file → **Open** → click **Open** in the dialog. Only needed once per file.
>
> A Terminal window will open and run automatically — you don't need to type anything.
> Keep the Terminal window open while using ancserTPX. Closing it stops the server.

---

## Usage

### Backtest

1. Choose a strategy or preset
2. Click **EXECUTE BACKTEST**

### Live Trading

1. Select a trading account
2. Confirm the TopstepX Auto OCO preset is enabled
3. Click **GO LIVE**
4. Use **STOP** or **FLATTEN** when you want to stop trading or close positions manually

---

## License

See [LICENSE](LICENSE).

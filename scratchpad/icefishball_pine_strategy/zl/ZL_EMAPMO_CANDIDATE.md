# ZL EMAPMO Research Candidate

Status: research candidate, not live-ready.

## Data

- Source: Yahoo Finance `ZL=F` continuous futures.
- Raw CSV rows: 10,154.
- Resampled 5m bars used by backtest: 10,153.
- Span UTC: 2026-04-24 04:00 to 2026-07-06 13:50.
- IS/OOS split UTC: 2026-06-12 03:25.
- This is about 2.5 months of recent data, not a 2024-2026 validation set.

## Contract Assumption

- Symbol: ZL, Soybean Oil futures.
- Quote: cents per pound.
- Contract size model: 60,000 lb.
- Point value: 600 USD per 1.00 price move.
- Tick size: 0.01.
- Tick value: 6 USD.
- Round-turn cost assumption: 3.80 USD.

## Signal

Ported from icefishball `EMAPMO`.

```text
pmo = ema(10 * ema(roc(close, 1), 100), 50)
signal = ema(pmo, 10)

long  = pmo < -0.10 and crossover(pmo, signal)
short = pmo >  0.06 and crossunder(pmo, signal)
```

Execution assumptions:

- Timeframe: 5m.
- Signal uses completed 5m candle.
- Entry is next 5m open.
- Both long and short enabled.
- All sessions enabled.
- Maximum 3 trades per trade date.

## Exit

Selected parameter: `IFB_0022`

- SL: 1.0 * ATR14.
- TP: 1.0 * ATR14.
- Max hold: 24 bars, 120 minutes.
- Opposite-signal exit: off.

## Performance

All:

- Trades: 80.
- PnL: 1928.00.
- PF: 2.2751.
- MaxDD: 179.40.
- Win rate: 62.5%.
- Expectancy: 24.10.

IS:

- Trades: 52.
- PnL: 858.40.
- PF: 1.8414.
- MaxDD: 163.60.

OOS:

- Trades: 28.
- PnL: 1069.60.
- PF: 3.1749.
- MaxDD: 179.40.

Monthly:

| Month | Trades | PnL | PF | MaxDD |
|---|---:|---:|---:|---:|
| 2026-04 | 7 | 33.40 | 1.2797 | 85.60 |
| 2026-05 | 32 | 652.40 | 2.1953 | 163.60 |
| 2026-06 | 34 | 1094.80 | 2.3733 | 179.40 |
| 2026-07 | 7 | 147.40 | 3.9718 | 49.60 |

Distribution:

- Short: 52 trades, 1032.40 PnL.
- Long: 28 trades, 895.60 PnL.
- TP: 50 trades.
- SL: 27 trades.
- Same-bar SL: 3 trades.

Session PnL:

- ASIA: 273.40.
- EURO: 462.80.
- PRE: 463.80.
- RTH: 728.00.

## Why IFB_0022

Some alternatives had higher total PnL, for example `IFB_0034`, but their IS PF
was weaker and they depended more on OOS outperformance. `IFB_0022` is the more
balanced selection:

- Highest selected balance between all-period PF, OOS PF, and low DD.
- PnL is spread across long/short and across sessions.
- Monthly PnL is positive in every available month.

The weakness is still clear: IS PF is 1.84, below the strict PF >= 2 standard.
This needs longer ZL history before any live preset decision.

## Files

- Candidate JSON: `scratchpad/icefishball_pine_strategy/zl/zl_emapmo_candidate.json`
- Full grid: `scratchpad/icefishball_pine_strategy/zl/grid_results.csv`
- Ranked grid: `scratchpad/icefishball_pine_strategy/zl/ranked.csv`
- Validated rows: `scratchpad/icefishball_pine_strategy/zl/validated.csv`
- Best trades: `scratchpad/icefishball_pine_strategy/zl/best_trades.csv`
- Monthly stats: `scratchpad/icefishball_pine_strategy/zl/best_monthly.csv`

## Re-run

```powershell
$env:PYTHONPATH='.'
$env:PYTHONIOENCODING='utf-8'
python scripts\icefishball_pine_strategy_test.py --symbol ZL --csv scratchpad\yahoo_futures_5m\ZL_5m_yahoo.csv --out scratchpad\icefishball_pine_strategy\zl
```

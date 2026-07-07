# MNQ London sweep -> HTF reversion -> Asia target

Rules:
- Asia range: 22:00-07:00 UTC.
- London/EURO sweep window: 07:00-11:00 UTC.
- Main short: sweep Asia high, completed HTF candle closes back under Asia high, target Asia low.
- Symmetric long tested only for comparison.
- Entry: next 5m open after completed HTF confirmation.

bars: 15129 2026-04-19 22:00:00+00:00 -> 2026-07-06 05:40:00+00:00
asia_days: 56
split: 2026-06-11 10:30:00+00:00
variants: 2916
validated: 0

Selected:
{
  "param_id": "LSA_0329",
  "side_mode": "short",
  "htf_minutes": 30,
  "sweep_buffer_ticks": 0,
  "reclaim_buffer_ticks": 0,
  "sl_buffer_ticks": 4,
  "min_asia_range_atr": 0.5,
  "max_sweep_atr": 3.0,
  "max_hold_bars": 48,
  "all_trades": 22,
  "all_pnl": 350.22,
  "all_max_dd": 455.7,
  "all_profit_factor": 1.3074,
  "all_win_rate": 0.2727,
  "all_expectancy": 15.919,
  "all_total_gain": 1489.56,
  "all_total_loss": -1139.34,
  "is_trades": 14,
  "is_pnl": 198.64,
  "is_max_dd": 327.68,
  "is_profit_factor": 1.3031,
  "is_win_rate": 0.2857,
  "is_expectancy": 14.189,
  "is_total_gain": 854.04,
  "is_total_loss": -655.4,
  "oos_trades": 8,
  "oos_pnl": 151.58,
  "oos_max_dd": 455.7,
  "oos_profit_factor": 1.3132,
  "oos_win_rate": 0.25,
  "oos_expectancy": 18.947,
  "oos_total_gain": 635.52,
  "oos_total_loss": -483.94,
  "wf_positive": false
}

ALL: trades=22 pnl=350.22 pf=1.31 dd=455.70 expect=15.92 win=27.3%
IS: trades=14 pnl=198.64 pf=1.30 dd=327.68 expect=14.19 win=28.6%
OOS: trades=8 pnl=151.58 pf=1.31 dd=455.70 expect=18.95 win=25.0%

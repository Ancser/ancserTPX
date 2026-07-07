# Icefishball Pine strategy test - MES

Indicator signals converted to strategy assumptions:
- EMAPMO green/bottom = long, red/top = short.
- KDJMA green R = long, red R = short.
- Signal on completed 5m bar, entry at next 5m open.
- Exits use ATR SL/TP grid plus optional opposite-signal exit.

bars: 13312 2026-04-24 04:05:00+00:00 -> 2026-07-06 14:10:00+00:00
split: 2026-06-12 14:15:00+00:00
variants: 1296
validated: 4

Selected:
{
  "param_id": "IFB_0894",
  "signal_name": "kdjma",
  "side_mode": "both",
  "session_set": "ALL",
  "sl_atr": 1.5,
  "tp_atr": 1.0,
  "max_hold_bars": 12,
  "exit_on_opposite": false,
  "max_trades_per_day": 3,
  "all_trades": 149,
  "all_pnl": 330.24,
  "all_max_dd": 173.49,
  "all_profit_factor": 1.2149,
  "all_win_rate": 0.6174,
  "all_expectancy": 2.216,
  "all_total_gain": 1867.17,
  "all_total_loss": -1536.93,
  "is_trades": 103,
  "is_pnl": 236.03,
  "is_max_dd": 173.49,
  "is_profit_factor": 1.2332,
  "is_win_rate": 0.6214,
  "is_expectancy": 2.292,
  "is_total_gain": 1248.14,
  "is_total_loss": -1012.11,
  "oos_trades": 46,
  "oos_pnl": 94.21,
  "oos_max_dd": 128.64,
  "oos_profit_factor": 1.1795,
  "oos_win_rate": 0.6087,
  "oos_expectancy": 2.048,
  "oos_total_gain": 619.03,
  "oos_total_loss": -524.82,
  "wf_positive": true
}

ALL: trades=149 pnl=330.24 pf=1.21 dd=173.49 expect=2.22 win=61.7%
IS: trades=103 pnl=236.03 pf=1.23 dd=173.49 expect=2.29 win=62.1%
OOS: trades=46 pnl=94.21 pf=1.18 dd=128.64 expect=2.05 win=60.9%

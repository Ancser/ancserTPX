# Icefishball Pine strategy test - ZL

Indicator signals converted to strategy assumptions:
- EMAPMO green/bottom = long, red/top = short.
- KDJMA green R = long, red R = short.
- Signal on completed 5m bar, entry at next 5m open.
- Exits use ATR SL/TP grid plus optional opposite-signal exit.

bars: 10153 2026-04-24 04:00:00+00:00 -> 2026-07-06 13:50:00+00:00
split: 2026-06-12 03:25:00+00:00
variants: 1296
validated: 38

Selected:
{
  "param_id": "IFB_0022",
  "signal_name": "emapmo_normal",
  "side_mode": "both",
  "session_set": "ALL",
  "sl_atr": 1.0,
  "tp_atr": 1.0,
  "max_hold_bars": 24,
  "exit_on_opposite": false,
  "max_trades_per_day": 3,
  "all_trades": 80,
  "all_pnl": 1928.0,
  "all_max_dd": 179.4,
  "all_profit_factor": 2.2751,
  "all_win_rate": 0.625,
  "all_expectancy": 24.1,
  "all_total_gain": 3440.0,
  "all_total_loss": -1512.0,
  "is_trades": 52,
  "is_pnl": 858.4,
  "is_max_dd": 163.6,
  "is_profit_factor": 1.8414,
  "is_win_rate": 0.6346,
  "is_expectancy": 16.508,
  "is_total_gain": 1878.6,
  "is_total_loss": -1020.2,
  "oos_trades": 28,
  "oos_pnl": 1069.6,
  "oos_max_dd": 179.4,
  "oos_profit_factor": 3.1749,
  "oos_win_rate": 0.6071,
  "oos_expectancy": 38.2,
  "oos_total_gain": 1561.4,
  "oos_total_loss": -491.8,
  "wf_positive": true
}

ALL: trades=80 pnl=1928.00 pf=2.28 dd=179.40 expect=24.10 win=62.5%
IS: trades=52 pnl=858.40 pf=1.84 dd=163.60 expect=16.51 win=63.5%
OOS: trades=28 pnl=1069.60 pf=3.17 dd=179.40 expect=38.20 win=60.7%

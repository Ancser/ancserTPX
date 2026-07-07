# Icefishball Pine strategy test - GC

Indicator signals converted to strategy assumptions:
- EMAPMO green/bottom = long, red/top = short.
- KDJMA green R = long, red R = short.
- Signal on completed 5m bar, entry at next 5m open.
- Exits use ATR SL/TP grid plus optional opposite-signal exit.

bars: 13420 2026-04-24 04:00:00+00:00 -> 2026-07-06 13:50:00+00:00
split: 2026-06-12 12:05:00+00:00
variants: 1296
validated: 4

Selected:
{
  "param_id": "IFB_1082",
  "signal_name": "kdjma",
  "side_mode": "long",
  "session_set": "PRE_RTH",
  "sl_atr": 1.5,
  "tp_atr": 1.0,
  "max_hold_bars": 6,
  "exit_on_opposite": false,
  "max_trades_per_day": 3,
  "all_trades": 74,
  "all_pnl": 7978.78,
  "all_max_dd": 2645.01,
  "all_profit_factor": 1.3661,
  "all_win_rate": 0.5946,
  "all_expectancy": 107.821,
  "all_total_gain": 29772.83,
  "all_total_loss": -21794.05,
  "is_trades": 49,
  "is_pnl": 5713.81,
  "is_max_dd": 2645.01,
  "is_profit_factor": 1.396,
  "is_win_rate": 0.6327,
  "is_expectancy": 116.608,
  "is_total_gain": 20142.23,
  "is_total_loss": -14428.42,
  "oos_trades": 25,
  "oos_pnl": 2264.97,
  "oos_max_dd": 2056.61,
  "oos_profit_factor": 1.3075,
  "oos_win_rate": 0.52,
  "oos_expectancy": 90.599,
  "oos_total_gain": 9630.6,
  "oos_total_loss": -7365.63,
  "wf_positive": true
}

ALL: trades=74 pnl=7978.78 pf=1.37 dd=2645.01 expect=107.82 win=59.5%
IS: trades=49 pnl=5713.81 pf=1.40 dd=2645.01 expect=116.61 win=63.3%
OOS: trades=25 pnl=2264.97 pf=1.31 dd=2056.61 expect=90.60 win=52.0%

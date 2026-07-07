# Icefishball Pine strategy test - MNQ

Indicator signals converted to strategy assumptions:
- EMAPMO green/bottom = long, red/top = short.
- KDJMA green R = long, red R = short.
- Signal on completed 5m bar, entry at next 5m open.
- Exits use ATR SL/TP grid plus optional opposite-signal exit.

bars: 15129 2026-04-19 22:00:00+00:00 -> 2026-07-06 05:40:00+00:00
split: 2026-06-11 10:30:00+00:00
variants: 1296
validated: 2

Selected:
{
  "param_id": "IFB_0038",
  "signal_name": "emapmo_normal",
  "side_mode": "both",
  "session_set": "ALL",
  "sl_atr": 1.5,
  "tp_atr": 1.5,
  "max_hold_bars": 6,
  "exit_on_opposite": false,
  "max_trades_per_day": 3,
  "all_trades": 59,
  "all_pnl": 697.84,
  "all_max_dd": 468.1,
  "all_profit_factor": 1.3479,
  "all_win_rate": 0.5254,
  "all_expectancy": 11.828,
  "all_total_gain": 2703.56,
  "all_total_loss": -2005.72,
  "is_trades": 39,
  "is_pnl": 383.14,
  "is_max_dd": 468.1,
  "is_profit_factor": 1.2829,
  "is_win_rate": 0.5128,
  "is_expectancy": 9.824,
  "is_total_gain": 1737.7,
  "is_total_loss": -1354.56,
  "oos_trades": 20,
  "oos_pnl": 314.7,
  "oos_max_dd": 236.98,
  "oos_profit_factor": 1.4833,
  "oos_win_rate": 0.55,
  "oos_expectancy": 15.735,
  "oos_total_gain": 965.86,
  "oos_total_loss": -651.16,
  "wf_positive": true
}

ALL: trades=59 pnl=697.84 pf=1.35 dd=468.10 expect=11.83 win=52.5%
IS: trades=39 pnl=383.14 pf=1.28 dd=468.10 expect=9.82 win=51.3%
OOS: trades=20 pnl=314.70 pf=1.48 dd=236.98 expect=15.73 win=55.0%

# ZL 200m momentum + 5m reversion

Research-only. Signal is completed 5m bar, entry is next 5m open.
200m momentum is exactly 40 completed 5m bars in the default grid.
ATR-mixed risk modes: atr, atr_blend, fixed_atr_blend, max_fixed_atr.

symbol: ZL, point_value: 600.0, tick_size: 0.01
bars: 10153 2026-04-24 04:00:00+00:00 -> 2026-07-06 13:50:00+00:00
split: 2026-06-12 03:25:00+00:00
variants: 6144
risk_modes: ['atr', 'atr_blend', 'fixed_atr_blend', 'max_fixed_atr']
fixed_base: 0.08071408952985491
validated: 0

Selected row:
{
  "param_id": "M200R5_3378",
  "mom_lookback_bars": 40,
  "mom_threshold": 0.7,
  "rev_span": 12,
  "rev_threshold": 0.8,
  "risk_mode": "atr_blend",
  "sl_atr": 1.5,
  "tp_atr": 1.25,
  "sl_fixed": 0.08071408952985491,
  "tp_fixed": 0.08071408952985491,
  "max_hold_bars": 6,
  "confirm_turn": true,
  "mean_exit": false,
  "session_set": "RTH",
  "max_trades_per_day": 3,
  "all_trades": 5,
  "all_pnl": 281.0,
  "all_max_dd": 45.8,
  "all_profit_factor": 4.2827,
  "all_win_rate": 0.6,
  "all_expectancy": 56.2,
  "all_total_gain": 366.6,
  "all_total_loss": -85.6,
  "is_trades": 4,
  "is_pnl": 86.8,
  "is_max_dd": 45.8,
  "is_profit_factor": 2.014,
  "is_win_rate": 0.5,
  "is_expectancy": 21.7,
  "is_total_gain": 172.4,
  "is_total_loss": -85.6,
  "oos_trades": 1,
  "oos_pnl": 194.2,
  "oos_max_dd": 0.0,
  "oos_profit_factor": 999.0,
  "oos_win_rate": 1.0,
  "oos_expectancy": 194.2,
  "oos_total_gain": 194.2,
  "oos_total_loss": 0.0,
  "wf_positive": false
}

ALL: trades=5 pnl=281.00 pf=4.28 dd=45.80 expect=56.20 win=60.0%
IS: trades=4 pnl=86.80 pf=2.01 dd=45.80 expect=21.70 win=50.0%
OOS: trades=1 pnl=194.20 pf=999.00 dd=0.00 expect=194.20 win=100.0%
bootstrap expectancy ci: [0.0, 0.0], prob_positive=0.0

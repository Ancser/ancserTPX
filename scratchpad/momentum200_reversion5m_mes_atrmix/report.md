# MES 200m momentum + 5m reversion

Research-only. Signal is completed 5m bar, entry is next 5m open.
200m momentum is exactly 40 completed 5m bars in the default grid.
ATR-mixed risk modes: atr, atr_blend, fixed_atr_blend, max_fixed_atr.

symbol: MES, point_value: 5.0, tick_size: 0.25
bars: 13312 2026-04-24 04:05:00+00:00 -> 2026-07-06 14:10:00+00:00
split: 2026-06-12 14:15:00+00:00
variants: 6144
risk_modes: ['atr', 'atr_blend', 'fixed_atr_blend', 'max_fixed_atr']
fixed_base: 4.321428571428571
validated: 0

Selected row:
{
  "param_id": "M200R5_4193",
  "mom_lookback_bars": 40,
  "mom_threshold": 0.7,
  "rev_span": 12,
  "rev_threshold": 1.1,
  "risk_mode": "atr_blend",
  "sl_atr": 1.5,
  "tp_atr": 1.5,
  "sl_fixed": 4.321428571428571,
  "tp_fixed": 4.321428571428571,
  "max_hold_bars": 6,
  "confirm_turn": true,
  "mean_exit": false,
  "session_set": "RTH",
  "max_trades_per_day": 1,
  "all_trades": 3,
  "all_pnl": 108.78,
  "all_max_dd": 0.0,
  "all_profit_factor": 999.0,
  "all_win_rate": 1.0,
  "all_expectancy": 36.26,
  "all_total_gain": 108.78,
  "all_total_loss": 0.0,
  "is_trades": 2,
  "is_pnl": 66.27,
  "is_max_dd": 0.0,
  "is_profit_factor": 999.0,
  "is_win_rate": 1.0,
  "is_expectancy": 33.135,
  "is_total_gain": 66.27,
  "is_total_loss": 0.0,
  "oos_trades": 1,
  "oos_pnl": 42.51,
  "oos_max_dd": 0.0,
  "oos_profit_factor": 999.0,
  "oos_win_rate": 1.0,
  "oos_expectancy": 42.51,
  "oos_total_gain": 42.51,
  "oos_total_loss": 0.0,
  "wf_positive": false
}

ALL: trades=3 pnl=108.78 pf=999.00 dd=0.00 expect=36.26 win=100.0%
IS: trades=2 pnl=66.27 pf=999.00 dd=0.00 expect=33.13 win=100.0%
OOS: trades=1 pnl=42.51 pf=999.00 dd=0.00 expect=42.51 win=100.0%
bootstrap expectancy ci: [0.0, 0.0], prob_positive=0.0

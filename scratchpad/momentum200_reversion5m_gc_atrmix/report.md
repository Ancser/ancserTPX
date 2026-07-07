# GC 200m momentum + 5m reversion

Research-only. Signal is completed 5m bar, entry is next 5m open.
200m momentum is exactly 40 completed 5m bars in the default grid.
ATR-mixed risk modes: atr, atr_blend, fixed_atr_blend, max_fixed_atr.

symbol: GC, point_value: 100.0, tick_size: 0.1
bars: 13420 2026-04-24 04:00:00+00:00 -> 2026-07-06 13:50:00+00:00
split: 2026-06-12 12:05:00+00:00
variants: 6144
risk_modes: ['atr', 'atr_blend', 'fixed_atr_blend', 'max_fixed_atr']
fixed_base: 5.192923409598214
validated: 16

Selected row:
{
  "param_id": "M200R5_0034",
  "mom_lookback_bars": 40,
  "mom_threshold": 0.4,
  "rev_span": 12,
  "rev_threshold": 0.8,
  "risk_mode": "atr",
  "sl_atr": 1.0,
  "tp_atr": 0.75,
  "sl_fixed": 5.192923409598214,
  "tp_fixed": 5.192923409598214,
  "max_hold_bars": 12,
  "confirm_turn": false,
  "mean_exit": true,
  "session_set": "PRE_RTH",
  "max_trades_per_day": 3,
  "all_trades": 63,
  "all_pnl": 6160.58,
  "all_max_dd": 3554.2,
  "all_profit_factor": 1.5584,
  "all_win_rate": 0.6984,
  "all_expectancy": 97.787,
  "all_total_gain": 17192.78,
  "all_total_loss": -11032.2,
  "is_trades": 42,
  "is_pnl": 3450.36,
  "is_max_dd": 3554.2,
  "is_profit_factor": 1.4405,
  "is_win_rate": 0.6667,
  "is_expectancy": 82.151,
  "is_total_gain": 11283.56,
  "is_total_loss": -7833.2,
  "oos_trades": 21,
  "oos_pnl": 2710.21,
  "oos_max_dd": 1837.6,
  "oos_profit_factor": 1.8472,
  "oos_win_rate": 0.7619,
  "oos_expectancy": 129.058,
  "oos_total_gain": 5909.21,
  "oos_total_loss": -3199.0,
  "wf_positive": true
}

ALL: trades=63 pnl=6160.58 pf=1.56 dd=3554.20 expect=97.79 win=69.8%
IS: trades=42 pnl=3450.36 pf=1.44 dd=3554.20 expect=82.15 win=66.7%
OOS: trades=21 pnl=2710.21 pf=1.85 dd=1837.60 expect=129.06 win=76.2%
bootstrap expectancy ci: [-23.006, 217.64], prob_positive=0.949

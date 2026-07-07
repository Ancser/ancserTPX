# MNQ 200m momentum + 5m reversion

Research-only. Signal is completed 5m bar, entry is next 5m open.
200m momentum is exactly 40 completed 5m bars in the default grid.

bars: 15129 2026-04-19 22:00:00+00:00 -> 2026-07-06 05:40:00+00:00
split: 2026-06-11 10:30:00+00:00
variants: 1536
validated: 2

Selected row:
{
  "param_id": "M200R5_0362",
  "mom_lookback_bars": 40,
  "mom_threshold": 0.4,
  "rev_span": 12,
  "rev_threshold": 1.1,
  "sl_atr": 1.5,
  "tp_atr": 1.5,
  "max_hold_bars": 12,
  "confirm_turn": false,
  "mean_exit": false,
  "session_set": "ALL",
  "max_trades_per_day": 3,
  "all_trades": 91,
  "all_pnl": 1245.66,
  "all_max_dd": 590.16,
  "all_profit_factor": 1.4122,
  "all_win_rate": 0.5495,
  "all_expectancy": 13.689,
  "all_total_gain": 4268.0,
  "all_total_loss": -3022.34,
  "is_trades": 69,
  "is_pnl": 1030.94,
  "is_max_dd": 374.8,
  "is_profit_factor": 1.4941,
  "is_win_rate": 0.5507,
  "is_expectancy": 14.941,
  "is_total_gain": 3117.38,
  "is_total_loss": -2086.44,
  "oos_trades": 22,
  "oos_pnl": 214.72,
  "oos_max_dd": 590.16,
  "oos_profit_factor": 1.2294,
  "oos_win_rate": 0.5455,
  "oos_expectancy": 9.76,
  "oos_total_gain": 1150.62,
  "oos_total_loss": -935.9,
  "wf_positive": true
}

ALL: trades=91 pnl=1245.66 pf=1.41 dd=590.16 expect=13.69 win=54.9%
IS: trades=69 pnl=1030.94 pf=1.49 dd=374.80 expect=14.94 win=55.1%
OOS: trades=22 pnl=214.72 pf=1.23 dd=590.16 expect=9.76 win=54.5%
bootstrap expectancy ci: [-5.215, 31.87], prob_positive=0.9225

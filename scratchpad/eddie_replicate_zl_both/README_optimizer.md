# Eddie screenshot replica research

This is a research-only approximation, not the original private Pine.

Visible clues translated into rules:
- MGC 5m style data.
- ATR stair-step regime filter, implemented as SuperTrend-like bands.
- Fib-style entries labelled S-0.382/S-0.5 or mirrored long labels.
- Optimizer compares fixed TP with ATR-regime SL against fixed SL with ATR-regime TP.
- Chronological IS/OOS validation.

source: scratchpad\yahoo_futures_5m\ZL_5m_yahoo.csv
symbol: ZL
bars: 10154 2026-04-24T04:00:00+00:00 -> 2026-07-06T13:54:43+00:00
IS: 7107 bars through 2026-06-12T03:25:00+00:00
OOS: 3047 bars after 2026-06-12T03:25:00+00:00

Output files:
- grid_results.csv: all tested variants.
- is_ranked_all.csv: all variants sorted by IS PF.
- validated_top.csv: variants passing IS/OOS filters.
- best_trades.csv: trade list for the best validated variant, or best ranked fallback.

Run examples:
python scripts/eddie_mgc_strategy_research.py --symbol MNQ
python scripts/eddie_mgc_strategy_research.py --symbol MGC --csv data/mgc_5m_2024_2026_merged.csv --csv-interval 5

Best selected row:
param_id=R0109, atr_len=10, st_mult=1.5, swing_len=24, fib=0.5, entry_mode=pullback, regime_mode=atr_sl_fixed_tp, sl_base=8.0, tp_base=16.0, atr_high_mult=1.25, is_trades=177.0, is_pnl=1697.4025634765985, is_pf=1.1238509567608377, is_max_dd=4443.204577636703, oos_trades=70.0, oos_pnl=4540.000183105475, oos_pf=1.651792489912237, oos_max_dd=3864.8064086914274

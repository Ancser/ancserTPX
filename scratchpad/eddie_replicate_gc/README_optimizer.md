# Eddie screenshot replica research

This is a research-only approximation, not the original private Pine.

Visible clues translated into rules:
- MGC 5m style data.
- ATR stair-step regime filter, implemented as SuperTrend-like bands.
- Fib-style entries labelled S-0.382/S-0.5 or mirrored long labels.
- Optimizer compares fixed TP with ATR-regime SL against fixed SL with ATR-regime TP.
- Chronological IS/OOS validation.

source: scratchpad\yahoo_futures_5m\GC_5m_yahoo.csv
symbol: GC
bars: 13421 2026-04-24T04:00:00+00:00 -> 2026-07-06T13:54:39+00:00
IS: 9394 bars through 2026-06-12T12:05:00+00:00
OOS: 4027 bars after 2026-06-12T12:05:00+00:00

Output files:
- grid_results.csv: all tested variants.
- is_ranked_all.csv: all variants sorted by IS PF.
- validated_top.csv: variants passing IS/OOS filters.
- best_trades.csv: trade list for the best validated variant, or best ranked fallback.

Run examples:
python scripts/eddie_mgc_strategy_research.py --symbol MNQ
python scripts/eddie_mgc_strategy_research.py --symbol MGC --csv data/mgc_5m_2024_2026_merged.csv --csv-interval 5

Best selected row:
param_id=R0430, atr_len=14, st_mult=1.5, swing_len=24, fib=0.5, entry_mode=pullback, regime_mode=atr_sl_fixed_tp, sl_base=12.0, tp_base=24.0, atr_high_mult=1.5, is_trades=186.0, is_pnl=39512.98515625089, is_pf=1.376212719467486, is_max_dd=19721.629296874893, oos_trades=93.0, oos_pnl=-11573.409765624332, oos_pf=0.828338552435948, oos_max_dd=30273.83906249954

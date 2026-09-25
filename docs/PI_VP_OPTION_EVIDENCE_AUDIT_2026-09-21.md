# PI / Volume Profile / Options Evidence Audit

**Audit date:** 2026-09-21  
**Status:** bounded, retrospective audit of saved rows  
**Reproduction:** `python scripts/pi_vp_option_evidence_audit.py --compact`  
**Targeted checks:** `python -m pytest tests/test_pi_vp_option_evidence_audit.py -q`

## Findings at a glance

- `backend.data.pi_history.load_rows()` yields 487 canonical messages from 530 raw messages, with 43 filtered messages and 481 canonical signal marks. QQQ contributes 251 marks over 122 independent source dates; SPY contributes 230 marks over 118 dates.
- The canonical QQQ source-level-2 deep-blue cohort contains five marks on five dates: 2026-03-30, 2026-06-05, 2026-06-09, 2026-07-20, and 2026-07-24. Its 1-day close sign is positive on 3/5 events; its 5-day close sign is positive on 4/5.
- Across those five QQQ spot paths, mean/median 5-day return is +2.054% / +1.692%; mean/median 5-day MFE is +3.694% / +2.302%; mean/median 5-day MAE is -2.324% / -2.995%.
- All five focus marks pass the saved snapshot-time check for both OI- and volume-derived gamma rows. This establishes `option_as_of <= PI source ts` on the same New York date. Publication lineage and PI `received_at` are unavailable, so the join remains timestamp-eligible and retrospective; point-in-time availability is unverified.
- The saved MNQ/Volume Profile context report contains 22 baseline trades over 17 dates. Nine trade rows have a timestamp-eligible GEX snapshot; 13 remain unmatched. Saved rows are baseline trades only, so subgroup results provide descriptive attribution.
- The available local option data and timestamps leave next-day option entry/exit P&L unmeasured. Underlying returns remain spot-path evidence.

## Inputs and canonical PI inventory

The audit calls the canonical loader and existing path/context helpers. Source files:

| Evidence | Path |
|---|---|
| PI message archive | `F:\ancserQuant\ancserMarketData\source\discord\pi\pi_signals.json` |
| QQQ one-minute underlying bars | `F:\ancserQuant\ancserMarketData\source\options\qqq_option_ml\raw\YYYY-MM-DD\qqq_ohlcv_1m.csv.gz` |
| Saved option-wall / value-area gamma snapshots | `F:\ancserQuant\ancserMarketData\source\options\qqq_option_ml\option_wall_value_area_gamma_rows.csv.gz` |
| Saved VP/GEX report | `F:\ancserQuant\ancserMarketData\derived\research\volume_profile_context_mnq_mbo_gex.json` |
| Saved VP/GEX trade rows | `F:\ancserQuant\ancserMarketData\derived\research\volume_profile_context_mnq_mbo_gex.csv` |

`load_rows()` reports 530 raw messages, 487 canonical messages, 43 filtered messages, and 481 signal marks. The canonical symbol totals are QQQ 251 and SPY 230. Mark counts by symbol / source level / kind:

| Symbol | Source level | Kind | Marks |
|---|---:|---|---:|
| QQQ | 1 | 淡蓝圈 | 57 |
| QQQ | 1 | 深蓝圈 | 1 |
| QQQ | 1 | 紫圈 | 92 |
| QQQ | 2 | 深蓝圈 | 5 |
| QQQ | 2 | 紫圈 | 14 |
| QQQ | 3 | 粉π | 48 |
| QQQ | 3 | 青π | 34 |
| SPY | 1 | 淡蓝圈 | 61 |
| SPY | 1 | 紫圈 | 85 |
| SPY | 2 | 深蓝圈 | 7 |
| SPY | 2 | 紫圈 | 17 |
| SPY | 3 | 粉π | 39 |
| SPY | 3 | 青π | 21 |

The PI source `ts` spans 2026-03-05 16:28:00 UTC through 2026-09-14 16:51:46.941 UTC. `discord_timestamp` is present. `received_at` is absent; message receipt time and live availability remain unaudited.

### Exact canonical exclusion behind the prior six-row count

The earlier raw count of six QQQ level-2 deep-blue marks includes one multi-mark aggregate row that `load_rows()` removes as a whole. The excluded message is QQQ on **2026-07-02**, source timestamp **2026-07-02 17:06 UTC (13:06 ET)**, message ID `1547162787925594112`. It carries both **淡蓝圈, source level 1** and **深蓝圈, source level 2**. The loader classifies it as `multi_mark_aggregate`; both marks are excluded from the canonical set. Its 13:06 ET source time places it during the regular session. A separate SPY deep-blue level-2 mark later that date remains outside the QQQ count.

## QQQ spot-path outcomes for canonical level-2 deep-blue marks

The existing path study selects the next eligible one-minute QQQ bar after each PI source timestamp. It reports one-, two-, three-, and five-session horizons. First-touch classification uses thresholds at reference price × (1 ± 0.50%), with reference price taken from the existing study's entry rule. Under the OHLC method, bars touching both thresholds remain ambiguous because intrabar ordering is unavailable.

| Horizon | Events / independent dates | Positive close | Mean return | Median return | Mean MFE | Median MFE | Mean MAE | Median MAE | First down / up / ambiguous / neither |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 day | 5 / 5 | 3 / 5 | +1.048% | +0.953% | +2.133% | +1.894% | -0.824% | -0.702% | 2 / 3 / 0 / 0 |
| 2 days | 5 / 5 | 3 / 5 | +1.427% | +1.201% | +2.735% | +2.302% | -1.565% | -1.772% | 2 / 3 / 0 / 0 |
| 3 days | 5 / 5 | 2 / 5 | +0.400% | -0.697% | +2.897% | +2.302% | -1.985% | -1.772% | 2 / 3 / 0 / 0 |
| 5 days | 5 / 5 | 4 / 5 | +2.054% | +1.692% | +3.694% | +2.302% | -2.324% | -2.995% | 2 / 3 / 0 / 0 |

The underlying archive contains 208 RTH days and 80,792 one-minute bars from 2025-11-04 through 2026-09-02. The five-event sample is retrospective and small. These values describe QQQ spot paths; option premium returns and executable option P&L remain unmeasured.

## PI marks and saved gamma snapshots

The local snapshot file contains 416 rows over 208 dates, split evenly into 208 `oi` and 208 `volume` rows. Its `option_as_of` range is 2025-11-04 15:00 UTC through 2026-09-02 14:00 UTC. For the five focus PI marks, both families have five same-date rows with `option_as_of <= PI source ts`; zero rows violate that timestamp comparison.

For clarity, the existing helper's `causal_timestamp_join` status establishes the timestamp inequality and same-date match only. OI publication lineage, vendor arrival time, and PI `received_at` are unavailable. The resulting partitions are **timestamp-eligible retrospective partitions**, with point-in-time availability unverified.

All five PI marks in this cohort are long-direction marks. Descriptive five-day spot-path summaries by saved gamma label:

| Snapshot family | Label / expected descriptive mode | Marks / dates | 1-day mean return | 5-day mean return | 5-day mean MAE |
|---|---|---:|---:|---:|---:|
| OI-derived | Negative / breakout | 3 / 3 | +1.418% | +2.646% | -2.285% |
| OI-derived | Positive / consolidation | 2 / 2 | +0.492% | +1.165% | -2.383% |
| Volume-derived | Negative / breakout | 4 / 4 | +1.492% | +1.458% | -2.463% |
| Volume-derived | Positive / consolidation | 1 / 1 | -0.730% | +4.438% | -1.772% |

The mode labels follow the existing context-study convention. These small groups support exploratory description.

### Gamma semantics and leakage checks

- `oi` is a QQQ 0DTE open-interest-derived gamma proxy. Dealer-side positioning and inventory remain unobserved in the saved fields.
- `volume` is a QQQ 0DTE volume-derived proxy. Its rows encode unsigned contract volume and assumed calls-positive / puts-negative signs; this is a volume-based proxy, with dealer OI interpretation unverified.
- The source file also contains `price_30m`, `price_60m`, `price_close`, and `boundary_event_status`. Forward outcome columns are present; the existing join selects timestamp/context fields only.
- All five PI context joins satisfy the snapshot-time inequality, with zero future timestamp violations. The availability caveat above still applies.

## Saved Volume Profile / GEX trade rows

The saved report covers 22 baseline trades on 17 independent dates: 13 long, 9 short, all `breakout_retest`; 12 discovery-period and 10 evaluation-period rows. MBO context coverage spans 26 RTH dates from 2026-08-07 through 2026-09-11, with 18 of the 22 trade rows joined to MBO context.

All displayed trade statistics use the already-saved baseline rows and their saved costs. The “unmatched” row is the same baseline trade tagged unknown for that family. Excluded-row drawdown summarizes the selected saved-row subset as a descriptive statistic.

| Saved-row slice | Trades / independent dates | PnL | PF | Win rate | Max DD field |
|---|---:|---:|---:|---:|---:|
| All baseline rows | 22 / 17 | -$147.28 | 0.8754 | 45.45% | $647.42 |
| Timestamp-eligible OI-matched rows | 9 / 8 | +$146.84 | 1.3405 | 66.67% | $374.70 |
| OI-unmatched rows (unknown bucket) | 13 / 12 | -$294.12 | 0.6082 | 30.77% | $327.64 |

OI-family partition:

| OI label / descriptive mode | Trades / dates | Long PnL / n | Short PnL / n | Total PnL | PF | Win rate |
|---|---:|---:|---:|---:|---:|---:|
| Positive / consolidation | 5 / 5 | +$244.52 / 2 | +$37.28 / 3 | +$281.80 | 3.6905 | 80.00% |
| Negative / breakout | 4 / 3 | +$191.52 / 2 | -$326.48 / 2 | -$134.96 | 0.5866 | 50.00% |
| Unknown / unmatched | 13 / 12 | -$136.16 / 9 | -$157.96 / 4 | -$294.12 | 0.6082 | 30.77% |

Volume-family partition:

| Volume label / descriptive mode | Trades / dates | Long PnL / n | Short PnL / n | Total PnL | PF | Win rate |
|---|---:|---:|---:|---:|---:|---:|
| Positive / consolidation | 7 / 6 | +$275.28 / 3 | -$90.96 / 4 | +$184.32 | 1.7911 | 71.43% |
| Negative / breakout | 2 / 2 | +$160.76 / 1 | -$198.24 / 1 | -$37.48 | 0.8109 | 50.00% |
| Unknown / unmatched | 13 / 12 | -$136.16 / 9 | -$157.96 / 4 | -$294.12 | 0.6082 | 30.77% |

The family join outcomes across 22 saved trades are nine timestamp-eligible rows, seven rows whose entry predates that day's snapshot, and six rows without a same-date snapshot. Matched and unmatched slices retain identical saved baseline trade logic and cost assumptions. Saved rows provide the baseline stream; a GEX-gated counterfactual stream and paired effect estimate remain unavailable. Strategy-cell selection awaits larger paired evidence.

## Coverage blockers and interpretation

- The checked local QQQ options tree contains 0DTE one-minute CBBO and underlying one-minute bars through 2026-09-02. Nonzero-DTE chain/next-day option quotes for calculating this PI cohort's next-day entry and exit marks remain unavailable locally. The PI archive continues through 2026-09-14.
- SPY OPRA/CBBO files are absent from the checked local options tree, leaving SPY option P&L coverage unavailable.
- PI `received_at` and option-snapshot publication/arrival lineage are absent. Source-time retrospection leaves live availability unverified for both signal and snapshot.
- Underlying spot returns and option premium returns are distinct payoff series. This audit leaves option P&L, fill/slippage, and executable option backtest results unmeasured.
- The main research task owns new Theta quotes and the QQQ 2026-03-30 / 2026-03-31 pilot; those results remain outside this audit.

The existing VP/GEX rows remain exploratory context tags. This report supports factual cohort counts and saved-row summaries; strategy selection awaits paired counterfactual evidence.

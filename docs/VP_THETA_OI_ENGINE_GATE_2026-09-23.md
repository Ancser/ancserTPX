# VP × QQQ OI: full-engine gate replay

Run date: 2026-09-23. Runner: `scripts/vp_theta_oi_engine_gate.py`.

## Scope and frozen rule

The saved MNQ VP candidate was replayed through the production `BacktestEngine` and passed the existing aggregate, yearly, and 2026 trade-row parity checks. The candidate remains `breakout`, 70% prior-RTH value area, two-bar confirmation, two-tick breakout/touch buffers, ATR stop 1.5×, and ATR target 2×.

The OI rule was frozen from the prior 2021–2023 complete-chain baseline-trade sensitivity:

- QQQ unsigned 0–60 DTE OI ≤ 6,916,378.5
- Put/call OI ratio ≤ 1.64158113
- Calendar DTE 0–1 share > 0.05444089

All three conditions must pass. Vendor rows are joined only when their timestamp is at or before the candidate bar. Missing or partial chains fail closed for this OI-confirmed experiment. The gate is installed on the strategy instance for this research process only; production strategy, preset, and live settings are unchanged.

## Results

| Period | OI-gated trades / dates | PnL | PF | 14-tick PF | 14-tick PnL |
|---|---:|---:|---:|---:|---:|
| 2021–2023 calibration | 32 / 27 | +$236.32 | 1.2186 | 1.0104 | +$12.32 |
| 2024–2025 validation | 172 / 142 | +$588.22 | 1.0652 | 0.9365 | −$615.78 |
| 2026 holdout | 95 / 78 | +$245.20 | 1.0419 | 0.9323 | −$419.80 |

For reference, the full 2021–2025 VP baseline had 970 trades, −$2,194.80 PnL, and 0.9462 PF. The 2026 baseline had 141 trades, −$817.34 PnL, and 0.9144 PF.

In the 2024–2025 validation subset, the annual split reverses: 2024 retained 75 trades, +$1,269.00, PF 1.4465; 2025 retained 97 trades, −$680.78, PF 0.8898. The 2022 Tuesday/Thursday weekly-expiry listing change also affects the near-expiry-share feature.

## What the engine replay established

The engine-level gate reproduced the same retained entries as the frozen-row filter:

| Replay | Baseline entries | Gated entries | Same entry times | New/shifted entries |
|---|---:|---:|---:|---:|
| 2021–2025 | 970 | 204 | 204 | 0 |
| 2026 holdout | 141 | 95 | 95 | 0 |

Thus, allowing the engine to continue after a rejected VP candidate did not uncover later replacement entries in these samples. For 2021–2025, the research gate saw 4,351 candidate bars: 1,781 had missing/partial OI, 2,366 failed the rule, and 204 passed. For 2026, it saw 266 candidate bars: 171 failed the rule and 95 passed. These are candidate-bar counts; repeated bars can represent the same continuing setup.

## Decision

This is a useful filter sensitivity, with nominal PF above 1 in each summarized period. The 14-tick cost-stress PF falls below 1 in both later periods, and the annual split is unstable. The 2021–2023 calibration has only 32 trades. The evidence does not support promoting this rule as the highest-PF or live VP preset.

Keep this option-data rule research-only. A next search should test a small, predeclared set of regime-aware alternatives on training dates, preserve 2024–2025 as validation and 2026 as retrospective holdout, and rank by cost-stressed stability plus sample size rather than raw PF. MBO/GEX evidence remains too sparse for a five-year VP gate.

## Verification

- Existing frozen-baseline parity: passed.
- `tests/test_vp_theta_oi_engine_gate.py` and `tests/test_vp_theta_oi_5yr_study.py`: 9 passed.
- No production strategy, preset, live state, or market-data archive was changed.

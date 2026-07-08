NO EMOJI
chat in Chinese
PF larger than 2, maxDD less 2k

## Preset naming rule

All newly saved presets must use this fixed format:

```text
MM.DD MODEL #N purpose contract-and-params
```

Rules:

- `MM.DD` is the local save date, for example `06.24`.
- `MODEL` is the actual strategy/model name: `TREND`, `DAY ZONE`, or
  `DISTRIBUTION`.
- `#N` is the next number for that model on that date.


## New model training workflow

Major goal (1.0.9 revision — profit-factor first, maxDD is a gate not the rank key):

- Rank and select by **profit factor and PF stability**, not by a single
  max-drawdown number. Rationale (user, 2026-07-05): maxDD is one worst-case
  point that a lucky/trending month can keep artificially low; profit factor
  (and how steady PF stays across the walk-forward thirds) is what keeps the
  equity curve survivable when regime shifts. A high total loss is unstable the
  moment PF drifts down, even if that month's maxDD looked small.
- Acceptance — a candidate must pass ALL of:
  - profit factor `>= ~2.0`, and every walk-forward third's PF `> 1`;
  - per-trade expectancy `> 0` net of commission + fees (RR1 baseline);
  - sample `>= 80` trades;
  - plateau: neighbouring `+/-1` param still positive (no cliff);
  - total loss `< PnL` (prefer total loss `< 70%` of PnL).
- `maxDD < $2,000` stays a **hard Topstep account gate** (trailing DD), NOT the
  ranking key. A candidate that clears PF/stability but breaches `$2,000` maxDD
  is research-only until the user accepts the higher DD or scales size down. A
  low maxDD alone must never promote a low-PF setup.
- PnL is not the first-place ranking target. Treat PnL as a sizing/capacity
  result after edge quality is proven: if PF and stability are high enough,
  higher PnL can be achieved by raising contracts. Do not promote a low-PF
  setup just because its raw PnL is high.

When a full strategy/algorithm description is provided:

1. Extract every measurable condition from the description as candidate
   features. Include price-location, value-area, session, sweep, trend,
   volatility, risk, distance, timeframe, and inter-market confirmations when
   data is available.
2. Implement feature extraction in the strategy feature module, keeping feature
   names explicit and explainable.
3. Train an append-only model version. Do not replace or delete older registry
   models.
4. Run a sweep over the controllable preset parameters, including RR, probability
   gate, EV gate, max risk, band, minimum distinct timeframe, session filter,
   and exit-management knobs. Output data in batch to avoid accident interuption 
   no timeout limit
   make sure high effieicent structure
5. Rank results by **profit factor and PF stability first** (walk-forward thirds
   all `> 1`), then total-loss-to-PnL ratio, expectancy, maxDD, trade count,
   Calmar, live/backtest realism, and only then raw PnL as a secondary sizing
   signal. Enforce `maxDD < $2,000` as a hard gate that separates live
   candidates from higher-DD research candidates, but do not let a low maxDD
   alone promote a low-PF setup.
6. Show the user in a table the best 5 presets for different purposes, and also
   list qualifying presets per model: `TREND`, `DAY ZONE`, and `DISTRIBUTION`.
   If a model has no qualifying preset, say `none` and explain which gate failed.
7. Create the best practical preset(s) using the preset naming rule above.
8. Report what features mattered, what parameters were useful/useless, and
   whether the setup is ready for live testing or only for research.

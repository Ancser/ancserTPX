# ancserTPX working rules

NO EMOJI

## Version rule

When making a product version commit, bump the visible app version by one patch
number and use a commit title like:

```text
X.Y.Z feature / fix / study
```

## Preset naming rule

All newly saved presets must use this fixed format:

```text
MM.DD USER|CODEX|CLAUDE #N purpose contract-and-params
```

Rules:

- `MM.DD` is the local save date, for example `06.24`.
- Author is one of `USER`, `CODEX`, or `CLAUDE`.
- `#N` is the next number for that author on that date.
- `purpose` should be a short 4-5 character summary when possible.
- `contract-and-params` must include contract/size and all material strategy
  controls, for example:

```text
MNQx1 RR1:3 POFF R80 W1m Trail50L5 SesON ASIA B4 TF2
```

## Model naming rule

All newly trained model ids must use this fixed format:

```text
MM.DD USER|CODEX|CLAUDE #N contract-and-model-params
```

Example:

```text
06.24 CODEX #1 MNQ RR1-3 B4 TF2 W1m
```

Use `RR1-3` instead of `RR1:3` in model ids because Windows filenames cannot
contain `:`. The human description belongs in model metadata, not in the model
id.

Immutable model versions live in:

```text
data/models/registry/
```

The active live/backtest copy is:

```text
data/models/confluence_scorer.json
```

## New model training workflow

Major goal:

- For funded-account candidates, prioritize strategies with tested max drawdown
  below `$2,000`. A preset above `$2,000` maxDD can be reported as research, but
  should not be treated as live-ready unless the user explicitly accepts the
  higher drawdown.

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
5. Rank results by max drawdown first, then PnL, profit factor, Calmar, trade
   count, and live/backtest realism. Clearly separate `< $2,000 maxDD` live
   candidates from higher-DD research candidates.
6. Show the user in a table 5 best preset for different purpose
7. Create the best practical preset(s) using the preset naming rule above.
8. Report what features mattered, what parameters were useful/useless, and
   whether the setup is ready for live testing or only for research.

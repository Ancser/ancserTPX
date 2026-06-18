# Model naming and versioning

Confluence models are append-only versions. Training a new model must never
overwrite or delete an older version.

## File name

Use:

```text
YYYYMMDD_<trainer>_<one-line-description>.json
```

Rules:

- `YYYYMMDD` is the local training date.
- `<trainer>` must be `codex` or `claude`.
- `<one-line-description>` is required, limited to 120 characters, and converted
  to a filesystem-safe slug. Chinese and other Unicode letters are preserved.
- The slug is limited to 48 characters.
- If the same name already exists, append `-02`, `-03`, and so on.

Examples:

```text
20260618_codex_rr3-band4-mintf2-production-baseline.json
20260619_claude_tighter-sl-wall-buffer.json
20260619_codex_縮小止損並保留rr3.json
```

## Storage and activation

- Immutable versions: `data/models/registry/<model-id>.json`
- Active live/backtest copy: `data/models/confluence_scorer.json`
- Selecting a model copies that registry version to the active path.
- Retraining creates a new registry file and may activate it; it never replaces
  another registry version.

## Required metadata

Every saved model contains:

- `model_id`
- `trained_at`
- `trainer`
- `description`
- model configuration (`cfg`)
- training sample and quality metrics when available

The web UI and command-line trainer must both follow this rule.

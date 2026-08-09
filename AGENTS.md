# ancserTPX — Agent Entry Point

This file is a **router**, not a rulebook. The rules live in `CLAUDE.md`;
duplicating them here would create two copies that drift apart — which is the
exact failure mode this repository keeps hitting.

## Read in this order

1. `CLAUDE.md` — working rules, verification standard, research discipline
2. `docs/INVARIANTS.md` — behavioural invariants + which test proves each one
3. `docs/HANDOFF.md` — current baseline, open questions, next research

Then:

```bash
python -m pytest tests/ -q          # ~40s
```

## Source-of-truth ranking

When two sources disagree, trust them in this order:

```
1. tests/          — executable, CI-enforced
2. current code
3. docs/INVARIANTS.md
4. CLAUDE.md / docs/HANDOFF.md
5. docs/1.0.x_*.md — historical evidence, NOT current specification
6. README.md       — user-facing summary, drifts fastest
```

Old version reports (`docs/1.0.8_*`, `1.0.9_*`, `1.0.10_*`) record *what was
decided at the time and why*. They are evidence for a decision, not a
description of today's system. Several describe strategies that no longer exist.

## Rules

- Do not change trading behaviour unless the task explicitly approves it.
- Do not delete an unexplained workaround, observer, fallback, or special case.
  Before removing one, state: why it exists, which invariant it protects, how
  the replacement preserves it, and which test proves equivalence.
- Never mix an architecture refactor with a strategy/risk behaviour change.
- Do not commit unless the user asks.

## Research agent → coding agent

A coding subagent may touch production code only after the research agent has
produced all of:

```
OBSERVED            what the code does today (with file:line)
INTENDED            what it should do, and on what evidence
EVIDENCE            code, tests, live logs, or research output
INVARIANTS          which docs/INVARIANTS.md entries are affected
BEHAVIOUR CHANGE?   yes / no  (yes needs user approval)
FILES ALLOWED       explicit allowlist
TESTS               to add or change
ACCEPTANCE          how we know it worked
```

If any of these is unknown, the answer is more research, not a patch.

Scope coding tickets narrowly. `"clean up the PI architecture"` and
`"simplify LiveTradingEngine"` are how previously-working code gets deleted.

## Verification standard

`ast.parse`, `import`, and "the existing tests are green" **do not count as
verification** — all three passed on the day a block-replace silently deleted
two regexes and left the live listener guaranteed to crash on its first signal.

See the verification section of `CLAUDE.md` before claiming something works.

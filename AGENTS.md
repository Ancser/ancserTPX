# ancserTPX — Agent Entry Point

This file is a **router**, not a rulebook. The rules live in `CLAUDE.md`;
duplicating them here would create two copies that drift apart — which is the
exact failure mode this repository keeps hitting.

## Read in this order

1. `CLAUDE.md` — working rules, verification standard, research discipline
2. `docs/INVARIANTS.md` — behavioural invariants + which test proves each one
3. `docs/HANDOFF.md` — current baseline, open questions, next research
4. `docs/RESEARCH_REPORT.md` — the single research report: verdicts, closed
   directions, and what is left to test
5. `docs/QA.md` — CI failures, UI screenshots, and the first-trading-day live
   checklist after any live-affecting change

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
5. docs/RESEARCH_REPORT.md — research evidence, NOT current specification
6. README.md       — user-facing summary, drifts fastest
```

`docs/RESEARCH_REPORT.md` replaced the old per-version and per-study reports
(`docs/1.0.x_*`, dated `*_2026-09-*` files) on 2026-09-27; its appendix A maps
each old file to a section and says how to recover the original from git.
Research records *what was decided at the time and why*; it is evidence for a
decision, not a description of today's system. Add new findings to that one
report instead of creating another dated file.

## Rules

All working rules — including "don't delete an unexplained special case,"
"never mix a refactor with a behaviour change," and "don't commit unless
asked" — live in `CLAUDE.md` only. Read them there; this file does not
restate them, or the two copies would drift.

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

See the verification section of `CLAUDE.md` before claiming something works.
Passing tests and a clean `import`/`ast.parse` are not sufficient on their own.

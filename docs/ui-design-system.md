# ancserTPX UI design direction

This is the implementation note for the first six design improvements. It is
visual/product guidance only; it does not change strategy, market-data, or
execution semantics.

## Design decisions

1. **Hierarchy** — the chart is the primary surface, the left rail contains
   configuration and risk, the lower panel contains trade review/output, and
   Research owns the full realized PNL curve plus robustness analysis. Detail
   layers are tools that can be opened when needed.
2. **Layout** — the app uses one stable workspace shell: an icon-only
   workspace rail on the left, a bounded configuration rail, a flexible chart,
   and one scrolling utility panel. Account connection controls sit at the
   bottom of the workspace rail; account status and market time sit in the
   chart's upper-right status cluster. There is no separate empty title strip;
   the rail has no brand mark (removed 1.5.1 at the user's request). The configuration rail
   is 10% wider than the prior design, while `min-width: 0` and horizontal
   clipping keep expanded content from creating left/right scrollbars.
3. **Tokens** — page, rail, panel, raised, control, and chart surfaces are
   distinct. Text has primary, secondary, and muted roles. Accent, positive,
   negative, and warning colors are semantic rather than decorative.
4. **Components** — fields, buttons, switches, tabs, tables, and focus rings
   share one geometry and one interaction language. Production surfaces use
   straight edges; buttons and inputs are borderless, buttons use a lighter
   semantic surface, inputs use a darker one, section titles are numbered micro
   labels with no bar, and horizontal overflow belongs to the table container.
   Long trade-limit controls are stacked vertically so their labels remain
   readable.
5. **Density** — the existing footprint renderer remains responsible for
   overview/detail data density. The UI gives the chart visual priority and
   does not duplicate a second full-data DOM view. A compact token mode exists
   for narrow layouts.
6. **States** — loading, ready, error, and offline are explicit semantic
   states. Color supports the state, while the status text remains the source
   of meaning.

## Production palettes

The main app has two final palettes: the legacy **deep-blue** workstation
theme is the default dark theme, and **Apple Light** is the light theme. The
icon selector is in the workspace rail; the choice is stored locally and
restored before the first paint. The chart background, grid, scale borders,
crosshair, candle colors, and watermark follow the same selection.

The Live workspace exposes one main account slot. The chart legend and the
model-selection description rows are intentionally removed so the chart and
controls keep their working space. Trade history combines entry and exit into
one New York-time column, showing the date once when both events share a day.

The standalone palette demo has been retired. The production selector exposes
only the two approved palettes so the app has one clear visual language.

## Review checklist

- Can the user identify market, last price, position, and connection state in
  under two seconds?
- Is the chart still the largest visual surface?
- Can every table be read without wrapped headers?
- Does every control expose hover, focus, disabled, loading, and error states?
- Does zooming from overview to detail reveal more information without adding
  a second competing panel?

## Relayout v2 progress (2026-11-05)

Source: `F:/ancserEntertatinment/ancserAssets/ancserTPX/UI_Relayout_v2/`
(the grok bot only read the repo before it stopped). Excluded by the user:
all light/glow effects, REVEAL_LIGHT_SPEC, blurred backdrop. The v2 palette
replaces the deep-blue legacy theme.

- [x] Phase 1: v2 tokens, Inter + JetBrains Mono, no left bars / cut
  corners / glow, `.btn-primary` (one per screen) and `.btn-danger`, shared
  `formatMoney` / `formatPrice` / `sideMarkup`, long/short candles.
- [x] Phase 1 tests (1.5.1g): design-system.spec.js now pins the v2 palette
  (#0e1116 / #15181d), Inter watermark, no title bar, v2 surfaces and `+$`
  money; the theme-transition check samples in the click's own task (no race).
- [x] Phase 2a (1.5.1g) line language from Age of Dev v2
  (`docs/UI_RENEWAL_FROM_AGEOFDEV.md`): shared contracts `.micro`, `.ticks`
  (「」 top-left + bottom-right, 8px x 1px outside the box, hover +2px) and
  `.hero-btn`, used by EXECUTE BACKTEST and GO LIVE (one hero per screen).
  Backtest progress-bar glow removed. State goes in `.hero-sub` only
  (`setHeroButtonState`).
- [ ] Phase 2b page/popup four-corner ticks; page title (micro + title +
  breadcrumb, reference "RESEARCH / 科技").
- [x] Phase 2 Live (1.5.1h): FUTURES / OPTIONS pane switch (Robinhood no
  longer stacks under GO LIVE); hero DAY NET P&L; LOSS LIMIT LEFT meter
  against `TPXTopstepEval.DEFAULTS.dailyLossLimit`; POSITION / PHASE tiles;
  MODE / MBO / LOSS LOCK / VOLATILITY only while abnormal; Daily start is one
  state chip (detail line only on failure); status dots lose their glow.
- [x] Phase 3 Backtest (1.5.1h): rare parameters (`.adv`) behind one
  ADVANCED toggle that counts hidden and preset-changed fields; help "?"
  shown on field hover/focus; PERFORMANCE = hero net P&L + four key tiles +
  MORE METRICS (its warnings still surface on the summary line).
- [x] Phase 4 Research (1.5.1h): candidate cards = verdict chip + NET / PF /
  +14T + diverging net-vs-stress bars on one shared scale; chip wall and
  prose in DETAILS. Robustness · Topstep: neutral verdict, inline PASS bar,
  MLL FAIL red only when failing is likelier than passing, current size row.
- [ ] Phase 5 Settings page (no light options).
- [x] Phase 6 motion (1.5.1h): sliding selection block on bottom tabs and
  segmented switches (not an underline); digit odometer on live P&L and loss
  limit; workspace panels enter 12px up, 40ms apart (max 8); all disabled by
  `prefers-reduced-motion`. Signal card and canvas fonts remain open.

Design methods used (no installable UI skill was available): Nielsen Norman
Group progressive disclosure; Stephen Few, *Information Dashboard Design*
(glanceable hero numbers, exception-based highlighting); Edward Tufte
(data-ink, small multiples on a shared scale); the bundled `dataviz` skill
(stat-tile / hero / meter contracts, proportional figures for big numbers,
text in text tokens, diverging bars from a zero baseline).

Screenshots: `TPX_UI_SHOTS=1` + `tests/ui/ui-relayout-shots.spec.js`.

User decisions on the Age of Dev handoff (2026-11-08):

1. Product copy stays **English** (UI-026). The single exception: a hero
   button shows its Chinese name as a decorative watermark (`data-wm` of an
   aria-hidden `.wm-clip`), about 20% cut by the bottom edge. Labels are two
   English lines (micro `EXECUTE` + name `BACKTEST`; `GO` + `LIVE`).
2. Hero buttons use the **gray** button surface, not the accent fill.
3. Accent stays **cyan** (not Age of Dev orange).
4. The left rail stays an **icon rail**; no hub of entry buttons.

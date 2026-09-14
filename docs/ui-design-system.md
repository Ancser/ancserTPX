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
   the compact rail brand carries the product identity. The configuration rail
   is 10% wider than the prior design, while `min-width: 0` and horizontal
   clipping keep expanded content from creating left/right scrollbars.
3. **Tokens** — page, rail, panel, raised, control, and chart surfaces are
   distinct. Text has primary, secondary, and muted roles. Accent, positive,
   negative, and warning colors are semantic rather than decorative.
4. **Components** — fields, buttons, switches, tabs, tables, and focus rings
   share one geometry and one interaction language. Production surfaces use
   straight edges; buttons and inputs are borderless, buttons use a lighter
   semantic surface, inputs use a darker one, section titles retain only their
   left accent bar, and horizontal overflow belongs to the table container.
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

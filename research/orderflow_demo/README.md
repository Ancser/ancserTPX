# Isolated order-flow chart demo

This folder is deliberately separate from the ancserTPX runtime. It has no
frontend layer, backend route, strategy registry entry, or production import.

The generated `orderflow_2026-09-02.html` is an offline ATAS-style preview of
the purchased Databento MNQ MBO file:

- five-minute RTH footprint cells (left sell aggressor / right buy aggressor)
- per-bar delta strip
- observed displayed-depth heatmap from reconstructed MBO state
- normal candlesticks plus a right-hand selected-bar ladder with sell, buy,
  F.bid, F.ask, and delta columns
- one-minute red/green bubbles filtered by imbalance ratio, minimum print, and
  matched passive-fill threshold (default 10:1 / 100 / 100)
- focus-bar slider and cell hover readout

For one-click opening on Windows, double-click
`open_orderflow_demo.bat`. It opens the local `preview.html` without starting
the ancserTPX server.

The source DBN file remains outside the project under
`F:\ancserData\databento_2026-09-02_mnq_mbo\...`.

The passive-fill control is a strict data filter, not a directional promise:
`F.bid`/`F.ask` are resting-side fills matched by MBO. Since each trade has an
aggressor and a resting side, a low threshold can be nearly redundant; raising
it to 150 or using 20:1 removes most bubbles. The default 10:1 / 100 / 100
setting produced 26 candidate cells on this day.

Rebuild the standalone HTML with:

```powershell
python research/orderflow_demo/build_demo.py `
  --dbn F:\ancserData\databento_2026-09-02_mnq_mbo\GLBX-20260906-595Q88T8HD\glbx-mdp3-20260902.mbo.dbn.zst
```

The display is intentionally limited to 06:30–13:00 Pacific RTH and five-
minute bins so the offline file stays small and readable. The source contains
the full session MBO and is not modified.

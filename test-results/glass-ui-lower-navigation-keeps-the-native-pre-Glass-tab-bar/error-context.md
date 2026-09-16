# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: glass-ui.spec.js >> lower navigation keeps the native pre-Glass tab bar
- Location: tests\ui\glass-ui.spec.js:51:1

# Error details

```
Error: expect(locator).toHaveCount(expected) failed

Locator:  locator('html[data-tpx-glass-skin="on"]')
Expected: 1
Received: 0
Timeout:  5000ms

Call log:
  - Expect "toHaveCount" with timeout 5000ms
  - waiting for locator('html[data-tpx-glass-skin="on"]')
    14 × locator resolved to 0 elements
       - unexpected value "0"

```

# Page snapshot

```yaml
- generic [active] [ref=e1]:
  - complementary "Workspace navigation" [ref=e2]:
    - generic "ancserTPX" [ref=e3]:
      - text: a
      - generic [ref=e4]: TPX
    - navigation "Workspace" [ref=e5]:
      - button "Research" [ref=e6] [cursor=pointer]
      - button "Backtest" [ref=e10] [cursor=pointer]
      - button "Live" [ref=e14] [cursor=pointer]
    - generic [ref=e18]:
      - button "Collapse parameter panel" [expanded] [ref=e19] [cursor=pointer]
      - button "Use Apple Light theme" [ref=e23] [cursor=pointer]:
        - generic [ref=e26]: Switch color theme
      - generic [ref=e27]:
        - generic "Account connection" [ref=e28] [cursor=pointer]:
          - generic [ref=e29]: "?"
          - generic [ref=e31]: ONLINE
        - generic [ref=e32]:
          - generic [ref=e33]:
            - generic [ref=e34]:
              - text: TOPSTEP USERNAME
              - button "Show parameter help" [ref=e35]: "?"
            - textbox "your_username" [invalid] [ref=e36]
          - generic [ref=e37]:
            - generic [ref=e38]:
              - text: TOPSTEP API
              - button "Show parameter help" [ref=e39]: "?"
            - textbox "your_api_key" [invalid] [ref=e40]
          - button "CONNECT" [ref=e41] [cursor=pointer]
  - generic [ref=e42]:
    - generic [ref=e44]:
      - generic [ref=e45]: ENVIRONMENT
      - generic [ref=e46]:
        - generic [ref=e47]:
          - text: PRESET
          - button "Show parameter help" [ref=e48]: "?"
        - combobox [ref=e49]:
          - option "Default"
          - 'option "0909 PI #1 ManualTest TR80 15m RR1:3 C7 ASIA MNQx3"'
          - option "BEST"
          - option "BETAFIB BEST"
          - option "DELTA ABSORPTION BEST" [selected]
          - option "MOMENTUM BEST"
          - option "PI 2MNQ BOTH BEST"
      - generic [ref=e50]:
        - button "SAVE" [ref=e51] [cursor=pointer]
        - button "DEL" [ref=e52] [cursor=pointer]
      - generic [ref=e53]:
        - generic [ref=e54]:
          - generic [ref=e55]:
            - text: CONTRACT
            - button "Show parameter help" [ref=e56]: "?"
          - combobox [ref=e57]:
            - option "MNQ ($2/pt)" [selected]
            - option "NQ ($20/pt)"
        - generic [ref=e58]: ×
        - generic [ref=e59]:
          - generic [ref=e60]:
            - text: SIZE
            - button "Show parameter help" [ref=e61]: "?"
          - combobox [ref=e62]:
            - option "1"
            - option "2" [selected]
            - option "3"
            - option "5"
            - option "10"
      - generic [ref=e64]:
        - generic [ref=e65]:
          - text: MODEL
          - button "Show parameter help" [ref=e66]: "?"
        - combobox [ref=e67]:
          - option "FADE"
          - option "SIGMA"
          - option "FACTOR"
          - option "MOMENTUM"
          - option "BETAFIB"
          - option "PI"
          - option "OPTION WALL"
          - option "DELTA ABSORPTION" [selected]
      - generic [ref=e68]:
        - generic [ref=e69]: ENTRY
        - generic "Entry type is fixed by the selected model engine, so changing it here would make backtest/live inconsistent." [ref=e71]:
          - generic [ref=e72]:
            - text: ENTRY TRIGGER
            - button "Show parameter help" [ref=e73]: "?"
          - textbox [ref=e74]: "DELTA ABSORPTION: completed 1m MBO bar; market entry after Delta + VA condition"
        - generic [ref=e75]:
          - generic [ref=e76]:
            - generic [ref=e77]:
              - generic [ref=e78]: PATTERN
              - combobox [ref=e79]:
                - option "ABSORPTION" [selected]
                - option "EXHAUSTION"
                - option "BOTH"
            - generic [ref=e80]:
              - generic [ref=e81]: SIDE
              - combobox [ref=e82]:
                - option "LONG + SHORT" [selected]
                - option "LONG ONLY"
                - option "SHORT ONLY"
          - generic [ref=e83]:
            - generic [ref=e84]:
              - generic [ref=e85]: DELTA SOURCE
              - combobox [ref=e86]:
                - option "WHOLE RTH BAR" [selected]
                - option "OUTSIDE PRIOR VA"
            - generic [ref=e87]:
              - generic [ref=e88]: VA GATE
              - combobox [ref=e89]:
                - option "VA TOUCH" [selected]
                - option "RAW DELTA"
                - option "RECLAIM + STALL"
                - option "RECLAIM + VWAP"
          - generic [ref=e90]:
            - generic [ref=e91]:
              - generic [ref=e92]: DELTA WINDOW
              - combobox [ref=e93]:
                - option "1m"
                - option "3m"
                - option "5m" [selected]
                - option "10m"
            - generic [ref=e94]:
              - generic [ref=e95]: BASELINE
              - combobox [ref=e96]:
                - option "20m"
                - option "30m" [selected]
                - option "40m"
                - option "60m"
          - generic [ref=e97]:
            - generic [ref=e98]:
              - generic [ref=e99]: STRENGTH
              - combobox [ref=e100]:
                - option "0.75× baseline"
                - option "1× baseline" [selected]
                - option "1.25× baseline"
                - option "1.5× baseline"
                - option "2× baseline"
            - generic [ref=e101]:
              - generic [ref=e102]: WEAKENING
              - combobox [ref=e103]:
                - option "0.50"
                - option "0.70" [selected]
                - option "0.85"
          - generic [ref=e104]:
            - generic [ref=e105]:
              - generic [ref=e106]: STALL TOLERANCE
              - combobox [ref=e107]:
                - option "0 ticks"
                - option "1 tick" [selected]
                - option "2 ticks"
                - option "4 ticks"
            - generic [ref=e108]:
              - generic [ref=e109]: VA TOUCH LOOKBACK
              - combobox [ref=e110]:
                - option "5m"
                - option "10m" [selected]
                - option "20m"
                - option "30m"
          - generic [ref=e111]:
            - generic [ref=e112]:
              - generic [ref=e113]: TOUCH TOLERANCE
              - combobox [ref=e114]:
                - option "0 ticks" [selected]
                - option "1 tick"
                - option "2 ticks"
            - generic [ref=e115]:
              - generic [ref=e116]: PRIOR RTH 70% VA
              - combobox [ref=e117]:
                - option "REQUIRED" [selected]
                - option "OPTIONAL"
          - generic [ref=e118]: Completed 1m Databento MBO Delta. VAH/VAL are the previous complete RTH 70% profile; entries are RTH-only.
        - generic [ref=e119]: EXIT
        - generic [ref=e121]:
          - generic [ref=e122]:
            - generic [ref=e123]: SL ANCHOR
            - combobox [ref=e124]:
              - option "ATR"
              - option "ATR BLEND" [selected]
              - option "AREA15 %"
              - option "DAILY ATR"
              - option "FIB LEVEL"
          - generic [ref=e125]:
            - generic [ref=e126]: LONG SL
            - combobox [ref=e127]:
              - option "1 x ATR"
              - option "1.5 x ATR"
              - option "2 x ATR"
              - option "2.5 x ATR"
              - option "3 x ATR"
              - option "3.5 x ATR"
              - option "4 x ATR" [selected]
          - generic [ref=e128]:
            - generic [ref=e129]:
              - text: LONG TIME EXIT
              - button "Show parameter help" [ref=e130]: "?"
            - combobox [ref=e131]:
              - option "0 (OFF)" [selected]
              - option "60"
              - option "120"
              - option "240"
          - generic [ref=e132]:
            - generic [ref=e133]: SHORT SL
            - combobox [ref=e134]:
              - option "1 x ATR"
              - option "1.5 x ATR" [selected]
              - option "2 x ATR"
              - option "2.5 x ATR"
              - option "3 x ATR"
              - option "3.5 x ATR"
              - option "4 x ATR"
          - generic [ref=e135]:
            - generic [ref=e136]:
              - text: SHORT TIME EXIT
              - button "Show parameter help" [ref=e137]: "?"
            - combobox [ref=e138]:
              - option "0 (OFF)"
              - option "60" [selected]
              - option "120"
              - option "240"
        - generic [ref=e139]:
          - generic [ref=e140]:
            - generic [ref=e141]: TP ANCHOR
            - combobox [ref=e142]:
              - option "FIXED RATIO" [selected]
          - generic [ref=e143]:
            - generic [ref=e144]:
              - text: TP INPUT 1:3
              - button "Show parameter help" [ref=e145]: "?"
            - combobox [ref=e146]:
              - option "1 x SL"
              - option "2 x SL"
              - option "3 x SL" [selected]
              - option "4 x SL"
              - option "5 x SL"
              - option "6 x SL"
        - generic [ref=e147]:
          - generic [ref=e148]:
            - generic [ref=e149]:
              - text: TRAIL TP TRIGGER
              - button "Show parameter help" [ref=e150]: "?"
            - combobox [ref=e151]:
              - option "OFF" [selected]
              - option "30% (45t / +$45)"
              - option "50% (75t / +$75)"
              - option "70% (105t / +$105)"
          - generic [ref=e152]:
            - generic [ref=e153]:
              - text: TRAIL SL OFF 0t / $0
              - button "Show parameter help" [ref=e154]: "?"
            - combobox [ref=e155]:
              - option "OFF" [selected]
        - generic [ref=e156]: RISK MANAGEMENT
        - generic [ref=e158]:
          - generic [ref=e159]:
            - text: MAX PROFIT
            - button "Show parameter help" [ref=e160]: "?"
          - generic [ref=e161]:
            - slider [ref=e162] [cursor=pointer]: "0"
            - generic [ref=e163]: "OFF"
        - generic [ref=e164]:
          - generic [ref=e165]: DAILY MAX TRADE LIMIT
          - combobox [ref=e166]:
            - option "OFF"
            - option "1"
            - option "2"
            - option "3" [selected]
            - option "4"
        - generic [ref=e167]:
          - generic [ref=e168]:
            - text: FULL LOSS LOCK
            - button "Show parameter help" [ref=e169]: "?"
          - combobox [ref=e170]:
            - option "OFF"
            - option "1" [selected]
            - option "2"
            - option "3"
            - option "4"
            - option "5"
            - option "6"
        - generic [ref=e171]:
          - generic [ref=e172]:
            - text: FULL WIN LOCK
            - button "Show parameter help" [ref=e173]: "?"
          - combobox [ref=e174]:
            - option "OFF" [selected]
            - option "1"
            - option "2"
            - option "3"
            - option "4"
            - option "5"
            - option "6"
        - generic [ref=e176]:
          - generic [ref=e177]:
            - text: SESSION MAX TRADE LIMIT
            - button "Show parameter help" [ref=e178]: "?"
          - combobox [ref=e179]:
            - option "ON"
            - option "OFF" [selected]
      - button "EXECUTE BACKTEST" [disabled] [ref=e181]
    - generic [ref=e182]:
      - generic [ref=e183]:
        - generic: ancserTPX
        - generic "Provider latency, account, and market time":
          - generic "Provider connection latency" [ref=e184]:
            - generic "DISCORD 0 ms · empty" [ref=e185]:
              - generic "DISCORD empty" [ref=e186]
              - generic [ref=e187]: DISCORD
              - generic [ref=e188]: "0"
              - generic [ref=e189]: ms
            - generic "TOPSTEP 0 ms · empty" [ref=e190]:
              - generic "TOPSTEP empty" [ref=e191]
              - generic [ref=e192]: TOPSTEP
              - generic [ref=e193]: "0"
              - generic [ref=e194]: ms
            - generic "DATABENTO 0 ms · error" [ref=e195]:
              - generic "DATABENTO error" [ref=e196]
              - generic [ref=e197]: DATABENTO
              - generic [ref=e198]: "0"
              - generic [ref=e199]: ms
          - generic:
            - generic: "--"
            - generic: 15:17:10
        - generic [ref=e200]:
          - button "Jump to the latest candle" [ref=e201] [cursor=pointer]
          - button "Layers" [ref=e204] [cursor=pointer]
        - table [ref=e209]:
          - row [ref=e210]:
            - cell
            - cell [ref=e211]
            - cell [ref=e215]
          - row [ref=e219]:
            - cell
            - cell [ref=e220]
            - cell [ref=e224]
      - generic [ref=e227]:
        - generic "Drag to resize" [ref=e228]
        - generic [ref=e230]:
          - generic [ref=e231] [cursor=pointer]: BACKTEST TRADES
          - generic [ref=e232] [cursor=pointer]: EXECUTE TRADES
          - generic [ref=e233] [cursor=pointer]: SYSTEM LOG
        - table [ref=e237]:
          - rowgroup [ref=e238]:
            - row [ref=e239]:
              - columnheader "CONTRACT" [ref=e240]
              - columnheader "TIME" [ref=e241]
              - columnheader "DURATION" [ref=e242]
              - columnheader "ENTRY" [ref=e243]
              - columnheader "EXIT" [ref=e244]
              - columnheader "P&L" [ref=e245]
              - columnheader "COMMISSION" [ref=e246]
              - columnheader "FEES" [ref=e247]
              - columnheader "DIR" [ref=e248]
          - rowgroup
```

# Test source

```ts
  1   | const path = require("node:path");
  2   | const { expect, test } = require("@playwright/test");
  3   | 
  4   | const chartBundle = path.resolve(
  5   |   "node_modules/lightweight-charts/dist/lightweight-charts.standalone.production.js",
  6   | );
  7   | 
  8   | const canonicalModels = [
  9   |   ["fade", "FADE"],
  10  |   ["sigma", "SIGMA"],
  11  |   ["factor", "FACTOR"],
  12  |   ["momentum", "MOMENTUM"],
  13  |   ["betafib", "BETAFIB"],
  14  |   ["pi", "PI"],
  15  |   ["optionwall", "OPTION WALL"],
  16  |   ["delta_absorption", "DELTA ABSORPTION"],
  17  | ];
  18  | 
  19  | async function openApp(page) {
  20  |   await page.addInitScript(() => {
  21  |     localStorage.setItem("ancserTPXTheme", "light");
  22  |   });
  23  |   await page.route("**/*", async (route) => {
  24  |     const url = new URL(route.request().url());
  25  |     if (url.hostname === "127.0.0.1") {
  26  |       await route.continue();
  27  |       return;
  28  |     }
  29  |     if (url.hostname === "unpkg.com" && url.pathname.endsWith(
  30  |       "/lightweight-charts.standalone.production.js",
  31  |     )) {
  32  |       await route.fulfill({
  33  |         path: chartBundle,
  34  |         contentType: "application/javascript",
  35  |       });
  36  |       return;
  37  |     }
  38  |     await route.abort("blockedbyclient");
  39  |   });
  40  | 
  41  |   await page.goto("/", { waitUntil: "domcontentloaded" });
> 42  |   await expect(page.locator('html[data-tpx-glass-skin="on"]')).toHaveCount(1);
      |                                                                ^ Error: expect(locator).toHaveCount(expected) failed
  43  |   await expect.poll(() => page.evaluate(() => (
  44  |     window.TpxGlass?.diagnostics?.components?.precision || 0
  45  |   ))).toBeGreaterThanOrEqual(2);
  46  |   await expect.poll(() => page.locator("#preset-bt option").count())
  47  |     .toBeGreaterThan(1);
  48  |   await settleTwoFrames(page);
  49  | }
  50  | 
  51  | test("lower navigation keeps the native pre-Glass tab bar", async ({ page }) => {
  52  |   await openApp(page);
  53  | 
  54  |   const topNavigation = page.locator(".glass-topbar > .header-tabs");
  55  |   const lowerNavigation = page.locator("#bottom-panel > .bottom-tabs");
  56  |   const activeLowerTab = lowerNavigation.locator(".bottom-tab.active");
  57  | 
  58  |   // Only the upper workspace navigation is a Liquid Glass dock. The lower
  59  |   // panel intentionally keeps the original flat underline-tab treatment.
  60  |   await expect(topNavigation).toHaveClass(/glass-dock/);
  61  |   await expect(lowerNavigation).not.toHaveClass(/glass-segment/);
  62  |   await expect(lowerNavigation.locator(":scope > .segment-indicator")).toHaveCount(0);
  63  |   await expect(lowerNavigation.locator(":scope > .control-container-glass")).toHaveCount(0);
  64  |   await expect(lowerNavigation.locator(".bottom-tab > .control-source-content")).toHaveCount(0);
  65  | 
  66  |   const style = await activeLowerTab.evaluate((tab) => {
  67  |     const tabStyle = getComputedStyle(tab);
  68  |     const barStyle = getComputedStyle(tab.parentElement);
  69  |     return {
  70  |       barDisplay: barStyle.display,
  71  |       barRadius: barStyle.borderRadius,
  72  |       tabBorderWidth: tabStyle.borderBottomWidth,
  73  |       tabBorderStyle: tabStyle.borderBottomStyle,
  74  |     };
  75  |   });
  76  |   expect(style).toEqual({
  77  |     barDisplay: "flex",
  78  |     barRadius: "0px",
  79  |     tabBorderWidth: "2px",
  80  |     tabBorderStyle: "solid",
  81  |   });
  82  | });
  83  | 
  84  | test("Execute Trades refreshes broker truth when the tab is opened", async ({ page }) => {
  85  |   await openApp(page);
  86  | 
  87  |   const requests = [];
  88  |   await page.route("**/api/live/trade-history**", async (route) => {
  89  |     requests.push(route.request().url());
  90  |     await route.fulfill({
  91  |       contentType: "application/json",
  92  |       body: JSON.stringify({
  93  |         source: "api",
  94  |         count: 1,
  95  |         account_id: 22373660,
  96  |         by_account: [],
  97  |         trades: [{
  98  |           trade_id: "3010999438_3011352573",
  99  |           account_id: 22373660,
  100 |           contract_id: "CON.F.US.MNQ.U26",
  101 |           direction: "buy",
  102 |           size: 1,
  103 |           entry_time: "2026-08-20T18:11:03.104848+00:00",
  104 |           exit_time: "2026-08-20T19:45:04.445174+00:00",
  105 |           entry_price: 29216.0,
  106 |           exit_price: 29308.75,
  107 |           gross_pnl: 185.5,
  108 |           pnl: 184.26,
  109 |           commission: 0.5,
  110 |           fees: 0.74,
  111 |           exit_reason: "tp",
  112 |           source: "topstep",
  113 |         }],
  114 |       }),
  115 |     });
  116 |   });
  117 | 
  118 |   const executeTab = page.locator('.bottom-tab[data-btab="execute"]').first();
  119 |   const executeBox = await executeTab.boundingBox();
  120 |   expect(executeBox).not.toBeNull();
  121 |   await page.mouse.click(
  122 |     executeBox.x + executeBox.width / 2,
  123 |     executeBox.y + executeBox.height / 2,
  124 |   );
  125 | 
  126 |   await expect.poll(() => requests.some((url) => (
  127 |     new URL(url).searchParams.get("refresh") === "true"
  128 |   ))).toBe(true);
  129 |   const firstTrade = page.locator("#execute-tbody tr").first();
  130 |   await expect(firstTrade.locator("td")).toHaveCount(9);
  131 |   await expect(firstTrade).not.toContainText("999438");
  132 |   await expect(firstTrade).toContainText("29216.00");
  133 |   await expect(firstTrade).toContainText("$+185.50");
  134 | 
  135 |   requests.length = 0;
  136 |   await page.evaluate(() => {
  137 |     _lastExecuteHistoryPassiveRequestMs = 0;
  138 |     refreshVisibleExecuteTrades(false);
  139 |   });
  140 |   await expect.poll(() => requests.some((url) => (
  141 |     new URL(url).searchParams.get("refresh") === null
  142 |   ))).toBe(true);
```
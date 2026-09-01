/* 1.0.10p: the RESEARCH robustness panel must render from the backend.
 *
 * Monte Carlo, walk-forward, series stats and slip injection moved out of
 * ancserTPX.js into backend/backtest/robustness.py. The static guard in
 * test_robustness.py ("the JS no longer defines _robMonteCarlo") cannot tell a
 * working port from a panel that now renders nothing — the functions are
 * absent either way. This drives the real render and asserts the cards come
 * back populated, which is the half a string check cannot see.
 */
const path = require("node:path");
const { test, expect } = require("@playwright/test");

const chartBundle = path.resolve(
  "node_modules/lightweight-charts/dist/lightweight-charts.standalone.production.js",
);

test("robustness panel renders from the endpoint", async ({ page }) => {
  test.setTimeout(120000);
  const calls = [];
  await page.addInitScript(() => {
    localStorage.setItem("ancserTPX.uiLang", "en");
    localStorage.setItem("ancserTPXTheme", "dark");
    // Seed through the app's own restore path — `backtestData` is a module
    // local, not a window property, so assigning it from outside does nothing.
    const pnls = [50, -20, 40, -10, 60, -30, 25, -15, 35, -5, 45, -25, 55, -18];
    const trades = pnls.map((p, i) => ({
      trade_id: "t" + i,
      entry_time: new Date(Date.UTC(2026, 5, 1 + i, 15)).toISOString(),
      exit_time: new Date(Date.UTC(2026, 5, 1 + i, 16)).toISOString(),
      pnl: p, size: 1, symbol: "MNQ", direction: "buy",
      entry_price: 20000, exit_price: 20010,
    }));
    localStorage.setItem("ancserTPX.lastBacktest.v1", JSON.stringify({
      market_clock_version: "america-new-york-v1",
      metrics: { total_trades: trades.length },
      trades, preset_name: "TEST", saved_at: new Date().toISOString(),
    }));
  });
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname === "127.0.0.1") { await route.continue(); return; }
    if (url.hostname === "unpkg.com" && url.pathname.endsWith(
      "/lightweight-charts.standalone.production.js")) {
      await route.fulfill({ path: chartBundle, contentType: "application/javascript" });
      return;
    }
    await route.abort("blockedbyclient");
  });
  page.on("request", (r) => {
    if (r.url().includes("/research/robustness")) calls.push(r.postDataJSON());
  });
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.waitForSelector('html[data-tpx-glass-skin="on"]');
  await page.waitForTimeout(1200);

  // Feed the panel a synthetic backtest and render it.
  const rendered = await page.evaluate(async () => {
    await window.renderResearchRobustness(true);
    const content = document.getElementById("robustness-content");
    const status = document.getElementById("robustness-status");
    const title = document.querySelector("#institution-panel .institution-title");
    return {
      title: title ? title.textContent.trim() : null,
      status: status ? status.textContent : null,
      html: content ? content.innerHTML.length : 0,
      cards: content ? [...content.querySelectorAll(".institution-card h3")]
        .map((h) => h.textContent.trim()) : [],
      summaryLabels: content ? [...content.querySelectorAll(".rob-summary-label")]
        .map((el) => el.textContent.trim()) : [],
      mcCharts: content ? content.querySelectorAll(".rob-mc-card [data-rob-chart]").length : 0,
      mcBands: content ? content.querySelectorAll(".rob-mc-card .rob-line-band.inner").length : 0,
      mcPercentiles: content ? content.querySelectorAll(".rob-mc-card .rob-line-key").length : 0,
      mcPaths: content ? content.querySelectorAll(".rob-mc-card .rob-line-path").length : 0,
      mcPathPointCounts: content ? [...content.querySelectorAll(".rob-mc-card .rob-line-path")]
        .map((path) => (path.getAttribute("d") || "").split("L").length) : [],
      wfCharts: content ? content.querySelectorAll(".rob-wf-card [data-rob-chart]").length : 0,
      slipCharts: content ? content.querySelectorAll(".rob-slip-card [data-rob-chart]").length : 0,
      topstepSizes: content ? [...content.querySelectorAll(".rob-topstep-card tbody tr td:first-child")]
        .map((el) => el.textContent.trim()) : [],
      questionDots: content ? content.querySelectorAll(".rob-help-dot").length : 0,
      text: content ? content.textContent : "",
    };
  });

  expect(calls.length).toBeGreaterThan(0);
  expect(calls[0].trades.length).toBe(14);
  expect(rendered.status).not.toContain("unavailable");
  expect(rendered.title).toBe("Robustness");
  expect(rendered.html).toBeGreaterThan(500);
  expect(rendered.cards[0]).toContain("MONTE CARLO");
  expect(rendered.cards.join(" ")).toContain("WALK-FORWARD");
  expect(rendered.cards.join(" ")).toContain("SLIPPAGE");
  expect(rendered.summaryLabels).toEqual(["TRADES", "CONTRACT", "DATE", "PF", "PNL/MO", "MAXDD"]);
  expect(rendered.mcCharts).toBe(2);
  expect(rendered.mcBands).toBe(2);
  expect(rendered.mcPercentiles).toBe(10);
  expect(rendered.mcPaths).toBe(10);
  expect(Math.min(...rendered.mcPathPointCounts)).toBeGreaterThan(10);
  expect(rendered.wfCharts).toBe(2);
  expect(rendered.slipCharts).toBe(2);
  expect(rendered.topstepSizes).toEqual([
    "1 MNQ", "2 MNQ", "3 MNQ", "5 MNQ", "10 MNQ",
    "1 MNQ", "2 MNQ", "3 MNQ", "5 MNQ", "10 MNQ",
  ]);
  expect(rendered.questionDots).toBeGreaterThan(0);
  expect(rendered.text.toLowerCase()).not.toContain("pass evaluation");
  expect(rendered.text).not.toContain("Robustness — Topstep");
  expect(rendered.text).not.toMatch(/\d+ trades · \d+ active days/);
  expect(consoleErrors.join(" ")).not.toMatch(/_rob|is not defined|undefined/);

  const warningClasses = await page.evaluate(() => ({
    safe: window._robMaxDdAlert(999, "test"),
    caution: window._robMaxDdAlert(1001, "test"),
    danger: window._robMaxDdAlert(2001, "test"),
    negativeP5: window._robPnlAlert(-1, "test"),
  }));
  expect(warningClasses.safe).toBe("");
  expect(warningClasses.caution).toContain("tpx-warn");
  expect(warningClasses.danger).toContain("tpx-danger");
  expect(warningClasses.negativeP5).toContain("tpx-warn");

  await page.locator(
    'body > .glass-topbar > .glass-dock > .tab[data-tab="calendar"]',
  ).click();
  await expect(page.locator("#calendar-view")).toBeVisible();
  const layout = await page.evaluate(() => {
    const summary = [...document.querySelectorAll("#robustness-content .rob-summary-item")];
    const inner = document.querySelector("#robustness-content .rob-line-band.inner");
    const outer = document.querySelector("#robustness-content .rob-line-band.outer");
    return {
      widths: summary.map((el) => el.getBoundingClientRect().width),
      innerColor: inner ? getComputedStyle(inner).fill : null,
      outerColor: outer ? getComputedStyle(outer).fill : null,
      incomeLegendRows: document.querySelectorAll(
        "#cal-income-curve .cal-curve-legend-row",
      ).length,
      incomeSvgTexts: document.querySelectorAll("#cal-income-curve svg text").length,
    };
  });
  expect(Math.max(...layout.widths) - Math.min(...layout.widths)).toBeLessThan(1);
  expect(layout.innerColor).not.toBe(layout.outerColor);
  expect(layout.incomeLegendRows).toBe(2);
  expect(layout.incomeSvgTexts).toBe(0);
  await page.locator("#robustness-content .rob-topstep-card .rob-help-dot").first().click();
  await expect(page.locator("#global-help-tooltip")).toContainText("Topstep 50K simulation");

  // The measured slip level has to reach the backend, or the slip table loses
  // the one row it highlights.
  expect(calls[0].slip_levels).toContain(14);

  // Percentiles must be reproducible now that the RNG is seeded server-side.
  // The old Math.random() version returned different numbers on every render.
  const first = await page.evaluate(() => document
    .querySelector("#robustness-content").textContent);
  const second = await page.evaluate(async () => {
    await window.renderResearchRobustness(true);
    return document.querySelector("#robustness-content").textContent;
  });
  expect(second).toBe(first);
});

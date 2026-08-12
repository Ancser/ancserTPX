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
    return {
      status: status ? status.textContent : null,
      html: content ? content.innerHTML.length : 0,
      cards: content ? [...content.querySelectorAll(".institution-card h3")]
        .map((h) => h.textContent.trim()) : [],
      mcRows: content
        ? content.querySelectorAll(".institution-card table tbody tr").length : 0,
      text: content ? content.textContent.slice(0, 400) : "",
    };
  });

  expect(calls.length).toBeGreaterThan(0);
  expect(calls[0].trades.length).toBe(14);
  expect(rendered.status).not.toContain("unavailable");
  expect(rendered.html).toBeGreaterThan(500);
  expect(rendered.cards.join(" ")).toContain("MONTE CARLO");
  expect(rendered.cards.join(" ")).toContain("WALK-FORWARD");
  expect(rendered.cards.join(" ")).toContain("SLIPPAGE");
  expect(rendered.mcRows).toBeGreaterThan(5);
  expect(consoleErrors.join(" ")).not.toMatch(/_rob|is not defined|undefined/);

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

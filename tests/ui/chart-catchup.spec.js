/* 1.0.10p: the live chart must repair itself after an idle stretch.
 *
 * `/data/latest-candles` always returns the newest 60 one-minute bars and
 * `since` only filters inside that window, so any pause longer than an hour
 * leaves a hole the live poll cannot fill. `pollLiveCandle` also returns early
 * on every CLOSED session, so an overnight gap was permanent until someone
 * reloaded the page by hand. Observed 2026-08-14: the backend held an unbroken
 * 60-bars-per-hour record all night while the open tab still showed the
 * previous afternoon — the engine was healthy and the screen said otherwise.
 *
 * A string check ("the code calls fetchAndShowChart") cannot tell a working
 * catch-up from one that reloads into an empty chart, so this drives the real
 * poll against a stubbed API and reads what the series ends up holding.
 */
const path = require("node:path");
const { test, expect } = require("@playwright/test");

const chartBundle = path.resolve(
  "node_modules/lightweight-charts/dist/lightweight-charts.standalone.production.js",
);

const MIN = 60 * 1000;

function bars(startMs, count) {
  return Array.from({ length: count }, (_, i) => {
    const t = new Date(startMs + i * MIN).toISOString();
    return { time: t, open: 100, high: 101, low: 99, close: 100.5, volume: 10 };
  });
}

async function openWithStubbedCandles(page, { storeFrom, storeCount, liveFrom, liveCount }) {
  await page.addInitScript(() => {
    localStorage.setItem("ancserTPX.uiLang", "en");
    localStorage.setItem("ancserTPXTheme", "dark");
  });

  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname === "unpkg.com" && url.pathname.endsWith(
      "/lightweight-charts.standalone.production.js")) {
      await route.fulfill({ path: chartBundle, contentType: "application/javascript" });
      return;
    }
    if (url.pathname === "/api/data/candles") {
      // The store: complete and current, which is what the backend really had.
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          candles: bars(storeFrom, storeCount),
          count: storeCount, shown: storeCount,
        }),
      });
      return;
    }
    if (url.pathname === "/api/data/latest-candles") {
      // The live endpoint: only ever the newest 60 bars.
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ candles: bars(liveFrom, liveCount), count: liveCount }),
      });
      return;
    }
    if (url.hostname === "127.0.0.1") { await route.continue(); return; }
    await route.abort("blockedbyclient");
  });

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.waitForSelector('html[data-tpx-glass-skin="on"]');
  await page.waitForTimeout(1200);
}

test("a chart left stale overnight repairs itself without a manual reload", async ({ page }) => {
  test.setTimeout(120000);
  const now = Date.now();
  await openWithStubbedCandles(page, {
    storeFrom: now - 24 * 60 * MIN, storeCount: 24 * 60,   // store is current
    liveFrom: now - 60 * MIN, liveCount: 60,               // live: last 60 only
  });

  // Plant a chart that stopped ~20h ago, the state a tab left open produces.
  const staleAt = await page.evaluate((nowMs) => {
    const stale = [];
    for (let i = 0; i < 120; i++) {
      const ms = nowMs - (20 * 60 + 120 - i) * 60 * 1000;
      stale.push({ time: new Date(ms).toISOString(), open: 100, high: 101, low: 99, close: 100.5 });
    }
    window.showCandleData(stale);
    // _rawCandleBuffer is a module local; _lastChartData is the same series
    // exposed on window, and both paths keep it in step.
    const buf = window._lastChartData;
    return buf[buf.length - 1].time;
  }, now);

  const staleAgeMin = Math.round((Date.now() - await page.evaluate(
    (t) => window.chartTimeToUtcMs(t), staleAt)) / MIN);
  expect(staleAgeMin).toBeGreaterThan(90);   // precondition: really is stale

  await page.evaluate(() => window.pollLiveCandle());
  await page.waitForTimeout(600);

  const after = await page.evaluate(() => {
    const buf = window._lastChartData || [];
    return {
      bars: buf.length,
      newestAgeMin: buf.length
        ? Math.round((Date.now() - window.chartTimeToUtcMs(buf[buf.length - 1].time)) / 60000)
        : null,
    };
  });

  expect(after.bars).toBeGreaterThan(120);           // reloaded, not appended
  expect(after.newestAgeMin).toBeLessThan(90);       // and it is current again
});

test("it also repairs while the market is CLOSED", async ({ page }) => {
  test.setTimeout(120000);
  // The scenario that actually bit: come back to a tab left open overnight and
  // look at it outside session hours. pollLiveCandle returns early on CLOSED,
  // so the in-response gap check below is never reached — only the clock-based
  // staleness check runs, and without it the chart stays on yesterday.
  const now = Date.now();
  await openWithStubbedCandles(page, {
    storeFrom: now - 24 * 60 * MIN, storeCount: 24 * 60,
    liveFrom: now - 60 * MIN, liveCount: 60,
  });

  await page.evaluate((nowMs) => {
    window.getMarketSession = () => ({ label: "CLOSED", color: "#888" });
    const stale = [];
    for (let i = 0; i < 120; i++) {
      const ms = nowMs - (20 * 60 + 120 - i) * 60 * 1000;
      stale.push({ time: new Date(ms).toISOString(), open: 100, high: 101, low: 99, close: 100.5 });
    }
    window.showCandleData(stale);
  }, now);

  const before = await page.evaluate(() => window._lastChartData.length);
  expect(before).toBe(120);   // precondition: the stale chart is what we planted

  await page.evaluate(() => window.pollLiveCandle());
  await page.waitForTimeout(600);

  const after = await page.evaluate(() => {
    const buf = window._lastChartData || [];
    return {
      bars: buf.length,
      newestAgeMin: buf.length
        ? Math.round((Date.now() - window.chartTimeToUtcMs(buf[buf.length - 1].time)) / 60000)
        : null,
    };
  });
  expect(after.bars).toBeGreaterThan(120);
  expect(after.newestAgeMin).toBeLessThan(90);
});

test("a short feed gap is filled from the store, not drawn across", async ({ page }) => {
  test.setTimeout(120000);
  // Below the 90-minute staleness threshold, so the clock check stays quiet.
  // Appending here would leave a hole in the series that the backend can
  // actually fill — the store has those bars, the live poll just cannot reach
  // back far enough to hand them over.
  const now = Date.now();
  await openWithStubbedCandles(page, {
    storeFrom: now - 24 * 60 * MIN, storeCount: 24 * 60,
    liveFrom: now - 60 * MIN, liveCount: 60,
  });

  await page.evaluate((nowMs) => {
    const stale = [];
    for (let i = 0; i < 120; i++) {
      const ms = nowMs - (75 + 120 - i) * 60 * 1000;   // stops ~75 min ago
      stale.push({ time: new Date(ms).toISOString(), open: 100, high: 101, low: 99, close: 100.5 });
    }
    window.showCandleData(stale);
  }, now);

  const before = await page.evaluate(() => ({
    n: window._lastChartData.length,
    ageMin: Math.round((Date.now() - window.chartTimeToUtcMs(
      window._lastChartData[window._lastChartData.length - 1].time)) / 60000),
  }));
  expect(before.n).toBe(120);
  expect(before.ageMin).toBeGreaterThan(60);   // a real gap...
  expect(before.ageMin).toBeLessThan(90);      // ...but under the clock threshold

  await page.evaluate(() => window.pollLiveCandle());
  await page.waitForTimeout(600);

  // Counting bars is not enough: plain appending also grows the series, it
  // just leaves a hole where the missing quarter hour should be. Measure the
  // hole instead.
  const after = await page.evaluate(() => {
    const buf = window._lastChartData || [];
    let widest = 0;
    for (let i = 1; i < buf.length; i++) {
      widest = Math.max(widest, buf[i].time - buf[i - 1].time);
    }
    return { n: buf.length, widestGapMin: Math.round(widest / 60) };
  });
  expect(after.n).toBeGreaterThan(120);
  expect(after.widestGapMin).toBeLessThanOrEqual(2);
});

test("chartTimeToUtcMs round-trips utcMsToChartTime", async ({ page }) => {
  test.setTimeout(60000);
  const now = Date.now();
  await openWithStubbedCandles(page, {
    storeFrom: now - 120 * MIN, storeCount: 120,
    liveFrom: now - 60 * MIN, liveCount: 60,
  });
  const drift = await page.evaluate(() => {
    // Include a mid-winter instant: the offset must come from the instant
    // being converted, not from today, or DST skews the staleness check by 1h.
    const samples = [Date.now(), Date.UTC(2026, 0, 15, 12), Date.UTC(2026, 6, 15, 12)];
    return samples.map((ms) => Math.abs(
      window.chartTimeToUtcMs(window.utcMsToChartTime(ms)) - Math.floor(ms / 1000) * 1000));
  });
  for (const d of drift) expect(d).toBeLessThan(1000);
});

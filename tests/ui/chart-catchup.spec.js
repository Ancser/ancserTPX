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

async function openWithStubbedCandles(page, {
  storeFrom, storeCount, liveFrom, liveCount, onBeforeRequest, optionWallData,
}) {
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
      const before = url.searchParams.get("before");
      if (before && onBeforeRequest) {
        const paged = onBeforeRequest(before);
        await route.fulfill({
          contentType: "application/json",
          body: JSON.stringify({
            candles: paged.candles,
            count: paged.count ?? paged.candles.length,
            shown: paged.candles.length,
            has_more_before: paged.has_more_before,
            has_more_after: false,
            source: "persistent_store",
          }),
        });
        return;
      }
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
    if (url.pathname === "/api/options-wall/demo" && optionWallData) {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(optionWallData),
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

test("panning to the left edge prepends an older chart page", async ({ page }) => {
  test.setTimeout(120000);
  const now = Math.floor(Date.now() / MIN) * MIN;
  const initialFrom = now - 180 * MIN;
  const requests = [];
  const signalRequestsDuringPaging = [];
  let capturePagingRequests = false;
  page.on("request", (request) => {
    if (!capturePagingRequests) return;
    const path = new URL(request.url()).pathname;
    if (path === "/api/data/mnq-signals" || path.startsWith("/api/pi/signals")) {
      signalRequestsDuringPaging.push(path);
    }
  });

  await openWithStubbedCandles(page, {
    storeFrom: initialFrom, storeCount: 181,
    liveFrom: now - 60 * MIN, liveCount: 60,
    onBeforeRequest: (before) => {
      requests.push(before);
      const beforeMs = Date.parse(before);
      const pageBars = bars(beforeMs - 60 * MIN, 60);
      return { candles: pageBars, count: 600, has_more_before: true };
    },
  });
  // The offline boot path has no broker workset, so seed the same recent slice
  // that CONNECT would have supplied before the user starts dragging left.
  await page.evaluate((initial) => window.showCandleData(initial), bars(initialFrom, 181));

  const before = await page.evaluate(() => {
    const rows = window._lastChartData || [];
    return {
      count: rows.length,
      first: rows.length ? window.chartTimeToUtcMs(rows[0].time) : null,
    };
  });
  expect(before.count).toBe(181);

  // This is the same callback used by Lightweight Charts when the visible
  // logical range reaches the left edge during a drag.
  await page.waitForTimeout(600);
  capturePagingRequests = true;
  await page.evaluate(() => window.maybeLoadOlderChartHistory({ from: 0, to: 120 }));
  await page.waitForFunction(() => (window._lastChartData || []).length > 181);
  capturePagingRequests = false;

  const after = await page.evaluate(() => {
    const rows = window._lastChartData || [];
    return {
      count: rows.length,
      first: rows.length ? window.chartTimeToUtcMs(rows[0].time) : null,
      widestGapMin: rows.slice(1).reduce((max, row, i) => Math.max(
        max, (row.time - rows[i].time) / 60,
      ), 0),
    };
  });
  expect(requests.length).toBeGreaterThanOrEqual(1);
  expect(after.count).toBeGreaterThan(181);
  expect(before.first - after.first).toBe(requests.length * 60 * MIN);
  expect(after.widestGapMin).toBeLessThanOrEqual(1);
  expect(signalRequestsDuringPaging).toEqual([]);
});

test("option-wall is continuous inside a session, breaks overnight, and keeps overlaps visible", async ({ page }) => {
  test.setTimeout(120000);
  const now = Math.floor(Date.now() / MIN) * MIN;
  const storeFrom = now - 180 * MIN;
  const at = (offsetMin) => new Date(storeFrom + offsetMin * MIN).toISOString();
  const snapshot = (offsetMin, call, put, flip) => ({
    as_of: at(offsetMin),
    call_wall_mnq: call,
    put_wall_mnq: put,
    gamma_flip_mnq: flip,
    net_oi_gex_1pct: 100000000,
    net_volume_gex_1pct: -200000000,
  });

  await page.addInitScript(() => {
    window.__optionWallSegments = [];
    window.__optionWallTexts = [];
    const proto = CanvasRenderingContext2D.prototype;
    const moveTo = proto.moveTo;
    const lineTo = proto.lineTo;
    const fillText = proto.fillText;
    proto.moveTo = function(x, y) {
      if (this.canvas && this.canvas.id === "option-wall-overlay") {
        this.__wallLastPoint = { x, y };
      }
      return moveTo.call(this, x, y);
    };
    proto.lineTo = function(x, y) {
      if (this.canvas && this.canvas.id === "option-wall-overlay" && this.__wallLastPoint) {
        window.__optionWallSegments.push({
          x1: this.__wallLastPoint.x,
          y1: this.__wallLastPoint.y,
          x2: x,
          y2: y,
          stroke: String(this.strokeStyle),
        });
        this.__wallLastPoint = { x, y };
      }
      return lineTo.call(this, x, y);
    };
    proto.fillText = function(text, ...args) {
      if (this.canvas && this.canvas.id === "option-wall-overlay") {
        window.__optionWallTexts.push(String(text));
      }
      return fillText.call(this, text, ...args);
    };
  });

  await openWithStubbedCandles(page, {
    storeFrom,
    storeCount: 181,
    liveFrom: now - 60 * MIN,
    liveCount: 60,
    optionWallData: {
      available: true,
      date: "test",
      paid_cost_usd: 0,
      snapshots: [
        snapshot(120, 102, 98, 100),
        snapshot(125, 102, 98, 100),
        snapshot(135, 97, 103, 100),
        snapshot(140, 100, 100, 99),
      ],
      pi_signals: [],
    },
  });
  await page.evaluate((initial) => window.showCandleData(initial), bars(storeFrom, 181));

  await page.evaluate(() => window.toggleChartLayer("optionwall", true));
  await page.waitForTimeout(1500);
  const painted = await page.evaluate(() => ({
    segments: window.__optionWallSegments,
    texts: window.__optionWallTexts,
    layerOn: window.layerOn("optionwall"),
    snapshotCount: _optionWallSnapshots.length,
    canvas: !!document.getElementById("option-wall-overlay"),
    sameSession: window._optionWallSameSession(
      { as_of: "2026-08-03T13:35:00Z" },
      { as_of: "2026-08-03T20:00:00Z" },
    ),
    crossSession: window._optionWallSameSession(
      { as_of: "2026-08-03T20:00:00Z" },
      { as_of: "2026-08-04T13:35:00Z" },
    ),
    closeEnd: window._optionWallSegmentEndTime({
      chartTime: window.isoToChartTime("2026-08-03T20:00:00Z"),
      as_of: "2026-08-03T20:00:00Z",
    }, null),
    closeStart: window.isoToChartTime("2026-08-03T20:00:00Z"),
    hourlyEnd: window._optionWallSegmentEndTime({
      chartTime: window.isoToChartTime("2026-08-03T19:00:00Z"),
      as_of: "2026-08-03T19:00:00Z",
      cadence_seconds: 3600,
    }, null),
  }));

  expect(painted.layerOn).toBe(true);
  expect(painted.snapshotCount).toBe(4);
  expect(painted.canvas).toBe(true);
  expect(painted.segments.length).toBeGreaterThan(0);
  expect(painted.segments.every((s) => (
    Math.abs(s.x2 - s.x1) < 0.01 || Math.abs(s.y2 - s.y1) < 0.01
  ))).toBe(true);
  expect(painted.segments.some((s) => (
    Math.abs(s.x2 - s.x1) < 0.01 && Math.abs(s.y2 - s.y1) > 0.01
  ))).toBe(true);
  expect(painted.texts).toContain("CALL+PUT WALL");
  expect(painted.sameSession).toBe(true);
  expect(painted.crossSession).toBe(false);
  expect(painted.closeEnd).toBe(painted.closeStart); // stop exactly at 16:00 ET
  expect(painted.hourlyEnd).toBe(painted.closeStart); // hourly fallback stays valid to close

  const green = painted.segments.filter((s) => s.stroke.includes("40, 209, 124"));
  const red = painted.segments.filter((s) => s.stroke.includes("255, 93, 115"));
  expect(green.length).toBeGreaterThan(0);
  expect(red.length).toBeGreaterThan(0);
  expect(green.some((g) => red.some((r) =>
    Math.abs(g.x1 - r.x1) < 0.01
      && Math.abs(g.x2 - r.x2) < 0.01
      && Math.abs(g.y1 - r.y1) >= 2.5,
  ))).toBe(true);
});

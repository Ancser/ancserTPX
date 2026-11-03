/* Chart timeframe selector (1m .. W).
 *
 * Higher-timeframe bars come from the backend aggregate (one definition in
 * backend/data/timeframes.py); the chart must request them with `tf`, keep 1m
 * on its original path, snap overlays/markers into the containing bar, and
 * update the last bucket live instead of appending raw 1m bars to it.
 */
const path = require("node:path");
const { test, expect } = require("@playwright/test");

const chartBundle = path.resolve(
  "node_modules/lightweight-charts/dist/lightweight-charts.standalone.production.js",
);
const MIN = 60 * 1000;
const HOUR = 60 * MIN;

function bars(startMs, count, stepMs, base = 100) {
  return Array.from({ length: count }, (_, i) => {
    const c = base + i * 0.1;
    return { time: new Date(startMs + i * stepMs).toISOString(), open: c, high: c + 1, low: c - 1, close: c + 0.5, volume: 10 };
  });
}

async function openStubbed(page, state) {
  await page.addInitScript(() => {
    try { localStorage.removeItem("ancserTPXChartTimeframe"); } catch (_) {}
  });
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname === "unpkg.com") {
      await route.fulfill({ path: chartBundle, contentType: "application/javascript" });
      return;
    }
    if (url.pathname === "/api/data/candles") {
      const tf = url.searchParams.get("tf") || "1m";
      state.requests.push({ tf, limit: url.searchParams.get("limit"), before: url.searchParams.get("before") });
      const rows = state.rows[tf] ? state.rows[tf](url) : [];
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ candles: rows, count: rows.length, shown: rows.length, has_more_before: true, timeframe: tf }),
      });
      return;
    }
    if (url.pathname === "/api/data/latest-candles") {
      const rows = state.latest || [];
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ candles: rows, count: rows.length }) });
      return;
    }
    if (url.hostname === "127.0.0.1") { await route.continue(); return; }
    await route.abort("blockedbyclient");
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => typeof candleSeries !== "undefined" && !!candleSeries);
}

test("switching to 1H requests tf bars, snaps markers and overlays into the bar", async ({ page }) => {
  test.setTimeout(90000);
  const hourStart = Math.floor(Date.now() / HOUR) * HOUR - 400 * HOUR;
  const state = {
    requests: [],
    rows: {
      "1m": () => bars(Date.now() - 300 * MIN, 300, MIN),
      "1h": () => bars(hourStart, 400, HOUR),
    },
  };
  await openStubbed(page, state);
  await page.evaluate(() => fetchAndShowChart("1m"));
  expect(state.requests.at(-1).tf).toBe("1m");

  await page.click('#chart-tf-group .chart-tf-btn[data-tf="1h"]');
  await page.waitForFunction(() => window._lastChartData && window._lastChartData.length === 400);
  const view = await page.evaluate(async () => {
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    const cd = window._lastChartData;
    const range = chart.timeScale().getVisibleLogicalRange();
    const bar = cd[cd.length - 20];
    // A marker 25 minutes into that hour, and one outside the loaded bars.
    _backtestMarkers = [
      { time: bar.time + 25 * 60, position: "belowBar", color: "#0f0", shape: "arrowUp", text: "in" },
      { time: cd[0].time - 3600 * 5, position: "belowBar", color: "#f00", shape: "arrowUp", text: "out" },
    ];
    _refreshAllMarkers();
    const spacing = chart.timeScale().options().barSpacing;
    const center = chart.timeScale().timeToCoordinate(bar.time);
    return {
      active: document.querySelector('#chart-tf-group .chart-tf-btn.active').dataset.tf,
      visibleBars: range.to - range.from,
      markerTimes: candleSeries.markers().map((m) => m.time),
      barTime: bar.time,
      startX: _chartTimeToCoordinate(bar.time),
      midX: _chartTimeToCoordinate(bar.time + 30 * 60),
      center, spacing,
      gapX: _chartTimeToCoordinate(cd[0].time - 3600 * 5),
    };
  });
  expect(state.requests.at(-1).tf).toBe("1h");
  expect(view.active).toBe("1h");
  expect(view.visibleBars).toBeGreaterThan(100);
  expect(view.visibleBars).toBeLessThan(200);
  expect(view.markerTimes).toEqual([view.barTime]);                 // snapped, outsider dropped
  expect(view.startX).toBeCloseTo(view.center - view.spacing / 2, 3);  // bucket start = left edge
  expect(view.midX).toBeCloseTo(view.center, 3);                     // half-way = bar center
  expect(view.gapX).toBeNull();

  await page.click('#chart-tf-group .chart-tf-btn[data-tf="1m"]');
  await page.waitForFunction(() => window._lastChartData && window._lastChartData.length === 300);
  expect(state.requests.at(-1).tf).toBe("1m");
});

test("a live poll on 1H updates the last bucket instead of appending 1m bars", async ({ page }) => {
  test.setTimeout(90000);
  const hourStart = Math.floor(Date.now() / HOUR) * HOUR - 99 * HOUR;
  let lastClose = 150;
  const state = {
    requests: [],
    rows: {
      "1h": (url) => {
        const all = bars(hourStart, 100, HOUR);
        all[all.length - 1].close = lastClose;
        const limit = Number(url.searchParams.get("limit"));
        return limit && limit < all.length ? all.slice(-limit) : all;
      },
    },
    latest: bars(Date.now() - 10 * MIN, 10, MIN),
  };
  await openStubbed(page, state);
  await page.evaluate(() => setChartTimeframe("1h"));
  await page.waitForFunction(() => window._lastChartData && window._lastChartData.length === 100);

  lastClose = 175.25;
  await page.evaluate(() => {
    window.getMarketSession = () => ({ label: "RTH", color: "#0f0" });
    return pollLiveCandle();
  });
  const after = await page.evaluate(() => ({
    n: window._lastChartData.length,
    last: window._lastChartData[window._lastChartData.length - 1].close,
    seriesLast: candleSeries.dataByIndex(candleSeries.data().length - 1).close,
  }));
  expect(after.n).toBe(100);                // no 1m bars appended
  expect(after.last).toBe(175.25);          // last hour bucket updated live
  expect(after.seriesLast).toBe(175.25);
  expect(state.requests.at(-1)).toMatchObject({ tf: "1h", limit: "3" });
});

test("left-edge paging on a higher timeframe asks for that timeframe", async ({ page }) => {
  test.setTimeout(90000);
  const dayStart = Math.floor(Date.now() / (24 * HOUR)) * 24 * HOUR - 200 * 24 * HOUR;
  const state = {
    requests: [],
    rows: {
      "1d": (url) => url.searchParams.get("before")
        ? bars(dayStart - 50 * 24 * HOUR, 50, 24 * HOUR, 80)
        : bars(dayStart, 200, 24 * HOUR),
    },
  };
  await openStubbed(page, state);
  await page.evaluate(() => setChartTimeframe("1d"));
  await page.waitForFunction(() => window._lastChartData && window._lastChartData.length === 200);
  await page.evaluate(() => loadOlderChartHistory());
  const paged = state.requests.find((r) => r.before);
  expect(paged).toMatchObject({ tf: "1d" });
  const n = await page.evaluate(() => window._lastChartData.length);
  expect(n).toBe(250);
});

const path = require("node:path");
const { expect, test } = require("@playwright/test");

const chartBundle = path.resolve(
  "node_modules/lightweight-charts/dist/lightweight-charts.standalone.production.js",
);

async function openNativeApp(page) {
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname === "unpkg.com" && url.pathname.endsWith(
      "/lightweight-charts.standalone.production.js",
    )) {
      await route.fulfill({ path: chartBundle, contentType: "application/javascript" });
      return;
    }
    if (url.hostname !== "127.0.0.1") {
      await route.abort("blockedbyclient");
      return;
    }
    await route.continue();
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.locator('link[href*="ancserTPX-design.css"]')).toHaveCount(1);
}

test("native design layer keeps hierarchy and controls usable", async ({ page }) => {
  await page.addInitScript(() => localStorage.removeItem("ancserTPX.chartLayers"));
  await openNativeApp(page);
  await expect(page.locator("body")).toHaveAttribute("data-ui-density", "standard");
  await expect(page.locator("#chart-container")).toHaveCount(1);
  const scanlines = await page.evaluate(() => getComputedStyle(document.body, "::before").backgroundImage);
  expect(scanlines).toBe("none");

  await page.locator("#chart-layer-btn").click();
  const switches = page.locator("#chart-layer-pop .ui-switch");
  await expect(switches).toHaveCount(17);
  const rows = page.locator("#chart-layer-pop .layer-row");
  const boxes = await switches.evaluateAll((nodes) => nodes.map((node) => {
    const box = node.getBoundingClientRect();
    return { x: box.x, right: box.right, width: box.width };
  }));
  expect(boxes).toHaveLength(17);
  expect(Math.max(...boxes.map((box) => box.x)) - Math.min(...boxes.map((box) => box.x))).toBeLessThanOrEqual(1);
  expect(Math.max(...boxes.map((box) => box.width)) - Math.min(...boxes.map((box) => box.width))).toBeLessThanOrEqual(1);
});

test("chart layer switches close, reopen, and toggle without proxy pixels", async ({ page }) => {
  await page.addInitScript(() => localStorage.removeItem("ancserTPX.chartLayers"));
  await openNativeApp(page);
  await page.locator("#chart-layer-btn").click();
  const popup = page.locator("#chart-layer-pop");
  const optionWall = popup.locator('.ui-switch[data-switch-proxy="lp-optionwall"]');
  await expect(optionWall).toHaveAttribute("aria-checked", "false");
  // No forwarding proxies remain: each switch owns its own click.
  await expect(popup.locator("[class*=proxy]")).toHaveCount(0);

  await optionWall.click();
  await expect(optionWall).toHaveAttribute("aria-checked", "true");
  await page.locator("#chart-layer-btn").click();
  await expect(popup).toHaveClass(/hidden/);
  await page.locator("#chart-layer-btn").click();
  await expect(popup).not.toHaveClass(/hidden/);
  await expect(optionWall).toHaveAttribute("aria-checked", "true");
  await optionWall.click();
  await expect(optionWall).toHaveAttribute("aria-checked", "false");
});

test("workspace navigation and account status use the left rail and chart corner", async ({ page }) => {
  await openNativeApp(page);
  const layout = await page.evaluate(() => {
    const rect = (selector) => {
      const node = document.querySelector(selector);
      if (!node) return null;
      const box = node.getBoundingClientRect();
      return { x: box.x, y: box.y, right: box.right, bottom: box.bottom, width: box.width, height: box.height };
    };
    return {
      rail: rect(".app-rail"),
      tabs: [...document.querySelectorAll(".app-rail .tab")].map((node) => {
        const box = node.getBoundingClientRect();
        return { x: box.x, y: box.y, width: box.width, height: box.height };
      }),
      theme: rect("#theme-toggle"),
      connection: rect(".rail-connection .conn-trigger"),
      chart: rect("#chart-container"),
      status: rect("#chart-status-cluster"),
      account: rect("#chart-status-cluster #account-badge"),
      clock: rect("#chart-status-cluster #clock"),
      quickButtons: rect("#chart-quick-btns"),
      activeRailTab: rect(".app-rail .tab.active"),
    };
  });
  expect(layout.rail).not.toBeNull();
  expect(layout.rail.width).toBeLessThanOrEqual(80);
  expect(layout.tabs).toHaveLength(3);
  expect(Math.max(...layout.tabs.map((tab) => tab.x)) - Math.min(...layout.tabs.map((tab) => tab.x))).toBeLessThanOrEqual(1);
  expect(layout.tabs[0].y).toBeLessThan(layout.tabs[1].y);
  expect(layout.tabs[1].y).toBeLessThan(layout.tabs[2].y);
  expect(layout.theme.y).toBeLessThan(layout.connection.y);
  expect(layout.chart.right - layout.status.right).toBeGreaterThanOrEqual(32);
  expect(Math.abs(layout.status.right - layout.quickButtons.right)).toBeLessThanOrEqual(1);
  expect(Math.abs(layout.activeRailTab.x - layout.rail.x)).toBeLessThanOrEqual(1);
  expect(Math.abs(layout.activeRailTab.right - layout.rail.right)).toBeLessThanOrEqual(1);
  expect(layout.status.x).toBeGreaterThan(layout.chart.x);
  expect(layout.account).not.toBeNull();
  expect(layout.clock).not.toBeNull();
});

test("connection panel keeps credentials only and removes manual data controls", async ({ page }) => {
  await openNativeApp(page);
  await page.locator("#conn-trigger").click();
  const panel = page.locator("#conn-panel");
  await expect(panel).toBeVisible();
  await expect(panel.locator("label")).toHaveCount(2);
  await expect(panel.locator("#btn-connect")).toHaveCount(1);
  await expect(panel.locator("#btn-fetch-full, #btn-offline, #contract-preset, #data-count, #data-range-info")).toHaveCount(0);

  const connection = await panel.evaluate((node) => ({
    text: node.innerText,
    labels: [...node.querySelectorAll("label")].map((label) => label.textContent.trim()),
    internalContract: node.querySelector("#contract-id")?.type,
  }));
  expect(connection.labels.join(" ")).toContain("TOPSTEP USERNAME");
  expect(connection.labels.join(" ")).toContain("TOPSTEP API");
  expect(connection.text).not.toMatch(/CONTRACT|INTERVAL|BARS|FETCH FULL DATA|OFFLINE MODE/i);
  expect(connection.internalContract).toBe("hidden");
});

test("connection identity and provider latency rail expose the requested states", async ({ page }) => {
  await openNativeApp(page);
  await expect(page.locator("#connection-initial")).toHaveText("?");
  // Without saved credentials the app opens the connection panel by itself
  // ~0.5 s after its config loads.  Wait for that instead of racing it: a
  // click that lands after the auto-open toggles the panel closed.
  const panel = page.locator("#conn-panel");
  await expect(panel).toHaveClass(/open/);
  await page.locator("#conn-trigger").click();
  await expect(panel).not.toHaveClass(/open/);
  await page.locator("#conn-trigger").click();
  await expect(panel).toHaveClass(/open/);
  await page.locator("#username").fill("ancser");
  await expect(page.locator("#connection-initial")).toHaveText("a");

  const status = await page.evaluate(() => {
    _renderProviderStatus("discord", {
      state: "connected",
      latency_ms: 42,
      detail: "listener ready",
    });
    _renderProviderStatus("topstep", {
      state: "starting",
      detail: "authentication pending",
    });
    _renderProviderStatus("databento", {
      state: "error",
      detail: "feed not started",
    });
    return ["discord", "topstep", "databento"].map((provider) => ({
      provider,
      dot: document.querySelector(`#latency-dot-${provider}`).className,
      value: document.querySelector(`#latency-${provider}`).textContent,
      title: document.querySelector(`.chart-latency-row[data-provider="${provider}"]`).title,
    }));
  });
  expect(status[0].dot).toContain("is-connected");
  expect(status[0].value).toBe("42");
  expect(status[0].title).toContain("listener ready");
  expect(status[1].dot).toContain("is-starting");
  expect(status[2].dot).toContain("is-error");
  expect(await page.locator(".chart-latency-row").count()).toBe(3);
  await expect(page.locator("#chart-status-cluster")).toHaveCSS("flex-direction", "row");
  await expect(page.locator("#chart-provider-status")).toHaveCSS("flex-direction", "column");
});

test("parameter sidebar can collapse from the rail without leaving a layout gap", async ({ page }) => {
  await openNativeApp(page);
  const toggle = page.locator("#sidebar-toggle");
  const sidebar = page.locator("#parameter-sidebar");
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(sidebar).toBeVisible();

  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator("body")).toHaveClass(/sidebar-collapsed/);
  await expect.poll(() => sidebar.evaluate((node) => ({
    width: getComputedStyle(node).width,
    opacity: getComputedStyle(node).opacity,
  }))).toMatchObject({ width: "0px", opacity: "0" });

  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect.poll(() => sidebar.evaluate((node) => Number.parseFloat(getComputedStyle(node).width)))
    .toBeGreaterThan(0);
  await expect(sidebar).toBeVisible();
  await expect(page.locator("body")).not.toHaveClass(/sidebar-collapsed/);
});

test("Apple Light removes the PI matrix slab without hiding its controls", async ({ page }) => {
  await openNativeApp(page);
  await page.locator("#theme-toggle").click();
  const matrix = page.locator('.pi-signal-matrix[data-pi-matrix="bt"]');
  await expect.poll(() => matrix.evaluate((node) => getComputedStyle(node).backgroundColor))
    .toBe("rgba(0, 0, 0, 0)");
  const style = await matrix.evaluate((node) => {
    const css = getComputedStyle(node);
    return {
      background: css.backgroundColor,
      border: css.borderTopWidth,
      controls: node.querySelectorAll(".pi-matrix-switch").length,
    };
  });
  expect(style.background).toBe("rgba(0, 0, 0, 0)");
  expect(style.border).toBe("1px");
  expect(style.controls).toBe(6);
});

test("trade headers stay pinned while the lower content scrolls", async ({ page }) => {
  await openNativeApp(page);
  await page.evaluate(() => {
    const body = document.querySelector("#trades-tbody");
    body.innerHTML = Array.from({ length: 80 }, (_, i) => (
      `<tr><td>1 MNQ</td><td>9/9/2026 10:30AM → 11:30AM</td>` +
      `<td>1hr</td><td>29000.00</td><td>29010.00</td><td>${i}</td>` +
      `<td>0</td><td>0</td><td>LONG</td></tr>`
    )).join("");
  });
  const sticky = await page.evaluate(() => {
    const scroller = document.querySelector(".bottom-content");
    const header = document.querySelector("#btab-trades thead th");
    const wrap = document.querySelector("#btab-trades .trade-table-wrap");
    const before = header.getBoundingClientRect().top;
    scroller.scrollTop = Math.min(240, scroller.scrollHeight - scroller.clientHeight);
    const after = header.getBoundingClientRect().top;
    return {
      scrolled: scroller.scrollTop > 0,
      delta: Math.abs(after - before),
      position: getComputedStyle(header).position,
      wrapOverflowY: getComputedStyle(wrap).overflowY,
    };
  });
  expect(sticky.scrolled).toBe(true);
  expect(sticky.delta).toBeLessThanOrEqual(1);
  expect(sticky.position).toBe("sticky");
  expect(sticky.wrapOverflowY).toBe("visible");
});

test("trade tables combine size and symbol into a contract label", async ({ page }) => {
  await openNativeApp(page);
  const result = await page.evaluate(() => {
    renderTrades([{
      entry_time: "2026-09-09T17:30:00Z",
      exit_time: "2026-09-09T18:00:00Z",
      entry_price: 29000,
      exit_price: 29010,
      direction: "buy",
      size: 3,
      symbol: "/MNQ",
      pnl: 30,
    }]);
    renderExecuteTrades([{
      entry_time: "2026-09-09T17:30:00Z",
      exit_time: "2026-09-09T18:00:00Z",
      entry_price: 29000,
      exit_price: 29010,
      direction: "buy",
      size: 3,
      symbol: "/MNQ",
      pnl: 30,
    }]);
    return {
      backtestHeaders: [...document.querySelectorAll("#btab-trades thead th")]
        .map((node) => node.textContent.trim()),
      executeHeaders: [...document.querySelectorAll("#btab-execute thead th")]
        .map((node) => node.textContent.trim()),
      backtestContract: document.querySelector("#trades-tbody .trade-contract-cell")?.textContent,
      executeContract: document.querySelector("#execute-tbody .trade-contract-cell")?.textContent,
      backtestCells: document.querySelectorAll("#trades-tbody tr:first-child td").length,
      executeCells: document.querySelectorAll("#execute-tbody tr:first-child td").length,
    };
  });
  expect(result.backtestHeaders[0]).toBe("CONTRACT");
  expect(result.executeHeaders[0]).toBe("CONTRACT");
  expect(result.backtestHeaders).not.toContain("SIZE");
  expect(result.backtestHeaders).not.toContain("SYMBOL");
  expect(result.executeHeaders).not.toContain("SIZE");
  expect(result.executeHeaders).not.toContain("SYMBOL");
  expect(result.backtestContract).toBe("3 MNQ");
  expect(result.executeContract).toBe("3 MNQ");
  expect(result.backtestCells).toBe(9);
  expect(result.executeCells).toBe(9);
});

test("trade content and bottom panes use one-shot motion", async ({ page }) => {
  await openNativeApp(page);
  const motion = await page.evaluate(async () => {
    const row = {
      entry_time: "2026-09-09T17:30:00Z",
      exit_time: "2026-09-09T18:00:00Z",
      entry_price: 29000,
      exit_price: 29010,
      direction: "buy",
      size: 1,
      symbol: "/MNQ",
      pnl: 30,
    };
    const nextFrame = () => new Promise((resolve) => requestAnimationFrame(resolve));
    renderTrades([row]);
    await nextFrame();
    const tbody = document.querySelector("#trades-tbody");
    const firstRow = tbody.querySelector("tr");
    const entering = {
      armed: tbody.classList.contains("trade-table-animating"),
      name: getComputedStyle(firstRow).animationName,
      duration: getComputedStyle(firstRow).animationDuration,
      cellTransition: getComputedStyle(firstRow.querySelector("td")).transitionDuration,
    };

    // A passive refresh with the same rows must not replay the motion.
    await new Promise((resolve) => setTimeout(resolve, 460));
    renderTrades([row]);
    await nextFrame();
    const repeated = {
      armed: tbody.classList.contains("trade-table-animating"),
      name: getComputedStyle(tbody.querySelector("tr")).animationName,
    };

    // The tab content is also animated when it becomes visible.
    const pane = document.querySelector("#btab-execute");
    pane.classList.remove("hidden");
    _animateBottomPane(pane);
    await nextFrame();
    const paneName = getComputedStyle(pane).animationName;
    return { entering, repeated, paneName };
  });

  expect(motion.entering.armed).toBe(true);
  expect(motion.entering.name).toBe("tpx-trade-row-enter");
  expect(motion.entering.duration).toContain("0.18s");
  expect(motion.entering.cellTransition).toContain("0.18s");
  expect(motion.repeated.armed).toBe(false);
  expect(motion.repeated.name).toBe("none");
  expect(motion.paneName).toBe("tpx-bottom-pane-enter");
});

test("sidebar scrolling does not reserve a scrollbar column", async ({ page }) => {
  await openNativeApp(page);
  const scrollbar = await page.locator(".sidebar").evaluate((node) => {
    const css = getComputedStyle(node);
    return {
      scrollable: node.scrollHeight > node.clientHeight,
      overflowY: css.overflowY,
      scrollbarWidth: css.scrollbarWidth,
      gutter: css.scrollbarGutter,
      reserved: node.offsetWidth - node.clientWidth - Number.parseFloat(css.borderRightWidth || "0"),
    };
  });
  expect(scrollbar.scrollable).toBe(true);
  expect(scrollbar.overflowY).toBe("auto");
  expect(scrollbar.scrollbarWidth).toBe("none");
  expect(scrollbar.gutter).toBe("auto");
  expect(scrollbar.reserved).toBeLessThanOrEqual(1);
});

test("left rail hover labels use a bounded tooltip box", async ({ page }) => {
  await openNativeApp(page);
  await page.locator('.app-rail .tab[data-tab="backtest"]').hover();
  const tooltip = await page.locator('.app-rail .tab[data-tab="backtest"]').evaluate((node) => {
    const css = getComputedStyle(node, "::after");
    return {
      content: css.content,
      display: css.display,
      boxSizing: css.boxSizing,
      minHeight: css.minHeight,
      lineHeight: css.lineHeight,
      whiteSpace: css.whiteSpace,
    };
  });
  expect(tooltip.content).toBe('"Backtest"');
  expect(tooltip.display).toBe("flex");
  expect(tooltip.boxSizing).toBe("border-box");
  expect(tooltip.minHeight).toBe("26px");
  expect(tooltip.whiteSpace).toBe("nowrap");
});

test("legacy deep-blue and Apple Light palettes switch and persist", async ({ page }) => {
  await openNativeApp(page);
  const theme = page.locator("#theme-toggle");
  await expect(theme).toHaveCount(1);
  await expect(theme).toHaveAttribute("data-theme", "graphite");
  await expect(page.locator("html")).toHaveAttribute("data-tpx-theme", "graphite");

  const graphite = await page.evaluate(() => ({
    page: getComputedStyle(document.documentElement).getPropertyValue("--surface-page").trim(),
    chart: getComputedStyle(document.documentElement).getPropertyValue("--surface-chart").trim(),
  }));
  expect(graphite).toEqual({ page: "#0e1116", chart: "#15181d" });   // Relayout v2 palette

  const baseMotion = await page.evaluate(() => ({
    motion: getComputedStyle(document.documentElement).getPropertyValue("--ui-motion-duration").trim(),
    theme: getComputedStyle(document.documentElement).getPropertyValue("--ui-theme-duration").trim(),
    chart: getComputedStyle(document.querySelector("#chart-container")).transitionDuration,
    info: getComputedStyle(document.querySelector("#bottom-panel")).transitionDuration,
    research: getComputedStyle(document.querySelector("#calendar-view")).transitionDuration,
    pnl: (() => {
      const canvas = document.createElement("canvas");
      canvas.id = "pnl-curve-canvas";
      document.querySelector("#pnl-curve-body").appendChild(canvas);
      return getComputedStyle(canvas).transitionDuration;
    })(),
  }));
  expect(baseMotion.motion).toBe("180ms");
  expect(baseMotion.theme).toBe("150ms");
  expect(baseMotion.chart).toContain("0.18s");
  expect(baseMotion.info).toContain("0.18s");
  expect(baseMotion.research).toContain("0.18s");
  expect(baseMotion.pnl).toContain("0.18s");

  // Click and sample in the same synchronous task: the 150 ms crossfade can end
  // before a separate evaluate() runs on a slow runner (CLAUDE.md: no sleep-
  // or race-based assertions). Measured mid-transition by construction.
  const themeMotion = await page.evaluate(() => {
    document.getElementById("theme-toggle").click();
    return {
    transitioning: document.documentElement.classList.contains("theme-transitioning"),
    chart: getComputedStyle(document.querySelector("#chart-container")).transitionDuration,
    info: getComputedStyle(document.querySelector("#bottom-panel")).transitionDuration,
    research: getComputedStyle(document.querySelector("#calendar-view")).transitionDuration,
    pnl: getComputedStyle(document.querySelector("#pnl-curve-canvas")).transitionDuration,
    };
  });
  await expect(page.locator("html")).toHaveAttribute("data-tpx-theme", "apple-light");
  await expect(theme).toHaveAttribute("data-theme", "apple-light");
  expect(themeMotion.transitioning).toBe(true);
  expect(themeMotion.chart).toContain("0.15s");
  expect(themeMotion.info).toContain("0.15s");
  expect(themeMotion.research).toContain("0.15s");
  expect(themeMotion.pnl).toContain("0.15s");
  await expect.poll(() => page.evaluate(() => getComputedStyle(document.body).backgroundColor))
    .toBe("rgb(245, 247, 251)");
  const light = await page.evaluate(() => ({
    page: getComputedStyle(document.documentElement).getPropertyValue("--surface-page").trim(),
    body: getComputedStyle(document.body).backgroundColor,
  }));
  expect(light).toEqual({ page: "#f5f7fb", body: "rgb(245, 247, 251)" });

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator("html")).toHaveAttribute("data-tpx-theme", "apple-light");
  await expect(page.locator("#theme-toggle")).toHaveAttribute("data-theme", "apple-light");
});

test("chart shell uses straight edges, restored watermark, and one account surface", async ({ page }) => {
  await openNativeApp(page);
  const shell = await page.evaluate(() => ({
    panelRadius: getComputedStyle(document.querySelector(".panel-title")).borderRadius,
    controlRadius: getComputedStyle(document.querySelector(".form-group select")).borderRadius,
    watermark: document.querySelector("#chart-watermark")?.textContent,
    watermarkFont: getComputedStyle(document.querySelector("#chart-watermark")).fontFamily,
    watermarkPosition: (() => {
      const style = getComputedStyle(document.querySelector("#chart-watermark"));
      return { left: style.left, bottom: style.bottom, transform: style.transform };
    })(),
    minor: document.querySelectorAll("#live-acct-select-2, #live-acct-preset-2, #live-slot-dot-2").length,
    legend: document.querySelector("#signal-legend"),
    modelDescriptions: document.querySelectorAll(".strategy-description").length,
    timeRange: formatTradeTimeRange("2026-09-10T02:30:00Z", "2026-09-10T03:32:00Z"),
    paddedClock: formatTradeTimeRange("2026-09-10T19:45:00Z", "2026-09-10T20:00:00Z"),
    duration: formatTradeDuration("2026-09-10T02:30:00Z", "2026-09-10T03:32:00Z"),
  }));
  expect(shell.panelRadius).toBe("0px");
  expect(shell.controlRadius).toBe("0px");
  expect(shell.watermark).toBe("ancserTPX");
  expect(shell.watermarkFont).toContain("Inter");   // Relayout v2: Orbitron retired
  expect(shell.watermarkPosition).toEqual({ left: "12px", bottom: "34px", transform: "none" });
  expect(shell.minor).toBe(0);
  expect(shell.legend).toBeNull();
  expect(shell.modelDescriptions).toBe(0);
  expect(shell.timeRange).toBe("09/09/2026 10:30PM → 11:32PM");
  expect(shell.paddedClock).toBe("09/10/2026 03:45PM → 04:00PM");
  expect(shell.duration).toBe("1hr 2m");
});

test("chart uses smooth-200 framing and keeps plot panning free", async ({ page }) => {
  await openNativeApp(page);
  await page.evaluate(() => {
    const bars = Array.from({ length: 3000 }, (_, i) => {
      const close = 100 + i * 0.01 + Math.sin(i / 13) * 0.5;
      return {
        time: new Date((1700000000 + i * 60) * 1000).toISOString(),
        open: close - 0.1,
        high: close + 0.5,
        low: close - 0.5,
        close,
      };
    });
    window.__chartPanProbe = bars[2700].close;
    showCandleData(bars);
  });

  await page.waitForFunction(() => {
    const range = chart.timeScale().getVisibleLogicalRange();
    return range && Math.abs((range.from + range.to) / 2 - 2899.5) < 2;
  });
  const initial = await page.evaluate(() => ({
    range: chart.timeScale().getVisibleLogicalRange(),
    autoScale: chart.priceScale("right").options().autoScale,
    kineticMouse: chart.options().kineticScroll.mouse,
  }));
  expect((initial.range.from + initial.range.to) / 2).toBeCloseTo(2899.5, 0);
  expect(initial.autoScale).toBe(true);
  // The library owns the whole plot drag, including its release inertia.
  expect(initial.kineticMouse).toBe(true);

  const box = await page.locator("#chart-container").boundingBox();
  const rightScaleWidth = await page.evaluate(() => chart.priceScale("right").width());
  const plotWidth = box.width - rightScaleWidth;
  const x = box.x + Math.max(24, plotWidth * 0.35);
  const y = box.y + box.height * 0.45;
  const before = await page.evaluate(() => ({
    scroll: chart.timeScale().scrollPosition(),
    spacing: chart.timeScale().options().barSpacing,
    priceY: candleSeries.priceToCoordinate(window.__chartPanProbe),
  }));

  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x, y + 100, { steps: 8 });
  const vertical = await page.evaluate(() => ({
    manual: _chartManualPriceRange,
    priceY: candleSeries.priceToCoordinate(window.__chartPanProbe),
  }));
  // A vertical drag takes the price window (manual) and price follows it.
  expect(vertical.manual).not.toBeNull();
  expect(Math.abs(vertical.priceY - before.priceY)).toBeGreaterThan(10);
  await page.mouse.up();

  await page.evaluate(() => _setChartPriceAutoScale(true));
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 100, y, { steps: 25 });
  // The library applies pointer moves on the next animation frame.
  const horizontal = await page.evaluate(() => new Promise((resolve) =>
    requestAnimationFrame(() => requestAnimationFrame(() => resolve({
      scroll: chart.timeScale().scrollPosition(),
      spacing: chart.timeScale().options().barSpacing,
      manual: _chartManualPriceRange,
    }))),
  ));
  expect(horizontal.manual).toBeNull();   // a horizontal pan keeps auto-fit
  // Cursor-locked 1:1 like TradingView. The library records the move that
  // crosses its 5px click threshold (the 2nd 4px step) as the scroll origin
  // and pans from the next move, so ~12px of the 100px is not credited; the
  // old 1.10x controller moved more than the cursor did.
  const cursorPan = 100 / Number(before.spacing);
  const moved = Math.abs(horizontal.scroll - before.scroll);
  expect(moved).toBeGreaterThan(cursorPan * 0.85);
  expect(moved).toBeLessThanOrEqual(cursorPan * 1.005);
  await page.mouse.up();
});

// Trending bars: panning to older (lower) bars must refit the price window.
async function openTrendChart(page) {
  await openNativeApp(page);
  await page.evaluate(() => {
    const bars = Array.from({ length: 3000 }, (_, i) => {
      const close = 100 + i * 0.05 + Math.sin(i / 13) * 0.5;
      return {
        time: new Date((1700000000 + i * 60) * 1000).toISOString(),
        open: close - 0.1, high: close + 0.5, low: close - 0.5, close,
      };
    });
    showCandleData(bars);
  });
  await page.waitForFunction(() => chart.timeScale().getVisibleLogicalRange());
  const box = await page.locator("#chart-container").boundingBox();
  const rightScaleWidth = await page.evaluate(() => chart.priceScale("right").width());
  return { box, rightScaleWidth, x: box.x + (box.width - rightScaleWidth) * 0.35, y: box.y + box.height * 0.45 };
}

// Let the library apply pending moves and the 220ms auto-fit easing finish.
const readScale = (page) => page.evaluate(async () => {
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  await new Promise((r) => setTimeout(r, 300));
  return {
    range: _chartPriceRangeFromCanvas(),
    manual: _chartManualPriceRange,
    scroll: chart.timeScale().scrollPosition(),
    spacing: chart.timeScale().options().barSpacing,
  };
});

test("a horizontal drag keeps auto-fit: the price window refits as bars change", async ({ page }) => {
  const { x, y } = await openTrendChart(page);
  const before = await readScale(page);
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 400, y + 6, { steps: 30 });   // right = older, lower bars
  await page.waitForTimeout(250);                          // settle: no release inertia
  await page.mouse.up();
  const after = await readScale(page);
  const detail = JSON.stringify({ before, after });
  expect(after.manual, detail).toBeNull();
  expect(after.scroll, detail).toBeLessThan(before.scroll - 5);
  // Auto-fit extended down to the older, lower bars instead of freezing the
  // old window (the newest bars are still in view, so the top stays).
  expect(after.range.minValue, detail).toBeLessThan(before.range.minValue - 5);
});

test("two-finger trackpad swipes pan price vertically and time horizontally", async ({ page }) => {
  const { x, y } = await openTrendChart(page);
  await page.mouse.move(x, y);
  const before = await readScale(page);
  for (let i = 0; i < 6; i++) await page.mouse.wheel(0, 8);       // small deltas = trackpad
  const vertical = await readScale(page);
  for (let i = 0; i < 6; i++) await page.mouse.wheel(12, 0);
  const horizontal = await readScale(page);
  const detail = JSON.stringify({ before, vertical, horizontal });
  expect(vertical.manual, detail).not.toBeNull();
  // Swipe up (deltaY > 0) moves content up: the window moves to lower prices.
  expect(vertical.range.maxValue, detail).toBeLessThan(before.range.maxValue);
  expect(vertical.spacing, detail).toBeCloseTo(before.spacing, 6);   // no zoom
  expect(Math.abs(horizontal.scroll - vertical.scroll), detail).toBeGreaterThan(0.5);
  expect(horizontal.spacing, detail).toBeCloseTo(before.spacing, 6);
});

test("a mouse-wheel notch still zooms and keeps auto-fit", async ({ page }) => {
  const { x, y } = await openTrendChart(page);
  await page.mouse.move(x, y);
  const before = await readScale(page);
  await page.mouse.wheel(0, -100);
  const after = await readScale(page);
  const detail = JSON.stringify({ before, after });
  expect(after.manual, detail).toBeNull();
  expect(after.spacing, detail).not.toBeCloseTo(before.spacing, 3);
});

test("double-clicking the price axis returns the price window to auto-fit", async ({ page }) => {
  const { box, rightScaleWidth, x, y } = await openTrendChart(page);
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x, y + 80, { steps: 10 });
  await page.mouse.up();
  const manual = await readScale(page);
  expect(manual.manual).not.toBeNull();
  await page.mouse.dblclick(box.x + box.width - rightScaleWidth / 2, y);
  const reset = await readScale(page);
  expect(reset.manual, JSON.stringify({ manual, reset })).toBeNull();
});

test("a diagonal plot drag moves time and price in the same gesture", async ({ page }) => {
  // The old controller locked each drag to one axis after 5px, so a diagonal
  // drag could only move time OR price. Sample mid-gesture, mouse still down.
  await openNativeApp(page);
  await page.evaluate(() => {
    const bars = Array.from({ length: 3000 }, (_, i) => {
      const close = 100 + i * 0.01 + Math.sin(i / 13) * 0.5;
      return {
        time: new Date((1700000000 + i * 60) * 1000).toISOString(),
        open: close - 0.1, high: close + 0.5, low: close - 0.5, close,
      };
    });
    window.__chartPanProbe = bars[2700].close;
    showCandleData(bars);
  });
  await page.waitForFunction(() => chart.timeScale().getVisibleLogicalRange());
  const box = await page.locator("#chart-container").boundingBox();
  const rightScaleWidth = await page.evaluate(() => chart.priceScale("right").width());
  const x = box.x + Math.max(24, (box.width - rightScaleWidth) * 0.35);
  const y = box.y + box.height * 0.45;
  const read = () => page.evaluate(() => ({
    scroll: chart.timeScale().scrollPosition(),
    priceY: candleSeries.priceToCoordinate(window.__chartPanProbe),
  }));
  const before = await read();
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 60, y + 40, { steps: 6 });
  const mid = await read();
  await page.mouse.move(x + 120, y + 80, { steps: 6 });
  const late = await read();
  await page.mouse.up();

  // Both axes moved during the drag, and kept moving as the drag continued.
  expect(Math.abs(mid.scroll - before.scroll)).toBeGreaterThan(1);
  expect(Math.abs(mid.priceY - before.priceY)).toBeGreaterThan(10);
  expect(Math.abs(late.scroll - mid.scroll)).toBeGreaterThan(1);
  expect(Math.abs(late.priceY - mid.priceY)).toBeGreaterThan(10);
});

test("holding the plot does not create a second horizontal drift", async ({ page }) => {
  await openNativeApp(page);
  await page.evaluate(() => {
    const bars = Array.from({ length: 1800 }, (_, i) => {
      const close = 100 + Math.sin(i / 17) * 0.5;
      return {
        time: new Date((1700000000 + i * 60) * 1000).toISOString(),
        open: close - 0.1,
        high: close + 0.3,
        low: close - 0.3,
        close,
      };
    });
    showCandleData(bars);
  });
  await page.waitForFunction(() => chart.timeScale().getVisibleLogicalRange());

  const box = await page.locator("#chart-container").boundingBox();
  const x = box.x + box.width * 0.35;
  const y = box.y + box.height * 0.45;
  await page.mouse.move(x, y);
  const before = await page.evaluate(() => chart.timeScale().scrollPosition());
  await page.mouse.down();
  await page.waitForTimeout(300);
  const held = await page.evaluate(() => chart.timeScale().scrollPosition());
  await page.mouse.up();
  expect(Math.abs(held - before)).toBeLessThanOrEqual(0.05);
});

test("plot drag follows one monotonic horizontal position while held", async ({ page }) => {
  await openNativeApp(page);
  await page.evaluate(() => {
    const bars = Array.from({ length: 1800 }, (_, i) => {
      const close = 100 + Math.sin(i / 17) * 0.5;
      return {
        time: new Date((1700000000 + i * 60) * 1000).toISOString(),
        open: close - 0.1,
        high: close + 0.3,
        low: close - 0.3,
        close,
      };
    });
    showCandleData(bars);
  });
  await page.waitForFunction(() => chart.timeScale().getVisibleLogicalRange());

  const box = await page.locator("#chart-container").boundingBox();
  const x = box.x + box.width * 0.35;
  const y = box.y + box.height * 0.45;
  await page.mouse.move(x, y);
  await page.mouse.down();
  const samples = [];
  for (const dx of [12, 24, 36, 48, 60, 72, 84]) {
    await page.mouse.move(x + dx, y, { steps: 1 });
    await page.waitForTimeout(24);
    samples.push(await page.evaluate(() => chart.timeScale().scrollPosition()));
  }
  await page.mouse.up();

  // samples[0] is the move that crosses the library's 5px start threshold;
  // scrolling begins there, and every later move must advance monotonically.
  const detail = JSON.stringify(samples);
  const diffs = samples.slice(2).map((value, index) => value - samples[index + 1]);
  expect(diffs.every((diff) => diff < -0.01), detail).toBe(true);
  expect(samples[1] - samples[0], detail).toBeLessThanOrEqual(0);
});

test("background candle refresh keeps the user's horizontal viewport", async ({ page }) => {
  await openNativeApp(page);
  await page.evaluate(() => {
    window.__refreshBars = Array.from({ length: 900 }, (_, i) => {
      const close = 100 + Math.sin(i / 11) * 0.4;
      return {
        time: new Date((1700000000 + i * 60) * 1000).toISOString(),
        open: close - 0.1,
        high: close + 0.3,
        low: close - 0.3,
        close,
      };
    });
    showCandleData(window.__refreshBars);
  });
  await page.waitForFunction(() => {
    const range = chart.timeScale().getVisibleLogicalRange();
    return range && range.to - range.from > 1;
  });
  const before = await page.evaluate(() => {
    chart.timeScale().setVisibleLogicalRange({ from: 250, to: 410 });
    return new Promise(resolve => requestAnimationFrame(() =>
      resolve(chart.timeScale().getVisibleLogicalRange())
    ));
  });
  await page.evaluate(() => showCandleData(window.__refreshBars, true));
  const after = await page.evaluate(() => chart.timeScale().getVisibleLogicalRange());
  expect(after.from).toBeCloseTo(before.from, 3);
  expect(after.to).toBeCloseTo(before.to, 3);
});

test("backtest rendering never re-centers the free horizontal viewport", async ({ page }) => {
  await openNativeApp(page);
  await page.evaluate(() => {
    const bars = Array.from({ length: 1200 }, (_, i) => {
      const close = 100 + Math.sin(i / 17) * 0.5;
      return {
        time: new Date((1700000000 + i * 60) * 1000).toISOString(),
        open: close - 0.1,
        high: close + 0.3,
        low: close - 0.3,
        close,
      };
    });
    showCandleData(bars);
  });
  await page.waitForFunction(() => chart.timeScale().getVisibleLogicalRange());

  const before = await page.evaluate(() => new Promise((resolve) => {
    chart.timeScale().setVisibleLogicalRange({ from: 240, to: 420 });
    requestAnimationFrame(() => resolve(chart.timeScale().getVisibleLogicalRange()));
  }));
  await page.evaluate(() => renderChart({ zones: [], trades: [] }));
  const after = await page.evaluate(() => chart.timeScale().getVisibleLogicalRange());

  expect(after.from).toBeCloseTo(before.from, 3);
  expect(after.to).toBeCloseTo(before.to, 3);
});

test("scroll autoscale keeps the last presented range as the next animation origin", async ({ page }) => {
  await openNativeApp(page);
  const result = await page.evaluate(async () => {
    const bars = Array.from({ length: 600 }, (_, i) => {
      const close = 100 + Math.sin(i / 19) * 3;
      return { time: new Date((1700000000 + i * 60) * 1000).toISOString(),
        open: close - 0.2, high: close + 0.6, low: close - 0.6, close };
    });
    showCandleData(bars);
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    _chartAutoscaleInitialised = true;
    _chartAutoscaleTarget = { minValue: 0, maxValue: 100 };
    _chartAutoscaleEase = null;
    _chartAutoscalePresentedRange = { minValue: 90, maxValue: 110 };
    _chartAutoscaleHoldRange = null;
    _chartAutoscaleHoldUntil = 0;
    _holdChartAutoscaleDuringScroll();
    const held = _smoothChartAutoscale(() => ({ priceRange: { minValue: 20, maxValue: 180 } }));
    clearTimeout(_chartAutoscaleReleaseTimer);
    _chartAutoscaleReleaseTimer = null;
    _chartAutoscaleHoldUntil = 0;
    _chartAutoscaleHoldRange = null;
    const next = _smoothChartAutoscale(() => ({ priceRange: { minValue: 20, maxValue: 180 } }));
    return {
      held: held.priceRange,
      next: next.priceRange,
      presented: _chartAutoscalePresentedRange,
    };
  });
  expect(result.held).toEqual({ minValue: 90, maxValue: 110 });
  expect(result.next.minValue).toBeGreaterThan(20);
  expect(result.next.maxValue).toBeLessThan(180);
  expect(result.presented.minValue).toBeGreaterThan(20);
  expect(result.presented.maxValue).toBeLessThan(180);
});

test("manual trades without SL/TP do not create a risk box", async ({ page }) => {
  await openNativeApp(page);
  const result = await page.evaluate(() => {
    const bars = Array.from({ length: 240 }, (_, i) => {
      const close = 100 + Math.sin(i / 11) * 2;
      return {
        time: new Date((1770000000 + i * 60) * 1000).toISOString(),
        open: close - 0.25,
        high: close + 0.5,
        low: close - 0.5,
        close,
      };
    });
    showCandleData(bars);
    for (const key of Object.keys(CHART_OVERLAYS)) CHART_OVERLAYS[key] = false;
    CHART_OVERLAYS.trades = true;

    const countPainted = () => {
      const canvas = document.getElementById("pos-tool-overlay");
      if (!canvas) return 0;
      const data = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
      let count = 0;
      for (let i = 3; i < data.length; i += 4) if (data[i]) count += 1;
      return count;
    };
    const base = {
      entry_time: bars[100].time,
      entry_price: 100,
      exit_time: bars[110].time,
      exit_price: 101,
      direction: "buy",
    };
    drawPositionTools([{ ...base, sl_price: 0, tp_price: 0 }]);
    const withoutProtection = countPainted();
    drawPositionTools([{ ...base, sl_price: 99, tp_price: 101 }]);
    const withProtection = countPainted();
    return {
      withoutProtection,
      withProtection,
      autoScale: chart.priceScale("right").options().autoScale,
      provider: typeof candleSeries.options().autoscaleInfoProvider,
    };
  });

  expect(result.withoutProtection).toBe(0);
  expect(result.withProtection).toBeGreaterThan(0);
  expect(result.autoScale).toBe(true);
  expect(result.provider).toBe("function");
});

test("SL/TP fills do not become brighter from no-trade windows or overlap", async ({ page }) => {
  await openNativeApp(page);
  const result = await page.evaluate(async () => {
    const bars = Array.from({ length: 600 }, (_, i) => {
      const close = 100 + Math.sin(i / 11) * 2;
      return {
        time: new Date(Date.parse("2026-09-09T18:00:00Z") + i * 60 * 1000).toISOString(),
        open: close - 0.25,
        high: close + 0.5,
        low: close - 0.5,
        close,
      };
    });
    showCandleData(bars);
    for (const key of Object.keys(CHART_OVERLAYS)) CHART_OVERLAYS[key] = false;
    CHART_OVERLAYS.trades = true;
    const nextFrames = () => new Promise((resolve) => requestAnimationFrame(() => (
      requestAnimationFrame(resolve)
    )));

    const trade = {
      entry_time: bars[30].time,
      entry_price: 100,
      exit_time: bars[210].time,
      exit_price: 101,
      sl_price: 99,
      tp_price: 101,
      direction: "buy",
    };
    const greenFillProfile = () => {
      const canvas = document.getElementById("pos-tool-overlay");
      if (!canvas) return null;
      const data = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
      let maxAlpha = 0;
      for (let i = 0; i < data.length; i += 4) {
        if (data[i + 3] && data[i + 1] > data[i] + 20 && data[i + 1] > data[i + 2] + 20) {
          maxAlpha = Math.max(maxAlpha, data[i + 3]);
        }
      }
      return maxAlpha || null;
    };

    await nextFrames();
    chart.timeScale().setVisibleLogicalRange({ from: 0, to: 80 });
    await nextFrames();
    drawPositionTools([trade]);
    const normal = greenFillProfile();
    chart.timeScale().setVisibleLogicalRange({ from: 0, to: 260 });
    await nextFrames();
    drawPositionTools([trade]);
    const withNoTradeWindow = greenFillProfile();
    drawPositionTools([trade, trade]);
    const withOverlappingTrade = greenFillProfile();
    return { normal, withNoTradeWindow, withOverlappingTrade };
  });

  expect(result.normal).not.toBeNull();
  expect(Math.abs(result.withNoTradeWindow - result.normal)).toBeLessThanOrEqual(1);
  expect(Math.abs(result.withOverlappingTrade - result.normal)).toBeLessThanOrEqual(1);
});

test("trade time ranges keep date, meridiem, and separator columns aligned", async ({ page }) => {
  await openNativeApp(page);
  const alignment = await page.evaluate(() => {
    const host = document.createElement("div");
    host.innerHTML = [
      formatTradeTimeRangeMarkup("2026-09-10T02:30:00Z", "2026-09-10T03:32:00Z"),
      formatTradeTimeRangeMarkup("2026-09-10T15:49:00Z", "2026-09-10T16:50:00Z"),
      formatTradeTimeRangeMarkup("2026-09-10T15:23:00Z", "2026-09-10T15:25:00Z"),
    ].map((html) => `<div class="alignment-row">${html}</div>`).join("");
    document.body.appendChild(host);
    const rows = [...host.querySelectorAll(".trade-time-range")];
    const right = (selector, row) => row.querySelectorAll(selector)[0].getBoundingClientRect().right;
    const centers = rows.map((row) => {
      const box = row.querySelector(".trade-arrow").getBoundingClientRect();
      return box.left + box.width / 2;
    });
    const result = {
      labels: rows.map((row) => row.getAttribute("aria-label")),
      years: rows.map((row) => right(".trade-year", row)),
      startMeridiems: rows.map((row) => right(".trade-meridiem", row)),
      endMeridiems: rows.map((row) => row.querySelectorAll(".trade-meridiem")[1].getBoundingClientRect().right),
      arrows: centers,
    };
    host.remove();
    return result;
  });
  expect(alignment.labels[0]).toContain("09/09/2026");
  expect(Math.max(...alignment.years) - Math.min(...alignment.years)).toBeLessThan(0.1);
  expect(Math.max(...alignment.startMeridiems) - Math.min(...alignment.startMeridiems)).toBeLessThan(0.1);
  expect(Math.max(...alignment.endMeridiems) - Math.min(...alignment.endMeridiems)).toBeLessThan(0.1);
  expect(Math.max(...alignment.arrows) - Math.min(...alignment.arrows)).toBeLessThan(0.1);
});

test("Research owns the realized PNL curve instead of the lower tab", async ({ page }) => {
  await page.addInitScript(() => {
    const trades = [50, -20, 80].map((pnl, i) => ({
      trade_id: "research-" + i,
      entry_time: new Date(Date.UTC(2026, 8, 10 + i, 15)).toISOString(),
      exit_time: new Date(Date.UTC(2026, 8, 10 + i, 16)).toISOString(),
      pnl,
      size: 1,
      symbol: "MNQ",
      direction: "buy",
      entry_price: 20000,
      exit_price: 20010,
    }));
    localStorage.setItem("ancserTPX.lastBacktest.v1", JSON.stringify({
      market_clock_version: "america-new-york-v1",
      metrics: { total_trades: trades.length },
      trades,
      preset_name: "RESEARCH TEST",
      saved_at: new Date().toISOString(),
    }));
  });
  await openNativeApp(page);
  await page.locator('.app-rail .tab[data-tab="calendar"]').click();
  await expect(page.locator("#calendar-view")).toBeVisible();
  await expect(page.locator("#pnl-curve-body canvas")).toHaveCount(1);
  const research = await page.evaluate(() => ({
    title: document.querySelector(".research-pnl-panel .institution-title")?.textContent.trim(),
    canvas: document.querySelectorAll("#pnl-curve-body canvas").length,
    lowerPnlTab: document.querySelectorAll('.bottom-tab[data-btab="pnl"]').length,
    lowerPnlPanel: document.querySelectorAll("#btab-pnl").length,
    monthlyComparison: document.querySelectorAll("#cal-income-curve").length,
  }));
  expect(research.title).toBe("PNL CURVE");
  expect(research.canvas).toBe(1);
  expect(research.lowerPnlTab).toBe(0);
  expect(research.lowerPnlPanel).toBe(0);
  expect(research.monthlyComparison).toBe(0);
});

test("removes the empty title strip and keeps the expanded sidebar inside the viewport", async ({ page }) => {
  await openNativeApp(page);
  const layout = await page.evaluate(() => {
    const main = document.querySelector(".main");
    const sidebar = document.querySelector(".sidebar");
    const mainBox = main.getBoundingClientRect();
    const sidebarBox = sidebar.getBoundingClientRect();
    const root = document.documentElement;
    return {
      titleStrip: document.querySelector(".header"),
      main: {
        x: mainBox.x,
        right: mainBox.right,
        bottom: mainBox.bottom,
        height: mainBox.height,
      },
      viewport: { width: window.innerWidth, height: window.innerHeight },
      sidebar: {
        width: sidebarBox.width,
        overflowX: getComputedStyle(sidebar).overflowX,
      },
      pageOverflowX: root.scrollWidth - root.clientWidth,
      mainOverflowX: main.scrollWidth - main.clientWidth,
    };
  });

  expect(layout.titleStrip).toBeNull();
  expect(layout.main.x).toBeGreaterThanOrEqual(0);
  expect(layout.main.right).toBeLessThanOrEqual(layout.viewport.width + 1);
  expect(layout.main.bottom).toBeLessThanOrEqual(layout.viewport.height + 1);
  expect(layout.main.height).toBe(layout.viewport.height);
  const expectedSidebarWidth = Math.max(330, Math.min(layout.viewport.width * 0.264, 418));
  expect(Math.abs(layout.sidebar.width - expectedSidebarWidth)).toBeLessThanOrEqual(1);
  expect(layout.sidebar.overflowX).toBe("hidden");
  expect(layout.pageOverflowX).toBe(0);
  expect(layout.mainOverflowX).toBe(0);
});

test("uses clear icons and flat, darker controls for the parameter rail", async ({ page }) => {
  await openNativeApp(page);
  const surface = await page.evaluate(() => {
    const title = document.querySelector(".panel-title");
    const button = document.querySelector(".btn");
    const input = document.querySelector(".form-group input");
    const metric = document.createElement("div");
    metric.className = "metric-card";
    document.body.appendChild(metric);
    const root = getComputedStyle(document.documentElement);
    const titleBox = getComputedStyle(title);
    const result = {
      quickIcons: document.querySelectorAll("#chart-quick-btns .chart-button-icon").length,
      themeIcon: document.querySelector("#theme-toggle .theme-icon") !== null,
      connectionIcon: document.querySelector("#conn-trigger .connection-icon") !== null,
      limitRows: document.querySelectorAll(".form-row-limits").length,
      titles: [...document.querySelectorAll(".panel-title")].map((node) => node.textContent.trim()),
      titleBorders: [titleBox.borderTopWidth, titleBox.borderRightWidth, titleBox.borderBottomWidth],
      titleLeftBorder: titleBox.borderLeftWidth,
      buttonBorder: getComputedStyle(button).borderWidth,
      inputBorder: getComputedStyle(input).borderWidth,
      metricBorder: getComputedStyle(document.querySelector(".metric-card")).borderWidth,
      buttonSurface: root.getPropertyValue("--surface-button").trim(),
      inputSurface: root.getPropertyValue("--surface-input").trim(),
      performanceSource: document.querySelector("#perf-source"),
      backtestButton: (() => {
        const btn = document.querySelector("#btn-backtest");
        const css = getComputedStyle(btn);
        const wm = btn.querySelector(".wm-clip");
        const wmCss = wm ? getComputedStyle(wm, "::after") : null;
        const tl = getComputedStyle(btn, "::before");
        const br = getComputedStyle(btn, "::after");
        return {
          background: css.backgroundColor, border: css.borderTopWidth, overflow: css.overflow,
          height: Math.round(btn.getBoundingClientRect().height),
          label: [btn.querySelector(".micro")?.textContent, btn.querySelector(".hero-name")?.textContent],
          watermark: wm ? { text: wm.dataset.wm, hidden: wm.getAttribute("aria-hidden"),
            clip: getComputedStyle(wm).overflow, content: wmCss.content } : null,
          ticks: [tl.borderTopWidth, tl.borderLeftWidth, br.borderBottomWidth, br.borderRightWidth, tl.top, br.bottom],
        };
      })(),
      switch: (() => {
        const track = document.querySelector(".ui-switch.on");
        const thumb = track?.querySelector(".switch-thumb");
        return {
          trackRadius: track ? getComputedStyle(track).borderRadius : null,
          trackBackground: track ? getComputedStyle(track).backgroundColor : null,
          thumbRadius: thumb ? getComputedStyle(thumb).borderRadius : null,
        };
      })(),
    };
    metric.remove();
    return result;
  });

  expect(surface.quickIcons).toBe(2);
  expect(surface.themeIcon).toBe(true);
  expect(surface.connectionIcon).toBe(true);
  expect(surface.limitRows).toBe(4);
  expect(surface.titles).toContain("ENTRY");
  expect(surface.titles).toContain("EXIT");
  expect(surface.titles).not.toContain("ENTRY SETTINGS");
  expect(surface.titles).not.toContain("EXIT SETTINGS");
  expect(surface.titleBorders).toEqual(["0px", "0px", "0px"]);
  expect(surface.titleLeftBorder).toBe("0px");   // Relayout v2: no title bar
  expect(surface.buttonBorder).toBe("0px");
  expect(surface.inputBorder).toBe("0px");
  expect(surface.metricBorder).toBe("0px");
  expect(surface.buttonSurface).toBe("#2f3136");
  expect(surface.inputSurface).toBe("#131518");
  expect(surface.performanceSource).toBeNull();
  // 1.5.1g hero button (docs/UI_RENEWAL_FROM_AGEOFDEV.md §4–§5, user decision
  // 2026-11-08): gray surface, English two-line label, Chinese watermark cut by
  // the edge inside an aria-hidden clip, 「」 ticks drawn outside an unclipped box.
  expect(surface.backtestButton).toEqual({
    background: "rgb(47, 49, 54)", border: "0px", overflow: "visible", height: 92,
    label: ["EXECUTE", "BACKTEST"],
    watermark: { text: "回測", hidden: "true", clip: "hidden", content: '"回測"' },
    ticks: ["1px", "1px", "1px", "1px", "-6px", "-6px"],
  });
  expect(surface.switch.trackRadius).toBe("0px");
  expect(surface.switch.trackBackground).toBe("rgba(0, 0, 0, 0)");
  expect(surface.switch.thumbRadius).toBe("0px");
});

test("chart layer choices survive a reload and the first click toggles from the restored state", async ({ page }) => {
  // UI-022 (browser half; the old check lived in the removed glass-ui spec).
  await page.addInitScript(() => {
    if (!sessionStorage.getItem("layerTestSeeded")) {
      localStorage.removeItem("ancserTPX.chartLayers");
      sessionStorage.setItem("layerTestSeeded", "1");
    }
  });
  await openNativeApp(page);
  await page.locator("#chart-layer-btn").click();
  const wall = page.locator('#chart-layer-pop .ui-switch[data-switch-proxy="lp-optionwall"]');
  await expect(wall).toHaveAttribute("aria-checked", "false");
  await wall.click();
  await expect(wall).toHaveAttribute("aria-checked", "true");

  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => typeof candleSeries !== "undefined" && !!candleSeries);
  const restored = page.locator('#chart-layer-pop .ui-switch[data-switch-proxy="lp-optionwall"]');
  await expect(restored).toHaveAttribute("aria-checked", "true");
  expect(await page.evaluate(() => layerOn("optionwall"))).toBe(true);
  await page.locator("#chart-layer-btn").click();
  await restored.click();                       // first interaction after reload
  await expect(restored).toHaveAttribute("aria-checked", "false");
  expect(await page.evaluate(() => layerOn("optionwall"))).toBe(false);
});

test("contract out of sync shows the amber warning mark just above the date axis", async ({ page }) => {
  await openNativeApp(page);
  await page.waitForFunction(() => typeof candleSeries !== "undefined" && !!candleSeries);
  let inSync = false;
  const detail = "Chart live feed (Topstep): Z26 / History & backtests (calendar roll): H27";
  await page.route("**/api/connection/status", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      providers: {},
      startup: { backend: "connected", provider_count: 3 },
      contract: {
        in_sync: inSync,
        codes: { chart: "Z26", calendar: inSync ? "Z26" : "H27", topstep: "Z26" },
        detail: inSync ? "" : detail,
      },
    }),
  }));
  await page.evaluate(() => refreshConnectionStatus());
  const mark = page.locator("#chart-contract-sync");
  await expect(mark).toBeVisible();
  await expect(mark.locator(".tpx-warn .tpx-alert-mark")).toHaveCount(1);   // the shared icon
  await expect(mark).toHaveAttribute("title", detail);
  await expect(page.locator("#chart-contract-sync-text")).toHaveText("CONTRACT Z26 / STORE H27");
  const geometry = await page.evaluate(() => {
    const box = document.getElementById("chart-contract-sync").getBoundingClientRect();
    const container = document.getElementById("chart-container").getBoundingClientRect();
    return { top: box.top, bottom: box.bottom, left: box.left, right: box.right,
             cTop: container.top, cBottom: container.bottom, cLeft: container.left,
             cRight: container.right, axis: _timeAxisHeight(),
             color: getComputedStyle(document.querySelector("#chart-contract-sync .tpx-warn")).color };
  });
  // Inside the chart, above the date axis, within one axis-height of it.
  expect(geometry.bottom).toBeLessThanOrEqual(geometry.cBottom - geometry.axis);
  expect(geometry.bottom).toBeGreaterThan(geometry.cBottom - geometry.axis - 24);
  expect(geometry.left).toBeGreaterThan(geometry.cLeft);
  expect(geometry.right).toBeLessThan(geometry.cRight);
  expect(geometry.color).toBe("rgb(251, 191, 36)");                        // same amber as elsewhere
  // Never covers the chart tools / timeframe selector on the same row.
  const buttons = await page.locator("#chart-quick-btns").boundingBox();
  expect(geometry.right).toBeLessThanOrEqual(buttons.x);
  expect(Math.abs(geometry.bottom - (buttons.y + buttons.height))).toBeLessThan(2);

  inSync = true;
  await page.evaluate(() => refreshConnectionStatus());
  await expect(mark).toBeHidden();
});

test("PI OPTIONS model uses its own panel and endpoint; the options account arms ALERT mode", async ({ page }) => {
  await openNativeApp(page);
  await page.waitForFunction(() => typeof candleSeries !== "undefined" && !!candleSeries);
  const posted = { backtest: null, account: null };
  await page.route("**/api/backtest/pi-options", async (route) => {
    posted.backtest = JSON.parse(route.request().postData() || "{}");
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({
      strategy: "pi_options", zones: [], equity_curve: [],
      options_summary: { trades: 1, signals: 1, total: 495, max_dd: 0, pf: 999, without_best_2_dates: 0, data_window: {} },
      metrics: { total_trades: 1, wins: 1, losses: 0, win_rate: 1, avg_win: 495, avg_loss: 0, avg_rr_ratio: 0,
                 expectancy: 495, max_drawdown: 0, max_drawdown_pct: 0, profit_factor: 999,
                 max_consecutive_losses: 0, total_pnl: 495, daily_pnl: {}, weekly_stats: {} },
      trades: [{ trade_id: "PIOPT-1", strategy: "pi_options", asset: "option", symbol: "QQQ 603C 07/31", size: 5,
                 direction: "buy", entry_price: 2.0, entry_time: "2026-07-30T19:00:00+00:00", exit_price: 3.0,
                 exit_time: "2026-07-31T19:30:00+00:00", pnl: 493.5, commission: 6.5, fees: 0, exit_reason: "time" }],
      option_rows: [
        { status: "limit_not_filled", symbol: "SPY", tier: "LV2", contract: "SPY 667C 07/30", contracts: 0,
          signal_time: "2026-07-29T18:00:00+00:00", signal_ask: 2.5, limit: 1.5, net: 0, ret: null },
        { status: "filled", symbol: "QQQ", tier: "LV3", contract: "QQQ 603C 07/31", contracts: 5,
          signal_time: "2026-07-30T19:00:00+00:00", signal_ask: 2.0, limit: 1.2,
          entry_time: "2026-07-31T13:31:00+00:00", entry_price: 1.2, exit_time: "2026-07-31T19:30:00+00:00",
          exit_price: 3.0, exit_reason: "time", net: 893.5, ret: 1.5 },
      ],
    }) });
  });
  await page.route("**/api/options/account", async (route) => {
    if (route.request().method() === "POST") posted.account = JSON.parse(route.request().postData() || "{}");
    const on = !!(posted.account && posted.account.enabled);
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({
      account: { mode: "alert", enabled: on, settings: (posted.account || {}).settings || {} },
      broker: { connected: false, detail: "Not connected" },
      status: { running: on, tickets: [] },
    }) });
  });

  await page.selectOption("#strategy-bt", "pi_options");
  await expect(page.locator("#pi-options-params-bt")).toBeVisible();
  await expect(page.locator("#tr-params-bt")).toBeHidden();
  await expect(page.locator("#piopt-strike-bt")).toHaveValue("otm_3");   // research defaults
  await expect(page.locator("#piopt-entry-bt")).toHaveValue("0");
  await expect(page.locator("#piopt-exit-bt")).toHaveValue("15:30");
  await expect(page.locator("#piopt-size-bt")).toHaveValue("1");            // one contract per signal
  await expect(page.locator("#piopt-budget-bt")).toBeDisabled();
  await page.selectOption("#piopt-size-bt", "");                            // BY BUDGET
  await expect(page.locator("#piopt-budget-bt")).toBeEnabled();
  await page.selectOption("#piopt-size-bt", "1");
  await expect(page.locator("#piopt-budget-bt")).toBeDisabled();

  await page.evaluate(() => runBacktest());
  await expect(page.locator("#trades-tbody")).toContainText("5 QQQ 603C 07/31");
  // Every signal gets its own row, with option columns; unfilled limits stay listed.
  const headers = () => page.locator("#btab-trades thead th").allTextContents();
  expect(await headers()).toEqual(["CONTRACT", "TIME", "TIER", "SIGNAL ASK", "ENTRY", "EXIT", "RETURN", "NET P&L", "RESULT"]);
  const rows = page.locator("#trades-tbody tr");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText("+150.0%");                    // newest first
  await expect(rows.nth(0)).toContainText("+$893.50");   // Relayout v2 formatMoney
  await expect(rows.nth(0)).toContainText("TIME");
  // A limit filled the next morning: TIME starts at the signal, ENTRY carries the fill time.
  await expect(rows.nth(0).locator("td").nth(1)).toHaveAttribute("class", /trade-time-cell/);
  expect(await rows.nth(0).locator("td").nth(1).locator("[aria-label]").getAttribute("aria-label"))
    .toMatch(/^07\/30\/2026 03:00PM → 07\/31\/2026 03:30PM$/);
  await expect(rows.nth(0).locator("td").nth(4)).toHaveText("1.20 @ 07/31 09:31AM");
  await expect(rows.nth(1)).toContainText("MISSED");
  await expect(rows.nth(1)).toContainText("2.50 → LMT 1.50");
  expect(await rows.nth(1).locator("td").count()).toBe(9);
  expect(posted.backtest).toMatchObject({ strike: "otm_3", entry_discount: 0, exit_time: "15:30",
                                          stop: null, target: null, tiers: ["LV2", "LV3"], contracts: 1 });

  await page.evaluate(() => optionsAccountSave(true));
  await expect(page.locator("#opt-status")).toHaveText("ALERTS ON");
  expect(posted.account).toMatchObject({ mode: "alert", enabled: true });
  expect(posted.account.settings.strike).toBe("otm_3");
  expect(posted.account.settings.contracts).toBe(1);

  await page.selectOption("#strategy-bt", "factor");
  await expect(page.locator("#pi-options-params-bt")).toBeHidden();
  await expect(page.locator("#tr-params-bt")).toBeVisible();
  // A futures result restores the futures columns.
  await page.evaluate(() => renderTrades([{ entry_time: "2026-09-09T17:30:00Z", exit_time: "2026-09-09T18:00:00Z",
    entry_price: 29000, exit_price: 29010, direction: "buy", size: 1, symbol: "/MNQ", pnl: 20 }]));
  expect(await headers()).toEqual(["CONTRACT", "TIME", "DURATION", "ENTRY", "EXIT", "P&L", "COMMISSION", "FEES", "DIR"]);
});

test("PI STRICT MNQ REOPEN loads into the LIVE panel with only the 青π continuation armed", async ({ page }) => {
  await openNativeApp(page);
  await page.waitForFunction(() => !!(_presetsCache.presets || {})["PI STRICT MNQ REOPEN"]);
  const payload = await page.evaluate(() => {
    applyStrategyParams("live", _presetsCache.presets["PI STRICT MNQ REOPEN"]);
    return collectStrategyParams("live");
  });
  // The LIVE panel posts collectStrategyParams("live") to /live/start.
  expect(payload.strategy).toBe("pi");
  expect(payload.pi_continue_long_kinds).toEqual(["青π"]);
  expect(payload.pi_continue_short_kinds).toEqual([]);
  expect(payload.pi_long_kinds).toEqual(["青π"]);
  expect(payload.pi_short_kinds).toEqual(["粉π"]);
  expect(payload.pi_reopen_max_gap_r).toBe(1);
  expect(payload.pi_short_hold_min).toBe(60);
  expect(payload.contract_size).toBe(1);
  await expect(page.locator("#pi-continue-live-long-pi")).toHaveAttribute("aria-checked", "true");
  for (const id of ["long-level2", "long-level1", "short-pi", "short-level2", "short-level1"]) {
    await expect(page.locator(`#pi-continue-live-${id}`)).toHaveAttribute("aria-checked", "false");
  }
});

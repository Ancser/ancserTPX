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
  const switches = page.locator("#chart-layer-pop .glass-switch");
  await expect(switches).toHaveCount(11);
  const rows = page.locator("#chart-layer-pop .layer-row");
  const boxes = await switches.evaluateAll((nodes) => nodes.map((node) => {
    const box = node.getBoundingClientRect();
    return { x: box.x, right: box.right, width: box.width };
  }));
  expect(boxes).toHaveLength(11);
  expect(Math.max(...boxes.map((box) => box.x)) - Math.min(...boxes.map((box) => box.x))).toBeLessThanOrEqual(1);
  expect(Math.max(...boxes.map((box) => box.width)) - Math.min(...boxes.map((box) => box.width))).toBeLessThanOrEqual(1);
});

test("chart layer switches close, reopen, and toggle without proxy pixels", async ({ page }) => {
  await page.addInitScript(() => localStorage.removeItem("ancserTPX.chartLayers"));
  await openNativeApp(page);
  await page.locator("#chart-layer-btn").click();
  const popup = page.locator("#chart-layer-pop");
  const optionWall = popup.locator('.glass-switch[data-switch-proxy="lp-optionwall"]');
  await expect(optionWall).toHaveAttribute("aria-checked", "false");
  await expect(popup.locator(".glass-proxy-hidden")).toHaveCount(11);
  await expect.poll(() => popup.locator(".glass-proxy-hidden").evaluateAll((nodes) => (
    nodes.every((node) => getComputedStyle(node).display === "none")
  ))).toBe(true);

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
  await page.locator("#conn-trigger").click();
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
  expect(graphite).toEqual({ page: "#08090d", chart: "#08090d" });

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
  expect(baseMotion.theme).toBe("500ms");
  expect(baseMotion.chart).toContain("0.18s");
  expect(baseMotion.info).toContain("0.18s");
  expect(baseMotion.research).toContain("0.18s");
  expect(baseMotion.pnl).toContain("0.18s");

  await theme.click();
  await expect(page.locator("html")).toHaveAttribute("data-tpx-theme", "apple-light");
  await expect(theme).toHaveAttribute("data-theme", "apple-light");
  const themeMotion = await page.evaluate(() => ({
    transitioning: document.documentElement.classList.contains("theme-transitioning"),
    chart: getComputedStyle(document.querySelector("#chart-container")).transitionDuration,
    info: getComputedStyle(document.querySelector("#bottom-panel")).transitionDuration,
    research: getComputedStyle(document.querySelector("#calendar-view")).transitionDuration,
    pnl: getComputedStyle(document.querySelector("#pnl-curve-canvas")).transitionDuration,
  }));
  expect(themeMotion.transitioning).toBe(true);
  expect(themeMotion.chart).toContain("0.5s");
  expect(themeMotion.info).toContain("0.5s");
  expect(themeMotion.research).toContain("0.5s");
  expect(themeMotion.pnl).toContain("0.5s");
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
  expect(shell.watermarkFont).toContain("Orbitron");
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
  // Mouse inertia is owned by the sensitivity-aware controller so release
  // cannot snap back to Lightweight Charts' uncorrected offset.
  expect(initial.kineticMouse).toBe(false);

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
    autoScale: chart.priceScale("right").options().autoScale,
    priceY: candleSeries.priceToCoordinate(window.__chartPanProbe),
  }));
  expect(vertical.autoScale).toBe(false);
  expect(Math.abs(vertical.priceY - before.priceY)).toBeGreaterThan(10);
  await page.mouse.up();

  await page.evaluate(() => chart.priceScale("right").applyOptions({ autoScale: true }));
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 100, y, { steps: 8 });
  const horizontal = await page.evaluate(() => ({
    scroll: chart.timeScale().scrollPosition(),
    spacing: chart.timeScale().options().barSpacing,
  }));
  const expectedPan = (100 / Number(before.spacing)) * 1.10;
  expect(Math.abs(horizontal.scroll - before.scroll)).toBeGreaterThan(expectedPan * 0.95);
  await page.mouse.up();
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

  const diffs = samples.slice(1).map((value, index) => value - samples[index]);
  expect(diffs.every((diff) => diff < -0.01)).toBe(true);
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
        const css = getComputedStyle(document.querySelector("#btn-backtest"));
        return { background: css.backgroundColor, border: css.borderTopWidth, color: css.color };
      })(),
      switch: (() => {
        const track = document.querySelector(".glass-switch.on");
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
  expect(surface.titleLeftBorder).toBe("3px");
  expect(surface.buttonBorder).toBe("0px");
  expect(surface.inputBorder).toBe("0px");
  expect(surface.metricBorder).toBe("0px");
  expect(surface.buttonSurface).toBe("#1b2535");
  expect(surface.inputSurface).toBe("#080c14");
  expect(surface.performanceSource).toBeNull();
  expect(surface.backtestButton).toEqual({ background: "rgba(0, 0, 0, 0)", border: "1px", color: "rgb(0, 229, 160)" });
  expect(surface.switch.trackRadius).toBe("999px");
  expect(surface.switch.trackBackground).toBe("rgba(0, 0, 0, 0)");
  expect(surface.switch.thumbRadius).toBe("50%");
});

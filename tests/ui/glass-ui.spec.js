const path = require("node:path");
const { expect, test } = require("@playwright/test");

const chartBundle = path.resolve(
  "node_modules/lightweight-charts/dist/lightweight-charts.standalone.production.js",
);

const canonicalModels = [
  ["fade", "FADE"],
  ["sigma", "SIGMA"],
  ["factor", "FACTOR"],
  ["momentum", "MOMENTUM"],
  ["betafib", "BETAFIB"],
  ["pi", "PI"],
];

async function openApp(page) {
  await page.addInitScript(() => {
    localStorage.setItem("ancserTPX.uiLang", "en");
    localStorage.setItem("ancserTPXTheme", "light");
  });
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname === "127.0.0.1") {
      await route.continue();
      return;
    }
    if (url.hostname === "unpkg.com" && url.pathname.endsWith(
      "/lightweight-charts.standalone.production.js",
    )) {
      await route.fulfill({
        path: chartBundle,
        contentType: "application/javascript",
      });
      return;
    }
    await route.abort("blockedbyclient");
  });

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.locator('html[data-tpx-glass-skin="on"]')).toHaveCount(1);
  await expect.poll(() => page.evaluate(() => (
    window.TpxGlass?.diagnostics?.components?.precision || 0
  ))).toBeGreaterThanOrEqual(2);
  await expect.poll(() => page.locator("#preset-bt option").count())
    .toBeGreaterThan(1);
  await settleTwoFrames(page);
}

/* The PI LONG/SHORT matrix only renders for a PI preset, and which preset the
 * app opens on comes from `last_used_bt` in the developer's live
 * data/presets.json — the running server rewrites that every time someone
 * clicks a preset in the UI. Tests that reach for #pi-matrix-* were therefore
 * passing or failing based on what was last clicked, with a null boundingBox
 * as the only clue. Select one explicitly instead of inheriting that state. */
async function selectPiPreset(page) {
  const chosen = await page.evaluate(async () => {
    const sel = document.querySelector("#preset-bt");
    if (!sel) return null;
    const opt = [...sel.options].find(o => /^PI\b/i.test(o.value || o.textContent));
    if (!opt) return null;
    if (sel.value !== opt.value) {
      sel.value = opt.value;
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    }
    return opt.value;
  });
  expect(chosen, "no PI preset in data/presets.json").not.toBeNull();
  await expect(page.locator("#pi-matrix-bt-long-pi")).toBeVisible();
  await settleTwoFrames(page);
  return chosen;
}

async function settleTwoFrames(page) {
  await page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
}

async function stageCopyCount(page) {
  return page.evaluate(() => document.querySelectorAll(".optical-stage-copy").length);
}

async function localeMaterialSnapshot(page) {
  return page.evaluate(() => {
    const live = document.querySelector("#lang-toggle");
    const copy = live?.querySelector(
      ':scope > .lang-thumb > .optical-layer '
      + '.optical-stage-copy[data-stage="switch"].lang-toggle.glass-switch',
    );
    const material = (node) => {
      if (!node) return null;
      const style = getComputedStyle(node);
      return {
        on: node.classList.contains("on"),
        background: style.backgroundColor,
        borderColor: style.borderTopColor,
        trackColor: style.getPropertyValue("--switch-track-color").trim(),
      };
    };
    return { live: material(live), copy: material(copy) };
  });
}

async function popupMaterialSnapshot(page) {
  return page.evaluate(() => {
    const live = document.querySelector("#chart-layer-pop");
    const lens = document.querySelector(
      'body > .main > .chart-lens[data-optical="precision"]',
    );
    const copies = lens
      ? [...lens.querySelectorAll(".optical-stage-copy.optical-tier-2-source")]
      : [];
    const copy = copies
      .flatMap((stage) => [...stage.querySelectorAll(
        '.chart-layer-pop[data-glass-tier="1"]',
      )])
      .find((node) => node.querySelector(".layer-title")?.textContent.trim()
        === "CHART LAYERS");
    const material = (node) => {
      if (!node) return null;
      const style = getComputedStyle(node);
      return {
        background: style.backgroundColor,
        borderWidth: style.borderTopWidth,
        borderColor: style.borderTopColor,
        radius: style.borderTopLeftRadius,
        shadow: style.boxShadow,
        backdrop: style.backdropFilter,
        before: getComputedStyle(node, "::before").content,
        after: getComputedStyle(node, "::after").content,
      };
    };
    return { live: material(live), copy: material(copy), lens: material(lens) };
  });
}

async function openLayerPopup(page) {
  const tools = page.getByRole("button", { name: "Chart tools", exact: true });
  await expect(tools).toHaveCount(1);
  await tools.click();

  const layerButton = page.locator("#chart-layer-btn");
  await expect(layerButton).toBeVisible();
  await layerButton.click();
  await expect(page.locator("#chart-layer-pop")).toBeVisible();
}

test("version shows in the top-left brand only, never in the tab title", async ({ page }) => {
  await openApp(page);

  // The tab title is deliberately version-free; the brand badge carries it.
  await expect(page).toHaveTitle("ancserTPX");
  const ver = page.locator("body > .glass-topbar > .topbar-brand > .ver");
  await expect(ver).toHaveText("1.1.1");
  // The skin used to hardcode this string as well as the markup, so the two
  // drifted apart. Pin that the badge is whatever index.html declares.
  const declared = await page.evaluate(async () => {
    const html = await (await fetch("/static/ancserTPX.html")).text();
    const m = html.match(/letter-spacing:2px;margin-left:2px;">\s*([^\s<]+)/);
    return m ? m[1] : null;
  });
  expect(declared).toBe("1.1.1");
  const watermark = page.locator("#chart-container > .chart-watermark");
  await expect(watermark).toHaveCount(1);
  await expect(watermark.locator("small")).toHaveCount(0);
  await expect(watermark).toHaveText("ancserTPX");
});

/* 1.0.10p. Every static guard around the popup switches passed while the
   control was visibly broken, because they all measured the RESTING state.
   These three drive the switch and read what is actually on screen. */
test("a popup switch keeps its own lens while it is being dragged", async ({ page }) => {
  await openApp(page);
  await page.evaluate(() => window.toggleChartLayerMenu(true));
  await page.waitForSelector("#chart-layer-pop:not(.hidden)");
  await settleTwoFrames(page);

  const track = page.locator("#chart-layer-pop > .layer-row > .glass-switch").first();
  const box = await track.boundingBox();
  await page.mouse.move(box.x + 8, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width - 8, box.y + box.height / 2, { steps: 8 });

  const live = await page.evaluate(() => {
    const sw = document.querySelector("#chart-layer-pop > .layer-row > .glass-switch");
    const thumb = sw.querySelector(":scope > .switch-thumb");
    const layer = thumb.querySelector(":scope > .optical-layer");
    const layerStyle = getComputedStyle(layer);
    return {
      interacting: sw.classList.contains("interacting"),
      // The positive half: the drag really did raise this switch's glass.
      glass: Number.parseFloat(sw.style.getPropertyValue("--switch-glass")),
      thumbVisibility: getComputedStyle(thumb).visibility,
      layerVisibility: layerStyle.visibility,
      layerOpacity: Number.parseFloat(layerStyle.opacity),
      // A clone carries no optical layer, so it must never paint the dark
      // lens backdrop: that is what drew a black pill over the live thumb.
      cloneBackdrops: [...document.querySelectorAll(
        ".optical-stage-copy .glass-switch, .optical-stage-copy.glass-switch",
      )].filter((clone) => {
        const kid = clone.querySelector(":scope > .switch-thumb");
        return kid && Number.parseFloat(
          getComputedStyle(kid, "::before").opacity || "0") > 0.01;
      }).length,
    };
  });
  await page.mouse.up();

  expect(live.interacting).toBe(true);
  expect(live.glass).toBeGreaterThan(0.5);
  expect(live.thumbVisibility).toBe("visible");
  expect(live.layerVisibility).toBe("visible");
  expect(live.layerOpacity).toBeGreaterThan(0.5);
  expect(live.cloneBackdrops).toBe(0);
});

test("releasing a switch never repaints the thumb solid --bg", async ({ page }) => {
  await openApp(page);
  await selectPiPreset(page);
  const track = page.locator("#pi-matrix-bt-long-pi");
  const restingFace = await page.evaluate(() => getComputedStyle(
    document.querySelector("#pi-matrix-bt-long-pi > .switch-thumb"),
  ).backgroundColor);
  const box = await track.boundingBox();
  await page.mouse.move(box.x + 8, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width - 8, box.y + box.height / 2, { steps: 8 });

  // Sample every frame across the release, where the lens-up material used to
  // outlive the lens itself and blink black.
  await page.evaluate(() => {
    const sw = document.querySelector("#pi-matrix-bt-long-pi");
    const thumb = sw.querySelector(":scope > .switch-thumb");
    window.__release = [];
    const t0 = performance.now();
    const tick = () => {
      const before = getComputedStyle(thumb, "::before");
      window.__release.push({
        face: getComputedStyle(thumb).backgroundColor,
        glass: Number.parseFloat(sw.style.getPropertyValue("--switch-glass") || "0"),
        backdrop: Number.parseFloat(before.opacity || "0"),
      });
      if (performance.now() - t0 < 500) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
  await page.mouse.up();
  await page.waitForTimeout(700);

  const frames = await page.evaluate(() => window.__release);
  expect(frames.length).toBeGreaterThan(3);
  // The thumb's own face is the resting colour on every single frame; only the
  // ::before backdrop moves, and it moves with --switch-glass, not on a
  // separate class-triggered transition.
  const faces = new Set(frames.map((f) => f.face));
  expect([...faces]).toEqual([restingFace]);
  // ...and the glass really was up at some point, or the loop above proves
  // nothing about a release it never saw.
  expect(Math.max(...frames.map((f) => f.glass))).toBeGreaterThan(0.1);
  for (const f of frames) {
    expect(Math.abs(f.backdrop - f.glass)).toBeLessThan(0.02);
  }
});

test("sweep model scope opens as a glass-switch dropdown", async ({ page }) => {
  await openApp(page);
  await selectPiPreset(page);

  const trigger = page.locator("#sweep-model-btn");
  await expect(trigger).toBeVisible();
  await expect(trigger).toHaveText("…");
  await trigger.click();

  const popup = page.locator("#sweep-model-pop");
  await expect(popup).toBeVisible();
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  const switches = popup.locator('.sweep-model-row > .sweep-model-switch[role="switch"]');
  // ALL + one switch per backend-dispatchable model. The exact roster is
  // owned by test_sweep_model_scope.py (it is checked against the backend);
  // this test only cares that every offered model is a working glass switch.
  // Count live switches only: every optical surface leaves a stage copy in the
  // DOM carrying the same data- attributes, so a bare querySelectorAll double
  // counts. This is the same filter _sweepModelButtons() applies in the app.
  const modelCount = await page.evaluate(() => [...document.querySelectorAll(
    "#sweep-model-pop [data-sweep-model]",
  )].filter((n) => !n.closest(".optical-stage-copy")).length - 1);
  expect(modelCount).toBeGreaterThan(0);
  await expect(switches).toHaveCount(modelCount + 1);
  await expect(switches.first()).toHaveAttribute("aria-checked", "true");
  const actionGeometry = await page.evaluate(() => {
    const sweep = document.querySelector("#btn-sweep").getBoundingClientRect();
    const execute = document.querySelector("#btn-backtest").getBoundingClientRect();
    const model = document.querySelector("#sweep-model-btn").getBoundingClientRect();
    return {
      executeHeight: Math.round(execute.height),
      sweepHeight: Math.round(sweep.height),
      modelHeight: Math.round(model.height),
      modelWidth: Math.round(model.width),
      modelThumb: (() => {
        const thumb = document.querySelector(
          "#sweep-model-pop > .sweep-model-row > .glass-switch > .switch-thumb",
        );
        const rect = thumb?.getBoundingClientRect();
        return rect ? [Math.round(rect.width), Math.round(rect.height)] : null;
      })(),
    };
  });
  expect(actionGeometry.executeHeight).toBe(42);
  expect(actionGeometry.sweepHeight).toBe(actionGeometry.executeHeight);
  expect(actionGeometry.modelHeight).toBe(actionGeometry.executeHeight);
  expect(actionGeometry.modelWidth).toBe(actionGeometry.modelHeight);
  expect(actionGeometry.modelThumb).toEqual([32, 22]);
  const geometry = await page.evaluate(() => {
    const track = document.querySelector("#sweep-model-pop > .sweep-model-row > .glass-switch");
    const thumb = track?.querySelector(":scope > .switch-thumb");
    const param = document.querySelector("#pi-matrix-bt-long-pi.glass-switch");
    const paramThumb = param?.querySelector(":scope > .switch-thumb");
    const size = (node, child) => [
      parseFloat(getComputedStyle(node).width),
      parseFloat(getComputedStyle(node).height),
      parseFloat(getComputedStyle(child).width),
      parseFloat(getComputedStyle(child).height),
    ];
    return {
      sweep: size(track, thumb),
      param: size(param, paramThumb),
    };
  });
  expect(geometry.sweep.map(Math.round)).toEqual(geometry.param.map(Math.round));

  await switches.nth(1).click({ delay: 60 });
  await expect(switches.nth(1)).toHaveAttribute("aria-checked", "true");
  await expect(switches.first()).toHaveAttribute("aria-checked", "false");
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  await trigger.click();
  await expect(popup).toBeHidden();
});

async function expectMainLensAt(page, target) {
  const box = await target.boundingBox();
  expect(box).not.toBeNull();
  const point = {
    x: box.x + box.width / 2,
    y: box.y + box.height / 2,
  };
  await page.mouse.move(point.x, point.y);

  await expect.poll(() => page.evaluate(({ x, y }) => {
    const lens = document.querySelector(
      'body > .main > .chart-lens[data-optical="precision"]',
    );
    const popup = document.querySelector("#chart-layer-pop");
    if (!lens || !popup) return false;
    const style = getComputedStyle(lens);
    const rect = lens.getBoundingClientRect();
    const lensZ = Number.parseInt(style.zIndex, 10);
    const popupZ = Number.parseInt(getComputedStyle(popup).zIndex, 10);
    return style.pointerEvents === "none"
      && Number(style.opacity) > 0.05
      && rect.width > 0
      && rect.height > 0
      && x >= rect.left
      && x <= rect.right
      && y >= rect.top
      && y <= rect.bottom
      && lensZ > popupZ;
  }, point), { timeout: 3_000 }).toBe(true);
}

async function installCanvasCanary(page) {
  const key = await page.evaluate(() => {
    const canvas = document.createElement("canvas");
    canvas.dataset.uiTestCanvas = "hydration";
    canvas.width = 64;
    canvas.height = 32;
    canvas.setAttribute("aria-hidden", "true");
    Object.assign(canvas.style, {
      position: "absolute",
      left: "360px",
      top: "260px",
      width: "64px",
      height: "32px",
      opacity: "0.01",
      pointerEvents: "none",
    });
    const context = canvas.getContext("2d");
    context.fillStyle = "#ff00aa";
    context.fillRect(0, 0, canvas.width, canvas.height);
    document.querySelector("#chart-container").appendChild(canvas);
    window.TpxGlass.resample(document.querySelector(".main"));
    return canvas.dataset.glassCanvasKey || "";
  });
  expect(key).toMatch(/^gc\d+$/);

  const chart = await page.locator("#chart-container").boundingBox();
  expect(chart).not.toBeNull();
  await page.mouse.move(
    chart.x + chart.width * 0.55,
    chart.y + chart.height * 0.45,
  );

  await expect.poll(() => canvasMirrorState(page, key)).toMatchObject({
    found: true,
    hydrated: true,
  });
  return key;
}

async function canvasMirrorState(page, key) {
  return page.evaluate((canvasKey) => {
    const lens = document.querySelector(
      'body > .main > .chart-lens[data-optical="precision"]',
    );
    const mirrors = lens
      ? [...lens.querySelectorAll(
        `canvas[data-glass-canvas-key="${canvasKey}"]`,
      )]
      : [];
    return {
      found: mirrors.length > 0,
      hydrated: mirrors.some((canvas) => canvas.width > 1 && canvas.height > 1),
      laidOut: mirrors.some((canvas) => (
        canvas.getBoundingClientRect().width > 1
        && canvas.getBoundingClientRect().height > 1
      )),
      dormant: mirrors.length > 0
        && mirrors.every((canvas) => canvas.width <= 1 && canvas.height <= 1),
    };
  }, key);
}

test.beforeEach(async ({ page }) => {
  await openApp(page);
});

test("model identities and language state have one presentation truth", async ({ page }) => {
  for (const id of ["strategy-bt", "strategy-live"]) {
    const options = await page.locator(`#${id} option`).evaluateAll((nodes) => (
      nodes.map((node) => [node.value, node.textContent.trim()])
    ));
    expect(options).toEqual(canonicalModels);
  }

  for (const mode of ["bt", "live"]) {
    await page.evaluate((selectedMode) => {
      const select = document.querySelector(`#strategy-${selectedMode}`);
      select.value = "momentum";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }, mode);
    await expect(page.locator(`#strategy-${mode} option:checked`)).toHaveText("MOMENTUM");
    await expect(page.locator(`#strategy-desc-${mode}`)).toHaveText(
      "Intraday momentum continuation.",
    );
    expect(await page.evaluate((selectedMode) => (
      collectStrategyParams(selectedMode).strategy
    ), mode)).toBe("momentum");
  }

  const language = page.locator("#lang-toggle");
  const languageThumb = language.locator(
    ":scope > .lang-thumb[data-optical=\"switch\"]",
  );
  await expect(language).toHaveCount(1);
  await expect(language).toHaveAttribute("role", "switch");
  await expect(language).toHaveClass(/\bglass-switch\b/);
  await expect(language).toHaveAttribute("data-stage", "switch");
  await expect(language).toHaveAttribute("aria-checked", "false");
  const languageGlyph = languageThumb.locator(":scope > .lang-glyph");
  await expect(languageGlyph).toHaveText("En");
  await expect(languageGlyph).toHaveCSS("opacity", "1");
  await expect.poll(() => languageThumb.locator(
    ":scope > .optical-layer .optical-stage-copy[data-stage=\"switch\"]",
  ).count()).toBeGreaterThan(0);
  expect(await languageThumb.locator(
    ":scope > .optical-layer .optical-stage-copy:not([data-stage=\"switch\"])",
  ).count()).toBe(0);
  const topbarGeometry = await page.evaluate(() => {
    const box = (node) => {
      const rect = node.getBoundingClientRect();
      return [rect.width, rect.height];
    };
    const locale = document.querySelector("#lang-toggle");
    const themeSwitch = document.querySelector("#theme-switch");
    return {
      localeTrack: box(locale),
      themeTrack: box(themeSwitch),
      localeThumb: box(locale.querySelector(":scope > .lang-thumb")),
      themeThumb: box(themeSwitch.querySelector(":scope > .switch-thumb")),
    };
  });
  expect(topbarGeometry.localeTrack).toEqual(topbarGeometry.themeTrack);
  expect(topbarGeometry.localeThumb).toEqual(topbarGeometry.themeThumb);
  await expect(page.locator("html")).toHaveAttribute("lang", "en");

  await expect.poll(async () => {
    const material = await localeMaterialSnapshot(page);
    return material.copy !== null
      && JSON.stringify(material.copy) === JSON.stringify(material.live);
  }).toBe(true);
  const englishMaterial = await localeMaterialSnapshot(page);
  expect(englishMaterial.copy).toEqual(englishMaterial.live);
  await page.evaluate(() => {
    const toggle = window.toggleLanguage;
    window.__tpxLanguageToggleCalls = 0;
    window.toggleLanguage = (...args) => {
      window.__tpxLanguageToggleCalls += 1;
      return toggle(...args);
    };
  });

  const languageBox = await language.boundingBox();
  expect(languageBox).not.toBeNull();
  await page.mouse.move(
    languageBox.x + languageBox.width / 2,
    languageBox.y + languageBox.height / 2,
  );
  await page.mouse.down();
  await expect(language).toHaveClass(/\binteracting\b/);
  await expect(languageGlyph).toHaveCSS("opacity", "0");
  await page.mouse.up();
  await expect.poll(() => page.evaluate(() => window.__tpxLanguageToggleCalls)).toBe(1);
  await expect(language).toHaveAttribute("aria-checked", "true");
  await expect(language).toHaveClass(/\bon\b/);
  await expect(languageGlyph).toHaveText("中");
  await expect.poll(() => language.evaluate((node) => (
    !node.classList.contains("interacting")
  )), { timeout: 8_000 }).toBe(true);
  await expect(languageGlyph).toHaveCSS("opacity", "1");
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-TW");
  expect(await page.evaluate(() => localStorage.getItem("ancserTPX.uiLang"))).toBe("zh");
  await expect.poll(async () => {
    const material = await localeMaterialSnapshot(page);
    return material.copy?.on === true
      && JSON.stringify(material.copy) === JSON.stringify(material.live);
  }).toBe(true);
  const chineseMaterial = await localeMaterialSnapshot(page);
  expect(chineseMaterial.copy).toEqual(chineseMaterial.live);
  expect(chineseMaterial.live.background).toBe(englishMaterial.live.background);
  expect(chineseMaterial.live.borderColor).toBe(englishMaterial.live.borderColor);
  for (const mode of ["bt", "live"]) {
    await expect(page.locator(`#strategy-${mode} option:checked`)).toHaveText("MOMENTUM");
    await expect(page.locator(`#strategy-desc-${mode}`)).toHaveText("日內動能延續。");
  }

  await language.focus();
  await language.press("Enter");
  await expect.poll(() => page.evaluate(() => window.__tpxLanguageToggleCalls)).toBe(2);
  await expect(language).toHaveAttribute("aria-checked", "false");
  await expect(language).not.toHaveClass(/\bon\b/);
  await expect(languageGlyph).toHaveText("En");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  expect(await page.evaluate(() => localStorage.getItem("ancserTPX.uiLang"))).toBe("en");

  await language.press(" ");
  await expect.poll(() => page.evaluate(() => window.__tpxLanguageToggleCalls)).toBe(3);
  await expect(language).toHaveAttribute("aria-checked", "true");
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-TW");

  await page.evaluate(() => document.querySelector("#lang-toggle").click());
  await expect.poll(() => page.evaluate(() => window.__tpxLanguageToggleCalls)).toBe(4);
  await expect(language).toHaveAttribute("aria-checked", "false");
  await expect(languageGlyph).toHaveText("En");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
});

test("English parameter chrome is canonical and round-trips to Chinese", async ({ page }) => {
  await page.evaluate(() => {
    for (const mode of ["bt", "live"]) {
      const select = document.querySelector(`#strategy-${mode}`);
      select.value = "pi";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });

  const expectedPiOptions = [
    ["long_pi_only", "LONG ONLY · π LEVELS (RECOMMENDED)"],
    ["long_all", "LONG ONLY · ALL BLUE (INCLUDES LIGHT-BLUE CIRCLE)"],
    ["pi_only", "π LEVELS + DARK-BLUE CIRCLE (INCLUDES SHORTS)"],
    ["pi_strict", "PURE π ONLY (CYAN π / PINK π)"],
    ["all", "ALL BLUE/PURPLE (INCLUDES WEAK SIGNALS)"],
  ];
  for (const mode of ["bt", "live"]) {
    expect(await page.locator(`#pi-signal-set-${mode} option`).evaluateAll((nodes) => (
      nodes.map((node) => [node.value, node.textContent.trim()])
    ))).toEqual(expectedPiOptions);
  }

  expect(await page.evaluate(() => [...document.querySelectorAll(
    ".sidebar label, .sidebar option, .sidebar .lbl-hint:not(.validation-hint)",
  )].filter((node) => !node.closest(".optical-layer, .optical-stage-copy"))
    .map((node) => node.textContent.trim())
    .filter((text) => /[\u3400-\u9fff]/u.test(text)))).toEqual([]);

  await expect(page.locator("#pi-params-bt label").first()).toContainText("SIGNAL SET");
  await expect(page.locator("#tp-cap-hint-bt")).toHaveText(
    "(per-trade profit cap · 0=unlimited)",
  );

  await page.evaluate(() => window.toggleLanguage());
  await expect(page.locator("#pi-params-bt label").first()).toContainText("使用訊號");
  await expect(page.locator("#pi-signal-set-bt option:checked")).toHaveText(
    "只做多 · π 級別 (推薦)",
  );
  await expect(page.locator("#tp-cap-hint-bt")).toHaveText(
    "(單筆獲利上限 · 0=不限)",
  );

  await page.evaluate(() => window.toggleLanguage());
  await expect(page.locator("#pi-params-bt label").first()).toContainText("SIGNAL SET");
  await expect(page.locator("#pi-signal-set-bt option:checked")).toHaveText(
    "LONG ONLY · π LEVELS (RECOMMENDED)",
  );

  await page.evaluate(() => {
    const strategy = document.querySelector("#strategy-bt");
    strategy.value = "betafib";
    strategy.dispatchEvent(new Event("change", { bubbles: true }));
    const rule = document.querySelector("#factor-sl-rule-bt");
    rule.value = "fib";
    window.onFactorRiskAnchorChange("bt", "sl");
  });
  await expect(page.locator("#factor-sl-value-bt option").first()).toHaveText(
    "Determined by SL fib",
  );
  await page.evaluate(() => window.toggleLanguage());
  await expect(page.locator("#factor-sl-value-bt option").first()).toHaveText(
    "由 SL fib 決定",
  );
});

test("PI parameters use the LONG/SHORT liquid-glass matrix and preserve preset payloads", async ({ page }) => {
  await page.evaluate(() => {
    const strategy = document.querySelector("#strategy-bt");
    strategy.value = "pi";
    strategy.dispatchEvent(new Event("change", { bubbles: true }));
  });

  const matrix = page.locator('#pi-params-bt [data-pi-matrix="bt"]').first();
  await expect(matrix).toBeVisible();
  await expect(matrix.locator(".pi-matrix-column")).toHaveText(["LONG", "SHORT"]);
  await expect(matrix.locator(".pi-matrix-row-label")).toHaveText(["PI", "LEVEL 2", "LEVEL 1"]);
  await expect(matrix.locator('.pi-matrix-grid > .pi-matrix-cell > .glass-switch')).toHaveCount(6);
  await expect(matrix.locator('.pi-matrix-grid > .pi-matrix-cell > .glass-switch > .switch-thumb.optical-surface[data-optical="switch"]')).toHaveCount(6);
  const thumbStyles = await matrix.locator('.pi-matrix-grid > .pi-matrix-cell > .glass-switch > .switch-thumb').evaluateAll((nodes) => nodes.map((node) => {
    const style = getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return { position: style.position, width: rect.width, height: rect.height, opacity: Number(style.opacity) };
  }));
  expect(thumbStyles.every((thumb) => thumb.position === "absolute" && thumb.width > 0 && thumb.height > 0 && thumb.opacity > 0)).toBe(true);
  await expect(page.locator("#factor-params-bt")).toBeHidden();

  let payload = await page.evaluate(() => collectStrategyParams("bt"));
  expect(payload.pi_signal_set).toBe("long_pi_only");
  expect(payload.pi_long_only).toBe(true);
  expect(payload.pi_long_kinds).toEqual(["青π", "深蓝圈"]);
  expect(payload.pi_short_kinds).toEqual([]);

  await page.locator("#pi-params-bt #pi-matrix-bt-long-level1").click();
  payload = await page.evaluate(() => collectStrategyParams("bt"));
  expect(payload.pi_signal_set).toBe("long_all");
  expect(payload.pi_long_kinds).toEqual(["青π", "深蓝圈", "淡蓝圈"]);

  await page.locator("#pi-params-bt #pi-matrix-bt-short-pi").click();
  payload = await page.evaluate(() => collectStrategyParams("bt"));
  expect(payload.pi_long_only).toBe(false);
  expect(payload.pi_short_kinds).toEqual(["粉π"]);

  await page.evaluate(() => applyStrategyParams("bt", {
    strategy: "pi",
    pi_signal_set: "all",
    pi_long_only: false,
  }));
  await expect(page.locator("#pi-params-bt #pi-matrix-bt-long-level1")).toHaveClass(/on/);
  await expect(page.locator("#pi-params-bt #pi-matrix-bt-short-level1")).not.toHaveClass(/on/);
  await expect(page.locator("#pi-params-bt #pi-matrix-bt-short-level1")).toBeDisabled();
  payload = await page.evaluate(() => collectStrategyParams("bt"));
  expect(payload.pi_signal_set).toBe("all");
  expect(payload.pi_short_kinds).toEqual(["粉π"]);
});

test("parameter help tooltip follows the single UI locale", async ({ page }) => {
  const help = page.locator(
    ".form-group:has(> #preset-bt) > label .help-dot",
  );
  const tooltip = page.locator("#global-help-tooltip");
  await expect(help).toHaveAttribute("data-tip-en", /Load or save/);
  await expect(help).toHaveAttribute("data-tip-zh", /載入或保存/);

  await help.focus();
  await expect(tooltip).toBeVisible();
  await expect(tooltip).toHaveText("Load or save the current parameter set.");
  expect(await tooltip.evaluate((node) => /[\u3400-\u9fff]/u.test(node.textContent))).toBe(false);

  await page.evaluate(() => window.toggleLanguage());
  await expect(tooltip).toContainText("載入或保存目前所有參數設定");
  await page.evaluate(() => window.toggleLanguage());
  await expect(tooltip).toHaveText("Load or save the current parameter set.");
  expect(await tooltip.evaluate((node) => /[\u3400-\u9fff]/u.test(node.textContent))).toBe(false);
});

test("requested compact-control geometry matches its surrounding controls", async ({ page }) => {
  const geometry = await page.evaluate(() => {
    const live = (selector) => [...document.querySelectorAll(selector)]
      .find((node) => !node.closest(".optical-layer, .optical-stage-copy"));
    const size = (selector) => {
      const rect = live(selector).getBoundingClientRect();
      return [rect.width, rect.height];
    };
    return {
      tuner: size(".glass-tuner .tuner-trigger"),
      avatar: size(".account-orb"),
      tunerOptical: live(".glass-tuner .tuner-trigger").hasAttribute("data-optical"),
      multiplierFonts: [...document.querySelectorAll(".form-mult")]
        .filter((node) => !node.closest(".optical-layer, .optical-stage-copy"))
        .map((node) => getComputedStyle(node).fontSize),
      selectFonts: ["#contract-bt", "#contract-live"]
        .map((selector) => getComputedStyle(live(selector)).fontSize),
    };
  });
  expect(geometry.tuner).toEqual([63, 63]);
  expect(geometry.tuner).toEqual(geometry.avatar);
  expect(geometry.tunerOptical).toBe(false);
  expect(geometry.multiplierFonts).toEqual(["18px", "18px"]);
  expect(geometry.multiplierFonts).toEqual(geometry.selectFonts);
});

test("Chart tools contains latest and layers only after auto-center removal", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await expect(page.locator("#btn-auto-center")).toHaveCount(0);

  const actions = page.locator("#chart-quick-btns > .fab-action");
  expect(await actions.evaluateAll((nodes) => nodes.map((node) => ({
    id: node.id,
    index: node.dataset.fabAction,
  })))).toEqual([
    { id: "btn-scroll-latest", index: "0" },
    { id: "chart-layer-btn", index: "1" },
  ]);

  const copiesBefore = await stageCopyCount(page);
  const tools = page.getByRole("button", { name: "Chart tools", exact: true });
  await tools.click();
  await expect(actions).toHaveCount(2);
  await expect(actions.nth(0)).toBeVisible();
  await expect(actions.nth(1)).toBeVisible();

  await page.evaluate(() => {
    const jump = window.scrollToLatest;
    window.__tpxLatestCalls = 0;
    window.scrollToLatest = (...args) => {
      window.__tpxLatestCalls += 1;
      return jump(...args);
    };
  });
  await page.locator("#btn-scroll-latest").click();
  await expect.poll(() => page.evaluate(() => window.__tpxLatestCalls)).toBe(1);
  await page.locator("#chart-layer-btn").click();
  await expect(page.locator("#chart-layer-pop")).toBeVisible();
  await settleTwoFrames(page);
  expect(await stageCopyCount(page)).toBe(copiesBefore);
  expect(pageErrors).toEqual([]);
});

test("Precision samples Tier-1 popup material without recursive Glass", async ({ page }) => {
  // Compares the popup switch against the PI matrix switch, so the PI matrix
  // has to exist regardless of which preset was last clicked in the live UI.
  await selectPiPreset(page);
  const tierSummary = await page.evaluate(() => {
    const liveRoots = [...document.querySelectorAll("[data-optical]")]
      .filter((node) => !node.closest(".optical-layer, .optical-stage-copy"));
    return {
      precision: liveRoots.filter((node) => (
        node.dataset.optical === "precision" && node.dataset.glassTier === "2"
      )).length,
      lower: liveRoots.filter((node) => (
        node.dataset.optical !== "precision" && node.dataset.glassTier === "1"
      )).length,
      wrong: liveRoots.filter((node) => (
        node.dataset.glassTier !== (node.dataset.optical === "precision" ? "2" : "1")
      )).length,
    };
  });
  expect(tierSummary.precision).toBeGreaterThanOrEqual(2);
  expect(tierSummary.lower).toBeGreaterThan(0);
  expect(tierSummary.wrong).toBe(0);

  const copiesBefore = await stageCopyCount(page);
  await openLayerPopup(page);

  const popup = page.locator("#chart-layer-pop");
  await expect(popup).toHaveAttribute("data-glass-tier", "1");
  expect(await popup.locator(".layer-name").allTextContents()).toEqual([
    "EMAPMO ▲▼",
    "PI π / CIRCLES",
    "TRADE BOXES SL/TP",
    "MREV BUBBLES",
    "KDJMA DOTS",
    "INTRAMOM ARROWS",
    "VAH/VAL/POC LINES",
    "SESSION VA",
    "BETAFIB LEVELS",
    "DAY ZONE LEVELS",
  ]);
  const popupSwitches = popup.locator('.layer-row > .glass-switch[role="switch"]');
  expect(await popupSwitches.count()).toBeGreaterThan(0);
  await expect(popupSwitches.first()).toHaveAttribute("data-glass-material", "local");
  await expect(popup.locator(
    '.layer-row > .glass-switch > .switch-thumb.optical-surface[data-optical="switch"]',
  )).toHaveCount(10);
  // 1.0.10p: no per-popup optics override — these sample exactly like the
  // parameter switches do.
  await expect(popup.locator(
    ".layer-row > .glass-switch > .switch-thumb[data-glass-shrink]",
  )).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => (
    [...document.querySelectorAll("#chart-layer-pop > .layer-row > .glass-switch")]
      .slice(0, 3)
      .every((track) => !track.classList.contains("interacting")
        && track.style.getPropertyValue("--switch-glass") === "0.0000")
  ))).toBe(true);
  await expect.poll(() => page.evaluate(() => (
    [...document.querySelectorAll(
      "#chart-layer-pop > .layer-row > .glass-switch > .switch-thumb.optical-surface",
    )].slice(0, 3).every((thumb) => {
      const layer = thumb.querySelector(":scope > .optical-layer");
      return Boolean(layer) && getComputedStyle(layer).opacity === "0";
    })
  ))).toBe(true);
  await page.evaluate(() => {
    const track = document.querySelector("#chart-layer-pop > .layer-row > .glass-switch");
    track?.tpxSetState?.(track.getAttribute("aria-checked") === "true");
  });
  await expect.poll(() => page.evaluate(() => {
    const track = document.querySelector("#chart-layer-pop > .layer-row > .glass-switch");
    const thumb = track?.querySelector(":scope > .switch-thumb");
    return Boolean(track && thumb)
      && !track.classList.contains("interacting")
      && getComputedStyle(thumb).backgroundColor !== "rgb(8, 9, 13)";
  })).toBe(true);

  await expect.poll(() => page.evaluate(() => {
    const lens = document.querySelector(
      'body > .main > .chart-lens[data-optical="precision"]',
    );
    const copies = lens
      ? [...lens.querySelectorAll(".optical-stage-copy.optical-tier-2-source")]
      : [];
    const popupCopy = copies
      .flatMap((copy) => [...copy.querySelectorAll(
        '.chart-layer-pop[data-glass-tier="1"]',
      )])
      .find((node) => node.querySelector(".layer-title")?.textContent.trim()
        === "CHART LAYERS");
    if (!popupCopy || popupCopy.classList.contains("hidden")) return false;
    const style = getComputedStyle(popupCopy);
    return style.display !== "none"
      && style.backgroundColor !== "rgba(0, 0, 0, 0)"
      && Number.parseFloat(style.borderTopWidth) >= 1;
  })).toBe(true);

  const assertPopupMaterial = (material) => {
    expect(material.live).not.toBeNull();
    expect(material.copy).toEqual(material.live);
    expect(material.live.borderWidth).toBe(material.lens.borderWidth);
    expect(material.live.borderColor).toBe(material.lens.borderColor);
    expect(material.live.shadow).toBe(material.lens.shadow);
    expect(material.live.backdrop).toContain("blur(20px)");
    expect(material.live.before).toBe("none");
    expect(material.live.after).toBe("none");
  };

  let popupMaterial = await popupMaterialSnapshot(page);
  assertPopupMaterial(popupMaterial);

  await page.evaluate(() => window.TpxGlass.setTheme("dark"));
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await settleTwoFrames(page);
  await expect.poll(async () => {
    const material = await popupMaterialSnapshot(page);
    return material.copy !== null
      && JSON.stringify(material.copy) === JSON.stringify(material.live);
  }).toBe(true);
  popupMaterial = await popupMaterialSnapshot(page);
  assertPopupMaterial(popupMaterial);

  const recursion = await page.evaluate(() => {
    const lens = document.querySelector(
      'body > .main > .chart-lens[data-optical="precision"]',
    );
    const copies = lens
      ? [...lens.querySelectorAll(".optical-stage-copy.optical-tier-2-source")]
      : [];
    return {
      copies: copies.length,
      nestedOpticalLayers: copies.reduce(
        (count, copy) => count + copy.querySelectorAll(".optical-layer").length,
        0,
      ),
      renderedTierTwo: copies.reduce((count, copy) => count + [
        ...copy.querySelectorAll('[data-glass-tier="2"]'),
      ].filter((node) => {
        const style = getComputedStyle(node);
        return style.display !== "none" && style.visibility !== "hidden";
      }).length, 0),
    };
  });
  expect(recursion.copies).toBeGreaterThan(0);
  expect(recursion.nestedOpticalLayers).toBe(0);
  expect(recursion.renderedTierTwo).toBe(0);

  await expectMainLensAt(page, popup.locator(".layer-title"));
  await expectMainLensAt(page, popupSwitches.first());

  const switchGeometry = await page.evaluate(() => {
    const popupSwitch = document.querySelector(
      "#chart-layer-pop > .layer-row > .glass-switch[role=\"switch\"]",
    );
    const paramSwitch = document.querySelector(
      "#pi-matrix-bt-long-pi.glass-switch",
    );
    const geometry = (track) => {
      const thumb = track?.querySelector(":scope > .switch-thumb");
      const style = track ? getComputedStyle(track) : null;
      const thumbStyle = thumb ? getComputedStyle(thumb) : null;
      return {
        width: style?.width,
        height: style?.height,
        thumbWidth: thumbStyle?.width,
        thumbHeight: thumbStyle?.height,
        after: thumb ? getComputedStyle(thumb, "::after").content : null,
      };
    };
    return { popup: geometry(popupSwitch), param: geometry(paramSwitch) };
  });
  expect(switchGeometry.popup).toEqual({
    ...switchGeometry.param,
    after: "none",
  });

  const checkedBefore = await popupSwitches.first().getAttribute("aria-checked");
  const proxyKey = await popupSwitches.first().getAttribute("data-switch-proxy");
  await page.evaluate((key) => {
    window.__tpxPopupPointerMaterial = null;
    const track = document.querySelector(
      `#chart-layer-pop > .layer-row > [data-switch-proxy="${key}"]`,
    );
    track.addEventListener("pointerdown", () => requestAnimationFrame(() => {
      window.__tpxPopupPointerMaterial = {
        interacting: track.classList.contains("interacting"),
        track: getComputedStyle(track).backgroundColor,
        thumb: getComputedStyle(track.querySelector(".switch-thumb")).backgroundColor,
        center: getComputedStyle(
          track.querySelector(".switch-thumb"), "::after",
        ).backgroundColor,
        centerContent: getComputedStyle(
          track.querySelector(".switch-thumb"), "::after",
        ).content,
        backing: getComputedStyle(track).getPropertyValue("--bg").trim(),
      };
    }), { once: true });
  }, proxyKey);
  await popupSwitches.first().click({ delay: 100 });
  await expect.poll(() => page.evaluate(() => (
    window.__tpxPopupPointerMaterial?.interacting || false
  ))).toBe(true);
  // 1.0.10p: the lens-up --bg material lives on .switch-thumb::before and is
  // driven by --switch-glass. The thumb's own face stays the resting colour
  // throughout, so read the backdrop, not the face.
  await expect.poll(() => page.evaluate(() => {
    const track = document.querySelector("#chart-layer-pop > .layer-row > .glass-switch");
    if (!track?.classList.contains("interacting")) return false;
    const thumb = track.querySelector(".switch-thumb");
    return getComputedStyle(thumb, "::before").backgroundColor === "rgb(8, 9, 13)"
      && Number.parseFloat(getComputedStyle(thumb, "::before").opacity) > 0.5;
  })).toBe(true);
  const localMaterial = await page.evaluate(() => {
    const track = document.querySelector("#chart-layer-pop > .layer-row > .glass-switch");
    const thumb = track?.querySelector(".switch-thumb");
    return {
      ...window.__tpxPopupPointerMaterial,
      track: getComputedStyle(track).backgroundColor,
      thumb: getComputedStyle(thumb, "::before").backgroundColor,
      center: getComputedStyle(thumb, "::after").backgroundColor,
      centerContent: getComputedStyle(thumb, "::after").content,
    };
  });
  expect(localMaterial.backing).toMatch(/^#[0-9a-f]{6}$/i);
  const expectedBacking = `rgb(${[1, 3, 5].map((offset) => (
    Number.parseInt(localMaterial.backing.slice(offset, offset + 2), 16)
  )).join(", ")})`;
  expect(localMaterial.thumb).toBe(expectedBacking);
  expect(localMaterial.thumb).not.toBe(localMaterial.track);
  expect(localMaterial.centerContent).toBe("none");
  await expect(popupSwitches.first()).toHaveAttribute(
    "aria-checked",
    checkedBefore === "true" ? "false" : "true",
  );
  const expectedOn = checkedBefore !== "true";
  await expect.poll(() => page.evaluate(({ key, on }) => {
    const lens = document.querySelector(
      'body > .main > .chart-lens[data-optical="precision"]',
    );
    const live = document.querySelector(
      `#chart-layer-pop > .layer-row > [data-switch-proxy="${key}"]`,
    );
    const copies = lens
      ? [...lens.querySelectorAll(
        `.optical-tier-2-source [data-switch-proxy="${key}"]`,
      )]
      : [];
    if (!live || !copies.length) return false;
    const liveThumb = live.querySelector(":scope > .switch-thumb");
    const progress = Number.parseFloat(
      live.style.getPropertyValue("--switch-progress"),
    );
    const finalPosition = Number.isFinite(progress)
      && (on ? progress >= 0.98 : progress <= 0.02);
    return finalPosition && copies.every((copy) => {
      const copyThumb = copy.querySelector(":scope > .switch-thumb");
      return copy.className === live.className
        && copy.style.getPropertyValue("--switch-progress")
          === live.style.getPropertyValue("--switch-progress")
        && copyThumb?.style.left === liveThumb?.style.left
        && copyThumb?.style.transform === liveThumb?.style.transform;
    });
  }, { key: proxyKey, on: expectedOn }), { timeout: 3_000 }).toBe(true);
  await settleTwoFrames(page);
  expect(await stageCopyCount(page)).toBe(copiesBefore);
});

test("ordinary switch samples only its local track material", async ({ page }) => {
  const theme = page.locator("#theme-switch");
  const thumb = theme.locator(':scope > .switch-thumb[data-optical="switch"]');
  const icon = thumb.locator(":scope > #theme-icon");
  await expect(thumb).toHaveAttribute("data-glass-tier", "1");
  await expect(icon).toHaveText("☀");
  await expect(icon).toHaveCSS("opacity", "1");
  await expect.poll(() => thumb.locator(
    '.optical-stage-copy[data-stage="switch"]',
  ).count()).toBeGreaterThan(0);

  const box = await theme.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await expect(theme).toHaveClass(/\binteracting\b/);
  await expect(icon).toHaveCSS("opacity", "0");
  await expect.poll(() => page.evaluate(() => {
    const track = document.querySelector("#theme-switch");
    const liveLayer = track.querySelector(":scope > .switch-thumb > .optical-layer");
    const copies = [...track.querySelectorAll(
      ':scope > .switch-thumb .optical-stage-copy[data-stage="switch"]',
    )];
    return {
      layerVisible: liveLayer && Number(getComputedStyle(liveLayer).opacity) > 0,
      cloneCount: copies.length,
      stateSynced: copies.length > 0 && copies.every((copy) => (
        copy.classList.contains("on") === track.classList.contains("on")
      )),
      sampledMarksHidden: copies.length > 0 && copies.every((copy) => {
        const mark = copy.querySelector(
          ":scope > .switch-thumb > .switch-state-icon",
        );
        return mark && Number(getComputedStyle(mark).opacity) === 0;
      }),
      materialSynced: copies.length > 0 && copies.every((copy) => (
        getComputedStyle(copy).backgroundColor
          === getComputedStyle(track).backgroundColor
      )),
    };
  })).toMatchObject({
    layerVisible: true,
    stateSynced: true,
    sampledMarksHidden: true,
    materialSynced: true,
  });
  await page.mouse.up();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(icon).toHaveText("☾");
  await expect.poll(() => theme.evaluate((node) => (
    !node.classList.contains("interacting")
  )), { timeout: 8_000 }).toBe(true);
  await expect(icon).toHaveCSS("opacity", "1");
});

test("dense parameter hints remain available on demand", async ({ page }) => {
  const countsBefore = await page.evaluate(() => ({
    surfaces: window.TpxGlass.diagnostics.surfaces,
    stageCopies: document.querySelectorAll(".optical-stage-copy").length,
  }));

  const migrated = await page.evaluate(() => {
    const hints = [...document.querySelectorAll(
      ".lbl-hint:not(.validation-hint)",
    )];
    return {
      count: hints.length,
      exposed: hints.filter((hint) => (
        getComputedStyle(hint).display !== "none"
        || hint.getAttribute("aria-hidden") !== "true"
      )).length,
    };
  });
  expect(migrated.count).toBeGreaterThan(0);
  expect(migrated.exposed).toBe(0);

  await page.locator("#strategy-bt").selectOption("pi");
  expect(await page.evaluate(() => [...document.querySelectorAll(
    ".sidebar .help-dot",
  )].filter((dot) => !dot.closest(".optical-layer, .optical-stage-copy"))
    .filter((dot) => !dot.closest("label")).length)).toBe(0);

  const help = page.locator(
    "#pi-params-bt label .help-dot[data-help-sources]",
  ).first();
  await expect(help).toBeVisible();
  expect((await help.getAttribute("data-help-sources")).split(",")).toHaveLength(2);
  await help.focus();
  await expect(page.locator("#global-help-tooltip")).toBeVisible();
  await expect(page.locator("#global-help-tooltip")).toContainText("Discord alerts");
  await expect(page.locator("#global-help-tooltip")).toContainText("level combination");
  await expect(help).toHaveAttribute("aria-expanded", "true");
  await help.click();
  await expect(page.locator("#global-help-tooltip")).toBeVisible();
  await help.press("Escape");
  await expect(page.locator("#global-help-tooltip")).toBeHidden();
  await expect(help).toHaveAttribute("aria-expanded", "false");

  await page.locator("#strategy-bt").selectOption("betafib");
  await page.locator("#factor-sl-rule-bt").selectOption("fib");
  const warning = page.locator("#betafib-fiblevels-bt .validation-hint");
  await expect(warning).toBeVisible();
  await expect(warning).toContainText("SL <");

  await settleTwoFrames(page);
  expect(await page.evaluate(() => ({
    surfaces: window.TpxGlass.diagnostics.surfaces,
    stageCopies: document.querySelectorAll(".optical-stage-copy").length,
  }))).toEqual(countsBefore);
});

for (const target of ["backtest", "live"]) {
  test(`dormant Precision canvas hydrates on Research to ${target}`, async ({ page }) => {
    const copiesBefore = await stageCopyCount(page);
    const tab = (name) => page.locator(
      `body > .glass-topbar > .glass-dock > .tab[data-tab="${name}"]`,
    );

    const key = await installCanvasCanary(page);

    await tab("calendar").click();
    await expect(tab("calendar")).toHaveClass(/\bactive\b/);
    await expect(page.locator("body > .main")).toBeHidden();
    await expect.poll(
      () => canvasMirrorState(page, key),
      { timeout: 5_000 },
    ).toMatchObject({ found: true, dormant: true });

    // Deterministic stale-root case: a maintenance rebuild while Research has
    // .main hidden captures display:none even when canvas topology is unchanged.
    await page.evaluate(() => window.TpxGlass.resample(
      document.querySelector("body > .main"),
    ));
    await expect.poll(() => page.evaluate(() => {
      const copy = document.querySelector(
        'body > .main > .chart-lens[data-optical="precision"] '
        + '> .optical-layer > .optical-world > .optical-stage-copy',
      );
      return copy ? getComputedStyle(copy).display : null;
    })).toBe("none");

    await tab(target).click();
    await expect(tab(target)).toHaveClass(/\bactive\b/);
    await expect(page.locator("body > .main")).toBeVisible();
    let chart = await page.locator("#chart-container").boundingBox();
    expect(chart).not.toBeNull();
    await page.mouse.move(
      chart.x + chart.width * 0.55,
      chart.y + chart.height * 0.45,
    );
    await settleTwoFrames(page);
    expect(await canvasMirrorState(page, key)).toMatchObject({
      found: true,
      hydrated: true,
      laidOut: true,
    });

    // A canvas inserted while the destination is hidden used to wait behind
    // Precision's active spring for about six seconds.
    await tab("calendar").click();
    await expect(page.locator("body > .main")).toBeHidden();

    const lateKey = await page.evaluate(() => {
      const canvas = document.createElement("canvas");
      canvas.dataset.uiTestCanvas = "late-workspace-canvas";
      canvas.dataset.glassCanvasKey = "gc-ui-late-workspace";
      canvas.width = 48;
      canvas.height = 24;
      Object.assign(canvas.style, {
        position: "absolute",
        left: "430px",
        top: "300px",
        width: "48px",
        height: "24px",
        opacity: "0.01",
        pointerEvents: "none",
      });
      const context = canvas.getContext("2d");
      context.fillStyle = "#00e5a0";
      context.fillRect(0, 0, canvas.width, canvas.height);
      document.querySelector("#chart-container").appendChild(canvas);
      return canvas.dataset.glassCanvasKey;
    });

    await tab(target).click();
    await expect(tab(target)).toHaveClass(/\bactive\b/);
    await expect(page.locator("body > .main")).toBeVisible();
    chart = await page.locator("#chart-container").boundingBox();
    expect(chart).not.toBeNull();
    await page.mouse.move(
      chart.x + chart.width * 0.55,
      chart.y + chart.height * 0.45,
    );

    await settleTwoFrames(page);
    expect(await canvasMirrorState(page, key)).toMatchObject({
      found: true,
      hydrated: true,
      laidOut: true,
    });
    expect(await canvasMirrorState(page, lateKey)).toMatchObject({
      found: true,
      hydrated: true,
      laidOut: true,
    });
    expect(await stageCopyCount(page)).toBe(copiesBefore);
  });
}

test("calendar grid owns each shared edge once and draws a complete today frame", async ({ page }) => {
  await openApp(page);
  await page.locator(
    'body > .glass-topbar > .glass-dock > .tab[data-tab="calendar"]',
  ).click();
  await expect(page.locator("#calendar-view")).toBeVisible();
  await expect.poll(() => page.locator("#cal-grid > .cal-cell").count())
    .toBeGreaterThan(0);

  const geometry = await page.evaluate(() => {
    const grid = document.querySelector("#cal-grid");
    const cells = [...(grid?.children || [])];
    const today = grid?.querySelector(".cal-cell.cal-today");
    const style = (node, pseudo) => node
      ? getComputedStyle(node, pseudo)
      : null;
    const first = style(cells[0]);
    const second = style(cells[1]);
    const firstBelow = style(cells[7]);
    const last = style(cells[cells.length - 1]);
    const lastRow = cells.slice(-7);
    const lastRowEmpty = lastRow.filter((cell) => cell.classList.contains("cal-empty"));
    const todayAfter = style(today, "::after");
    return {
      cellCount: cells.length,
      margin: today ? style(today).margin : null,
      todayZ: today ? style(today).zIndex : null,
      todayAfterBorder: todayAfter ? [
        todayAfter.borderTopWidth,
        todayAfter.borderRightWidth,
        todayAfter.borderBottomWidth,
        todayAfter.borderLeftWidth,
      ] : null,
      sharedVertical: first && second ? [
        first.borderRightWidth,
        second.borderLeftWidth,
      ] : null,
      sharedHorizontal: first && firstBelow ? [
        first.borderBottomWidth,
        firstBelow.borderTopWidth,
      ] : null,
      outerRight: last ? last.borderRightWidth : null,
      outerBottom: last ? last.borderBottomWidth : null,
      emptyBottom: lastRowEmpty.map((cell) => {
        const emptyStyle = style(cell);
        return {
          width: emptyStyle.borderBottomWidth,
          color: emptyStyle.borderBottomColor,
        };
      }),
    };
  });

  expect(geometry.cellCount % 7).toBe(0);
  expect(geometry.margin).toBe("0px");
  expect(Number(geometry.todayZ)).toBeGreaterThanOrEqual(2);
  expect(geometry.todayAfterBorder).toEqual(["1px", "1px", "1px", "1px"]);
  expect(geometry.sharedVertical).toEqual(["0px", "1px"]);
  expect(geometry.sharedHorizontal).toEqual(["0px", "1px"]);
  expect(geometry.outerRight).toBe("1px");
  expect(geometry.outerBottom).toBe("1px");
  expect(geometry.emptyBottom.every(({ width, color }) => (
    width === "1px" && color !== "rgba(0, 0, 0, 0)"
  ))).toBe(true);
});

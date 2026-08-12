/* TEMPORARY diagnostic probe — delete after investigation. */
const path = require("node:path");
const { test } = require("@playwright/test");

const chartBundle = path.resolve(
  "node_modules/lightweight-charts/dist/lightweight-charts.standalone.production.js",
);

async function openApp(page) {
  await page.addInitScript(() => {
    localStorage.setItem("ancserTPX.uiLang", "en");
    localStorage.setItem("ancserTPXTheme", "dark");
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
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.waitForSelector('html[data-tpx-glass-skin="on"]');
  await page.waitForTimeout(1500);
}

async function dragShot(page, locator, file) {
  const box = await locator.boundingBox();
  if (!box) return { missing: true };
  await page.mouse.move(box.x + 10, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width - 10, box.y + box.height / 2, { steps: 8 });
  await page.waitForTimeout(140);
  const state = await locator.evaluate((sw) => {
    const thumb = sw.querySelector(':scope > .switch-thumb');
    const layer = thumb && thumb.querySelector(':scope > .optical-layer');
    const cs = thumb && getComputedStyle(thumb);
    return {
      cls: sw.className,
      thumbVisibility: cs && cs.visibility,
      thumbBg: cs && cs.backgroundColor,
      layerOpacity: layer && getComputedStyle(layer).opacity,
      layerVisibility: layer && getComputedStyle(layer).visibility,
    };
  });
  await page.screenshot({ path: `.playwright-output/${file}`, clip: {
    x: Math.max(0, box.x - 90), y: Math.max(0, box.y - 26),
    width: box.width + 180, height: box.height + 52,
  }});
  await page.mouse.up();
  await page.waitForTimeout(350);
  return state;
}

test("probe", async ({ page }) => {
  const out = {};
  await openApp(page);

  out.diagnostics = await page.evaluate(() => ({
    surfaces: window.TpxGlass?.diagnostics?.surfaces,
    stageCopies: document.querySelectorAll('.optical-stage-copy').length,
    biggestCopyNodes: Math.max(...[...document.querySelectorAll('.optical-stage-copy')]
      .map(c => c.querySelectorAll('*').length)),
    totalCopyNodes: [...document.querySelectorAll('.optical-stage-copy')]
      .reduce((n, c) => n + c.querySelectorAll('*').length, 0),
  }));

  // ---- reference: a PI signal-level switch (user says these work) ----
  const pi = page.locator('.glass-switch').filter({ hasNot: page.locator('x') })
    .nth(0);
  out.piSwitchSel = await page.evaluate(() => {
    const n = [...document.querySelectorAll('.glass-switch')]
      .filter(x => !x.closest('.optical-stage-copy') && !x.closest('#chart-layer-pop')
                && !x.closest('#sweep-model-pop') && !x.closest('.glass-topbar'))[0];
    if (n && !n.id) n.id = 'zz-ref-switch';
    return n ? { id: n.id, cls: n.className, parent: n.parentElement.className } : null;
  });
  out.REF_pi_drag = await dragShot(page, page.locator('#zz-ref-switch'), 'ref-pi-drag.png');

  // ---- broken: chart layer switch ----
  await page.evaluate(() => window.toggleChartLayerMenu(true));
  await page.waitForTimeout(500);
  out.LAYER_drag_before = await dragShot(
    page, page.locator('#chart-layer-pop .glass-switch').first(), 'layer-drag-before.png');
  await page.locator('#chart-layer-pop').screenshot({ path: '.playwright-output/layer-pop-before.png' });

  // ---- experiment: neutralise codex's precision-under-lens occlusion ----
  await page.evaluate(() => {
    const s = document.createElement('style');
    s.id = 'zz-fix';
    s.textContent = `.chart-layer-pop .glass-switch.precision-under-lens > .switch-thumb,
      .sweep-model-pop .glass-switch.precision-under-lens > .switch-thumb {
        visibility: visible !important; }`;
    document.head.appendChild(s);
  });
  out.LAYER_drag_after = await dragShot(
    page, page.locator('#chart-layer-pop .glass-switch').nth(3), 'layer-drag-after.png');
  await page.locator('#chart-layer-pop').screenshot({ path: '.playwright-output/layer-pop-after.png' });

  // ---- sweep popup ----
  await page.evaluate(() => window.toggleChartLayerMenu(false));
  const sweepBtn = page.locator('#sweep-model-btn');
  out.sweepBtnVisible = await sweepBtn.isVisible().catch(() => false);
  if (out.sweepBtnVisible) {
    await sweepBtn.scrollIntoViewIfNeeded();
    await sweepBtn.click();
    await page.waitForTimeout(500);
    await page.locator('#sweep-model-pop').screenshot({ path: '.playwright-output/sweep-pop.png' });
    out.SWEEP_drag = await dragShot(
      page, page.locator('#sweep-model-pop .glass-switch').nth(1), 'sweep-drag.png');
    await page.locator('#sweep-action-row').screenshot({ path: '.playwright-output/sweep-row.png' });
  }

  console.log("PROBE " + JSON.stringify(out, null, 1));
});

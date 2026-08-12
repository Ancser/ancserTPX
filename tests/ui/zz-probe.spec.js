/* TEMPORARY diagnostic probe — delete after investigation. */
const path = require("node:path");
const fs = require("node:fs");
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

const out = [];
async function realDrag(page, sel, name) {
  const box = await page.locator(sel).first().boundingBox();
  await page.mouse.move(box.x + 8, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 4, box.y + box.height / 2, { steps: 10 });
  await page.waitForTimeout(180);
  const state = await page.evaluate((s) => {
    const sw = document.querySelector(s);
    const thumb = sw.querySelector(':scope > .switch-thumb');
    const layer = thumb.querySelector(':scope > .optical-layer');
    const clone = [...document.querySelectorAll(
      '.optical-stage-copy.optical-tier-2-source .glass-switch.interacting > .switch-thumb')][0];
    return {
      cls: sw.className,
      liveThumbVis: getComputedStyle(thumb).visibility,
      liveThumbBg: getComputedStyle(thumb).backgroundColor,
      liveLayerVis: getComputedStyle(layer).visibility,
      liveLayerOpacity: getComputedStyle(layer).opacity,
      clonedInteractingThumbBg: clone ? getComputedStyle(clone).backgroundColor : 'none',
    };
  }, sel);
  await page.screenshot({ path: `.playwright-output/f-${name}.png`, timeout: 25000 });
  out.push({ name, box, state });
  await page.mouse.up();
  await page.waitForTimeout(450);
}

test("verify fix", async ({ page }) => {
  test.setTimeout(180000);
  await openApp(page);

  await realDrag(page, '#pi-matrix-bt-long-pi', 'param');

  await page.locator('#sweep-model-btn').click();
  await page.waitForSelector('#sweep-model-pop:not(.hidden)');
  await page.waitForTimeout(500);
  await realDrag(page, '#sweep-model-pop > .sweep-model-row > .glass-switch', 'sweep');
  await page.locator('#sweep-model-btn').click();
  await page.waitForTimeout(300);

  await page.evaluate(() => window.toggleChartLayerMenu(true));
  await page.waitForSelector('#chart-layer-pop:not(.hidden)');
  await page.waitForTimeout(600);
  await realDrag(page, '#chart-layer-pop > .layer-row > .glass-switch', 'layer');
  // second row too, so we see one ON and one OFF switch behave
  await realDrag(page, '#chart-layer-pop > .layer-row:nth-child(5) > .glass-switch', 'layer-off');
  await page.locator('#chart-layer-pop').screenshot({
    path: '.playwright-output/f-popup.png', timeout: 25000 });

  fs.writeFileSync('.playwright-output/zz-boxes.json', JSON.stringify(out, null, 1));
  console.log("VERIFY " + JSON.stringify(out, null, 1));
});

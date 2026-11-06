// Opt-in 1920×1080 screenshots of the main screens for the UI relayout v2
// (docs/ui-design-system.md "Relayout v2 progress").  Runs against the normal
// UI test server (no broker, --lifespan off) with the real candle store and
// presets.  Fonts are allowed through so the captures show the real faces.
//
//   TPX_UI_SHOTS=1 TPX_UI_SHOTS_DIR=<dir> TPX_UI_SHOTS_TAG=after \
//     node tests/ui/playwright-cli.cjs test tests/ui/ui-relayout-shots.spec.js
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('@playwright/test');

const enabled = process.env.TPX_UI_SHOTS === '1';
const outDir = process.env.TPX_UI_SHOTS_DIR || path.resolve('.playwright-output', 'ui-shots');
const tag = process.env.TPX_UI_SHOTS_TAG || 'shot';
const chartBundle = path.resolve('node_modules/lightweight-charts/dist/lightweight-charts.standalone.production.js');
const FONT_HOSTS = new Set(['fonts.googleapis.com', 'fonts.gstatic.com']);
const btFixturePath = process.env.TPX_UI_SHOTS_BT_FIXTURE || '';
const btFixture = btFixturePath && fs.existsSync(btFixturePath) ? fs.readFileSync(btFixturePath, 'utf8') : '';
const candlesFixturePath = process.env.TPX_UI_SHOTS_CANDLES_FIXTURE || '';
const candlesFixture = candlesFixturePath && fs.existsSync(candlesFixturePath)
  ? fs.readFileSync(candlesFixturePath, 'utf8') : '';

test.use({ viewport: { width: 1920, height: 1080 }, trace: 'off', screenshot: 'off', video: 'off' });

async function openApp(page) {
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname === 'unpkg.com' && url.pathname.endsWith('/lightweight-charts.standalone.production.js')) {
      await route.fulfill({ path: chartBundle, contentType: 'application/javascript' });
      return;
    }
    if (url.hostname !== '127.0.0.1' && !FONT_HOSTS.has(url.hostname)) {
      await route.abort('blockedbyclient');
      return;
    }
    if (url.pathname === '/api/data/candles' && candlesFixture) {
      // First page = the same store window the backtest used; no older pages.
      const paged = url.searchParams.has('before') || url.searchParams.has('after');
      const tf = url.searchParams.get('tf');
      if (!paged && (!tf || tf === '1m')) {
        await route.fulfill({ contentType: 'application/json', body: candlesFixture });
      } else {
        await route.fulfill({ contentType: 'application/json',
          body: JSON.stringify({ candles: [], count: 0, has_more_before: false, has_more_after: false }) });
      }
      return;
    }
    if (url.pathname === '/api/backtest/run' && route.request().method() === 'POST' && btFixture) {
      // The test server has no broker to refresh data; replay a payload the
      // production route produced from the local store (see TPX_UI_SHOTS_BT_FIXTURE).
      await route.fulfill({ contentType: 'application/json', body: btFixture });
      return;
    }
    await route.continue();
  });
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => typeof candleSeries !== 'undefined' && !!candleSeries, null, { timeout: 30_000 });
  await page.evaluate(() => document.fonts && document.fonts.ready);
}

async function shoot(page, name) {
  fs.mkdirSync(outDir, { recursive: true });
  await page.waitForTimeout(600);   // let one-shot entry motion settle
  await page.screenshot({ path: path.join(outDir, `${tag}_${name}.png`) });
}

test('relayout screenshots: backtest, live, research', async ({ page }) => {
  test.skip(!enabled, 'Set TPX_UI_SHOTS=1 to capture the relayout screenshots');
  test.setTimeout(300_000);
  await openApp(page);

  // Backtest with a real run of the shipped PI preset.
  await page.waitForFunction(() => !!(_presetsCache.presets || {})['PI STRICT MNQ REOPEN'], null, { timeout: 30_000 });
  await page.evaluate(() => {
    const select = document.getElementById('preset-bt');
    if (select) { select.value = 'PI STRICT MNQ REOPEN'; select.dispatchEvent(new Event('change', { bubbles: true })); }
  });
  test.skip(!btFixture, 'Set TPX_UI_SHOTS_BT_FIXTURE to a /api/backtest/run payload');
  // Data preparation needs the broker; the fixture already holds the result.
  await page.evaluate(() => {
    window._ensureBacktestData = async () => { await fetchAndShowChart('1m'); return true; };
  });
  await page.evaluate(() => runBacktest());
  try {
    await page.waitForFunction(() => document.querySelectorAll('#trades-tbody tr').length > 3
      && !_backtestInProgress, null, { timeout: 240_000 });
  } catch (error) {
    const log = await page.evaluate(() => (document.getElementById('log-container') || {}).innerText || '');
    console.log('APP LOG (tail):\n' + log.slice(-2500));
    throw error;
  }
  await shoot(page, '01_backtest');

  await page.locator('.app-rail [data-tab="live"]').click();
  await shoot(page, '02_live');

  await page.locator('.app-rail [data-tab="calendar"]').click();
  await shoot(page, '03_research');
});

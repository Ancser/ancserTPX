const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { test, expect } = require('@playwright/test');
// DOM snapshots disproportionately burden Glass; timing runs must not trace.
test.use({ trace: 'off', screenshot: 'off', video: 'off' });

const enabled = process.env.CHART_PERFORMANCE === '1';
const source = process.env.CHART_PERFORMANCE_DATA ||
  'F:/ancserQuant/ancserMarketData/derived/orderflow/mnq/footprint_mnq_2026-08-07.json.gz';
const sha = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');
const percentile = (values, p) => {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted.length ? sorted[Math.ceil(sorted.length * p) - 1] : null;
};
const summarize = (values) => ({ count: values.length,
  p50: percentile(values, .5), p95: percentile(values, .95) });

test('opt-in real-day chart performance', async ({ browser }, testInfo) => {
  test.skip(!enabled, 'Set CHART_PERFORMANCE=1 to run the local real-data benchmark');
  test.setTimeout(600_000);
  const packed = fs.readFileSync(source);
  const payload = JSON.parse(zlib.gunzipSync(packed));
  const baselineJs = execFileSync('git', ['show', 'HEAD:frontend/static/ancserTPX.js'], { encoding: 'utf8', maxBuffer: 8 * 1024 * 1024 });
  const currentJs = fs.readFileSync('frontend/static/ancserTPX.js', 'utf8');
  expect(payload.meta.schema_version).toBeGreaterThanOrEqual(5);
  expect(payload.bars).toHaveLength(390);
  const assets = ['ancserTPX.js', 'ancserTPX.css', 'ancserTPX.html',
    'tpx-glass.js', 'tpx-glass.css', 'tpx-glass-skin.js', 'tpx-glass-tuner.js'];
  const assetHashes = () => Object.fromEntries(assets.map(name =>
    [name, sha(fs.readFileSync(path.resolve('frontend/static', name)))]));
  const report = { label: process.env.CHART_PERFORMANCE_LABEL || 'baseline',
    startedAt: new Date().toISOString(), source, sourceSha256: sha(packed),
    head: execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
    assets: assetHashes(), variants: { baseline: sha(baselineJs), current: sha(currentJs) }, browser: browser.version(), platform: process.platform,
    viewport: { width: 1600, height: 1000 }, dpr: 1, trace: 'off', status: 'running',
    workload: 'Real compact cache; six completed minutes appended at 150ms replay cadence. No live feed.',
    samples: [] };
  const label = report.label.replace(/[^a-zA-Z0-9_-]/g, '_');
  const destination = path.resolve('F:/ancserQuant/ancserMarketData/derived/research/chart_performance', `chart-performance-${label}-${Date.now()}.json`);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  const checkpoint = () => fs.writeFileSync(destination, JSON.stringify(report, null, 2));
  checkpoint();
  console.log(`CHART_PERFORMANCE_REPORT=${destination}`);
  const modes = ['glass-on', 'mirror-off', 'glass-off'];
  for (let repetition = 0; repetition < 3; repetition++) {
    // Rotate order to reduce systematic warmup/thermal bias.
    for (let index = 0; index < modes.length; index++) {
      const mode = modes[(index + repetition) % modes.length];
      for (const variant of (repetition % 2 ? ['current', 'baseline'] : ['baseline', 'current'])) {
      const context = await browser.newContext({ viewport: report.viewport,
        deviceScaleFactor: 1, colorScheme: 'light', serviceWorkers: 'block' });
      const page = await context.newPage();
      page.setDefaultTimeout(15_000);
      let requests = 0;
      let releasedBars = 200;
      const omitted = new Set();
      try {
        await page.addInitScript(() => localStorage.setItem('ancserTPXTheme', 'light'));
        await page.route('**/*', async route => {
          const url = new URL(route.request().url());
          if (url.hostname === 'unpkg.com' && url.pathname.includes('lightweight-charts')) {
            return route.fulfill({ path: path.resolve('node_modules/lightweight-charts/dist/lightweight-charts.standalone.production.js'), contentType: 'application/javascript' });
          }
          if (url.hostname !== '127.0.0.1') return route.abort();
          if (url.pathname === '/static/ancserTPX.js') return route.fulfill({
            body: variant === 'baseline' ? baselineJs : currentJs, contentType: 'application/javascript' });
          if (mode === 'glass-off' && /\/tpx-glass(?:-skin|-tuner)?\.(js|css)$/.test(url.pathname)) {
            omitted.add(url.pathname);
            return route.fulfill({ body: '/* benchmark-only Glass omission */', contentType: url.pathname.endsWith('.css') ? 'text/css' : 'application/javascript' });
          }
          if (url.pathname === '/api/data/orderflow/footprint') {
            requests++;
            const start = Date.parse(url.searchParams.get('start'));
            const end = Date.parse(url.searchParams.get('end'));
            expect(end - start).toBeLessThan(14 * 86400_000);
            return route.fulfill({ json: { meta: payload.meta, available: true,
              bars: payload.bars.slice(0, releasedBars).filter(bar => Date.parse(bar.time) >= start && Date.parse(bar.time) <= end) } });
          }
          if (url.pathname.startsWith('/api/') && !['/api/config', '/api/health'].includes(url.pathname)) {
            // Isolate broker, history and background UI traffic from this rendering experiment.
            return route.fulfill({ json: {} });
          }
          return route.continue();
        });
        await page.goto(String(testInfo.project.use.baseURL), { waitUntil: 'load' });
        await page.bringToFront();
        await page.waitForFunction(() => typeof chart !== 'undefined' && chart && candleSeries);
        if (mode === 'glass-off') {
          expect(omitted.size).toBe(4);
          expect(await page.evaluate(() => typeof window.TpxGlass)).toBe('undefined');
          await page.addStyleTag({ content: '*,*::before,*::after {backdrop-filter:none!important;-webkit-backdrop-filter:none!important;}' });
        } else {
          await page.waitForFunction(() => window.TpxGlass?.diagnostics.surfaces > 0);
          await page.evaluate(on => TpxGlass.setCanvasMirror(on), mode === 'glass-on');
        }
        // Equal canvas dimensions even though omitted skin changes surrounding layout.
        await page.addStyleTag({ content: '#chart-container {box-sizing:content-box!important;border:0!important;padding:0!important;width:1000px!important;height:500px!important;min-height:500px!important;max-height:500px!important;flex:none!important;}' });
        await page.evaluate(() => {
          for (const key of Object.keys(CHART_OVERLAYS)) CHART_OVERLAYS[key] = false;
          window.__perf = { active: false, frames: [], draws: [], tasks: [], fills: 0, labels: 0 };
          const originalOptions = candleSeries.applyOptions.bind(candleSeries);
          candleSeries.applyOptions = function (options) {
            if (__perf.active && Object.hasOwn(options, 'visible')) __perf.visibilityCalls++;
            return originalOptions(options);
          };
          const originalDraw = drawFootprintLayer;
          drawFootprintLayer = function (...args) {
            const start = performance.now();
            try { return originalDraw.apply(this, args); }
            finally { if (__perf.active) __perf.draws.push(performance.now() - start); }
          };
          const originalFill = CanvasRenderingContext2D.prototype.fillRect;
          CanvasRenderingContext2D.prototype.fillRect = function (...args) {
            if (__perf.active && this.canvas.id === 'footprint-overlay') __perf.fills++;
            return originalFill.apply(this, args);
          };
          const originalText = CanvasRenderingContext2D.prototype.fillText;
          CanvasRenderingContext2D.prototype.fillText = function (text, ...args) {
            if (__perf.active && this.canvas.id === 'footprint-overlay' && /^[+-]\d+$/.test(text)) __perf.labels++;
            return originalText.call(this, text, ...args);
          };
          const observer = new PerformanceObserver(list => {
            if (__perf.active) __perf.tasks.push(...list.getEntries().map(e => ({ start: e.startTime, duration: e.duration })));
          });
          observer.observe({ type: 'longtask', buffered: false });
          __perf.observer = observer;
          function frame(now) {
            if (__perf.active && __perf.previous != null) __perf.frames.push(now - __perf.previous);
            __perf.previous = __perf.active ? now : null;
            requestAnimationFrame(frame);
          }
          requestAnimationFrame(frame);
        });
        for (const density of ['overview', 'detail']) {
          releasedBars = 200;
          const range = density === 'overview' ? { from: 30, to: 210 } : { from: 195, to: 207 };
          await page.evaluate(({ bars, range }) => {
            toggleChartLayer('footprint', false);
            showCandleData(bars.slice(0, 200));
            chart.timeScale().setVisibleLogicalRange(range);
            toggleChartLayer('footprint', true);
          }, { bars: payload.bars, range });
          await page.evaluate(() => refreshOrderflowLayers(false));
          await page.waitForFunction(() => performance.now() > _orderflowInteractionUntil && !_orderflowDataPromise);
          await page.evaluate(() => { drawFootprintLayer(); TpxGlassSafeSync();
            function TpxGlassSafeSync() { window.TpxGlass?.resample(); } });
          const ready = await page.evaluate(() => {
            const canvas = document.getElementById('footprint-overlay');
            const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
            let painted = 0;
            for (let i = 3; i < pixels.length; i += 4) if (pixels[i]) painted++;
            return { hidden: document.hidden, width: canvas.width, height: canvas.height,
              cells: _footprintBars.reduce((n, b) => n + b.cells.length, 0), painted,
              candlesVisible: candleSeries.options().visible, png: canvas.toDataURL() };
          });
          ready.pixelSha256 = sha(Buffer.from(ready.png.split(',')[1], 'base64'));
          delete ready.png;
          console.log(`READY ${variant} ${mode} ${density} rep=${repetition + 1}`);
          expect(ready.hidden).toBe(false);
          expect(ready.width).toBe(1000);
          expect(ready.height).toBe(500);
          expect(ready.cells).toBeGreaterThan(0);
          expect(ready.painted).toBeGreaterThan(100);
          expect(ready.candlesVisible).toBe(false);
          const beforeRequests = requests;
          await page.evaluate(() => {
            Object.assign(__perf, { active: true, frames: [], draws: [], tasks: [], fills: 0, labels: 0, visibilityCalls: 0, previous: null,
              started: performance.now(), mirrorBefore: window.TpxGlass?.diagnostics.mirror || null });
          });
          const stable = await page.evaluate(() => {
            const before = __perf.visibilityCalls;
            const start = performance.now();
            for (let i = 0; i < 5; i++) drawFootprintLayer();
            return { redraws: 5, visibilityCalls: __perf.visibilityCalls - before, durationMs: performance.now() - start };
          });
          const box = await page.locator('#chart-container').boundingBox();
          expect(box.x).toBeGreaterThanOrEqual(0);
          expect(box.y).toBeGreaterThanOrEqual(0);
          expect(box.x + box.width).toBeLessThanOrEqual(report.viewport.width);
          expect(box.y + box.height).toBeLessThanOrEqual(report.viewport.height);
          const x = box.x + 500, y = box.y + 240;
          const beforeRange = await page.evaluate(() => chart.timeScale().getVisibleLogicalRange());
          await page.mouse.move(x, y);
          await page.mouse.down();
          await page.mouse.move(x + 110, y + 15, { steps: 14 });
          await page.mouse.up();
          const draggedRange = await page.evaluate(() => chart.timeScale().getVisibleLogicalRange());
          console.log(`DRAG ${variant} ${mode} ${density}: ${beforeRange.from} -> ${draggedRange.from}`);
          expect(Math.abs(draggedRange.from - beforeRange.from)).toBeGreaterThan(.01);
          await page.mouse.wheel(0, -100);
          await page.waitForFunction(old => {
            const r = chart.timeScale().getVisibleLogicalRange();
            return Math.abs((r.to - r.from) - old) > .01;
          }, draggedRange.to - draggedRange.from);
          // Return to a deterministic view, then append actual completed bars in accelerated replay.
          await page.evaluate(range => chart.timeScale().setVisibleLogicalRange(range), range);
          await page.waitForFunction(() => performance.now() > _orderflowInteractionUntil);
          for (const bar of payload.bars.slice(200, 206)) {
            releasedBars++;
            await page.evaluate(async bar => {
              const row = { time: isoToChartTime(bar.time), open: bar.open, high: bar.high, low: bar.low, close: bar.close };
              candleSeries.update(row);
              window._lastChartData.push(row);
              await refreshOrderflowLayers(true);
              scheduleChartOverlayRedraw();
            }, bar);
            await page.waitForTimeout(150); // workload pacing, never a correctness assertion
          }
          const raw = await page.evaluate(() => {
            __perf.tasks.push(...__perf.observer.takeRecords().map(e => ({ start: e.startTime, duration: e.duration })));
            __perf.active = false;
            return { frames: __perf.frames, draws: __perf.draws, tasks: __perf.tasks,
              fills: __perf.fills, labels: __perf.labels, visibilityCalls: __perf.visibilityCalls, durationMs: performance.now() - __perf.started,
              mirrorBefore: __perf.mirrorBefore, mirrorAfter: window.TpxGlass?.diagnostics.mirror || null };
          });
          expect(raw.frames.length).toBeGreaterThan(5);
          expect(raw.draws.length).toBeGreaterThan(0);
          expect(raw.fills).toBeGreaterThan(0);
          if (density === 'detail') expect(raw.labels).toBeGreaterThan(0);
          if (mode === 'glass-on') expect(raw.mirrorAfter.blits - raw.mirrorBefore.blits).toBeGreaterThan(0);
          if (mode === 'mirror-off') {
            expect(raw.mirrorAfter.enabled).toBe(false);
            expect(raw.mirrorAfter.blits).toBe(raw.mirrorBefore.blits);
          }
          const sample = { variant, mode, density, repetition: repetition + 1, ready, stable,
            requests: requests - beforeRequests, frameMs: summarize(raw.frames), drawMs: summarize(raw.draws),
            longTasks: { count: raw.tasks.length, totalMs: raw.tasks.reduce((n, t) => n + t.duration, 0),
              ...summarize(raw.tasks.map(t => t.duration)) }, raw };
          report.samples.push(sample);
          checkpoint();
          console.log(JSON.stringify({ variant, mode, density, repetition: repetition + 1, stable,
            frameMs: sample.frameMs, drawMs: sample.drawMs, longTasks: sample.longTasks }));
        }
      } finally { await context.close(); }
      }
    }
  }
  report.assetsAtEnd = assetHashes();
  expect(report.samples).toHaveLength(36);
  report.status = 'complete';
  report.finishedAt = new Date().toISOString();
  // External generated research output survives all Playwright output cleanup.
  checkpoint();
  await testInfo.attach('chart-performance', { path: destination, contentType: 'application/json' });
  console.log(`CHART_PERFORMANCE_SAVED=${destination}`);
});

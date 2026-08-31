const { test, expect } = require('@playwright/test');

test('local page receives secure session and can call protected API', async ({ page }) => {
  const response = await page.goto('/', { waitUntil: 'domcontentloaded' });
  expect(response.status()).toBe(200);

  const headers = await response.allHeaders();
  expect(headers['x-frame-options']).toBe('DENY');
  expect(headers['x-content-type-options']).toBe('nosniff');
  expect(headers['referrer-policy']).toBe('no-referrer');
  expect(headers['content-security-policy']).toContain("frame-ancestors 'none'");
  expect(headers['access-control-allow-origin']).toBeUndefined();
  expect(headers['cache-control']).toContain('no-store');

  const staticResponse = await page.request.get('/static/ancserTPX.js');
  expect(staticResponse.status()).toBe(200);
  expect(staticResponse.headers()['cache-control']).toContain('no-store');

  const port = new URL(page.url()).port || '80';
  const cookies = await page.context().cookies();
  const session = cookies.find(c => c.name === `ancsertpx_session_${port}`);
  const csrf = cookies.find(c => c.name === `ancsertpx_csrf_${port}`);
  expect(session).toBeTruthy();
  expect(session.httpOnly).toBe(true);
  expect(session.sameSite).toBe('Strict');
  expect(csrf).toBeTruthy();
  expect(csrf.httpOnly).toBe(false);
  expect(csrf.sameSite).toBe('Strict');

  const protectedCall = await page.evaluate(async () => {
    const result = await fetch('/api/research/robustness', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ trades: [] }),
    });
    return { status: result.status, body: await result.json() };
  });
  expect(protectedCall.status).toBe(200);
  expect(protectedCall.body.trades).toBe(0);
});

test('API docs are not exposed by default', async ({ request }) => {
  expect((await request.get('/docs')).status()).toBe(404);
  expect((await request.get('/openapi.json')).status()).toBe(404);
});

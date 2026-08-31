const { test, expect } = require('@playwright/test');

test('market sessions and chart boundaries follow New York DST', async ({ page }) => {
  await page.goto('/');

  const result = await page.evaluate(() => ({
    summerBeforeRth: getSessionCodeFromDate(new Date('2026-07-15T13:29:00Z')),
    summerRth: getSessionCodeFromDate(new Date('2026-07-15T13:30:00Z')),
    winterBeforeRth: getSessionCodeFromDate(new Date('2026-01-15T14:29:00Z')),
    winterRth: getSessionCodeFromDate(new Date('2026-01-15T14:30:00Z')),
    yearEndAsia: getSessionCodeFromDate(new Date('2027-01-01T07:59:00Z')),
    yearStartEuro: getSessionCodeFromDate(new Date('2027-01-01T08:00:00Z')),
    springBeforeEuro: getSessionCodeFromDate(new Date('2026-03-08T06:59:00Z')),
    springEuro: getSessionCodeFromDate(new Date('2026-03-08T07:00:00Z')),
    fallBeforeEuro: getSessionCodeFromDate(new Date('2026-11-01T07:59:00Z')),
    fallEuro: getSessionCodeFromDate(new Date('2026-11-01T08:00:00Z')),
    nextWinterBoundary: new Date(
      getNextSessionBoundaryMs('2026-01-15T14:29:00Z')
    ).toISOString(),
    nextSpringBoundary: new Date(
      getNextSessionBoundaryMs('2026-03-08T06:59:00Z')
    ).toISOString(),
  }));

  expect(result).toEqual({
    summerBeforeRth: 'PRE',
    summerRth: 'RTH',
    winterBeforeRth: 'PRE',
    winterRth: 'RTH',
    yearEndAsia: 'ASIA',
    yearStartEuro: 'EURO',
    springBeforeEuro: 'ASIA',
    springEuro: 'EURO',
    fallBeforeEuro: 'ASIA',
    fallEuro: 'EURO',
    nextWinterBoundary: '2026-01-15T14:30:00.000Z',
    nextSpringBoundary: '2026-03-08T07:00:00.000Z',
  });
});

test('BETAFIB windows are stored as New York-local hours', async ({ page }) => {
  await page.goto('/');
  const values = await page.locator('#betafib-window-bt option').evaluateAll(
    options => options.map(option => option.value)
  );
  expect(values).toEqual(['', '18,21', '18,0', '21,3', '18,3', '3,9']);
});

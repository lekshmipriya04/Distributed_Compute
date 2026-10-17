// What a browser can get wrong, run in Chromium, Firefox, WebKit (Safari) and
// on Android and iOS phones: rendering, routing, layout, accessibility,
// offline and sign-in-required servers, sign-in, and one real run.

import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';
import { ORIGINS, RUN, addWorkerViaApi, jwt, removeAllWorkers, signIn } from './helpers';

const PAGES = [
  { path: '/ssh', heading: 'Add a worker' },
  { path: '/execute', heading: 'Language' },
  { path: '/train', heading: 'Compute' },
];

test.describe.configure({ mode: 'serial' });

test('every page renders without errors, and deep links work', async ({ page, isMobile }) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
  for (const p of PAGES) {
    await page.goto(p.path); // a direct load exercises nginx's SPA fallback
    await expect(page.getByRole('heading', { name: p.heading }).first()).toBeVisible();
    await expect(page.getByTestId('gateway-status')).toContainText('Online');
  }
  await expect(page).toHaveTitle('Distributed Compute');
  if (!isMobile) {
    await expect(page.getByTestId('app-version')).toHaveText('Version 2.0.0');
  }
  await page.goto('/no-such-page');
  await expect(page).toHaveURL(/\/ssh$/);
  expect(errors).toEqual([]);
});

test('navigation works at this screen size', async ({ page }) => {
  await page.goto('/ssh');
  const nav = page.getByRole('navigation', { name: 'Main' }).filter({ visible: true });
  await nav.getByRole('link', { name: 'Task Executor' }).click();
  await expect(page).toHaveURL(/\/execute$/);
  await nav.getByRole('link', { name: 'Train Job' }).click();
  await expect(page).toHaveURL(/\/train$/);
  await nav.getByRole('link', { name: 'SSH Connection' }).click();
  await expect(page).toHaveURL(/\/ssh$/);
});

test('nothing overflows sideways', async ({ page }) => {
  for (const p of PAGES) {
    await page.goto(p.path);
    await expect(page.getByRole('heading', { name: p.heading }).first()).toBeVisible();
    const overflow = await page.evaluate(() => {
      const root = document.scrollingElement ?? document.documentElement;
      const main = document.querySelector('main');
      return {
        page: root.scrollWidth - window.innerWidth,
        main: main ? main.scrollWidth - main.clientWidth : 0,
      };
    });
    expect(overflow.page, `${p.path} page`).toBeLessThanOrEqual(0);
    expect(overflow.main, `${p.path} main`).toBeLessThanOrEqual(0);
  }
});

test('no serious accessibility problems', async ({ page }) => {
  for (const p of PAGES) {
    await page.goto(p.path);
    await expect(page.getByRole('heading', { name: p.heading }).first()).toBeVisible();
    const { violations } = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
    const serious = violations
      .filter((v) => v.impact === 'serious' || v.impact === 'critical')
      .map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).slice(0, 3).join(', ')}`);
    expect(serious, p.path).toEqual([]);
  }
});

test('says the gateway is offline when it is', async ({ page }) => {
  await page.route('**/health', (route) => route.abort());
  await page.goto('/ssh');
  await expect(page.getByTestId('gateway-status')).toContainText('Offline');
});

test('asks for sign-in when the server requires it', async ({ page }) => {
  await page.route('**/api/**', (route) =>
    route.fulfill({
      status: 401,
      headers: { 'WWW-Authenticate': 'Bearer', 'Content-Type': 'application/json' },
      body: JSON.stringify({ detail: 'Not authenticated' }),
    }),
  );
  await page.goto('/ssh');
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('This server requires sign-in');
  // The dialog is fully on screen, above everything, wherever it opens from.
  const box = await dialog.locator('h3').boundingBox();
  expect(box?.y ?? -1).toBeGreaterThan(0);
  const viewport = page.viewportSize();
  const panel = await dialog.locator('> div').boundingBox();
  expect((panel?.y ?? 0) + (panel?.height ?? 0)).toBeLessThanOrEqual(viewport?.height ?? 0);
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
});

test('signs in and out with the keyboard', async ({ page, browserName, isMobile }) => {
  const sub = `${browserName}${isMobile ? '-mobile' : ''}-${RUN}`;
  await page.goto(`${ORIGINS.alice}/ssh`);
  await page.getByTestId('account-button').focus();
  await page.keyboard.press('Enter');
  await page.getByRole('dialog').getByLabel('Token').fill(jwt(sub));
  await page.getByRole('dialog').getByRole('button', { name: 'Sign in', exact: true }).press('Enter');
  await expect(page.getByTestId('account-button')).toHaveText(sub);
  await page.getByTestId('account-button').click();
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByTestId('account-button')).toHaveText('Local user');
});

test('runs a task on a real worker', async ({ page, browserName, isMobile }) => {
  const sub = `run-${browserName}${isMobile ? '-mobile' : ''}-${RUN}`;
  await signIn(page, ORIGINS.alice, jwt(sub), sub);
  try {
    await addWorkerViaApi(page, undefined, sub);
    // The worker's name stays on one line at this screen size.
    await page.goto(`${ORIGINS.alice}/ssh`);
    const name = page.getByText(sub, { exact: true }).first();
    await expect(name).toBeVisible();
    const box = await name.boundingBox();
    expect(box?.height ?? 0, 'worker name wraps').toBeLessThan(40);
    await page.goto(`${ORIGINS.alice}/execute`);
    await page.getByRole('radio', { name: /Python/ }).click();
    await page.getByLabel(/^Code/).fill(`print("hello from ${browserName}", input())`);
    await page.getByLabel(/^Inputs/).fill('one\ntwo');
    await page.getByRole('button', { name: 'Run in parallel' }).click();
    await expect(page.getByTestId('batch-summary')).toHaveText(/2\/2 done · 0 failed/, { timeout: 90_000 });
    await expect(page.getByText(`hello from ${browserName} two`, { exact: true })).toBeVisible();
  } finally {
    await removeAllWorkers(page);
  }
});

test('serves the dashboard with security headers', async ({ request, browserName, isMobile }) => {
  test.skip(browserName !== 'chromium' || isMobile, 'the headers do not depend on the browser');
  const html = await (await request.get('/ssh')).text();
  const asset = /src="(\/assets\/[^"]+\.js)"/.exec(html)?.[1];
  expect(asset).toBeTruthy();
  for (const path of ['/', '/ssh', '/execute', asset as string, '/health', '/api/v1/ssh/runtimes']) {
    const res = await request.get(path);
    expect(res.status(), path).toBe(200);
    const h = res.headers();
    expect(h['x-frame-options'], path).toBe('DENY');
    expect(h['x-content-type-options'], path).toBe('nosniff');
    expect(h['referrer-policy'], path).toBe('strict-origin-when-cross-origin');
    expect(h['content-security-policy'], path).toContain("frame-ancestors 'none'");
    expect(h['server'] ?? '', path).not.toMatch(/\d/); // no nginx version
  }
  expect((await request.get(asset as string)).headers()['cache-control']).toContain('immutable');
});

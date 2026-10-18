// Screenshots for frontend/README.md, taken from the real dashboard.
// Start it first (node e2e/serve.mjs, test workers running), then:
//   npx playwright test --project=docs
// Writes docs/images/*.png next to the README.

import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type Locator, type Page } from '@playwright/test';
import { ORIGINS, WORKER, WORKER_2, addWorkerViaApi, api, jwt, removeAllWorkers } from '../helpers';

/** The example the screenshots run: a scikit-learn sweep (examples/sklearn-sweep). */
const SKLEARN_EXAMPLE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'examples', 'sklearn-sweep');

const OUT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'docs', 'images');
const USER = 'alice';

test.use({ viewport: { width: 1360, height: 860 }, deviceScaleFactor: 2 });

async function dismissToasts(page: Page) {
  for (const b of await page.getByRole('button', { name: 'Dismiss notification' }).all()) await b.click();
}

/** A card (white panel) around an element, for a cropped screenshot. */
const card = (inner: Locator) => inner.locator('xpath=ancestor::div[contains(@class, "rounded-2xl")][1]');

async function save(target: Page | Locator, name: string) {
  await target.screenshot({ path: path.join(OUT, `${name}.png`), animations: 'disabled' });
}

test('dashboard walkthrough screenshots', async ({ page }) => {
  test.setTimeout(600_000);
  const origin = ORIGINS.alice;

  // 1. Sign in.
  await page.goto(`${origin}/ssh`);
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await page.getByTestId('account-button').click();
  await page.getByRole('dialog').getByLabel('Token').fill(jwt(USER, undefined, 30 * 24 * 3600));
  await save(page, '01-sign-in');
  await page.getByRole('dialog').getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByTestId('account-button')).toHaveText(USER);
  await removeAllWorkers(page);

  try {
    // 2. Add a worker: address, host key + password, slice.
    await page.getByLabel('Name (optional)').fill('lab-box-1');
    await page.getByLabel('Host / IP').fill(WORKER.host);
    await page.getByLabel('Port', { exact: true }).fill(WORKER.port);
    await page.getByLabel('Username', { exact: true }).fill(WORKER.user);
    await save(card(page.getByRole('heading', { name: 'Add a worker' })), '02-add-worker');
    await page.getByRole('button', { name: 'Connect' }).click();
    await page.getByLabel(/Password for/).fill(WORKER.password);
    await save(card(page.getByRole('heading', { name: 'Add a worker' })), '03-confirm-host-key');
    await page.getByRole('button', { name: 'Confirm & Add' }).click();
    await expect(page.getByText('CPU cores')).toBeVisible({ timeout: 90_000 });
    await dismissToasts(page);
    await save(card(page.getByRole('heading', { name: 'Add a worker' })), '04-detected');
    await page.getByRole('button', { name: 'Register' }).click();
    await expect(page.getByText(`${WORKER.user}@${WORKER.host}:${WORKER.port}`)).toBeVisible({ timeout: 30_000 });

    // 3. A second worker, the list, and editing a slice.
    await addWorkerViaApi(page, WORKER_2, 'lab-box-2');
    await page.reload();
    await expect(page.getByText('lab-box-2').first()).toBeVisible();
    await dismissToasts(page);
    await save(card(page.getByRole('heading', { name: 'Workers' })), '05-workers');
    await page.getByTitle('Edit name and allocation').first().click();
    await save(card(page.getByRole('heading', { name: 'Workers' })), '06-edit-slice');
    await page.getByRole('button', { name: 'Cancel' }).click();

    // 4. Run code in parallel.
    await page.goto(`${origin}/execute`);
    await page.getByRole('radio', { name: /Python/ }).click();
    await save(page, '07-choose-language');
    await page.getByLabel(/^Code/).fill(
      'import sys\n\nn = int(input())\nif n == 5:\n    sys.exit(f"cannot handle {n}")\nprint(f"{n} squared is {n * n}")',
    );
    await page.getByLabel(/^Inputs/).fill('1\n2\n3\n4\n5\n6');
    await page.getByLabel(/^Inputs/).scrollIntoViewIfNeeded();
    await save(card(page.getByLabel(/^Inputs/)), '08-code-and-inputs');
    await page.getByRole('button', { name: 'Run in parallel' }).click();
    await expect(page.getByTestId('batch-summary')).toHaveText(/5\/6 done · 1 failed/, { timeout: 90_000 });
    await dismissToasts(page);
    const results = card(page.getByTestId('batch-summary'));
    await results.scrollIntoViewIfNeeded();
    await save(page, '09-results');
    const failed = page.getByRole('alert').filter({ hasText: 'cannot handle 5' }).first();
    await failed.evaluate((el) => el.scrollIntoView({ block: 'center' }));
    await save(page, '10-failed-task');
    await page.getByRole('button', { name: 'Retry 1 failed' }).click();
    await expect(page.getByTestId('batch-summary')).toHaveText(/0\/1 done · 1 failed/, { timeout: 90_000 });
    await dismissToasts(page);
    await page.getByTestId('batch-summary').scrollIntoViewIfNeeded();
    await save(page, '11-retry');

    // 4b. Run a whole project folder: scikit-learn, installed on the workers.
    const { items: workers } = await api<{ items: { id: string; name: string }[] }>(page, 'GET', '/api/v1/ssh/nodes');
    const first = workers.find((w) => w.name === 'lab-box-1');
    if (first) await api(page, 'PATCH', `/api/v1/ssh/nodes/${first.id}`, { env_cache_gb: 10 });
    await page.goto(`${origin}/execute`);
    await page.getByRole('radio', { name: 'Project folder' }).click();
    await page.getByTestId('project-folder').setInputFiles(SKLEARN_EXAMPLE);
    await expect(page.getByTestId('project-summary')).toContainText('sklearn-sweep');
    await page.getByLabel(/^Inputs/).fill('0.1\n1\n10');
    await save(page, '17-project-folder');
    await page.getByRole('button', { name: 'Run project' }).click();
    await expect(page.getByTestId('batch-summary')).toHaveText(/3\/3 done · 0 failed/, { timeout: 480_000 });
    await dismissToasts(page);
    await page.getByTestId('batch-summary').evaluate((el) => el.closest('.rounded-2xl')?.scrollIntoView({ block: 'start' }));
    await save(page, '18-project-results');

    // 5. Train a model.
    await page.goto(`${origin}/train`);
    await save(page, '12-train-engine-model');
    const rows = ['x1,x2,label'];
    for (let i = 0; i < 300; i++) {
      const a = (i % 17) / 17;
      const b = (i % 11) / 11;
      rows.push(`${a.toFixed(3)},${b.toFixed(3)},${a + b > 1 ? 1 : 0}`);
    }
    await page.locator('input[type=file]').setInputFiles({ name: 'points.csv', mimeType: 'text/csv', buffer: Buffer.from(rows.join('\n') + '\n') });
    await page.getByRole('button', { name: 'Upload' }).click();
    await page.getByLabel('Target column').selectOption('label', { timeout: 60_000 });
    await dismissToasts(page);
    await page.getByLabel('Target column').scrollIntoViewIfNeeded();
    await save(card(page.getByLabel('Target column')), '13-dataset');
    await page.getByRole('button', { name: 'Start Training' }).scrollIntoViewIfNeeded();
    await save(page, '14-start-training');
    await page.getByRole('button', { name: 'Start Training' }).click();
    await expect(page.getByText(/Training completed/)).toBeVisible({ timeout: 180_000 });
    await dismissToasts(page);
    await page.getByText(/Training completed/).evaluate((el) => el.closest('.rounded-2xl')?.scrollIntoView({ block: 'start' }));
    await save(page, '15-training-result');

    // 6. On a phone.
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${origin}/execute`);
    await page.getByRole('radio', { name: /Python/ }).click();
    await save(page, '16-phone');
  } finally {
    await page.setViewportSize({ width: 1360, height: 860 });
    await removeAllWorkers(page);
  }
});

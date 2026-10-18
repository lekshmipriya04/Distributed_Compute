// Full flows against the real platform: three users on three dashboard
// ports, real SSH workers, real runs and training.

import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { expect, test, type Browser, type Page } from '@playwright/test';
import { ORIGINS, RUN, WORKER, addWorkerViaApi, api, jwt, removeAllWorkers, signIn } from './helpers';

const ALICE = `alice-${RUN}`;
const BOB = `bob-${RUN}`;

test.describe.configure({ mode: 'serial' });

let alice: Page;
let bob: Page;
let local: Page;

async function newPage(browser: Browser): Promise<Page> {
  const page = await (await browser.newContext({ acceptDownloads: true })).newPage();
  page.on('pageerror', (err) => {
    throw err;
  });
  return page;
}

test.beforeAll(async ({ browser }) => {
  [alice, bob, local] = [await newPage(browser), await newPage(browser), await newPage(browser)];
});

test.afterAll(async () => {
  for (const page of [alice, bob]) {
    await removeAllWorkers(page).catch(() => undefined);
  }
});

test('each port keeps its own signed-in user', async () => {
  await signIn(alice, ORIGINS.alice, jwt(ALICE), ALICE);
  await signIn(bob, ORIGINS.bob, jwt(BOB), BOB);
  await local.goto(`${ORIGINS.local}/ssh`);
  await expect(local.getByTestId('account-button')).toHaveText('Local user');

  // Survives a reload. In the same browser, another port does not inherit
  // alice's token: each origin keeps its own sign-in.
  await alice.reload();
  await expect(alice.getByTestId('account-button')).toHaveText(ALICE);
  const otherPort = await alice.context().newPage();
  await otherPort.goto(`${ORIGINS.bob}/ssh`);
  await expect(otherPort.getByTestId('account-button')).toHaveText('Local user');
  await otherPort.close();
});

test('sign-in rejects malformed, forged and expired tokens', async () => {
  const page = local;
  await page.getByTestId('account-button').click();
  const dialog = page.getByRole('dialog');
  const token = dialog.getByLabel('Token');
  const signInButton = dialog.getByRole('button', { name: 'Sign in', exact: true });

  await token.fill('not-a-token');
  await signInButton.click();
  await expect(dialog.getByRole('alert')).toContainText('not a platform token');

  await token.fill(jwt('mallory', 'wrong-secret'));
  await signInButton.click();
  await expect(dialog.getByRole('alert')).toContainText('rejected');

  await token.fill(jwt('late', undefined, -60));
  await signInButton.click();
  await expect(dialog.getByRole('alert')).toContainText('expired');

  await page.getByRole('button', { name: 'Close' }).click();
  await expect(page.getByTestId('account-button')).toHaveText('Local user');
});

test('alice adds a worker through the onboarding form', async () => {
  const page = alice;
  await page.goto(`${ORIGINS.alice}/ssh`);
  await page.getByLabel('Name (optional)').fill(`alice-${RUN}`);
  await page.getByLabel('Host / IP').fill(WORKER.host);
  await page.getByLabel('Port', { exact: true }).fill(WORKER.port);
  await page.getByLabel('Username', { exact: true }).fill(WORKER.user);
  await page.getByRole('button', { name: 'Connect' }).click();
  await expect(page.getByText(/SHA256:/).first()).toBeVisible({ timeout: 60_000 });

  // A wrong password is explained, and the form stays usable.
  await page.getByLabel(/Password for/).fill('definitely-wrong');
  await page.getByRole('button', { name: 'Confirm & Add' }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'Wrong password' })).toBeVisible({ timeout: 60_000 });
  // A refused SSH password is not a website sign-in problem.
  await expect(page.getByRole('dialog')).toHaveCount(0);

  await page.getByLabel(/Password for/).fill(WORKER.password);
  await page.getByRole('button', { name: 'Confirm & Add' }).click();
  await expect(page.getByText('CPU cores')).toBeVisible({ timeout: 90_000 });
  await page.getByRole('button', { name: 'Register' }).click();
  await expect(page.getByText(`${WORKER.user}@${WORKER.host}:${WORKER.port}`)).toBeVisible({ timeout: 30_000 });
});

test('bob cannot see alice’s worker, and the local user neither', async () => {
  await bob.goto(`${ORIGINS.bob}/ssh`);
  await expect(bob.getByRole('heading', { name: 'Workers' })).toBeVisible();
  await expect(bob.getByText(`alice-${RUN}`)).toHaveCount(0);
  await local.goto(`${ORIGINS.local}/ssh`);
  await expect(local.getByText(`alice-${RUN}`)).toHaveCount(0);
  const nodes = await api<{ items: unknown[] }>(bob, 'GET', '/api/v1/ssh/nodes');
  expect(nodes.items).toHaveLength(0);
});

test('health check, drain and resume from the worker row', async () => {
  const page = alice;
  await page.getByTitle('Health check').click();
  await expect(page.getByText(`alice-${RUN}: online`)).toBeVisible({ timeout: 60_000 });

  await page.getByRole('button', { name: 'Drain worker' }).click();
  await expect(page.getByText('is draining')).toBeVisible();
  await expect(page.getByText('draining', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Resume worker' }).click();
  await expect(page.getByText('takes new work again')).toBeVisible();
});

async function runTask(page: Page, code: string, inputs: string) {
  await page.goto(`${ORIGINS.alice}/execute`);
  await page.getByRole('radio', { name: /Python/ }).click();
  await page.getByLabel(/^Code/).fill(code);
  await page.getByLabel(/^Inputs/).fill(inputs);
  await page.getByRole('button', { name: 'Run in parallel' }).click();
}

test('runs one task per input and shows each output', async () => {
  const page = alice;
  await runTask(page, 'n = int(input())\nprint("square", n * n)', '1\n2\n3\n4');
  await expect(page.getByTestId('batch-summary')).toHaveText(/4\/4 done · 0 failed/, { timeout: 90_000 });
  for (const v of ['square 1', 'square 4', 'square 9', 'square 16']) {
    await expect(page.getByText(v, { exact: true })).toBeVisible();
  }
});

test('shows the error of failed tasks and retries only those', async () => {
  const page = alice;
  await runTask(page, 'n = int(input())\nif n % 2:\n    raise ValueError("odd input " + str(n))\nprint("even", n)', '1\n2\n3\n4');
  await expect(page.getByTestId('batch-summary')).toHaveText(/2\/4 done · 2 failed/, { timeout: 90_000 });
  await expect(page.getByRole('alert').filter({ hasText: 'ValueError' }).first()).toBeVisible();

  await page.getByRole('button', { name: 'Retry 2 failed' }).click();
  await expect(page.getByText(/Started again \(2 tasks\)/)).toBeVisible();
  await expect(page.getByTestId('batch-summary')).toHaveText(/0\/2 done · 2 failed/, { timeout: 90_000 });
});

test('cancels a running batch', async () => {
  const page = alice;
  await runTask(page, 'import time\ntime.sleep(120)', 'a\nb');
  const cancel = page.getByRole('button', { name: 'Cancel' });
  await expect(cancel).toBeVisible({ timeout: 30_000 });
  await cancel.click();
  await expect(page.getByText('cancelled', { exact: true }).first()).toBeVisible({ timeout: 60_000 });
});

test('trains a model from an uploaded CSV and downloads it', async () => {
  const page = alice;
  await page.goto(`${ORIGINS.alice}/train`);
  const rows = ['x1,x2,label'];
  for (let i = 0; i < 200; i++) {
    const a = (i % 17) / 17;
    const b = (i % 11) / 11;
    rows.push(`${a.toFixed(3)},${b.toFixed(3)},${a + b > 1 ? 1 : 0}`);
  }
  await page.locator('input[type=file]').setInputFiles({
    name: `web-${RUN}.csv`,
    mimeType: 'text/csv',
    buffer: Buffer.from(rows.join('\n') + '\n'),
  });
  await page.getByRole('button', { name: 'Upload' }).click();
  await page.getByLabel('Target column').selectOption('label', { timeout: 60_000 });
  await page.getByRole('button', { name: 'Start Training' }).click();
  await expect(page.getByText(/Training completed/)).toBeVisible({ timeout: 180_000 });

  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: /ONNX|Model/ }).click();
  expect((await download).suggestedFilename()).toMatch(/\.onnx$/);

  // Bob does not see alice's dataset.
  const datasets = await api<{ items: { name: string }[] }>(bob, 'GET', '/api/v1/storage/datasets?limit=200');
  expect(datasets.items.map((d) => d.name)).not.toContain(`web-${RUN}`);
});

test('runs a project folder with its requirements and downloads a task’s outputs', async () => {
  const page = alice;
  // A folder like a user's: its own module, a requirements file, and junk that must stay behind.
  const dir = path.join(mkdtempSync(path.join(tmpdir(), 'dc-web-')), 'web-project');
  for (const [rel, text] of Object.entries({
    'main.py': 'import os, sys\nfrom tabulate import tabulate\nfrom helpers.calc import double\n'
      + 'n = int(sys.stdin.read())\nos.makedirs(os.environ["DC_OUTPUT_DIR"], exist_ok=True)\n'
      + 'open(os.path.join(os.environ["DC_OUTPUT_DIR"], "result.txt"), "w").write(str(double(n)))\n'
      + 'print(tabulate([["input", n], ["double", double(n)]]))\n',
    'helpers/__init__.py': '',
    'helpers/calc.py': 'def double(x):\n    return 2 * x\n',
    'requirements.txt': 'tabulate==0.9.0\n',
    '.venv/bin/python': 'not sent',
    'outputs/old.txt': 'not sent',
  })) {
    mkdirSync(path.dirname(path.join(dir, rel)), { recursive: true });
    writeFileSync(path.join(dir, rel), text);
  }
  await page.goto(`${ORIGINS.alice}/execute`);
  await page.getByRole('radio', { name: 'Project folder' }).click();
  await page.getByTestId('project-folder').setInputFiles(dir);
  await expect(page.getByTestId('project-summary')).toContainText('web-project · 4 files');
  await expect(page.getByPlaceholder('python3 train.py --epochs 3')).toHaveValue('python3 main.py');
  await expect(page.getByPlaceholder('requirements.txt')).toHaveValue('requirements.txt');
  await page.getByLabel(/^Inputs/).fill('3\n21');
  await page.getByRole('button', { name: 'Run project' }).click();
  await expect(page.getByTestId('batch-summary')).toHaveText(/2\/2 done · 0 failed/, { timeout: 300_000 });
  await expect(page.getByText(/double\s+42/)).toBeVisible();
  await expect(page.getByText(/\[dc\] environment: built with pip/).first()).toBeVisible();

  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: /Outputs \(1 file/ }).first().click();
  expect((await download).suggestedFilename()).toMatch(/^outputs-[0-9a-f]{8}-task\d\.tar\.gz$/);

  // Bob, on his own port, sees neither the run nor its outputs.
  const mine = await api<{ items: { id: string; runtime: string }[] }>(page, 'GET', '/api/v1/ssh/batches');
  const run = mine.items.find((b) => b.runtime === 'project');
  expect(run).toBeTruthy();
  await expect(api(bob, 'GET', `/api/v1/ssh/batches/${run!.id}/tasks`)).rejects.toThrow(/404/);
});

test('bob works in parallel with his own worker', async () => {
  await addWorkerViaApi(bob, { ...WORKER, host: 'ssh-provider-2' }, `bob-${RUN}`);
  await bob.goto(`${ORIGINS.bob}/execute`);
  await bob.getByRole('radio', { name: /Python/ }).click();
  await bob.getByLabel(/^Code/).fill('print("bob says", input())');
  await bob.getByLabel(/^Inputs/).fill('hi');
  await bob.getByRole('button', { name: 'Run in parallel' }).click();
  await expect(bob.getByText('bob says hi', { exact: true })).toBeVisible({ timeout: 90_000 });
  // Alice's history does not include bob's run.
  const batches = await api<{ items: { id: string }[] }>(alice, 'GET', '/api/v1/ssh/batches');
  const bobs = await api<{ items: { id: string }[] }>(bob, 'GET', '/api/v1/ssh/batches');
  expect(batches.items.map((b) => b.id)).not.toContain(bobs.items[0].id);
});

test('removes the worker and its key', async () => {
  const page = alice;
  await page.goto(`${ORIGINS.alice}/ssh`);
  await page.getByTitle('Remove worker').click();
  await page.getByRole('button', { name: 'Confirm' }).click();
  await expect(page.getByText(/access key deleted|key line/)).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText(`${WORKER.user}@${WORKER.host}:${WORKER.port}`)).toHaveCount(0);
});

test('signing out returns to the local user', async () => {
  await alice.getByTestId('account-button').click();
  await alice.getByRole('button', { name: 'Sign out' }).click();
  await expect(alice.getByTestId('account-button')).toHaveText('Local user');
});

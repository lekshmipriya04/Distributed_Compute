// Takes the README screenshots by driving a real VS Code with Playwright.
// Needs the platform with its test workers (`make up-dev`) on localhost:8090.
//
//   npm run compile && node e2e/screenshots/capture.mjs
//
// Writes media/readme/*.png. The local user's workers and sample data are
// removed again at the end.

import { _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

delete process.env.ELECTRON_RUN_AS_NODE; // set when run from a VS Code terminal

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const OUT = path.join(root, 'media', 'readme');
const GATEWAY = process.env.DC_E2E_URL ?? 'http://127.0.0.1:8090';
const WORKERS = [
  { host: 'ssh-provider', port: 2222, name: 'lab-box-1' },
  { host: 'ssh-provider-2', port: 2222, name: 'lab-box-2' },
];
fs.mkdirSync(OUT, { recursive: true });

function vscodeExecutable() {
  const base = path.join(root, '.vscode-test');
  const dir = fs.readdirSync(base).find((d) => d.startsWith('vscode-'));
  if (!dir) throw new Error('Run `npm run test:vscode` once to download VS Code.');
  if (process.platform === 'darwin') return path.join(base, dir, 'Visual Studio Code.app/Contents/MacOS/Code');
  if (process.platform === 'win32') return path.join(base, dir, 'Code.exe');
  return path.join(base, dir, 'code');
}

// --- API helpers (the local user, as the extension signs in) ---------------
let token = '';
async function api(method, route, body) {
  const res = await fetch(GATEWAY + route, {
    method,
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`${method} ${route} -> ${res.status} ${text}`);
  return text ? JSON.parse(text) : undefined;
}
const STORAGE = process.env.DC_STORAGE_URL ?? 'http://127.0.0.1:8000/api/v1/storage';
async function removeSampleData() {
  for (const d of (await api('GET', '/datasets?limit=200')).items.filter((x) => x.name === 'points')) {
    await fetch(`${STORAGE}/datasets/${d.id}`, { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } });
  }
  for (const r of await api('GET', '/training')) await api('DELETE', `/training/${r.id}`).catch(() => undefined);
}
async function removeWorkers() {
  for (const n of (await api('GET', '/nodes')).items) await api('DELETE', `/nodes/${n.id}`);
}
async function addWorker(w) {
  const c = await api('POST', '/nodes/connect', { host: w.host, port: w.port, username: 'provider' });
  const p = await api('POST', '/nodes/provision', { onboarding_token: c.onboarding_token, password: 'providerpass' });
  return api('POST', '/nodes', { provision_id: p.provision_id, name: w.name, allocated_cpu: 2, allocated_memory_gb: 1 });
}

// --- VS Code helpers ---------------------------------------------------------
async function launch(settings, files = {}) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'dc-shots-'));
  const ws = path.join(tmp, 'my-project');
  fs.mkdirSync(path.join(tmp, 'ud', 'User'), { recursive: true });
  fs.mkdirSync(ws);
  for (const [name, text] of Object.entries(files)) fs.writeFileSync(path.join(ws, name), text);
  fs.writeFileSync(
    path.join(tmp, 'ud', 'User', 'settings.json'),
    JSON.stringify({
      'workbench.colorTheme': 'Default Light Modern',
      'window.dialogStyle': 'custom',
      'window.menuStyle': 'custom',
      'window.commandCenter': false,
      'workbench.startupEditor': 'none',
      'workbench.tips.enabled': false,
      'workbench.secondarySideBar.defaultVisibility': 'hidden',
      'chat.disableAIFeatures': true,
      'editor.minimap.enabled': false,
      'editor.fontSize': 14,
      'security.workspace.trust.enabled': false,
      'update.mode': 'none',
      'extensions.ignoreRecommendations': true,
      ...settings,
    }),
  );
  const app = await electron.launch({
    executablePath: vscodeExecutable(),
    args: [
      ws,
      `--extensionDevelopmentPath=${root}`,
      `--user-data-dir=${path.join(tmp, 'ud')}`,
      `--extensions-dir=${path.join(tmp, 'ext')}`,
      '--skip-welcome',
      '--skip-release-notes',
    ],
  });
  const win = await app.firstWindow();
  await win.waitForSelector('.monaco-workbench', { timeout: 60_000 });
  await app.evaluate(({ BrowserWindow }) => {
    const w = BrowserWindow.getAllWindows()[0];
    w.setSize(1440, 900);
    w.center();
  });
  await win.waitForTimeout(3000);
  return { app, win, tmp };
}

const shot = async (win, name) => {
  await win.mouse.move(1100, 700); // no hover tooltips over the views
  await win.waitForTimeout(700);
  await win.screenshot({ path: path.join(OUT, `${name}.png`) });
  console.log(`  ${name}.png`);
};

const quickInput = (win) => win.locator('.quick-input-widget');
const quickInputBox = (win) => win.locator('.quick-input-widget .quick-input-box input');

async function command(win, title) {
  await win.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  await quickInputBox(win).fill(`>${title}`);
  await win.locator('.quick-input-list .monaco-list-row', { hasText: title }).first().waitFor();
  await win.keyboard.press('Enter');
}

/** Waits for a quick input whose title contains `title`. */
async function step(win, title) {
  await quickInput(win).locator('.quick-input-title', { hasText: title }).waitFor({ timeout: 120_000 });
}

async function pick(win, label) {
  await quickInput(win).locator('.monaco-list-row', { hasText: label }).first().click();
}

/** The command palette, filtered (fuzzy) to some commands. */
async function filteredCommands(win, text) {
  await win.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  await quickInputBox(win).fill(`>${text}`);
  await win.locator('.quick-input-list .monaco-list-row').first().waitFor();
}

async function openView(win) {
  await win.locator('.activitybar [aria-label^="Distributed Compute"]').first().click();
  await win.locator('.pane-header', { hasText: 'Compute Nodes' }).waitFor();
}

// --- sessions ----------------------------------------------------------------

async function firstStart() {
  // No platform yet: the extension offers the one-click setup.
  const { app, win } = await launch({ 'distributedCompute.masterUrl': 'http://127.0.0.1:59999' });
  try {
    await openView(win);
    await win.locator('.notifications-toasts', { hasText: 'Run the Distributed Compute platform' }).waitFor({ timeout: 30_000 });
    await shot(win, '01-first-start');

    await command(win, 'Welcome: Open Walkthrough');
    await quickInputBox(win).fill('Distributed Compute');
    await win.locator('.quick-input-list .monaco-list-row', { hasText: 'Distributed Compute' }).first().click();
    await win.locator('.gettingStartedContainer', { hasText: 'Start the platform' }).waitFor();
    await shot(win, '02-walkthrough');
  } finally {
    await app.close();
  }
}

async function everyday() {
  const example = path.resolve(root, '..', 'examples', 'sklearn-sweep');
  const files = {
    'square.py': 'import sys\n\nn = int(sys.stdin.read().strip() or 7)\nprint(f"{n} squared is {n * n}")\n',
    // A real project for the "Run Project on Workers" steps (examples/sklearn-sweep).
    'train.py': fs.readFileSync(path.join(example, 'train.py'), 'utf8'),
    'requirements.txt': fs.readFileSync(path.join(example, 'requirements.txt'), 'utf8'),
  };
  const { app, win } = await launch({ 'distributedCompute.masterUrl': GATEWAY }, files);
  try {
    await everydaySteps(win);
  } catch (err) {
    await win.screenshot({ path: path.join(OUT, 'zz-failure.png') });
    throw err;
  } finally {
    await app.close();
  }
}

async function everydaySteps(win) {
  {
    await openView(win);
    await win.locator('.statusbar', { hasText: 'nodes online' }).waitFor();
    await shot(win, '03-connected');

    // Add a worker with the wizard.
    await command(win, 'Distributed Compute: Add Compute Node');
    await step(win, 'address');
    await quickInputBox(win).fill('ssh-provider');
    await shot(win, '04-add-node-address');
    await win.keyboard.press('Enter');
    await step(win, 'SSH port');
    await win.keyboard.press('Enter');
    await step(win, 'SSH user');
    await win.keyboard.press('Enter');
    const dialog = win.locator('.monaco-dialog-box');
    await dialog.waitFor({ timeout: 60_000 });
    await shot(win, '05-add-node-trust');
    await dialog.locator('.monaco-button', { hasText: 'Trust and Continue' }).click();
    await step(win, 'password');
    await quickInputBox(win).fill('providerpass');
    await shot(win, '06-add-node-password');
    await win.keyboard.press('Enter');
    await step(win, 'How much of');
    await shot(win, '07-add-node-slice');
    await win.keyboard.press('Enter');
    await step(win, 'Name this node');
    await quickInputBox(win).fill('lab-box-1');
    await win.keyboard.press('Enter');
    await win.locator('.monaco-list-row', { hasText: 'lab-box-1' }).first().waitFor({ timeout: 30_000 });

    // A second worker through the API, then the node list.
    await addWorker(WORKERS[1]);
    await command(win, 'Distributed Compute: Refresh Nodes');
    await win.locator('.monaco-list-row', { hasText: 'lab-box-2' }).first().waitFor({ timeout: 30_000 });
    const node = win.locator('.monaco-list-row', { hasText: 'lab-box-1' }).first();
    if ((await node.getAttribute('aria-expanded')) !== 'true') await node.locator('.monaco-tl-twistie').click();
    await win.locator('.monaco-list-row', { hasText: 'CPU allocatable' }).first().waitFor();
    await shot(win, '08-nodes');
    // Right-click menus are native on macOS; the same actions in the palette:
    await filteredCommands(win, 'Distributed Compute: Node');
    await shot(win, '08b-node-actions');
    await win.keyboard.press('Escape');

    // Run a file in parallel.
    await win.keyboard.press(process.platform === 'darwin' ? 'Meta+P' : 'Control+P');
    await quickInputBox(win).fill('square.py');
    await win.locator('.quick-input-list .monaco-list-row', { hasText: 'square.py' }).first().waitFor();
    await win.keyboard.press('Enter');
    await win.locator('.monaco-editor .view-lines', { hasText: 'squared' }).waitFor();
    // The dropdown next to the Run button (clicking the button itself runs the file).
    await win.locator('.editor-actions .monaco-dropdown-with-default .dropdown-action-container a').first().click();
    await win.locator('.context-view .monaco-menu', { hasText: 'Run File in Parallel' }).waitFor();
    await shot(win, '09-run-menu');
    await win.keyboard.press('Escape');

    await command(win, 'Distributed Compute: Run File in Parallel');
    await step(win, 'language');
    await shot(win, '10-parallel-language');
    await pick(win, 'Python');
    await step(win, 'tasks');
    await shot(win, '11-parallel-tasks');
    await pick(win, 'Type the inputs');
    await step(win, 'separated by commas');
    await quickInputBox(win).fill('1, 2, 3, 4, 5, 6, 7, 8');
    await win.keyboard.press('Enter');
    await step(win, 'nodes');
    await shot(win, '12-parallel-nodes');
    await win.keyboard.press('Enter');
    await step(win, 'cores per task');
    await win.keyboard.press('Enter');
    const output = win.locator('.monaco-editor .view-lines', { hasText: 'Task 8' });
    await output.waitFor({ timeout: 90_000 });
    await win.locator('.monaco-editor .view-lines', { hasText: '8/8 tasks finished' }).waitFor({ timeout: 90_000 });
    await shot(win, '13-live-output');

    await filteredCommands(win, 'Distributed Compute: Execution');
    await shot(win, '14-execution-actions');
    await win.keyboard.press('Escape');

    // Run the whole folder (train.py + requirements.txt) on the workers.
    await win.keyboard.press(process.platform === 'darwin' ? 'Meta+P' : 'Control+P');
    await quickInputBox(win).fill('train.py');
    await win.locator('.quick-input-list .monaco-list-row', { hasText: 'train.py' }).first().waitFor();
    await win.keyboard.press('Enter');
    await win.locator('.monaco-editor .view-lines', { hasText: 'load_digits' }).first().waitFor();
    await command(win, 'Distributed Compute: Run Project on Workers');
    await step(win, 'command to run in the folder');
    await shot(win, '20-project-command');
    await win.keyboard.press('Enter');
    await step(win, 'Run Project (2/3)');
    await pick(win, 'Type the inputs');
    await step(win, 'separated by commas');
    await quickInputBox(win).fill('0.1, 1, 10');
    await win.keyboard.press('Enter');
    await step(win, 'Run Project (3/3)');
    await win.keyboard.press('Enter');
    await win.locator('.monaco-editor .view-lines', { hasText: '3/3 tasks finished' }).waitFor({ timeout: 480_000 });
    await win.locator('.monaco-editor .view-lines', { hasText: 'accuracy' }).first().waitFor();
    await shot(win, '21-project-output');
    await command(win, 'Distributed Compute: Download Project Outputs');
    await quickInput(win).locator('.monaco-list-row', { hasText: 'python3 train.py' }).first().click();
    await win.locator('.notifications-toasts', { hasText: 'output file(s)' }).waitFor({ timeout: 60_000 });
    await win.locator('.notifications-toasts .monaco-button', { hasText: 'Open Folder' }).click();
    await win.locator('.explorer-folders-view .monaco-list-row', { hasText: 'model.joblib' }).first().waitFor({ timeout: 30_000 });
    await shot(win, '22-project-outputs');
    await openView(win); // back from the Explorer to the Distributed Compute views

    // Train a model on a dataset uploaded beforehand.
    await command(win, 'Distributed Compute: Submit Training Job');
    await step(win, 'choose a dataset');
    await shot(win, '15-train-dataset');
    await pick(win, 'points');
    await step(win, 'choose an engine');
    await shot(win, '16-train-engine');
    await pick(win, 'Pure Python');
    await step(win, 'choose a model');
    await shot(win, '17-train-model');
    await pick(win, 'Logistic Regression');
    await quickInput(win).locator('.quick-input-title', { hasText: /predict|target column/ }).waitFor();
    if (await quickInput(win).locator('.monaco-list-row', { hasText: 'label' }).count()) {
      await pick(win, 'label');
    } else {
      await quickInputBox(win).fill('label');
      await win.keyboard.press('Enter');
    }
    await step(win, 'where should it train');
    await pick(win, 'Worker nodes');
    await step(win, 'rounds and epochs');
    await win.keyboard.press('Enter');
    await win.locator('.pane', { hasText: 'Training Runs' }).locator('.monaco-list-row', { hasText: 'completed' }).first().waitFor({ timeout: 180_000 });
    await win.locator('.notifications-toasts .codicon-notifications-clear-all, .notifications-toasts .codicon-close').first().click().catch(() => undefined);
    const run = win.locator('.pane', { hasText: 'Training Runs' }).locator('.monaco-list-row', { hasText: 'completed' }).first();
    await run.click();
    await win.keyboard.press('ArrowRight');
    await run.hover();
    await shot(win, '18-training-runs');

    await win.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
    await quickInputBox(win).fill('>Distributed Compute: ');
    await win.locator('.quick-input-list .monaco-list-row').first().waitFor();
    await shot(win, '19-commands');
    await win.keyboard.press('Escape');
  }
}

async function main() {
  token = (await api('POST', '/auth/local-token')).token;
  await removeWorkers();
  await removeSampleData();
  // Sample dataset for the training steps.
  const rows = ['x1,x2,label'];
  for (let i = 0; i < 300; i++) {
    const a = (i % 17) / 17;
    const b = (i % 11) / 11;
    rows.push(`${a.toFixed(3)},${b.toFixed(3)},${a + b > 1 ? 1 : 0}`);
  }
  const form = new FormData();
  form.append('name', 'points');
  form.append('file', new Blob([rows.join('\n') + '\n']), 'points.csv');
  const res = await fetch(`${GATEWAY}/datasets`, { method: 'POST', headers: { Authorization: `Bearer ${token}` }, body: form });
  if (!res.ok) throw new Error(`dataset upload -> ${res.status} ${await res.text()}`);
  try {
    const only = process.argv[2];
    if (!only || only === 'first') await firstStart();
    if (!only || only === 'everyday') await everyday();
  } finally {
    await removeWorkers();
    await removeSampleData();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});

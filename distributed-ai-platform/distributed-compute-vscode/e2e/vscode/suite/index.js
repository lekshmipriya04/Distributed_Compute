// Runs inside the VS Code extension host. Plain assertions, no framework.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vscode = require('vscode');

const ID = 'aaron43210.distributed-ai-compute';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function step(name, fn) {
  const started = Date.now();
  try {
    await fn();
    console.log(`  ok   ${name} (${Date.now() - started} ms)`);
  } catch (err) {
    console.log(`  FAIL ${name}`);
    throw err;
  }
}

async function waitFor(what, fn, timeoutMs = 120000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const v = await fn();
    if (v) {
      return v;
    }
    if (Date.now() > deadline) {
      throw new Error(`timed out: ${what}`);
    }
    await sleep(500);
  }
}

async function api(url, method, route, token, body) {
  const res = await fetch(url + route, {
    method,
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) {
    throw new Error(`${method} ${route} -> ${res.status} ${text}`);
  }
  return text ? JSON.parse(text) : undefined;
}

exports.run = async function run() {
  console.log(`VS Code ${vscode.version} on ${process.platform}/${process.arch}`);
  const ext = vscode.extensions.getExtension(ID);
  let dc;

  await step('extension is installed and activates', async () => {
    assert.ok(ext, `${ID} not found`);
    dc = await ext.activate();
    assert.equal(ext.isActive, true);
    await dc.startup;
  });

  await step('every contributed command is registered', async () => {
    const declared = ext.packageJSON.contributes.commands.map((c) => c.command);
    const registered = new Set(await vscode.commands.getCommands(true));
    const missing = declared.filter((c) => !registered.has(c));
    assert.deepEqual(missing, []);
  });

  await step('views and the walkthrough are contributed', async () => {
    const views = ext.packageJSON.contributes.views.distributedCompute.map((v) => v.id);
    assert.deepEqual(views, [
      'distributedCompute.nodes',
      'distributedCompute.jobs',
      'distributedCompute.executions',
      'distributedCompute.training',
    ]);
    for (const step of ext.packageJSON.contributes.walkthroughs[0].steps) {
      assert.ok(fs.existsSync(path.join(ext.extensionPath, step.media.markdown)), step.media.markdown);
    }
  });

  await step('safe commands run without throwing', async () => {
    for (const cmd of ['distributedCompute.refreshState', 'distributedCompute.showDiagnostics', 'distributedCompute.refreshTraining']) {
      await vscode.commands.executeCommand(cmd);
    }
  });

  const url = process.env.DC_E2E_URL;
  if (!url) {
    await step('stays offline-safe with no master', async () => {
      assert.equal(dc.connectionState(), 'disconnected');
    });
    return;
  }

  await step(`connects by itself to ${url}`, async () => {
    await waitFor('connected', () => dc.connectionState() === 'connected', 30000);
  });

  // Give this user a worker through the API (the wizard needs keyboard input).
  const { token } = await api(url, 'POST', '/auth/local-token');
  let nodeId;
  await step('a worker is added for the signed-in user', async () => {
    const c = await api(url, 'POST', '/nodes/connect', token, { host: 'ssh-provider', port: 2222, username: 'provider' });
    const p = await api(url, 'POST', '/nodes/provision', token, { onboarding_token: c.onboarding_token, password: 'providerpass' });
    const n = await api(url, 'POST', '/nodes', token, {
      provision_id: p.provision_id, name: 'vscode-smoke', allocated_cpu: 1, allocated_memory_gb: 1,
    });
    nodeId = n.id;
  });

  try {
    await step('Run Python File opens live output that finishes', async () => {
      const file = vscode.Uri.file(path.join(process.env.DC_E2E_WORKSPACE, 'square.py'));
      await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(file));
      await vscode.commands.executeCommand('distributedCompute.runPythonFile');
      const doc = await waitFor('log document', () =>
        vscode.workspace.textDocuments.find((d) => d.uri.scheme === 'distributed-logs'),
      );
      await waitFor('task output', () => /square 49/.test(doc.getText()) && /completed/.test(doc.getText()), 90000);
    });
  } finally {
    if (nodeId) {
      await api(url, 'DELETE', `/nodes/${nodeId}`, token);
    }
  }
};

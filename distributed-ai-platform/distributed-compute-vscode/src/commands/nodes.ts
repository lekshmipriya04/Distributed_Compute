// Worker lifecycle from the editor: add a node (host key check, one-time
// password or manual key install, detection, slice, register), test it,
// detect it again, rotate its key, drain/resume it and remove it.

import * as vscode from 'vscode';
import { validateMasterUrl } from '../api/urls';
import { readSettings } from '../configuration/settings';
import type { GatewayApi } from '../api/gateway';
import { ApiError } from '../models/http';
import type { ComputeNode } from '../models/node';
import {
  detectionSummary,
  isLoopbackHost,
  parsePort,
  sliceChoices,
  validateHost,
  type NodeConnectResult,
  type NodeProvisionResult,
  coresError,
  memoryError,
} from '../models/onboarding';
import type { CommandDeps } from './register';

const TITLE = 'Add Compute Node';

function gateway(deps: CommandDeps): GatewayApi | undefined {
  const gw = deps.connection.gateway();
  if (!gw) {
    void vscode.window.showWarningMessage('Not connected. Run "Distributed Compute: Set Up Platform" or "Connect to Master" first.');
  }
  return gw;
}

function messageOf(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

function fail(deps: CommandDeps, err: unknown, fallback: string): void {
  const message = messageOf(err, fallback);
  deps.logger.error('node command failed', { message });
  void vscode.window.showErrorMessage(message);
}

/** The node behind a tree item, or one picked from the list. */
async function resolveNode(gw: GatewayApi, arg: unknown, placeHolder: string): Promise<ComputeNode | undefined> {
  if (arg && typeof arg === 'object' && 'node' in arg) {
    const node = (arg as { node?: ComputeNode }).node;
    if (node?.id) {
      return node;
    }
  }
  const { items } = await gw.listNodes();
  if (items.length === 0) {
    void vscode.window.showInformationMessage('No compute nodes yet. Run "Add Compute Node" first.');
    return undefined;
  }
  const pick = await vscode.window.showQuickPick(
    items.map((n) => ({ label: n.name || n.id, description: n.status, detail: n.host, node: n })),
    { placeHolder },
  );
  return pick?.node;
}

const label = (n: ComputeNode) => n.name || n.host || n.id;

// --- add ---------------------------------------------------------------------

export async function addNode(deps: CommandDeps): Promise<void> {
  const gw = gateway(deps);
  if (!gw) {
    return;
  }
  const masterIsLocal = validateMasterUrl(readSettings().masterUrl).isLocal === true;

  let host = await vscode.window.showInputBox({
    title: `${TITLE} (1/4): address`,
    prompt: masterIsLocal
      ? 'Hostname or IP of the worker. For this computer use host.docker.internal; for another machine its LAN IP.'
      : 'Hostname or IP of the worker, as the master can reach it.',
    placeHolder: '192.168.1.20  ·  host.docker.internal  ·  ssh-provider',
    ignoreFocusOut: true,
    validateInput: validateHost,
  });
  if (host === undefined) {
    return;
  }
  host = host.trim();
  if (masterIsLocal && isLoopbackHost(host)) {
    const choice = await vscode.window.showWarningMessage(
      `"${host}" means the master's own container when the platform runs in Docker.`,
      { modal: true, detail: 'To add this computer, use host.docker.internal instead.' },
      'Use host.docker.internal',
      `Keep ${host}`,
    );
    if (choice === undefined) {
      return;
    }
    if (choice === 'Use host.docker.internal') {
      host = 'host.docker.internal';
    }
  }

  const portRaw = await vscode.window.showInputBox({
    title: `${TITLE} (2/4): SSH port`,
    value: host === 'ssh-provider' || host === 'ssh-provider-2' ? '2222' : '22',
    ignoreFocusOut: true,
    validateInput: (v) => (parsePort(v) ? undefined : 'Enter a port between 1 and 65535'),
  });
  const port = portRaw === undefined ? undefined : parsePort(portRaw);
  if (!port) {
    return;
  }

  const username = (
    await vscode.window.showInputBox({
      title: `${TITLE} (3/4): SSH user`,
      prompt: 'Use a dedicated, non-admin account: tasks run with its permissions.',
      value: host.startsWith('ssh-provider') ? 'provider' : '',
      ignoreFocusOut: true,
      validateInput: (v) => (/^[A-Za-z0-9._@\\-]{1,128}$/.test(v.trim()) ? undefined : 'Enter the SSH user name'),
    })
  )?.trim();
  if (!username) {
    return;
  }

  let connected: NodeConnectResult;
  try {
    connected = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Contacting ${host}:${port}…` },
      () => gw.connectNode(host as string, port, username),
    );
  } catch (err) {
    fail(deps, err, `Could not reach ${host}:${port}.`);
    return;
  }

  const trust = await vscode.window.showWarningMessage(
    `Trust ${username}@${host}:${port}?`,
    {
      modal: true,
      detail:
        `Host key (${connected.host_key_type}):\n${connected.fingerprint}\n\n` +
        'Compare it with the worker’s own key (ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub) if you are unsure.' +
        (connected.already_registered ? '\n\nOne of your nodes already uses this machine.' : ''),
    },
    'Trust and Continue',
  );
  if (trust !== 'Trust and Continue') {
    return;
  }

  const provisioned = await provisionWithPassword(deps, gw, connected);
  if (!provisioned) {
    return;
  }
  await registerProvisioned(deps, gw, provisioned);
}

/** Password step, with "try again" and "install the key myself" fallbacks. */
async function provisionWithPassword(
  deps: CommandDeps,
  gw: GatewayApi,
  c: NodeConnectResult,
): Promise<NodeProvisionResult | undefined> {
  for (;;) {
    const password = await vscode.window.showInputBox({
      title: `${TITLE} (4/4): password for ${c.username}@${c.host}`,
      prompt:
        'Used once to install the platform’s key, then forgotten. Never stored. ' +
        'Leave it empty to install the key yourself instead.',
      password: true,
      ignoreFocusOut: true,
    });
    if (password === undefined) {
      return undefined;
    }
    if (password === '') {
      return provisionManually(deps, gw, c);
    }
    try {
      return await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `Installing the key on ${c.host} and detecting the machine…` },
        () => gw.provisionNode(c.onboarding_token, password),
      );
    } catch (err) {
      const message = messageOf(err, 'Provisioning failed.');
      deps.logger.warn('provision failed', { message });
      const next = await vscode.window.showErrorMessage(message, 'Try Again', 'Install Key Manually');
      if (next === 'Install Key Manually') {
        return provisionManually(deps, gw, c);
      }
      if (next !== 'Try Again') {
        return undefined;
      }
    }
  }
}

/** For workers that refuse password logins: run one command there, then verify. */
async function provisionManually(
  deps: CommandDeps,
  gw: GatewayApi,
  c: NodeConnectResult,
): Promise<NodeProvisionResult | undefined> {
  let manual;
  try {
    manual = await gw.provisionNodeManually(c.onboarding_token);
  } catch (err) {
    fail(deps, err, 'Could not prepare a key for manual install.');
    return undefined;
  }
  const doc = await vscode.workspace.openTextDocument({
    language: 'shellscript',
    content: [
      `# Run ONE of these on ${c.host}, as ${c.username}, then return to VS Code.`,
      '',
      '# Linux / macOS:',
      manual.install_command_posix,
      '',
      '# Windows (PowerShell):',
      manual.install_command_windows,
      '',
    ].join('\n'),
  });
  await vscode.window.showTextDocument(doc, { preview: true });
  await vscode.env.clipboard.writeText(manual.install_command_posix);
  for (;;) {
    const ready = await vscode.window.showInformationMessage(
      `The Linux/macOS command is on your clipboard. Run it on ${c.host}, then continue.`,
      { modal: true },
      'I Ran It',
    );
    if (ready !== 'I Ran It') {
      return undefined;
    }
    try {
      return await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `Checking the key on ${c.host}…` },
        () => gw.verifyProvision(manual.provision_id),
      );
    } catch (err) {
      void vscode.window.showErrorMessage(messageOf(err, 'The key does not work yet.'));
    }
  }
}

/**
 * Ask for the cores and memory to lend, each on its own. Undefined when the
 * user cancels.
 */
export async function askSliceAmounts(o: {
  host: string;
  maxCores?: number;
  maxMemoryGb?: number;
  cores: number;
  memoryGb: number;
}): Promise<{ cpu: number; memoryGb: number } | undefined> {
  const cores = await vscode.window.showInputBox({
    title: `Cores ${o.host} may use${o.maxCores ? ` (1 – ${o.maxCores})` : ''}`,
    value: String(o.cores),
    ignoreFocusOut: true,
    validateInput: (v) => coresError(v, o.maxCores),
  });
  if (cores === undefined) {
    return undefined;
  }
  const memory = await vscode.window.showInputBox({
    title: `Memory (GB) ${o.host} may use${o.maxMemoryGb ? ` (up to ${o.maxMemoryGb})` : ''}`,
    value: String(o.memoryGb),
    ignoreFocusOut: true,
    validateInput: (v) => memoryError(v, o.maxMemoryGb),
  });
  if (memory === undefined) {
    return undefined;
  }
  return { cpu: Number(cores.trim()), memoryGb: Number(memory.trim()) };
}

async function registerProvisioned(deps: CommandDeps, gw: GatewayApi, p: NodeProvisionResult): Promise<void> {
  const presets = sliceChoices(p);
  const picked = await vscode.window.showQuickPick(
    [
      ...presets.map((c, i) => ({ ...c, picked: i === 0, custom: false })),
      { label: '$(edit) Custom…', description: 'choose the cores and memory yourself', cpu: 0, memoryGb: 0, custom: true },
    ],
    {
      title: `How much of ${p.host} may the platform use?`,
      placeHolder: `Detected: ${detectionSummary(p)}`,
      ignoreFocusOut: true,
    },
  );
  if (!picked) {
    return;
  }
  const slice = picked.custom
    ? await askSliceAmounts({
        host: p.host,
        maxCores: Math.max(1, p.detected_cpu),
        maxMemoryGb: p.detected_memory_gb > 0 ? p.detected_memory_gb : undefined,
        cores: presets[0].cpu,
        memoryGb: presets[0].memoryGb,
      })
    : picked;
  if (!slice) {
    return;
  }
  let gpuCount = 0;
  let gpuMemoryGb = 0;
  if (p.gpu_count > 0 && p.gpu_memory_gb) {
    const lend = await vscode.window.showQuickPick(
      [
        { label: 'Lend half the GPU memory', gb: Math.round((p.gpu_memory_gb / 2) * 10) / 10 },
        { label: 'Lend all GPU memory', gb: p.gpu_memory_gb },
        { label: 'Keep the GPU to myself', gb: 0 },
      ],
      {
        title: `GPU: ${p.gpu_model ?? 'GPU'} (${p.gpu_memory_gb} GB)`,
        placeHolder: p.gpu_runtime ? undefined : 'PyTorch was not found there, so a GPU memory cap cannot be enforced',
        ignoreFocusOut: true,
      },
    );
    if (!lend) {
      return;
    }
    gpuMemoryGb = lend.gb;
    gpuCount = lend.gb > 0 ? p.gpu_count : 0;
  }
  const name = await vscode.window.showInputBox({
    title: 'Name this node',
    value: p.host === 'host.docker.internal' ? 'this-computer' : p.host,
    ignoreFocusOut: true,
  });
  if (name === undefined) {
    return;
  }
  try {
    const node = await gw.registerNode({
      provision_id: p.provision_id,
      name: name.trim(),
      allocated_cpu: slice.cpu,
      allocated_memory_gb: slice.memoryGb,
      allocated_gpu_count: gpuCount,
      allocated_gpu_memory_gb: gpuMemoryGb,
    });
    deps.logger.info('node added', { id: node.id, host: node.host });
    deps.refreshAll();
    void vscode.window.showInformationMessage(`Added ${node.name}: ${detectionSummary(p)}.`);
  } catch (err) {
    fail(deps, err, 'Could not register the node.');
  }
}

// --- lifecycle ---------------------------------------------------------------

export async function testNode(deps: CommandDeps, arg: unknown): Promise<void> {
  const gw = gateway(deps);
  const node = gw && (await pick(deps, gw, arg, 'Test which node?'));
  if (!gw || !node) {
    return;
  }
  const started = Date.now();
  try {
    const result = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Testing ${label(node)}…` },
      () => gw.healthCheckNode(node.id),
    );
    const ms = Date.now() - started;
    deps.refreshAll();
    if (result.health_status === 'online') {
      void vscode.window.showInformationMessage(`${label(node)} is reachable over SSH (check took ${ms} ms).`);
    } else {
      void vscode.window.showWarningMessage(`${label(node)} is ${result.health_status}. Check that it is on and sshd is running.`);
    }
  } catch (err) {
    fail(deps, err, `Could not test ${label(node)}.`);
  }
}

export async function refreshNodeCapabilities(deps: CommandDeps, arg: unknown): Promise<void> {
  const gw = gateway(deps);
  const node = gw && (await pick(deps, gw, arg, 'Detect which node again?'));
  if (!gw || !node) {
    return;
  }
  try {
    const result = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Detecting ${label(node)}…` },
      () => gw.refreshNode(node.id),
    );
    deps.refreshAll();
    void vscode.window.showInformationMessage(`${label(node)}: ${detectionSummary(result)}.`);
  } catch (err) {
    fail(deps, err, `Could not detect ${label(node)}.`);
  }
}

export async function rotateNodeKey(deps: CommandDeps, arg: unknown): Promise<void> {
  const gw = gateway(deps);
  const node = gw && (await pick(deps, gw, arg, 'Rotate the key of which node?'));
  if (!gw || !node) {
    return;
  }
  try {
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Rotating the key on ${label(node)}…` },
      () => gw.rotateNodeKey(node.id),
    );
    deps.refreshAll();
    void vscode.window.showInformationMessage(`${label(node)} now uses a new platform key; the old one was removed.`);
  } catch (err) {
    fail(deps, err, `Could not rotate the key on ${label(node)}.`);
  }
}

export async function setDraining(deps: CommandDeps, arg: unknown, draining: boolean): Promise<void> {
  const gw = gateway(deps);
  const node = gw && (await pick(deps, gw, arg, draining ? 'Drain which node?' : 'Resume which node?'));
  if (!gw || !node) {
    return;
  }
  try {
    await gw.setNodeStatus(node.id, draining ? 'draining' : 'online');
    deps.refreshAll();
    void vscode.window.showInformationMessage(
      draining
        ? `${label(node)} is draining: running work finishes, and it gets no new tasks or training.`
        : `${label(node)} takes new work again.`,
    );
  } catch (err) {
    fail(deps, err, `Could not update ${label(node)}.`);
  }
}

export async function removeNode(deps: CommandDeps, arg: unknown): Promise<void> {
  const gw = gateway(deps);
  const node = gw && (await pick(deps, gw, arg, 'Remove which node?'));
  if (!gw || !node) {
    return;
  }
  const confirm = await vscode.window.showWarningMessage(
    `Remove ${label(node)}?`,
    { modal: true, detail: 'The master deletes its key line from the worker and forgets the node. Nothing else on the worker changes.' },
    'Remove',
  );
  if (confirm !== 'Remove') {
    return;
  }
  try {
    const result = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Removing ${label(node)}…` },
      () => gw.deleteNode(node.id),
    );
    deps.refreshAll();
    if (result.key_removed) {
      void vscode.window.showInformationMessage(result.message);
    } else {
      void vscode.window.showWarningMessage(result.message);
    }
  } catch (err) {
    fail(deps, err, `Could not remove ${label(node)}.`);
  }
}

async function pick(deps: CommandDeps, gw: GatewayApi, arg: unknown, placeHolder: string): Promise<ComputeNode | undefined> {
  try {
    return await resolveNode(gw, arg, placeHolder);
  } catch (err) {
    fail(deps, err, 'Could not load your nodes.');
    return undefined;
  }
}

// Command handlers. Every command is wired to a real gateway route: nodes
// (add, test, detect, rotate key, slice, drain/resume, remove), executions
// (run, run in parallel, live logs, cancel, retry, results) and training.

import * as vscode from 'vscode';
import { ConnectionManager } from '../connection/connectionManager';
import { TokenStore } from '../security/secretStorage';
import { Logger } from '../utilities/logger';
import { readSettings, updateMasterUrl } from '../configuration/settings';
import { validateMasterUrl } from '../api/urls';
import { ApiError } from '../models/http';
import type { Runtime } from '../models/execution';
import { gpuPresetGb, hasGpu, type ComputeNode, type NodeSliceRequest } from '../models/node';
import { buildDefault, validateConfig } from '../configuration/projectConfig';
import { disableSparkRay, enableSparkRay } from './engines';
import { setUpPlatform, stopPlatform } from './platform';
import { normalizeLineEndings } from '../models/platform';
import { addNode, refreshNodeCapabilities, removeNode, rotateNodeKey, setDraining, testNode } from './nodes';
import {
  ExecutionLogProvider,
  LOG_SCHEME,
  cancelExecution,
  retrieveResults,
  retryExecution,
  runInParallel,
  runnableEditor,
  viewLogs,
} from './executions';
import { downloadOutputs, runProject, setEnvironmentCache } from './projects';
import {
  deleteRun,
  downloadModel,
  newCustomModel,
  runTrainingAgain,
  showRunDetails,
  submitTrainingWizard,
} from './training';

export interface CommandDeps {
  context: vscode.ExtensionContext;
  connection: ConnectionManager;
  tokens: TokenStore;
  logger: Logger;
  refreshAll: () => void;
}

const inFlightRuntimes = new Set<string>();

export function registerCommands(deps: CommandDeps): void {
  const { context } = deps;
  const reg = (id: string, fn: (...args: unknown[]) => unknown): void => {
    context.subscriptions.push(vscode.commands.registerCommand(id, fn));
  };

  reg('distributedCompute.setUpPlatform', () => setUpPlatform(deps));
  reg('distributedCompute.stopPlatform', () => stopPlatform(deps));
  reg('distributedCompute.connect', () => doConnect(deps));
  reg('distributedCompute.disconnect', () => {
    deps.connection.disconnect();
    deps.refreshAll();
  });
  reg('distributedCompute.configureMaster', () => configureMaster(deps));
  reg('distributedCompute.configureAuth', () => configureAuth(deps));
  reg('distributedCompute.showConnectionStatus', () => showConnectionStatus(deps));
  reg('distributedCompute.refreshState', () => deps.refreshAll());
  reg('distributedCompute.refreshNodes', () => deps.refreshAll());
  reg('distributedCompute.viewJobs', () => vscode.commands.executeCommand('distributedCompute.jobs.focus'));
  reg('distributedCompute.showDiagnostics', () => deps.logger.show());
  reg('distributedCompute.showCluster', () => showCluster(deps));
  reg('distributedCompute.viewNode', (...a) => viewNode(deps, a[0]));
  reg('distributedCompute.configureSlice', (...a) => configureSlice(deps, a[0]));

  const logs = new ExecutionLogProvider();
  context.subscriptions.push(logs, vscode.workspace.registerTextDocumentContentProvider(LOG_SCHEME, logs));
  reg('distributedCompute.runPythonFile', () => runActiveFile(deps, logs, 'python'));
  reg('distributedCompute.runDistributed', () => runInParallel(deps, logs));
  reg('distributedCompute.viewLogs', (...a) => viewLogs(deps, logs, a[0]));
  reg('distributedCompute.cancelExecution', (...a) => cancelExecution(deps, a[0]));
  reg('distributedCompute.retryExecution', (...a) => retryExecution(deps, logs, a[0]));
  reg('distributedCompute.retrieveResults', (...a) => retrieveResults(deps, a[0]));
  reg('distributedCompute.runProject', (...a) => runProject(deps, logs, a[0]));
  reg('distributedCompute.downloadOutputs', (...a) => downloadOutputs(deps, a[0]));
  reg('distributedCompute.setEnvironmentCache', (...a) => setEnvironmentCache(deps, extractNodeId(a[0])));

  reg('distributedCompute.addNode', () => addNode(deps));
  reg('distributedCompute.testNode', (...a) => testNode(deps, a[0]));
  reg('distributedCompute.refreshNodeCapabilities', (...a) => refreshNodeCapabilities(deps, a[0]));
  reg('distributedCompute.rotateNodeKey', (...a) => rotateNodeKey(deps, a[0]));
  reg('distributedCompute.drainNode', (...a) => setDraining(deps, a[0], true));
  reg('distributedCompute.resumeNode', (...a) => setDraining(deps, a[0], false));
  reg('distributedCompute.removeNode', (...a) => removeNode(deps, a[0]));
  reg('distributedCompute.runWithRuntime', () => runWithPickedRuntime(deps, logs));

  reg('distributedCompute.submitTraining', () => submitTrainingWizard(deps));
  reg('distributedCompute.trainWithModelFile', (...a) => submitTrainingWizard(deps, { modelFile: uriArg(a[0]) ?? activePythonFile() }));
  reg('distributedCompute.trainOnDataset', async (...a) => {
    const file = uriArg(a[0]);
    if (file) {
      await submitTrainingWizard(deps, { datasetFile: file });
    } else {
      await submitTrainingWizard(deps);
    }
  });
  reg('distributedCompute.runTrainingAgain', () => runTrainingAgain(deps));
  reg('distributedCompute.newCustomModel', () => newCustomModel());
  reg('distributedCompute.enableSparkRay', () => enableSparkRay(deps));
  reg('distributedCompute.disableSparkRay', () => disableSparkRay(deps));
  reg('distributedCompute.downloadModel', (...a) => downloadModel(deps, a[0]));
  reg('distributedCompute.showTrainingRun', (...a) => showRunDetails(deps, a[0]));
  reg('distributedCompute.deleteTrainingRun', (...a) => deleteRun(deps, a[0]));
  reg('distributedCompute.refreshTraining', () => deps.refreshAll());

  reg('distributedCompute.initProject', () => initProject(deps));
  reg('distributedCompute.validateProject', () => validateProject(deps));

  deps.logger.info('commands registered');
}

function uriArg(arg: unknown): vscode.Uri | undefined {
  return arg instanceof vscode.Uri ? arg : undefined;
}

function activePythonFile(): vscode.Uri | undefined {
  const doc = vscode.window.activeTextEditor?.document;
  return doc && doc.languageId === 'python' && doc.uri.scheme === 'file' ? doc.uri : undefined;
}

async function doConnect(deps: CommandDeps): Promise<void> {
  const ok = await deps.connection.connect();
  deps.refreshAll();
  const snap = deps.connection.snapshot();
  if (ok) {
    if (readSettings().showNotifications) {
      void vscode.window.showInformationMessage(`Connected to ${snap.masterUrl}.`);
    }
  } else {
    void vscode.window.showErrorMessage(snap.lastError ?? 'Failed to connect to the master.');
  }
}

async function configureMaster(deps: CommandDeps): Promise<void> {
  const current = readSettings().masterUrl;
  const url = await vscode.window.showInputBox({
    title: 'Configure Master URL',
    prompt: 'Base URL of the extension gateway (https for remote; http only for local dev).',
    value: current,
    ignoreFocusOut: true,
    validateInput: (v) => (validateMasterUrl(v).ok ? undefined : validateMasterUrl(v).reason),
  });
  if (url === undefined) {
    return;
  }
  const validation = validateMasterUrl(url);
  if (validation.isLocal && url.startsWith('http:')) {
    const proceed = await vscode.window.showWarningMessage(
      'This is an unencrypted http:// endpoint. Only use it for local development.',
      { modal: true },
      'Use anyway',
    );
    if (proceed !== 'Use anyway') {
      return;
    }
  }
  const target = vscode.workspace.workspaceFolders?.length
    ? await pickConfigTarget()
    : vscode.ConfigurationTarget.Global;
  if (target === undefined) {
    return;
  }
  await updateMasterUrl(url, target);
  deps.logger.info('master URL updated');
  const connectNow = await vscode.window.showInformationMessage('Master URL saved.', 'Connect now');
  if (connectNow === 'Connect now') {
    await doConnect(deps);
  }
}

async function pickConfigTarget(): Promise<vscode.ConfigurationTarget | undefined> {
  const pick = await vscode.window.showQuickPick(
    [
      { label: 'User', description: 'Applies to all workspaces', target: vscode.ConfigurationTarget.Global },
      { label: 'Workspace', description: 'This workspace only', target: vscode.ConfigurationTarget.Workspace },
    ],
    { title: 'Where should the master URL be saved?' },
  );
  return pick?.target;
}

async function configureAuth(deps: CommandDeps): Promise<void> {
  const token = await vscode.window.showInputBox({
    title: 'Configure Authentication',
    prompt: 'Paste the platform JWT bearer token. Stored in VS Code SecretStorage only.',
    password: true,
    ignoreFocusOut: true,
  });
  if (token === undefined) {
    return;
  }
  if (token.trim() === '') {
    await deps.tokens.clear();
    void vscode.window.showInformationMessage('Authentication token cleared.');
    return;
  }
  await deps.tokens.set(token.trim());
  deps.logger.info('auth token stored in SecretStorage');
  const connectNow = await vscode.window.showInformationMessage('Token saved.', 'Connect now');
  if (connectNow === 'Connect now') {
    await doConnect(deps);
  }
}

async function showConnectionStatus(deps: CommandDeps): Promise<void> {
  const snap = deps.connection.snapshot();
  const connected = snap.state === 'connected';
  const actions = [
    connected ? 'Add Compute Node' : 'Start Platform on This Computer',
    connected ? 'View Cluster Status' : 'Connect to Master',
    'View Jobs',
    'Refresh Platform State',
    'Open Diagnostics',
    connected ? 'Disconnect' : 'Configure Master URL',
  ];
  const pick = await vscode.window.showQuickPick(actions, {
    title: `Distributed Compute — ${snap.state}${snap.lastError ? ` (${snap.lastError})` : ''}`,
  });
  switch (pick) {
    case 'Start Platform on This Computer':
      return void vscode.commands.executeCommand('distributedCompute.setUpPlatform');
    case 'Add Compute Node':
      return void vscode.commands.executeCommand('distributedCompute.addNode');
    case 'Connect to Master':
      return void vscode.commands.executeCommand('distributedCompute.connect');
    case 'View Cluster Status':
      return void vscode.commands.executeCommand('distributedCompute.showCluster');
    case 'View Jobs':
      return void vscode.commands.executeCommand('distributedCompute.viewJobs');
    case 'Refresh Platform State':
      return void vscode.commands.executeCommand('distributedCompute.refreshState');
    case 'Open Diagnostics':
      return void vscode.commands.executeCommand('distributedCompute.showDiagnostics');
    case 'Disconnect':
      return void vscode.commands.executeCommand('distributedCompute.disconnect');
    case 'Configure Master URL':
      return void vscode.commands.executeCommand('distributedCompute.configureMaster');
    default:
      return;
  }
}

function requireGateway(deps: CommandDeps) {
  const gw = deps.connection.gateway();
  if (!gw) {
    void vscode.window.showWarningMessage('Not connected. Run "Distributed Compute: Connect to Master" first.');
    return undefined;
  }
  return gw;
}

async function showCluster(deps: CommandDeps): Promise<void> {
  const gw = requireGateway(deps);
  if (!gw) {
    return;
  }
  try {
    const cs = await gw.clusterStatus();
    deps.logger.info('cluster status', cs);
    void vscode.window.showInformationMessage(
      `Nodes ${cs.nodesOnline}/${cs.nodesTotal} online · CPU reserved ${cs.cpuReserved}/${cs.cpuTotal} · Jobs running ${cs.jobsRunning}`,
    );
    deps.logger.show();
  } catch (err) {
    reportError(deps, err, 'Failed to load cluster status.');
  }
}

async function viewNode(deps: CommandDeps, arg: unknown): Promise<void> {
  const gw = requireGateway(deps);
  if (!gw) {
    return;
  }
  const id = extractNodeId(arg) ?? (await vscode.window.showInputBox({ title: 'Node id', ignoreFocusOut: true }));
  if (!id) {
    return;
  }
  try {
    const node = await gw.getNode(id);
    deps.logger.info('node details', node);
    deps.logger.show();
  } catch (err) {
    reportError(deps, err, 'Failed to load node details.');
  }
}

async function configureSlice(deps: CommandDeps, arg: unknown): Promise<void> {
  const gw = requireGateway(deps);
  if (!gw) {
    return;
  }
  const id = extractNodeId(arg) ?? (await vscode.window.showInputBox({ title: 'Node id', ignoreFocusOut: true }));
  if (!id) {
    return;
  }
  let node: ComputeNode;
  try {
    node = await gw.getNode(id);
  } catch (err) {
    reportError(deps, err, 'Failed to load node details.');
    return;
  }
  const req = await pickSlice(node);
  if (!req) {
    return;
  }
  try {
    const updated = await gw.updateNodeSlice(id, req);
    deps.logger.info('node slice updated', { id, ...req });
    void vscode.window.showInformationMessage(`Updated slice for ${updated.name || updated.id}.`);
    deps.refreshAll();
  } catch (err) {
    reportError(deps, err, 'Failed to update the node slice.');
  }
}

/** Quick-pick flow: GPU VRAM preset (when a GPU exists), then cores. */
async function pickSlice(node: ComputeNode): Promise<NodeSliceRequest | undefined> {
  const req: NodeSliceRequest = {};
  if (hasGpu(node)) {
    const total = node.gpuMemoryTotalGb as number;
    const presets = [0.25, 1 / 3, 0.5].map((f) => ({
      label: `${Math.round(f * 100)}%`,
      description: `${gpuPresetGb(total, f)} GB of ${total} GB VRAM`,
      gb: gpuPresetGb(total, f) as number | undefined,
    }));
    const choice = await vscode.window.showQuickPick(
      [...presets, { label: 'Custom GB…', description: `0 – ${total} GB`, gb: undefined }],
      { title: `GPU slice for ${node.name || node.id}${node.gpuModel ? ` (${node.gpuModel})` : ''}`, ignoreFocusOut: true },
    );
    if (!choice) {
      return undefined;
    }
    let gb = choice.gb;
    if (gb === undefined) {
      const raw = await vscode.window.showInputBox({
        title: 'GPU memory to lend (GB)',
        value: String(node.gpuMemoryAllocatableGb ?? 0),
        ignoreFocusOut: true,
        validateInput: (v) => {
          const n = Number(v);
          return Number.isFinite(n) && n >= 0 && n <= total ? undefined : `Enter a number between 0 and ${total}`;
        },
      });
      if (raw === undefined) {
        return undefined;
      }
      gb = Number(raw);
    }
    req.gpuMemoryAllocatableGb = gb;
    req.gpuAllocatableCount = gb > 0 ? node.gpuCount : 0;
  }
  const maxCores = node.cpuTotal;
  const rawCores = await vscode.window.showInputBox({
    title: `Cores to lend${maxCores ? ` (1 – ${maxCores})` : ''}`,
    value: String(node.cpuAllocatable ?? 1),
    ignoreFocusOut: true,
    validateInput: (v) => {
      const n = Number(v);
      return Number.isInteger(n) && n >= 1 && (maxCores === undefined || n <= maxCores)
        ? undefined
        : `Enter a whole number${maxCores ? ` between 1 and ${maxCores}` : ' >= 1'}`;
    },
  });
  if (rawCores === undefined) {
    return undefined;
  }
  req.cpuAllocatable = Number(rawCores);
  return req;
}

function extractNodeId(arg: unknown): string | undefined {
  if (arg && typeof arg === 'object' && 'node' in arg) {
    const node = (arg as { node?: { id?: string } }).node;
    return node?.id;
  }
  return undefined;
}

/**
 * Ask the backend which runtimes are detected on the user's online nodes, let
 * the user pick one, then submit the active file with it. `custom` is left out
 * here because it needs a command template the editor does not know.
 */
async function runWithPickedRuntime(deps: CommandDeps, logs: ExecutionLogProvider): Promise<void> {
  const gw = requireGateway(deps);
  if (!gw) {
    return;
  }
  let runtimes;
  try {
    runtimes = await gw.listRuntimes();
  } catch (err) {
    reportError(deps, err, 'Failed to load the available runtimes.');
    return;
  }
  const usable = runtimes.filter((r) => r.id !== 'custom' && r.availableNodeIds.length > 0);
  if (usable.length === 0) {
    void vscode.window.showWarningMessage(
      'No runtime is available on your online nodes. Runtimes are detected on each worker; nothing is installed there.',
    );
    return;
  }
  const picked = await vscode.window.showQuickPick(
    usable.map((r) => ({
      label: r.name,
      description: r.id,
      detail: `${r.availableNodeIds.length} node(s) · ${r.kind}`,
      id: r.id,
    })),
    { placeHolder: 'Run the active file with…' },
  );
  if (picked) {
    await runActiveFile(deps, logs, picked.id);
  }
}

async function runActiveFile(
  deps: CommandDeps,
  logs: ExecutionLogProvider,
  forcedRuntime: Runtime | undefined,
): Promise<void> {
  const gw = requireGateway(deps);
  if (!gw) {
    return;
  }
  const editor = runnableEditor();
  if (!editor) {
    void vscode.window.showWarningMessage('Open a file to run before submitting an execution.');
    return;
  }
  const settings = readSettings();
  const runtime: Runtime = forcedRuntime ?? settings.defaultRuntime;
  if (inFlightRuntimes.has(runtime)) {
    void vscode.window.showWarningMessage(`A ${runtime} submission is already in progress.`);
    return;
  }
  const code = normalizeLineEndings(editor.document.getText());
  if (code.trim() === '') {
    void vscode.window.showWarningMessage('The active file is empty.');
    return;
  }
  // Stable id per logical submission so a double-click cannot double-submit.
  const requestId = `dc-submit-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  inFlightRuntimes.add(runtime);
  try {
    const exec = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Submitting ${runtime} execution…` },
      () =>
        gw.createExecution(
          { runtime, code, timeoutSeconds: settings.defaultTimeoutSeconds, executionPolicy: settings.executionPolicy },
          requestId,
        ),
    );
    deps.logger.info('execution submitted', { id: exec.id, runtime });
    deps.refreshAll();
    // The output opens beside the code and follows the run live.
    await viewLogs(deps, logs, { execution: exec });
  } catch (err) {
    reportError(deps, err, 'Failed to submit the execution.');
  } finally {
    inFlightRuntimes.delete(runtime);
  }
}

async function initProject(deps: CommandDeps): Promise<void> {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    void vscode.window.showWarningMessage('Open a workspace folder to initialize a project.');
    return;
  }
  const settings = readSettings();
  const target = vscode.Uri.joinPath(folder.uri, 'distributed.json');
  try {
    await vscode.workspace.fs.stat(target);
    const overwrite = await vscode.window.showWarningMessage(
      'distributed.json already exists. Overwrite it?',
      { modal: true },
      'Overwrite',
    );
    if (overwrite !== 'Overwrite') {
      return;
    }
  } catch {
    // Does not exist — safe to create.
  }
  const config = buildDefault(folder.name, settings.defaultRuntime, settings.defaultTimeoutSeconds);
  const content = Buffer.from(JSON.stringify(config, null, 2) + '\n', 'utf8');
  await vscode.workspace.fs.writeFile(target, content);
  const doc = await vscode.workspace.openTextDocument(target);
  await vscode.window.showTextDocument(doc);
  deps.logger.info('distributed.json created');
}

async function validateProject(deps: CommandDeps): Promise<void> {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    void vscode.window.showWarningMessage('Open a workspace folder with a distributed.json to validate.');
    return;
  }
  const target = vscode.Uri.joinPath(folder.uri, 'distributed.json');
  let parsed: unknown;
  try {
    const bytes = await vscode.workspace.fs.readFile(target);
    parsed = JSON.parse(Buffer.from(bytes).toString('utf8'));
  } catch (err) {
    const reason = err instanceof SyntaxError ? 'is not valid JSON' : 'could not be read';
    void vscode.window.showErrorMessage(`distributed.json ${reason}.`);
    return;
  }
  const result = validateConfig(parsed);
  if (result.ok) {
    void vscode.window.showInformationMessage('distributed.json is valid.');
  } else {
    deps.logger.warn('distributed.json invalid', { errors: result.errors });
    deps.logger.show();
    void vscode.window.showErrorMessage(`distributed.json has ${result.errors.length} problem(s). See diagnostics.`);
  }
}

function reportError(deps: CommandDeps, err: unknown, fallback: string): void {
  const message = err instanceof ApiError ? err.message : fallback;
  deps.logger.error('command failed', { message });
  void vscode.window.showErrorMessage(message);
}

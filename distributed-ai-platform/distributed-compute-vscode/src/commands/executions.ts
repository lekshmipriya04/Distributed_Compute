// Executions from the editor: run a file as many parallel tasks, follow every
// task's output live, cancel, retry (all or only failed tasks) and save the
// results into the workspace.

import * as vscode from 'vscode';
import { readSettings } from '../configuration/settings';
import type { GatewayApi } from '../api/gateway';
import { ApiError } from '../models/http';
import { isRunnableScheme, isTerminal, type Execution, type ExecutionStatus, type RuntimeInfo } from '../models/execution';
import {
  formatTaskLog,
  parseInputLines,
  parseInputList,
  resultFileName,
  type ExecutionTasks,
} from '../models/onboarding';
import { normalizeLineEndings } from '../models/platform';
import type { CommandDeps } from './register';

export const LOG_SCHEME = 'distributed-logs';
const POLL_MS = 2000;

function gateway(deps: CommandDeps): GatewayApi | undefined {
  const gw = deps.connection.gateway();
  if (!gw) {
    void vscode.window.showWarningMessage('Not connected. Run "Distributed Compute: Set Up Platform" or "Connect to Master" first.');
  }
  return gw;
}

function fail(deps: CommandDeps, err: unknown, fallback: string): void {
  const message = err instanceof ApiError ? err.message : fallback;
  deps.logger.error('execution command failed', { message });
  void vscode.window.showErrorMessage(message);
}

async function resolveExecution(
  gw: GatewayApi,
  arg: unknown,
  placeHolder: string,
  filter: (e: Execution) => boolean = () => true,
): Promise<Execution | undefined> {
  if (arg && typeof arg === 'object' && 'execution' in arg) {
    const e = (arg as { execution?: Execution }).execution;
    if (e?.id) {
      return e;
    }
  }
  const { items } = await gw.listExecutions();
  const candidates = items.filter(filter).sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1));
  if (candidates.length === 0) {
    void vscode.window.showInformationMessage('No matching executions.');
    return undefined;
  }
  const pick = await vscode.window.showQuickPick(
    candidates.map((e) => ({
      label: `${e.runtime} · ${e.status}`,
      description: `${e.completed}/${e.total} done${e.failed ? ` · ${e.failed} failed` : ''}`,
      detail: `${e.id} · ${new Date(e.createdAt).toLocaleString()}`,
      execution: e,
    })),
    { placeHolder },
  );
  return pick?.execution;
}

// --- live logs ---------------------------------------------------------------

/** Read-only documents holding each execution's task output, updated live. */
export class ExecutionLogProvider implements vscode.TextDocumentContentProvider, vscode.Disposable {
  private readonly emitter = new vscode.EventEmitter<vscode.Uri>();
  readonly onDidChange = this.emitter.event;
  private readonly content = new Map<string, string>();
  private readonly following = new Set<string>();

  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.content.get(uri.toString()) ?? 'Loading…\n';
  }

  set(uri: vscode.Uri, text: string): void {
    if (this.content.get(uri.toString()) !== text) {
      this.content.set(uri.toString(), text);
      this.emitter.fire(uri);
    }
  }

  /** Poll until the execution finishes or its document is closed. */
  async follow(uri: vscode.Uri, load: () => Promise<{ text: string; done: boolean }>): Promise<void> {
    const key = uri.toString();
    if (this.following.has(key)) {
      return;
    }
    this.following.add(key);
    try {
      for (;;) {
        const { text, done } = await load();
        this.set(uri, text);
        const open = vscode.workspace.textDocuments.some((d) => d.uri.toString() === key);
        if (done || !open) {
          return;
        }
        await new Promise((r) => setTimeout(r, POLL_MS));
      }
    } finally {
      this.following.delete(key);
    }
  }

  dispose(): void {
    this.emitter.dispose();
  }
}

export async function viewLogs(deps: CommandDeps, logs: ExecutionLogProvider, arg: unknown): Promise<void> {
  const gw = gateway(deps);
  if (!gw) {
    return;
  }
  let exec: Execution | undefined;
  try {
    exec = await resolveExecution(gw, arg, 'Show the output of which execution?');
  } catch (err) {
    fail(deps, err, 'Could not load executions.');
    return;
  }
  if (!exec) {
    return;
  }
  const id = exec.id;
  const uri = vscode.Uri.from({ scheme: LOG_SCHEME, path: `/${exec.runtime}-${id.slice(0, 8)}.log`, query: id });
  const names = await nodeNames(gw);
  const load = async () => {
    try {
      const data = await gw.executionTasks(id);
      return { text: formatTaskLog(data, names), done: isTerminal(data.batch.status as ExecutionStatus) };
    } catch (err) {
      return { text: `Could not load the output: ${err instanceof ApiError ? err.message : String(err)}\n`, done: true };
    }
  };
  const first = await load();
  logs.set(uri, first.text);
  const doc = await vscode.workspace.openTextDocument(uri);
  await vscode.languages.setTextDocumentLanguage(doc, 'log');
  // Beside the code, without taking focus, so the file stays the active one.
  await vscode.window.showTextDocument(doc, { preview: false, viewColumn: vscode.ViewColumn.Beside, preserveFocus: true });
  if (!first.done) {
    void logs.follow(uri, load).then(() => deps.refreshAll());
  }
}

async function nodeNames(gw: GatewayApi): Promise<Record<string, string>> {
  try {
    const { items } = await gw.listNodes();
    return Object.fromEntries(items.map((n) => [n.id, n.name || n.id]));
  } catch {
    return {};
  }
}

// --- cancel / retry / results ------------------------------------------------

export async function cancelExecution(deps: CommandDeps, arg: unknown): Promise<void> {
  const gw = gateway(deps);
  if (!gw) {
    return;
  }
  let exec: Execution | undefined;
  try {
    exec = await resolveExecution(gw, arg, 'Cancel which execution?', (e) => !isTerminal(e.status));
  } catch (err) {
    fail(deps, err, 'Could not load executions.');
    return;
  }
  if (!exec) {
    return;
  }
  const confirm = await vscode.window.showWarningMessage(
    `Cancel this ${exec.runtime} execution?`,
    { modal: true, detail: 'Running tasks are stopped and their sandboxes removed; finished tasks keep their output.' },
    'Cancel Execution',
  );
  if (confirm !== 'Cancel Execution') {
    return;
  }
  try {
    await gw.cancelExecution(exec.id);
    deps.refreshAll();
    void vscode.window.showInformationMessage('Cancelling. Running tasks stop within a few seconds.');
  } catch (err) {
    fail(deps, err, 'Could not cancel the execution.');
  }
}

export async function retryExecution(deps: CommandDeps, logs: ExecutionLogProvider, arg: unknown): Promise<void> {
  const gw = gateway(deps);
  if (!gw) {
    return;
  }
  let exec: Execution | undefined;
  try {
    exec = await resolveExecution(gw, arg, 'Run which execution again?', (e) => isTerminal(e.status));
  } catch (err) {
    fail(deps, err, 'Could not load executions.');
    return;
  }
  if (!exec) {
    return;
  }
  const unfinished = (exec.failed ?? 0) + (exec.cancelled ?? 0);
  let failedOnly = false;
  if (unfinished > 0 && unfinished < exec.total) {
    const choice = await vscode.window.showQuickPick(
      [
        { label: `Only the ${unfinished} failed or cancelled task(s)`, failedOnly: true },
        { label: `All ${exec.total} tasks`, failedOnly: false },
      ],
      { placeHolder: 'Run again' },
    );
    if (!choice) {
      return;
    }
    failedOnly = choice.failedOnly;
  }
  try {
    const next = await gw.retryExecution(exec.id, failedOnly);
    deps.refreshAll();
    const open = await vscode.window.showInformationMessage(`Started again as ${next.id.slice(0, 8)}.`, 'Show Output');
    if (open) {
      await viewLogs(deps, logs, { execution: { ...exec, id: next.id, status: 'pending' } });
    }
  } catch (err) {
    fail(deps, err, 'Could not run the execution again.');
  }
}

export async function retrieveResults(deps: CommandDeps, arg: unknown): Promise<void> {
  const gw = gateway(deps);
  if (!gw) {
    return;
  }
  let exec: Execution | undefined;
  try {
    exec = await resolveExecution(gw, arg, 'Save the results of which execution?', (e) => isTerminal(e.status));
  } catch (err) {
    fail(deps, err, 'Could not load executions.');
    return;
  }
  if (!exec) {
    return;
  }
  let data: ExecutionTasks;
  try {
    data = await gw.executionTasks(exec.id);
  } catch (err) {
    fail(deps, err, 'Could not load the results.');
    return;
  }
  const base = await resultsFolder();
  if (!base) {
    return;
  }
  const dir = vscode.Uri.joinPath(base, 'distributed-results', exec.id);
  await vscode.workspace.fs.createDirectory(dir);
  const write = (name: string, text: string) =>
    vscode.workspace.fs.writeFile(vscode.Uri.joinPath(dir, name), Buffer.from(text, 'utf8'));
  const total = data.tasks.length;
  for (const t of data.tasks) {
    await write(resultFileName(t.seq, total, 'out'), t.stdout ?? '');
    if (t.stderr) {
      await write(resultFileName(t.seq, total, 'err'), t.stderr);
    }
  }
  await write(
    'summary.json',
    JSON.stringify(
      {
        execution: data.batch,
        tasks: data.tasks.map(({ stdout: _o, stderr: _e, ...rest }) => rest),
      },
      null,
      2,
    ) + '\n',
  );
  deps.logger.info('results saved', { id: exec.id, dir: dir.fsPath, tasks: total });
  const action = await vscode.window.showInformationMessage(
    `Saved ${total} task result(s) to distributed-results/${exec.id.slice(0, 8)}….`,
    'Open Folder',
  );
  if (action) {
    await vscode.commands.executeCommand('revealInExplorer', dir);
  }
}

async function resultsFolder(): Promise<vscode.Uri | undefined> {
  const ws = vscode.workspace.workspaceFolders?.[0]?.uri;
  if (ws) {
    return ws;
  }
  const picked = await vscode.window.showOpenDialog({
    canSelectFolders: true,
    canSelectFiles: false,
    openLabel: 'Save results here',
  });
  return picked?.[0];
}

// --- run in parallel ---------------------------------------------------------

/**
 * The editor holding the code to run: the active one if it is a file (or an
 * unsaved editor), else a visible one that is. Never an output view.
 */
export function runnableEditor(): vscode.TextEditor | undefined {
  const active = vscode.window.activeTextEditor;
  if (active && isRunnableScheme(active.document.uri.scheme)) {
    return active;
  }
  return vscode.window.visibleTextEditors.find((e) => isRunnableScheme(e.document.uri.scheme));
}

/**
 * Run the active file as many tasks at once: one per input line, list item or
 * copy, spread over the nodes you pick (all online ones by default).
 */
export async function runInParallel(deps: CommandDeps, logs: ExecutionLogProvider): Promise<void> {
  const gw = gateway(deps);
  if (!gw) {
    return;
  }
  const editor = runnableEditor();
  const code = normalizeLineEndings(editor?.document.getText() ?? '');
  if (!editor || code.trim() === '') {
    void vscode.window.showWarningMessage('Open the file to run first.');
    return;
  }
  let runtimes: RuntimeInfo[];
  let nodes;
  try {
    [runtimes, nodes] = await Promise.all([gw.listRuntimes(), gw.listNodes()]);
  } catch (err) {
    fail(deps, err, 'Could not load runtimes and nodes.');
    return;
  }
  const usable = runtimes.filter((r) => r.availableNodeIds.length > 0);
  if (usable.length === 0) {
    void vscode.window.showWarningMessage('No language is available on your online nodes. Add a node first.');
    return;
  }
  const guess = guessRuntime(editor.document, usable);
  const runtimePick = await vscode.window.showQuickPick(
    usable
      .map((r) => ({ label: r.name, description: r.id, detail: `on ${r.availableNodeIds.length} node(s)`, runtime: r }))
      .sort((a, b) => (a.runtime.id === guess ? -1 : b.runtime.id === guess ? 1 : 0)),
    { title: 'Run in Parallel (1/4): language', ignoreFocusOut: true },
  );
  if (!runtimePick) {
    return;
  }
  const runtime = runtimePick.runtime;
  let customCommand: string | undefined;
  let customFilename: string | undefined;
  if (runtime.id === 'custom') {
    customFilename = editor.document.fileName.split(/[\\/]/).pop() || 'main.txt';
    customCommand = await vscode.window.showInputBox({
      title: 'Command that runs the file ({file} is replaced by its name)',
      placeHolder: 'zig run {file}',
      ignoreFocusOut: true,
      validateInput: (v) => (v.includes('{file}') ? undefined : 'The command must contain {file}'),
    });
    if (!customCommand) {
      return;
    }
  }

  const inputs = await pickInputs();
  if (!inputs) {
    return;
  }

  const available = nodes.items.filter((n) => runtime.availableNodeIds.includes(n.id));
  let nodeIds: string[] = [];
  if (available.length > 1) {
    const chosen = await vscode.window.showQuickPick(
      available.map((n) => ({ label: n.name || n.id, description: `${n.cpuAllocatable ?? '?'} cores`, picked: true, id: n.id })),
      { title: 'Run in Parallel (3/4): nodes', canPickMany: true, ignoreFocusOut: true },
    );
    if (!chosen || chosen.length === 0) {
      return;
    }
    nodeIds = chosen.length === available.length ? [] : chosen.map((c) => c.id);
  }

  const cpuRaw = await vscode.window.showInputBox({
    title: 'Run in Parallel (4/4): cores per task',
    value: '1',
    prompt: 'Tasks run side by side until every chosen node’s lent cores are busy.',
    ignoreFocusOut: true,
    validateInput: (v) => (/^\d+$/.test(v) && Number(v) >= 1 ? undefined : 'Enter a whole number >= 1'),
  });
  if (cpuRaw === undefined) {
    return;
  }

  const settings = readSettings();
  try {
    const exec = await gw.createExecution(
      {
        runtime: runtime.id,
        code,
        inputs,
        nodeIds,
        customCommand,
        customFilename,
        cpuPerTask: Number(cpuRaw),
        timeoutSeconds: settings.defaultTimeoutSeconds,
        executionPolicy: settings.executionPolicy,
      },
      `dc-parallel-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
    );
    deps.refreshAll();
    await viewLogs(deps, logs, { execution: exec });
  } catch (err) {
    fail(deps, err, 'Could not start the run.');
  }
}

export async function pickInputs(title = 'Run in Parallel (2/4): tasks'): Promise<string[] | undefined> {
  const how = await vscode.window.showQuickPick(
    [
      { label: '$(list-unordered) Type the inputs', description: 'Comma-separated, one task each', id: 'list' },
      { label: '$(file) One task per line of a file', description: 'Each line becomes a task’s stdin and input.txt', id: 'file' },
      { label: '$(copy) Same program N times', description: 'Each task gets its number (1…N) as input', id: 'copies' },
      { label: '$(play) Just once', description: 'A single task', id: 'once' },
    ],
    { title, ignoreFocusOut: true },
  );
  switch (how?.id) {
    case 'list': {
      const raw = await vscode.window.showInputBox({
        title: 'Inputs, separated by commas',
        placeHolder: '10, 20, 30, 40',
        ignoreFocusOut: true,
        validateInput: (v) => (parseInputList(v).length ? undefined : 'Enter at least one input'),
      });
      return raw === undefined ? undefined : parseInputList(raw);
    }
    case 'file': {
      const picked = await vscode.window.showOpenDialog({ canSelectMany: false, openLabel: 'Use these lines' });
      if (!picked?.[0]) {
        return undefined;
      }
      const lines = parseInputLines(Buffer.from(await vscode.workspace.fs.readFile(picked[0])).toString('utf8'));
      if (lines.length === 0) {
        void vscode.window.showWarningMessage('That file has no non-empty lines.');
        return undefined;
      }
      return lines;
    }
    case 'copies': {
      const raw = await vscode.window.showInputBox({
        title: 'How many tasks?',
        value: '4',
        ignoreFocusOut: true,
        validateInput: (v) => (/^\d+$/.test(v) && Number(v) >= 1 && Number(v) <= 1000 ? undefined : 'Enter 1 – 1000'),
      });
      return raw === undefined ? undefined : Array.from({ length: Number(raw) }, (_, i) => String(i + 1));
    }
    case 'once':
      return [];
    default:
      return undefined;
  }
}

const EXTENSION_RUNTIME: Record<string, string> = {
  py: 'python', sh: 'bash', js: 'node', mjs: 'node', go: 'go', rs: 'rust', java: 'java',
  c: 'c', cpp: 'cpp', cc: 'cpp', rb: 'ruby', r: 'r', jl: 'julia', php: 'php', pl: 'perl', lua: 'lua',
};

function guessRuntime(doc: vscode.TextDocument, usable: RuntimeInfo[]): string | undefined {
  const ext = doc.fileName.split('.').pop()?.toLowerCase() ?? '';
  const byFile = usable.find((r) => r.filename.split('.').pop()?.toLowerCase() === ext);
  return byFile?.id ?? EXTENSION_RUNTIME[ext];
}

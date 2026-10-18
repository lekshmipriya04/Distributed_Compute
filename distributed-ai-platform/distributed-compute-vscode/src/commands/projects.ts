// Projects from the editor: run a whole folder on the workers (its modules,
// configs and requirements), and download what each task wrote to its
// output folders.

import { execFile } from 'node:child_process';
import * as vscode from 'vscode';
import { readSettings } from '../configuration/settings';
import type { GatewayApi } from '../api/gateway';
import { ApiError } from '../models/http';
import { isTerminal, type Execution, type ProjectSpec } from '../models/execution';
import type { ComputeNode } from '../models/node';
import {
  DEFAULT_EXCLUDES,
  MAX_PROJECT_BYTES,
  MAX_PROJECT_FILES,
  createTarGz,
  formatBytes,
  guessCommand,
  isExcluded,
  readTarGz,
  sha256Hex,
  validateProjectBlock,
  type ProjectBlock,
  type TarEntry,
} from '../models/project';
import { pickInputs, viewLogs, type ExecutionLogProvider } from './executions';
import type { CommandDeps } from './register';

function gateway(deps: CommandDeps): GatewayApi | undefined {
  const gw = deps.connection.gateway();
  if (!gw) {
    void vscode.window.showWarningMessage('Not connected. Run "Distributed Compute: Set Up Platform" or "Connect to Master" first.');
  }
  return gw;
}

function fail(deps: CommandDeps, err: unknown, fallback: string): void {
  const message = err instanceof ApiError ? err.message : err instanceof Error ? `${fallback} ${err.message}` : fallback;
  deps.logger.error('project command failed', { message });
  void vscode.window.showErrorMessage(message);
}

/** The folder to run: the one right-clicked in the Explorer, else the workspace. */
async function pickFolder(arg: unknown): Promise<vscode.Uri | undefined> {
  if (arg instanceof vscode.Uri) {
    const stat = await vscode.workspace.fs.stat(arg);
    return stat.type & vscode.FileType.Directory ? arg : vscode.Uri.joinPath(arg, '..');
  }
  const folders = vscode.workspace.workspaceFolders ?? [];
  if (folders.length === 1) {
    return folders[0].uri;
  }
  if (folders.length > 1) {
    const pick = await vscode.window.showWorkspaceFolderPick({ placeHolder: 'Run which folder on the workers?' });
    return pick?.uri;
  }
  const picked = await vscode.window.showOpenDialog({ canSelectFolders: true, canSelectFiles: false, openLabel: 'Run this folder' });
  return picked?.[0];
}

async function readBlock(folder: vscode.Uri): Promise<ProjectBlock | undefined> {
  try {
    const raw = Buffer.from(await vscode.workspace.fs.readFile(vscode.Uri.joinPath(folder, 'distributed.json'))).toString('utf8');
    const parsed = JSON.parse(raw) as { project?: unknown };
    if (parsed.project === undefined) {
      return undefined;
    }
    const errors = validateProjectBlock(parsed.project);
    if (errors.length) {
      throw new Error(`distributed.json: ${errors.join(' ')}`);
    }
    return parsed.project as ProjectBlock;
  } catch (err) {
    if (err instanceof vscode.FileSystemError || (err as { code?: string }).code === 'FileNotFound') {
      return undefined;
    }
    throw err;
  }
}

/** git's view of the folder (tracked + untracked, .gitignore applied), if it is a repository. */
function gitFiles(folder: string): Promise<string[] | undefined> {
  return new Promise((resolve) => {
    execFile(
      'git',
      ['-c', 'core.fsmonitor=false', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
      { cwd: folder, maxBuffer: 64 * 1024 * 1024, timeout: 20000 },
      (err, stdout) => resolve(err ? undefined : stdout.split('\0').filter(Boolean)),
    );
  });
}

async function walk(root: vscode.Uri, excludes: readonly string[]): Promise<string[]> {
  const out: string[] = [];
  const queue: string[] = [''];
  while (queue.length) {
    const rel = queue.shift() as string;
    const entries = await vscode.workspace.fs.readDirectory(rel ? vscode.Uri.joinPath(root, rel) : root);
    for (const [name, type] of entries) {
      const path = rel ? `${rel}/${name}` : name;
      if (type & vscode.FileType.SymbolicLink) {
        continue;
      }
      if (type & vscode.FileType.Directory) {
        if (!isExcluded(`${path}/x`, excludes)) {
          queue.push(path);
        }
      } else if (!isExcluded(path, excludes)) {
        out.push(path);
        if (out.length > MAX_PROJECT_FILES) {
          throw new Error(`The folder has more than ${MAX_PROJECT_FILES} files; add patterns to "project.exclude".`);
        }
      }
    }
  }
  return out;
}

/** The project's files, packed: git's file list when possible, else a walk. */
async function pack(folder: vscode.Uri, excludes: readonly string[]): Promise<{ archive: Uint8Array; files: string[]; bytes: number }> {
  // In an untrusted workspace, git (which reads the folder's own config) is not run.
  const fromGit = vscode.workspace.isTrusted && folder.scheme === 'file' ? await gitFiles(folder.fsPath) : undefined;
  const files = (fromGit ?? (await walk(folder, excludes))).filter((f) => !isExcluded(f, excludes)).sort();
  if (files.length === 0) {
    throw new Error('There are no files to send in this folder.');
  }
  const entries: TarEntry[] = [];
  let bytes = 0;
  for (const path of files) {
    const uri = vscode.Uri.joinPath(folder, path);
    let data: Uint8Array;
    try {
      data = await vscode.workspace.fs.readFile(uri);
    } catch {
      continue; // deleted since it was listed
    }
    bytes += data.length;
    if (bytes > MAX_PROJECT_BYTES) {
      throw new Error(
        `The folder holds more than ${formatBytes(MAX_PROJECT_BYTES)}. Upload large data as a dataset and add ` +
          'patterns to "project.exclude" in distributed.json.',
      );
    }
    const executable = /\.(sh|bash)$/.test(path) || (await isExecutable(uri));
    entries.push({ path, data, mode: executable ? 0o755 : 0o644 });
  }
  return { archive: createTarGz(entries), files: entries.map((e) => e.path), bytes };
}

async function isExecutable(uri: vscode.Uri): Promise<boolean> {
  if (uri.scheme !== 'file' || process.platform === 'win32') {
    return false;
  }
  const { stat } = await import('node:fs/promises');
  try {
    return ((await stat(uri.fsPath)).mode & 0o111) !== 0;
  } catch {
    return false;
  }
}

function projectNodes(nodes: ComputeNode[], pythonNodeIds: Set<string>): ComputeNode[] {
  return nodes.filter((n) => pythonNodeIds.has(n.id) && n.osType !== 'windows');
}

/**
 * Run a folder on the workers: pack it, upload it once, and run the command
 * from distributed.json ("project" block) or the one typed here, one task per
 * input, with its requirements installed on each worker.
 */
export async function runProject(deps: CommandDeps, logs: ExecutionLogProvider, arg?: unknown): Promise<void> {
  const gw = gateway(deps);
  if (!gw) {
    return;
  }
  const folder = await pickFolder(arg);
  if (!folder) {
    return;
  }
  let block: ProjectBlock | undefined;
  try {
    block = await readBlock(folder);
  } catch (err) {
    fail(deps, err, 'Could not read distributed.json.');
    return;
  }

  let nodes: ComputeNode[];
  let pythonIds: Set<string>;
  try {
    const [runtimes, list] = await Promise.all([gw.listRuntimes(), gw.listNodes()]);
    pythonIds = new Set(runtimes.find((r) => r.id === 'python')?.availableNodeIds ?? []);
    nodes = projectNodes(list.items, pythonIds);
  } catch (err) {
    fail(deps, err, 'Could not load your nodes.');
    return;
  }
  if (nodes.length === 0) {
    void vscode.window.showWarningMessage('Projects need an online Linux or macOS node with python3. Add a node first.');
    return;
  }

  const excludes = [...DEFAULT_EXCLUDES, ...(block?.exclude ?? [])];
  let packed: Awaited<ReturnType<typeof pack>>;
  try {
    packed = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: 'Packing the project…' },
      () => pack(folder, excludes),
    );
  } catch (err) {
    fail(deps, err, 'Could not pack the folder.');
    return;
  }

  let command = block?.command;
  if (!command) {
    command = await vscode.window.showInputBox({
      title: 'Run Project (1/3): command to run in the folder',
      value: guessCommand(packed.files),
      prompt: `${packed.files.length} files, ${formatBytes(packed.bytes)}. Save it as "project.command" in distributed.json to skip this step.`,
      ignoreFocusOut: true,
      validateInput: (v) => (v.trim() ? undefined : 'Enter a command, for example python3 train.py'),
    });
    if (!command) {
      return;
    }
  }
  const requirements =
    block?.requirements ?? (packed.files.includes('requirements.txt') ? 'requirements.txt' : undefined);

  const inputs = await pickInputs('Run Project (2/3): tasks');
  if (!inputs) {
    return;
  }
  let nodeIds: string[] = [];
  if (nodes.length > 1) {
    const chosen = await vscode.window.showQuickPick(
      nodes.map((n) => ({
        label: n.name || n.id,
        description: `${n.cpuAllocatable ?? '?'} cores${n.envCacheGb ? ` · ${n.envCacheGb} GB environment cache` : ''}`,
        picked: true,
        id: n.id,
      })),
      { title: 'Run Project (3/3): nodes', canPickMany: true, ignoreFocusOut: true },
    );
    if (!chosen || chosen.length === 0) {
      return;
    }
    nodeIds = chosen.length === nodes.length ? [] : chosen.map((c) => c.id);
  }

  const project: ProjectSpec = {
    bundleId: sha256Hex(packed.archive),
    command,
    requirements,
    outputs: block?.outputs,
    datasets: block?.datasets?.map((d) => ({ datasetId: d.dataset, path: d.path })),
  };
  const settings = readSettings();
  try {
    const exec = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: 'Sending the project…' },
      async (progress) => {
        if (await gw.hasBundle(project.bundleId)) {
          progress.report({ message: 'unchanged since the last run, not uploaded again' });
        } else {
          progress.report({ message: formatBytes(packed.archive.length) });
          await gw.uploadBundle(packed.archive);
        }
        return gw.createExecution(
          {
            runtime: 'project',
            project,
            inputs,
            nodeIds,
            timeoutSeconds: settings.defaultTimeoutSeconds,
            executionPolicy: settings.executionPolicy,
          },
          `dc-project-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
        );
      },
    );
    deps.logger.info('project started', { id: exec.id, files: packed.files.length, bytes: packed.bytes, requirements });
    deps.refreshAll();
    await viewLogs(deps, logs, { execution: exec });
  } catch (err) {
    fail(deps, err, 'Could not start the project.');
  }
}

/** Unpack every task's outputs into distributed-results/<id>/task-N/. */
export async function downloadOutputs(deps: CommandDeps, arg: unknown): Promise<void> {
  const gw = gateway(deps);
  if (!gw) {
    return;
  }
  let exec: Execution | undefined =
    arg && typeof arg === 'object' && 'execution' in arg ? (arg as { execution?: Execution }).execution : undefined;
  try {
    if (!exec) {
      const { items } = await gw.listExecutions();
      const done = items.filter((e) => e.project && isTerminal(e.status)).sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1));
      if (done.length === 0) {
        void vscode.window.showInformationMessage('No finished project runs yet.');
        return;
      }
      const pick = await vscode.window.showQuickPick(
        done.map((e) => ({
          label: e.project?.command ?? e.runtime,
          description: `${e.status} · ${e.total} task(s)`,
          detail: `${e.id} · ${new Date(e.createdAt).toLocaleString()}`,
          execution: e,
        })),
        { placeHolder: 'Download the outputs of which project run?' },
      );
      exec = pick?.execution;
    }
    if (!exec) {
      return;
    }
    const data = await gw.executionTasks(exec.id);
    const withFiles = data.tasks.filter((t) => (t.output_files ?? 0) > 0);
    if (withFiles.length === 0) {
      void vscode.window.showInformationMessage(
        'This run brought back no files. Tasks bring back what they write to the "outputs" folder (or $DC_OUTPUT_DIR).',
      );
      return;
    }
    const base = vscode.workspace.workspaceFolders?.[0]?.uri ??
      (await vscode.window.showOpenDialog({ canSelectFolders: true, canSelectFiles: false, openLabel: 'Save outputs here' }))?.[0];
    if (!base) {
      return;
    }
    const dir = vscode.Uri.joinPath(base, 'distributed-results', exec.id);
    let files = 0;
    let firstFile: vscode.Uri | undefined;
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: 'Downloading outputs…' },
      async (progress) => {
        for (const t of withFiles) {
          progress.report({ message: `task ${t.seq + 1}` });
          const entries = readTarGz(await gw.downloadTaskOutputs(exec!.id, t.id));
          const taskDir = vscode.Uri.joinPath(dir, `task-${String(t.seq + 1).padStart(3, '0')}`);
          for (const entry of entries) {
            const target = vscode.Uri.joinPath(taskDir, ...entry.path.split('/'));
            await vscode.workspace.fs.writeFile(target, entry.data);
            firstFile ??= target;
            files++;
          }
        }
      },
    );
    deps.logger.info('outputs downloaded', { id: exec.id, files, dir: dir.fsPath });
    const action = await vscode.window.showInformationMessage(
      `Saved ${files} output file(s) from ${withFiles.length} task(s) to distributed-results/${exec.id.slice(0, 8)}….`,
      'Open Folder',
    );
    if (action) {
      // Revealing a file opens the folders down to it.
      await vscode.commands.executeCommand('revealInExplorer', firstFile ?? dir);
    }
  } catch (err) {
    fail(deps, err, 'Could not download the outputs.');
  }
}

/** Lend (or stop lending) disk for cached project environments on a node. */
export async function setEnvironmentCache(deps: CommandDeps, nodeId: string | undefined): Promise<void> {
  const gw = gateway(deps);
  if (!gw || !nodeId) {
    return;
  }
  let node: ComputeNode;
  try {
    node = await gw.getNode(nodeId);
  } catch (err) {
    fail(deps, err, 'Could not load the node.');
    return;
  }
  const choice = await vscode.window.showQuickPick(
    [
      { label: 'Off', description: 'Build each environment in the task sandbox; nothing stays on the node', gb: 0 },
      { label: '10 GB', description: 'Enough for a few small environments', gb: 10 },
      { label: '30 GB', description: 'PyTorch-sized environments', gb: 30 },
      { label: 'Custom…', gb: -1 },
    ],
    {
      title: `Environment cache on ${node.name || node.id} (now ${node.envCacheGb ? `${node.envCacheGb} GB` : 'off'})`,
      placeHolder: 'Kept in ~/.cache/distributed-compute on the node, and deleted when you remove the node',
      ignoreFocusOut: true,
    },
  );
  if (!choice) {
    return;
  }
  let gb = choice.gb;
  if (gb < 0) {
    const raw = await vscode.window.showInputBox({
      title: 'Disk to lend for environments (GB)',
      value: String(node.envCacheGb ?? 10),
      ignoreFocusOut: true,
      validateInput: (v) => (Number.isFinite(Number(v)) && Number(v) >= 0 && Number(v) <= 10000 ? undefined : 'Enter 0 – 10000'),
    });
    if (raw === undefined) {
      return;
    }
    gb = Number(raw);
  }
  try {
    await gw.updateNodeSlice(nodeId, { envCacheGb: gb });
    deps.refreshAll();
    void vscode.window.showInformationMessage(
      gb > 0 ? `${node.name || node.id} keeps up to ${gb} GB of project environments.` : `Environment cache off on ${node.name || node.id}.`,
    );
  } catch (err) {
    fail(deps, err, 'Could not change the environment cache.');
  }
}

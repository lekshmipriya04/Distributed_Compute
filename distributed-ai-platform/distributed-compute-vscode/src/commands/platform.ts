// Run the platform on this machine with Docker, so the user never has to open
// a terminal: check Docker (start Docker Desktop if it is stopped), find or
// download the platform, create its config, start the containers, wait for the
// gateway, and connect.

import { spawn } from 'node:child_process';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { DEFAULT_MASTER_URL, readSettings, updateMasterUrl, updatePlatformPath } from '../configuration/settings';
import { joinUrl, validateMasterUrl } from '../api/urls';
import { isPlatformCompose } from '../models/training';
import {
  DOCKER_DOWNLOAD_URL,
  PLATFORM_RELEASE,
  PLATFORM_SUBDIR,
  archiveUrl,
  platformTag,
  composeFailureHint,
  composeInvocation,
  dockerLaunchCommand,
  dockerStartHint,
  dockerStateFrom,
  startupAction,
  withDockerPath,
  type DockerState,
} from '../models/platform';
import type { Logger } from '../utilities/logger';
import type { CommandDeps } from './register';

const SET_UP_KEY = 'distributedCompute.platformSetUp';
const DISMISSED_KEY = 'distributedCompute.setupDismissed';

export interface RunResult {
  exitCode: number;
  spawnFailed: boolean;
  /** The tail of the command's combined output (stdout and stderr). */
  output: string;
}

/** Run a command, forwarding its output to the Distributed Compute log. */
export function runProcess(
  logger: Logger,
  cmd: string,
  args: string[],
  cwd: string | undefined,
  token?: vscode.CancellationToken,
  quiet = false,
): Promise<RunResult> {
  return new Promise((resolve) => {
    if (!quiet) {
      logger.info(`$ ${cmd} ${args.join(' ')}${cwd ? `  (in ${cwd})` : ''}`);
    }
    let child;
    try {
      child = spawn(cmd, args, { cwd, env: withDockerPath(process.platform, process.env) });
    } catch (err) {
      logger.error(`could not start ${cmd}`, { message: (err as Error).message });
      resolve({ exitCode: -1, spawnFailed: true, output: '' });
      return;
    }
    let output = '';
    const forward = (chunk: Buffer) => {
      output = (output + chunk.toString('utf8')).slice(-16384);
      if (quiet) {
        return;
      }
      for (const line of chunk.toString('utf8').split(/\r?\n/)) {
        if (line.trim() !== '') {
          logger.info(`[${cmd}] ${line}`);
        }
      }
    };
    child.stdout?.on('data', forward);
    child.stderr?.on('data', forward);
    const cancel = token?.onCancellationRequested(() => child.kill());
    let spawnFailed = false;
    child.on('error', (err) => {
      spawnFailed = true;
      if (!quiet) {
        logger.error(`${cmd} is not available on this machine`, { message: err.message });
      }
    });
    child.on('close', (code) => {
      cancel?.dispose();
      if (!quiet) {
        logger.info(`${cmd} exited with code ${code}`);
      }
      resolve({ exitCode: code ?? -1, spawnFailed, output });
    });
  });
}

let composeV2: boolean | undefined;

/**
 * Run docker. "compose ..." uses Compose v2 when present, else the older
 * standalone docker-compose (still the default on some Linux distributions).
 */
export async function runDocker(
  deps: CommandDeps,
  cwd: string,
  args: string[],
  token?: vscode.CancellationToken,
): Promise<RunResult> {
  if (args[0] !== 'compose') {
    return runProcess(deps.logger, 'docker', args, cwd, token);
  }
  if (composeV2 === undefined) {
    const probe = await runProcess(deps.logger, 'docker', ['compose', 'version'], undefined, undefined, true);
    composeV2 = probe.exitCode === 0;
  }
  const run = composeInvocation(composeV2, args.slice(1));
  return runProcess(deps.logger, run.cmd, run.args, cwd, token);
}

/**
 * After a failed `compose up`: write each container's state and recent log to
 * the output, and show the likely cause with its fix.
 */
export async function reportComposeFailure(deps: CommandDeps, folder: string, output: string, what: string): Promise<void> {
  deps.logger.error(`${what}. Container states and recent logs follow.`);
  await runDocker(deps, folder, ['compose', 'ps', '-a']);
  await runDocker(deps, folder, ['compose', 'logs', '--no-color', '--tail', '40']);
  const hint = composeFailureHint(output);
  void vscode.window.showErrorMessage(
    hint ? `${what}. ${hint}` : `${what}. See the Distributed Compute output for the Docker messages.`,
  );
}

/** Start a program and leave it running (Docker Desktop on Windows). */
function launchDetached(logger: Logger, cmd: string, args: string[]): void {
  logger.info(`$ ${cmd} ${args.join(' ')}  (detached)`);
  try {
    const child = spawn(cmd, args, { detached: true, stdio: 'ignore' });
    child.on('error', (err) => logger.warn(`could not start ${cmd}`, { message: err.message }));
    child.unref();
  } catch (err) {
    logger.warn(`could not start ${cmd}`, { message: (err as Error).message });
  }
}

async function dockerState(logger: Logger): Promise<DockerState> {
  return dockerStateFrom(await runProcess(logger, 'docker', ['info'], undefined, undefined, true));
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Make sure the Docker daemon runs, starting Docker Desktop when it is stopped. */
async function ensureDocker(
  deps: CommandDeps,
  progress: vscode.Progress<{ message?: string }>,
  token: vscode.CancellationToken,
): Promise<boolean> {
  let state = await dockerState(deps.logger);
  if (state === 'missing') {
    const choice = await vscode.window.showErrorMessage(
      'Docker is needed to run the platform on this computer.',
      { modal: true, detail: 'Install Docker Desktop (free), open it once, then run "Set Up Platform" again.' },
      'Get Docker Desktop',
    );
    if (choice) {
      void vscode.env.openExternal(vscode.Uri.parse(DOCKER_DOWNLOAD_URL));
    }
    return false;
  }
  if (state === 'running') {
    return true;
  }
  if (state === 'no-permission') {
    void vscode.window.showErrorMessage(dockerStartHint(process.platform, state));
    return false;
  }
  progress.report({ message: 'Starting Docker…' });
  const launch = dockerLaunchCommand(process.platform, process.env);
  if (launch?.detached) {
    launchDetached(deps.logger, launch.cmd, launch.args);
  } else if (launch) {
    await runProcess(deps.logger, launch.cmd, launch.args, undefined, token);
  }
  const deadline = Date.now() + 3 * 60 * 1000;
  while (Date.now() < deadline && !token.isCancellationRequested) {
    await sleep(3000);
    state = await dockerState(deps.logger);
    if (state === 'running') {
      return true;
    }
    if (state === 'no-permission') {
      break;
    }
  }
  if (!token.isCancellationRequested) {
    void vscode.window.showErrorMessage(`Docker did not start. ${dockerStartHint(process.platform, state)}`);
  }
  return false;
}

async function isPlatformFolder(folder: string): Promise<boolean> {
  try {
    const bytes = await vscode.workspace.fs.readFile(vscode.Uri.file(path.join(folder, 'docker-compose.yml')));
    return isPlatformCompose(Buffer.from(bytes).toString('utf8'));
  } catch {
    return false;
  }
}

function downloadDir(deps: CommandDeps): string {
  return path.join(deps.context.globalStorageUri.fsPath, 'platform');
}

/** The platform folder from settings, the workspace, or an earlier download; no prompts. */
export async function knownPlatformFolder(deps?: CommandDeps): Promise<string | undefined> {
  const configured = readSettings().platformPath;
  if (configured && (await isPlatformFolder(configured))) {
    return configured;
  }
  const candidates: string[] = [];
  // A docker-compose.yml in an untrusted workspace is never run: it could
  // build and start anything.
  if (vscode.workspace.isTrusted) {
    for (const folder of vscode.workspace.workspaceFolders ?? []) {
      candidates.push(folder.uri.fsPath, path.join(folder.uri.fsPath, PLATFORM_SUBDIR));
    }
  }
  if (deps) {
    candidates.push(path.join(downloadDir(deps), PLATFORM_SUBDIR));
  }
  for (const candidate of candidates) {
    if (await isPlatformFolder(candidate)) {
      await updatePlatformPath(candidate);
      return candidate;
    }
  }
  return undefined;
}

/** The platform folder; asks the user to pick it when it cannot be found. */
export async function platformFolder(deps?: CommandDeps): Promise<string | undefined> {
  const known = await knownPlatformFolder(deps);
  if (known) {
    return known;
  }
  return pickPlatformFolder();
}

async function pickPlatformFolder(): Promise<string | undefined> {
  const picked = await vscode.window.showOpenDialog({
    canSelectFolders: true,
    canSelectFiles: false,
    openLabel: 'Use this folder',
    title: "Select the platform folder (the one with the platform's docker-compose.yml)",
  });
  const chosen = picked?.[0]?.fsPath;
  if (!chosen) {
    return undefined;
  }
  for (const candidate of [chosen, path.join(chosen, PLATFORM_SUBDIR)]) {
    if (await isPlatformFolder(candidate)) {
      await updatePlatformPath(candidate);
      return candidate;
    }
  }
  void vscode.window.showErrorMessage(
    "That folder has no docker-compose.yml for this platform. Pick the 'distributed-ai-platform' folder.",
  );
  return undefined;
}

function releaseMarker(deps: CommandDeps): vscode.Uri {
  return vscode.Uri.file(path.join(downloadDir(deps), 'release.txt'));
}

async function readText(uri: vscode.Uri): Promise<string | undefined> {
  try {
    return Buffer.from(await vscode.workspace.fs.readFile(uri)).toString('utf8');
  } catch {
    return undefined;
  }
}

/** A copy this extension downloaded for an older release (replaced on upgrade). */
async function isOutdatedDownload(deps: CommandDeps, folder: string): Promise<boolean> {
  if (path.resolve(folder) !== path.resolve(downloadDir(deps), PLATFORM_SUBDIR)) {
    return false;
  }
  return (await readText(releaseMarker(deps)))?.trim() !== PLATFORM_RELEASE;
}

/** Find the platform, or download it into the extension's storage. */
async function obtainPlatformFolder(
  deps: CommandDeps,
  progress: vscode.Progress<{ message?: string }>,
  token: vscode.CancellationToken,
): Promise<string | undefined> {
  const known = await knownPlatformFolder(deps);
  if (known && !(await isOutdatedDownload(deps, known))) {
    return known;
  }
  if (known) {
    // Same folder, so the same Compose project: the new release takes over the
    // existing containers' data volumes.
    deps.logger.info(`updating the downloaded platform to ${PLATFORM_RELEASE}`);
  } else {
    const choice = await vscode.window.showQuickPick(
      [
        { label: '$(cloud-download) Download it for me', description: 'Recommended', id: 'download' },
        { label: '$(folder-opened) I already have it', description: 'Pick the folder', id: 'pick' },
      ],
      { title: 'Where is the platform?', placeHolder: 'The platform code is needed once to build its containers', ignoreFocusOut: true },
    );
    if (!choice) {
      return undefined;
    }
    if (choice.id === 'pick') {
      return pickPlatformFolder();
    }
  }
  progress.report({ message: 'Downloading the platform…' });
  const folder = path.join(downloadDir(deps), PLATFORM_SUBDIR);
  // Keep the settings (secrets, Spark + Ray choice) of an earlier download.
  const kept = new Map<string, string>();
  for (const name of [path.join('config', '.env'), '.env']) {
    const text = await readText(vscode.Uri.file(path.join(folder, name)));
    if (text !== undefined) {
      kept.set(name, text);
    }
  }
  if (!(await downloadPlatform(deps, token)) || !(await isPlatformFolder(folder))) {
    if (!token.isCancellationRequested) {
      void vscode.window.showErrorMessage(
        'Could not download the platform. Check the internet connection, or download the repository yourself and choose "I already have it". Details are in the Distributed Compute output.',
      );
    }
    return undefined;
  }
  for (const [name, text] of kept) {
    await vscode.workspace.fs.writeFile(vscode.Uri.file(path.join(folder, name)), Buffer.from(text, 'utf8'));
  }
  await vscode.workspace.fs.writeFile(releaseMarker(deps), Buffer.from(PLATFORM_RELEASE, 'utf8'));
  await updatePlatformPath(folder);
  return folder;
}

/**
 * Download the release's source archive (the default branch when that tag does
 * not exist yet) and unpack it into the download folder with tar, which ships
 * with macOS, Linux and Windows 10+.
 */
async function downloadPlatform(deps: CommandDeps, token: vscode.CancellationToken): Promise<boolean> {
  const target = downloadDir(deps);
  for (const release of [PLATFORM_RELEASE, undefined]) {
    const url = archiveUrl(release);
    deps.logger.info(`downloading ${url}`);
    const abort = new AbortController();
    const cancel = token.onCancellationRequested(() => abort.abort());
    const timer = setTimeout(() => abort.abort(), 10 * 60 * 1000);
    let bytes: Uint8Array;
    try {
      const res = await fetch(url, { signal: abort.signal });
      if (res.status === 404 && release) {
        deps.logger.warn(`no platform tag ${platformTag(release)}; downloading the default branch instead`);
        continue;
      }
      if (!res.ok) {
        deps.logger.error(`download failed: HTTP ${res.status}`);
        return false;
      }
      bytes = new Uint8Array(await res.arrayBuffer());
    } catch (err) {
      deps.logger.error('download failed', { message: (err as Error).message });
      return false;
    } finally {
      clearTimeout(timer);
      cancel.dispose();
    }
    await vscode.workspace.fs.delete(vscode.Uri.file(target), { recursive: true, useTrash: false }).then(
      () => undefined,
      () => undefined,
    );
    await vscode.workspace.fs.createDirectory(vscode.Uri.file(target));
    // Relative archive name with cwd = target: GNU tar (e.g. from Git for
    // Windows) would read "C:\..." as a remote host.
    const archive = 'platform.tar.gz';
    await vscode.workspace.fs.writeFile(vscode.Uri.file(path.join(target, archive)), bytes);
    const result = await runProcess(deps.logger, 'tar', ['-xzf', archive, '--strip-components=1'], target, token);
    await vscode.workspace.fs.delete(vscode.Uri.file(path.join(target, archive))).then(
      () => undefined,
      () => undefined,
    );
    if (result.spawnFailed) {
      deps.logger.error('tar is needed to unpack the platform and was not found on this machine');
    }
    return result.exitCode === 0;
  }
  return false;
}

/** config/.env holds the platform's settings; create it from the example once. */
async function ensureEnvFile(folder: string, logger: Logger): Promise<void> {
  const env = vscode.Uri.file(path.join(folder, 'config', '.env'));
  try {
    await vscode.workspace.fs.stat(env);
  } catch {
    await vscode.workspace.fs.copy(vscode.Uri.file(path.join(folder, 'config', '.env.example')), env);
    logger.info(`created ${env.fsPath} with local defaults`);
  }
}

export async function gatewayHealthy(masterUrl: string): Promise<boolean> {
  try {
    const res = await fetch(joinUrl(masterUrl, '/health'), { signal: AbortSignal.timeout(2000) });
    return res.ok;
  } catch {
    return false;
  }
}

async function waitForGateway(masterUrl: string, token: vscode.CancellationToken): Promise<boolean> {
  const deadline = Date.now() + 5 * 60 * 1000;
  while (Date.now() < deadline && !token.isCancellationRequested) {
    if (await gatewayHealthy(masterUrl)) {
      return true;
    }
    await sleep(3000);
  }
  return false;
}

/**
 * Everything needed to run the platform here, in one step. `firstTime`
 * explains that the first build takes a while.
 */
export async function setUpPlatform(deps: CommandDeps): Promise<boolean> {
  if (!validateMasterUrl(readSettings().masterUrl).isLocal) {
    await updateMasterUrl(DEFAULT_MASTER_URL, vscode.ConfigurationTarget.Global);
  }
  const masterUrl = readSettings().masterUrl;
  deps.logger.show();
  const firstTime = !deps.context.globalState.get<boolean>(SET_UP_KEY);

  const ok = await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: 'Distributed Compute',
      cancellable: true,
    },
    async (progress, token) => {
      progress.report({ message: 'Checking Docker…' });
      if (!(await ensureDocker(deps, progress, token))) {
        return false;
      }
      const folder = await obtainPlatformFolder(deps, progress, token);
      if (!folder || token.isCancellationRequested) {
        return false;
      }
      await ensureEnvFile(folder, deps.logger);
      progress.report({
        message: firstTime
          ? 'Building and starting the platform (the first time takes several minutes)…'
          : 'Starting the platform…',
      });
      const up = await runDocker(deps, folder, ['compose', 'up', '-d', '--build'], token);
      if (up.exitCode !== 0) {
        if (!token.isCancellationRequested) {
          await reportComposeFailure(deps, folder, up.output, 'The platform did not start');
        }
        return false;
      }
      progress.report({ message: 'Waiting for the platform to be ready…' });
      if (!(await waitForGateway(masterUrl, token))) {
        if (!token.isCancellationRequested) {
          void vscode.window.showErrorMessage(
            `The platform started but ${masterUrl} does not answer yet. Run "Connect to Master" in a minute.`,
          );
        }
        return false;
      }
      return true;
    },
  );
  if (!ok) {
    return false;
  }
  await deps.context.globalState.update(SET_UP_KEY, true);
  await deps.connection.connect();
  deps.refreshAll();
  if (firstTime) {
    const next = await vscode.window.showInformationMessage(
      'The platform is running and VS Code is connected. Next, add a worker machine.',
      'Add Compute Node',
    );
    if (next) {
      await vscode.commands.executeCommand('distributedCompute.addNode');
    }
  }
  return true;
}

export async function stopPlatform(deps: CommandDeps): Promise<void> {
  const folder = await knownPlatformFolder(deps);
  if (!folder) {
    void vscode.window.showWarningMessage('No platform has been set up on this computer.');
    return;
  }
  deps.connection.disconnect();
  deps.refreshAll();
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: 'Stopping the platform (your data is kept)…', cancellable: true },
    async (_progress, token) => {
      const { exitCode } = await runDocker(deps, folder, ['compose', 'stop'], token);
      if (exitCode === 0) {
        void vscode.window.showInformationMessage('The platform is stopped. "Set Up Platform" starts it again.');
      } else {
        void vscode.window.showErrorMessage('Could not stop the platform. See the Distributed Compute output.');
      }
    },
  );
}

/** On VS Code start: connect, start the platform, or offer to set it up. */
export async function runStartup(deps: CommandDeps): Promise<void> {
  const settings = readSettings();
  const masterIsLocal = validateMasterUrl(settings.masterUrl).isLocal === true;
  const action = startupAction({
    masterIsLocal,
    gatewayHealthy: masterIsLocal && (await gatewayHealthy(settings.masterUrl)),
    platformKnown:
      deps.context.globalState.get<boolean>(SET_UP_KEY) === true && (await knownPlatformFolder(deps)) !== undefined,
    autoStart: settings.autoStartPlatform,
    setupDismissed: deps.context.globalState.get<boolean>(DISMISSED_KEY) === true,
  });
  deps.logger.info(`startup: ${action}`);
  switch (action) {
    case 'connect':
      await deps.connection.connect();
      deps.refreshAll();
      return;
    case 'start':
      await setUpPlatform(deps);
      return;
    case 'offer-setup':
      // Not awaited: startup is done once the offer is shown; the answer may
      // come much later, or never.
      void offerSetup(deps);
      return;
    case 'none':
      if (settings.autoConnect) {
        await deps.connection.connect();
        deps.refreshAll();
      }
  }
}

async function offerSetup(deps: CommandDeps): Promise<void> {
  const choice = await vscode.window.showInformationMessage(
    'Run the Distributed Compute platform on this computer? One click: Docker builds and starts it, and VS Code connects.',
    'Set Up Now',
    'Use Another Master',
    "Don't Ask Again",
  );
  if (choice === 'Set Up Now') {
    await setUpPlatform(deps);
  } else if (choice === 'Use Another Master') {
    await vscode.commands.executeCommand('distributedCompute.configureMaster');
  } else if (choice === "Don't Ask Again") {
    await deps.context.globalState.update(DISMISSED_KEY, true);
  }
}

// The optional Spark + Ray engine on the master: detect it, and install it
// into the local master container only when the user asks.

import * as path from 'node:path';
import * as vscode from 'vscode';
import { readSettings } from '../configuration/settings';
import type { GatewayApi } from '../api/gateway';
import { engineDecision, masterIsOnThisMachine, upsertEnvLine, type EngineInfo } from '../models/training';
import { platformFolder, runDocker } from './platform';
import type { CommandDeps } from './register';

export async function sparkRayInfo(gw: GatewayApi, refresh = false): Promise<EngineInfo | undefined> {
  return (await gw.listEngines(refresh)).find((e) => e.id === 'spark_ray');
}

/**
 * Make sure Spark + Ray can run on the master. Returns true when it is ready,
 * installing it first if the user agrees.
 */
export async function ensureSparkRay(deps: CommandDeps, gw: GatewayApi): Promise<boolean> {
  const info = await sparkRayInfo(gw, true);
  const decision = engineDecision(info, masterIsOnThisMachine(readSettings().masterUrl));
  if (decision.action === 'ready') {
    return true;
  }
  if (decision.action === 'remote') {
    void vscode.window.showWarningMessage(
      `Spark + Ray is not installed on the connected master (missing: ${decision.missing.join(', ') || 'unknown'}). ` +
        'On that machine, run "make enable-spark-ray" in the platform folder, then try again.',
    );
    return false;
  }
  const choice = await vscode.window.showInformationMessage(
    'Install Spark + Ray on the master?',
    {
      modal: true,
      detail:
        'Downloads Java, PySpark and Ray into the master container (about 1 GB) and restarts it. ' +
        'Your worker machines are not changed.',
    },
    'Install',
  );
  if (choice !== 'Install') {
    return false;
  }
  return setSparkRay(deps, gw, true);
}

export async function enableSparkRay(deps: CommandDeps): Promise<void> {
  const gw = deps.connection.gateway();
  if (!gw) {
    void vscode.window.showWarningMessage('Not connected. Run "Distributed Compute: Connect to Master" first.');
    return;
  }
  try {
    if (await ensureSparkRay(deps, gw)) {
      const info = await sparkRayInfo(gw);
      void vscode.window.showInformationMessage(
        `Spark + Ray is ready on the master (Spark ${info?.versions.spark ?? '?'}, Ray ${info?.versions.ray ?? '?'}).`,
      );
    }
  } catch (err) {
    deps.logger.error('enable Spark + Ray failed', { message: (err as Error).message });
    void vscode.window.showErrorMessage(`Could not check the master's engines: ${(err as Error).message}`);
  }
}

export async function disableSparkRay(deps: CommandDeps): Promise<void> {
  const gw = deps.connection.gateway();
  if (!gw) {
    void vscode.window.showWarningMessage('Not connected. Run "Distributed Compute: Connect to Master" first.');
    return;
  }
  if (!masterIsOnThisMachine(readSettings().masterUrl)) {
    void vscode.window.showWarningMessage(
      'The connected master is not on this machine. Run "make disable-spark-ray" on that machine instead.',
    );
    return;
  }
  const choice = await vscode.window.showWarningMessage(
    'Remove Spark + Ray from the master? Runs then use pure Python.',
    { modal: true },
    'Remove',
  );
  if (choice === 'Remove') {
    await setSparkRay(deps, gw, false);
  }
}

/** Write WITH_SPARK_RAY to the platform's .env and rebuild the master. */
async function setSparkRay(deps: CommandDeps, gw: GatewayApi, enabled: boolean): Promise<boolean> {
  const folder = await platformFolder(deps);
  if (!folder) {
    return false;
  }
  const envUri = vscode.Uri.file(path.join(folder, '.env'));
  let current = '';
  try {
    current = Buffer.from(await vscode.workspace.fs.readFile(envUri)).toString('utf8');
  } catch {
    current = '';
  }
  await vscode.workspace.fs.writeFile(
    envUri,
    Buffer.from(upsertEnvLine(current, 'WITH_SPARK_RAY', enabled ? 'true' : 'false'), 'utf8'),
  );
  deps.logger.info(`WITH_SPARK_RAY=${enabled} written to ${envUri.fsPath}`);
  deps.logger.show();

  const ok = await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: enabled ? 'Installing Spark + Ray on the master' : 'Removing Spark + Ray from the master',
      cancellable: true,
    },
    async (progress, token) => {
      progress.report({ message: 'Rebuilding the master container (see the Distributed Compute output)…' });
      const { exitCode } = await runDocker(deps, folder, ['compose', 'up', '-d', '--build', 'ssh-executor'], token);
      if (exitCode !== 0) {
        return false;
      }
      progress.report({ message: 'Waiting for the master to come back…' });
      return waitForEngine(gw, enabled, token);
    },
  );
  if (!ok) {
    void vscode.window.showErrorMessage(
      'The master was not rebuilt. See the Distributed Compute output for the docker messages.',
    );
    return false;
  }
  deps.refreshAll();
  return true;
}

async function waitForEngine(gw: GatewayApi, enabled: boolean, token: vscode.CancellationToken): Promise<boolean> {
  const deadline = Date.now() + 3 * 60 * 1000;
  while (Date.now() < deadline && !token.isCancellationRequested) {
    try {
      const info = await sparkRayInfo(gw, true);
      if (info && info.available === enabled) {
        return true;
      }
    } catch {
      // The master is restarting.
    }
    await new Promise((r) => setTimeout(r, 3000));
  }
  return false;
}

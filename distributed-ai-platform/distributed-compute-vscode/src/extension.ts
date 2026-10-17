// Distributed Compute — VS Code extension entry point.
//
// Wires the connection manager, SecretStorage-backed token store, typed gateway
// client, the tree views (Compute Nodes, Jobs, Executions, Training Runs), the status
// bar, and all commands. On start it only probes a local master's /health; it
// starts Docker containers only for a platform the user set up here before.

import * as vscode from 'vscode';
import { ConnectionManager } from './connection/connectionManager';
import { TokenStore } from './security/secretStorage';
import { Logger } from './utilities/logger';
import { readSettings } from './configuration/settings';
import { NodeTreeProvider } from './providers/nodeTreeProvider';
import { ExecutionTreeProvider } from './providers/executionTreeProvider';
import { TrainingTreeProvider } from './providers/trainingTreeProvider';
import { registerCommands, type CommandDeps } from './commands/register';
import { clusterSummary } from './models/execution';
import { runStartup } from './commands/platform';

let output: vscode.OutputChannel;
let statusBar: vscode.StatusBarItem;
let pollTimer: ReturnType<typeof setInterval> | undefined;

/** Returned from activate(); read by the VS Code smoke test (e2e/vscode). */
export interface DistributedComputeApi {
  connectionState(): string;
  /** Resolves after the startup check (connect / start / offer setup) ran. */
  startup: Promise<void>;
}

export function activate(context: vscode.ExtensionContext): DistributedComputeApi {
  output = vscode.window.createOutputChannel('Distributed Compute');
  context.subscriptions.push(output);

  const logger = new Logger(output);
  logger.setLevel(readSettings().logLevel);

  const tokens = new TokenStore(context.secrets);
  const connection = new ConnectionManager(tokens, logger);
  context.subscriptions.push(connection);

  const nodeProvider = new NodeTreeProvider(connection, logger);
  const jobsProvider = new ExecutionTreeProvider(connection, logger, false);
  const execProvider = new ExecutionTreeProvider(connection, logger, true);
  const trainingProvider = new TrainingTreeProvider(connection, logger);
  context.subscriptions.push(nodeProvider, jobsProvider, execProvider, trainingProvider);

  context.subscriptions.push(
    vscode.window.createTreeView('distributedCompute.nodes', { treeDataProvider: nodeProvider }),
    vscode.window.createTreeView('distributedCompute.jobs', { treeDataProvider: jobsProvider }),
    vscode.window.createTreeView('distributedCompute.executions', { treeDataProvider: execProvider }),
    vscode.window.createTreeView('distributedCompute.training', { treeDataProvider: trainingProvider }),
  );

  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBar.command = 'distributedCompute.showConnectionStatus';
  context.subscriptions.push(statusBar);
  renderStatusBar(connection.snapshot().state, undefined);
  statusBar.show();

  // The status bar counts follow every refresh (polling, commands), not only
  // the moment of connecting.
  const updateStatusBar = async (): Promise<void> => {
    const state = connection.snapshot().state;
    if (state !== 'connected') {
      renderStatusBar(state, undefined);
      return;
    }
    try {
      const cs = await connection.gateway()?.clusterStatus();
      renderStatusBar('connected', cs ? clusterSummary(cs.nodesOnline, cs.nodesTotal, cs.jobsRunning) : undefined);
    } catch {
      renderStatusBar('connected', undefined);
    }
  };

  const refreshAll = (): void => {
    nodeProvider.refresh();
    jobsProvider.refresh();
    execProvider.refresh();
    trainingProvider.refresh();
    void updateStatusBar();
  };

  // Reflect connection changes in the status bar and refresh authoritative
  // state from REST after any (re)connect.
  context.subscriptions.push(
    connection.onDidChangeState((snap) => {
      void vscode.commands.executeCommand('setContext', 'distributedCompute.connected', snap.state === 'connected');
      refreshAll();
    }),
  );

  const deps: CommandDeps = { context, connection, tokens, logger, refreshAll };
  registerCommands(deps);

  // React to settings changes: log level, refresh interval.
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (!e.affectsConfiguration('distributedCompute')) {
        return;
      }
      logger.setLevel(readSettings().logLevel);
      restartPolling(connection, refreshAll, logger);
    }),
  );

  restartPolling(connection, refreshAll, logger);

  // Connect to a running local platform, start it if it was set up before,
  // or offer the one-click setup; remote masters follow autoConnect.
  const startup = runStartup(deps).catch((err) => logger.error('startup failed', { message: (err as Error).message }));

  logger.info('Distributed Compute extension activated.');
  return { connectionState: () => connection.snapshot().state, startup };
}

function renderStatusBar(state: string, detail: string | undefined): void {
  if (state === 'connected') {
    statusBar.text = `$(server) ${detail ?? 'Connected'}`;
    statusBar.tooltip = 'Distributed Compute: connected. Click for actions.';
  } else if (state === 'connecting') {
    statusBar.text = '$(loading~spin) Distributed Compute: connecting…';
  } else {
    statusBar.text = '$(debug-disconnect) Distributed Compute: not connected';
    statusBar.tooltip = 'Distributed Compute: not connected. Click to set up or connect.';
  }
}

function restartPolling(connection: ConnectionManager, refreshAll: () => void, logger: Logger): void {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = undefined;
  }
  const seconds = readSettings().refreshIntervalSeconds;
  // REST polling until the backend offers a client event stream (WebSocket).
  pollTimer = setInterval(() => {
    if (connection.snapshot().state === 'connected') {
      refreshAll();
    }
  }, seconds * 1000);
  logger.debug('polling (re)started', { seconds });
}

export function deactivate(): void {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = undefined;
  }
  statusBar?.dispose();
  output?.dispose();
}

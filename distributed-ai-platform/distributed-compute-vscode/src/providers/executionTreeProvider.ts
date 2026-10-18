// Executions tree, used for both the "Jobs" view (compact: one row per
// submitted execution) and the "Executions" view (expandable: per-execution
// attempt detail). Both read GET /executions from the gateway — the only job
// surface the current backend exposes — presented at two levels of detail.

import * as vscode from 'vscode';
import { ConnectionManager } from '../connection/connectionManager';
import { ApiError } from '../models/http';
import { executionLabel, isTerminal, progressFraction, statusText, type Execution } from '../models/execution';
import { Logger } from '../utilities/logger';

type ExecTreeItem =
  | { kind: 'message'; label: string }
  | { kind: 'execution'; execution: Execution }
  | { kind: 'detail'; label: string; value: string };

const STATUS_ICON: Record<string, string> = {
  pending: 'watch',
  cancelling: 'loading~spin',
  completed_with_errors: 'warning',
  created: 'circle-outline',
  queued: 'watch',
  scheduling: 'loading~spin',
  running: 'loading~spin',
  completed: 'pass',
  failed: 'error',
  cancelled: 'circle-slash',
  timed_out: 'clock',
  node_lost: 'warning',
};

export class ExecutionTreeProvider implements vscode.TreeDataProvider<ExecTreeItem> {
  private readonly emitter = new vscode.EventEmitter<ExecTreeItem | undefined>();
  readonly onDidChangeTreeData = this.emitter.event;

  constructor(
    private readonly connection: ConnectionManager,
    private readonly logger: Logger,
    /** When true, executions expand into attempt-detail rows. */
    private readonly detailed: boolean,
  ) {}

  refresh(): void {
    this.emitter.fire(undefined);
  }

  getTreeItem(element: ExecTreeItem): vscode.TreeItem {
    if (element.kind === 'message') {
      return new vscode.TreeItem(element.label, vscode.TreeItemCollapsibleState.None);
    }
    if (element.kind === 'detail') {
      const item = new vscode.TreeItem(element.label, vscode.TreeItemCollapsibleState.None);
      item.description = element.value;
      return item;
    }
    const e = element.execution;
    const collapsible = this.detailed
      ? vscode.TreeItemCollapsibleState.Collapsed
      : vscode.TreeItemCollapsibleState.None;
    const item = new vscode.TreeItem(executionLabel(e), collapsible);
    item.description = `${this.summary(e)} · ${e.id.slice(0, 8)}`;
    item.tooltip = `${e.id}\nStarted ${new Date(e.createdAt).toLocaleString()}`;
    item.iconPath = new vscode.ThemeIcon(STATUS_ICON[e.status] ?? 'circle-outline');
    item.id = `${this.detailed ? 'exec' : 'job'}:${e.id}`;
    // "execution:project:…" marks project runs (they can have output files).
    item.contextValue = `execution:${e.project ? 'project:' : ''}${e.status}:${isTerminal(e.status) ? 'terminal' : 'active'}`;
    item.command = { command: 'distributedCompute.viewLogs', title: 'View Logs', arguments: [element] };
    return item;
  }

  private summary(e: Execution): string {
    const frac = progressFraction(e);
    const pct = frac === undefined ? '' : ` · ${Math.round(frac * 100)}%`;
    return `${statusText(e.status)}${pct}`;
  }

  async getChildren(element?: ExecTreeItem): Promise<ExecTreeItem[]> {
    if (!element) {
      return this.rootChildren();
    }
    if (element.kind === 'execution' && this.detailed) {
      return this.detailRows(element.execution);
    }
    return [];
  }

  private async rootChildren(): Promise<ExecTreeItem[]> {
    const gw = this.connection.gateway();
    if (!gw) {
      // Empty: the view's welcome content (package.json viewsWelcome) explains
      // what to do, with buttons.
      return [];
    }
    try {
      const { items } = await gw.listExecutions();
      if (items.length === 0) {
        // Empty: the view's welcome content (package.json viewsWelcome) explains
        // what to do, with buttons.
        return [];
      }
      return items
        .slice()
        .sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1))
        .map((execution) => ({ kind: 'execution', execution }));
    } catch (err) {
      const message = err instanceof ApiError ? err.message : 'Failed to load executions.';
      this.logger.warn('execution list failed', { message });
      return [{ kind: 'message', label: message }];
    }
  }

  private detailRows(e: Execution): ExecTreeItem[] {
    return [
      { kind: 'detail', label: 'Runtime', value: e.runtime },
      { kind: 'detail', label: 'Status', value: e.status },
      { kind: 'detail', label: 'Tasks total', value: String(e.total) },
      { kind: 'detail', label: 'Completed', value: String(e.completed) },
      { kind: 'detail', label: 'Failed', value: String(e.failed) },
      ...(e.cancelled ? [{ kind: 'detail' as const, label: 'Cancelled', value: String(e.cancelled) }] : []),
      ...(e.message ? [{ kind: 'detail' as const, label: 'Message', value: e.message }] : []),
      { kind: 'detail', label: 'Created', value: e.createdAt },
      { kind: 'detail', label: 'Ended', value: e.endedAt ?? 'Not ended' },
    ];
  }

  dispose(): void {
    this.emitter.dispose();
  }
}

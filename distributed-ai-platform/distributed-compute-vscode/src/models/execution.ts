// Execution / cluster models, mirrored from the extension-gateway contract
// (`services/extension-gateway/server/types.go`).

/**
 * A runtime id from the backend registry (`GET /runtimes`), e.g. `python`,
 * `bash`, `node`, `go`, `rust` or `custom`. The set is open-ended: which ones a
 * user can run depends on what is detected on their workers (nothing is ever
 * installed there), so the extension only checks the id's shape and lets the
 * backend reject unknown or unavailable runtimes with a clear 400.
 */
export type Runtime = string;

/** Mirrors `runtimeIDPattern` in the extension-gateway (handlers.go). */
export const RUNTIME_ID_PATTERN = /^[a-z0-9][a-z0-9_.+-]{0,31}$/;

/** Common runtime ids, used only as examples in messages. */
export const RUNTIMES: readonly Runtime[] = ['python', 'bash', 'node', 'go', 'rust', 'java', 'custom'];

/** One entry of `GET /runtimes`. */
export interface RuntimeInfo {
  id: Runtime;
  name: string;
  filename: string;
  kind: 'interpreted' | 'compiled' | 'custom' | string;
  exampleCode?: string;
  windowsSupported: boolean;
  /** The caller's online nodes on which this runtime was detected. */
  availableNodeIds: string[];
}

// The backend's batch states (pending … completed_with_errors) plus the
// older names some masters still report.
export type ExecutionStatus =
  | 'pending'
  | 'cancelling'
  | 'completed_with_errors'
  | 'created'
  | 'queued'
  | 'scheduling'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'timed_out'
  | 'node_lost';

export interface Execution {
  id: string;
  runtime: Runtime;
  status: ExecutionStatus;
  total: number;
  completed: number;
  failed: number;
  cancelled?: number;
  message?: string;
  createdAt: string;
  endedAt?: string;
  /** Set for project runs (runtime "project"). */
  project?: ProjectSpec;
}

/** Run a folder: the bundle uploaded with POST /bundles and a command. */
export interface ProjectSpec {
  /** sha256 of the project's .tar.gz. */
  bundleId: string;
  command: string;
  requirements?: string;
  outputs?: string[];
  datasets?: { datasetId: string; path?: string }[];
}

export interface ExecutionList {
  items: Execution[];
  total: number;
}

/** Body for POST /executions. Only `runtime` is required by the gateway. */
export interface ExecutionRequest {
  runtime: Runtime;
  code?: string;
  /** One task per input (its stdin and input.txt); empty = one task. */
  inputs?: string[];
  /** Restrict to these nodes; empty = any of your online nodes. */
  nodeIds?: string[];
  /** Runtime "custom" only: command containing {file}, and the file name. */
  customCommand?: string;
  customFilename?: string;
  datasetId?: string;
  cpuPerTask?: number;
  memoryPerTaskGb?: number;
  timeoutSeconds?: number;
  executionPolicy?: ExecutionPolicy;
  /** Run a folder instead of `code`; the runtime is then "project". */
  project?: ProjectSpec;
}

export type ExecutionPolicy = 'safe_background' | 'balanced' | 'max_available';
export const EXECUTION_POLICIES: readonly ExecutionPolicy[] = ['safe_background', 'balanced', 'max_available'];

export function isExecutionPolicy(v: unknown): v is ExecutionPolicy {
  return typeof v === 'string' && (EXECUTION_POLICIES as readonly string[]).includes(v);
}

export interface ClusterStatus {
  nodesTotal: number;
  nodesOnline: number;
  cpuTotal: number;
  cpuAllocatable: number;
  cpuReserved: number;
  memoryTotalGb: number;
  memoryAllocatableGb: number;
  memoryReservedGb: number;
  jobsRunning: number;
}

export interface Health {
  status: string;
}

export function isRuntime(value: unknown): value is Runtime {
  return typeof value === 'string' && RUNTIME_ID_PATTERN.test(value);
}

/**
 * Progress fraction in [0,1] when the backend reports a meaningful task total,
 * otherwise `undefined` (so the UI shows no misleading bar for single-task or
 * not-yet-scheduled executions).
 */
export function progressFraction(e: Pick<Execution, 'total' | 'completed' | 'failed'>): number | undefined {
  if (!e.total || e.total <= 0) {
    return undefined;
  }
  const done = Math.min(e.total, (e.completed ?? 0) + (e.failed ?? 0));
  return done / e.total;
}

const TERMINAL: ReadonlySet<ExecutionStatus> = new Set<ExecutionStatus>([
  'completed',
  'completed_with_errors',
  'failed',
  'cancelled',
  'timed_out',
  'node_lost',
]);

export function isTerminal(status: ExecutionStatus): boolean {
  return TERMINAL.has(status);
}

/**
 * Documents whose text may be sent to workers: files on disk and unsaved
 * editors. Not the extension's own output views, settings, or other
 * virtual documents.
 */
export function isRunnableScheme(scheme: string): boolean {
  return scheme === 'file' || scheme === 'untitled';
}

/** "python · 8 tasks", the readable name of an execution in the views. */
export function executionLabel(e: Pick<Execution, 'runtime' | 'total'> & { project?: ProjectSpec }): string {
  const what = e.project ? shortCommand(e.project.command) : e.runtime;
  return `${what} · ${e.total} task${e.total === 1 ? '' : 's'}`;
}

/** "python train.py --epochs 3" -> at most 40 characters. */
export function shortCommand(command: string): string {
  const flat = command.replace(/\s+/g, ' ').trim();
  return flat.length > 40 ? `${flat.slice(0, 39)}…` : flat;
}

/** Status bar text: "2/2 nodes online · 1 job running". */
export function clusterSummary(online: number, total: number, running: number): string {
  const jobs = running === 0 ? 'no jobs running' : `${running} job${running === 1 ? '' : 's'} running`;
  return `${online}/${total} node${total === 1 ? '' : 's'} online · ${jobs}`;
}

/** "completed_with_errors" -> "completed with errors". */
export function statusText(status: string): string {
  return status.replace(/_/g, ' ');
}

// Node onboarding and execution-output shapes (ssh-executor's snake_case JSON,
// passed through the gateway) plus the pure helpers the commands use.

/** POST /nodes/connect: the worker's host key, before any credential is sent. */
export interface NodeConnectResult {
  host: string;
  port: number;
  username: string;
  host_key_type: string;
  fingerprint: string;
  onboarding_token: string;
  already_registered: boolean;
}

/** What the master detected on the worker (provision and node responses). */
export interface NodeCapabilities {
  os_type?: string | null;
  os_info?: string | null;
  detected_cpu: number;
  detected_memory_gb: number;
  gpu_model?: string | null;
  gpu_vendor?: string | null;
  gpu_count: number;
  gpu_memory_gb?: number | null;
  gpu_runtime?: string | null;
  python_version?: string | null;
  runtimes: Record<string, string>;
}

/** POST /nodes/provision (or .../verify): the key is installed and verified. */
export interface NodeProvisionResult extends NodeCapabilities {
  provision_id: string;
  host: string;
  port: number;
  username: string;
  fingerprint: string;
  expires_at: string;
}

/** POST /nodes/provision/manual: install the key yourself, then verify. */
export interface ManualProvisionResult {
  provision_id: string;
  public_key: string;
  install_command_posix: string;
  install_command_windows: string;
  expires_at: string;
}

/** POST /nodes body. Detected values come from the server, never from here. */
export interface NodeRegisterRequest {
  provision_id: string;
  name: string;
  allocated_cpu: number;
  allocated_memory_gb: number;
  allocated_gpu_count?: number;
  allocated_gpu_memory_gb?: number;
}

/** The backend's node record (health-check, refresh, rotate-key). */
export interface BackendNode extends NodeCapabilities {
  id: string;
  name: string;
  host: string;
  status: string;
  health_status: string;
  last_health_check?: string | null;
}

export interface NodeDeleteResult {
  deleted: boolean;
  key_removed: boolean;
  message: string;
}

/** One task of an execution (GET /executions/{id}/tasks). */
export interface TaskOutput {
  id: string;
  seq: number;
  node_id?: string | null;
  status: string;
  exit_code?: number | null;
  stdout?: string | null;
  stderr?: string | null;
  attempts: number;
  duration_ms?: number | null;
  /** Project runs: files brought back from the output folders. */
  output_files?: number | null;
  output_bytes?: number | null;
}

export interface ExecutionTasks {
  batch: {
    id: string;
    runtime: string;
    status: string;
    message?: string | null;
    total: number;
    project?: { command: string; requirements?: string | null; outputs?: string[] } | null;
  };
  tasks: TaskOutput[];
}

// --- helpers -----------------------------------------------------------------

/** Host field check: a hostname or IP, nothing that looks like a URL or command. */
export function validateHost(raw: string): string | undefined {
  const v = raw.trim();
  if (v === '') {
    return 'Enter the worker\u2019s hostname or IP address';
  }
  if (v.length > 253 || /[\s/@;|&$`'"\\]/.test(v)) {
    return 'Enter only a hostname or IP address, for example 192.168.1.20';
  }
  return undefined;
}

/** Inside Docker, localhost is the master's own container, not this computer. */
export function isLoopbackHost(host: string): boolean {
  const h = host.trim().toLowerCase();
  return h === 'localhost' || h === '::1' || h === '[::1]' || /^127\./.test(h);
}

export function parsePort(raw: string): number | undefined {
  const v = raw.trim();
  if (!/^\d{1,5}$/.test(v)) {
    return undefined;
  }
  const n = Number(v);
  return n >= 1 && n <= 65535 ? n : undefined;
}

/** "8 cores · 16 GB RAM · Apple M2 (torch-mps) · python 3.11, node 20.1" */
export function detectionSummary(c: NodeCapabilities): string {
  const parts = [`${c.detected_cpu} cores`, `${round1(c.detected_memory_gb)} GB RAM`];
  if (c.gpu_count > 0 || c.gpu_model) {
    const name = c.gpu_model || c.gpu_vendor || 'GPU';
    parts.push(`${c.gpu_count > 1 ? `${c.gpu_count}× ` : ''}${name}${c.gpu_runtime ? ` (${c.gpu_runtime})` : ''}`);
  } else {
    parts.push('no GPU');
  }
  const runtimes = Object.entries(c.runtimes ?? {});
  parts.push(runtimes.length ? runtimes.map(([id, v]) => runtimeWithVersion(id, v)).join(', ') : 'no languages found');
  return parts.join(' · ');
}

export interface SliceChoice {
  label: string;
  description: string;
  cpu: number;
  memoryGb: number;
}

/** Ready-made slices offered after detection; the first is the default. */
export function sliceChoices(c: Pick<NodeCapabilities, 'detected_cpu' | 'detected_memory_gb'>): SliceChoice[] {
  const cores = Math.max(1, c.detected_cpu);
  const mem = Math.max(0, c.detected_memory_gb);
  const make = (label: string, cpu: number, memoryGb: number): SliceChoice => ({
    label,
    description: `${cpu} of ${cores} cores · ${round1(memoryGb)} of ${round1(mem)} GB`,
    cpu,
    memoryGb: round1(memoryGb),
  });
  const choices = [make('Half the machine', Math.max(1, Math.floor(cores / 2)), mem / 2)];
  if (cores > 2) {
    choices.push(make('All but one core', cores - 1, Math.max(0, mem - 2)));
  }
  choices.push(make('Everything', cores, mem));
  choices.push(make('One core', 1, Math.min(mem, 1)));
  return choices;
}

/** One input per line; blank lines are skipped. */
export function parseInputLines(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((l) => l.replace(/\s+$/, ''))
    .filter((l) => l.trim() !== '');
}

/** "1,2,3" or "1, 2, 3" → ["1","2","3"]. */
export function parseInputList(text: string): string[] {
  return text
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s !== '');
}

/** Plain-text log of every task, for the live logs view. */
export function formatTaskLog(data: ExecutionTasks, nodeNames: Record<string, string> = {}): string {
  const b = data.batch;
  const done = data.tasks.filter((t) => !['pending', 'running'].includes(t.status)).length;
  const lines = [
    `Execution ${b.id}`,
    b.project
      ? `Project · ${b.project.command} · ${b.status} · ${done}/${data.tasks.length || b.total} tasks finished`
      : `Runtime ${b.runtime} · ${b.status} · ${done}/${data.tasks.length || b.total} tasks finished`,
  ];
  if (b.message) {
    lines.push(b.message);
  }
  for (const t of [...data.tasks].sort((x, y) => x.seq - y.seq)) {
    const node = t.node_id ? nodeNames[t.node_id] ?? t.node_id : 'not scheduled yet';
    const exit = t.exit_code === null || t.exit_code === undefined ? '' : ` · exit ${t.exit_code}`;
    const time = t.duration_ms ? ` · ${(t.duration_ms / 1000).toFixed(1)}s` : '';
    const files = t.output_files ? ` · ${t.output_files} output file${t.output_files === 1 ? '' : 's'}` : '';
    lines.push('', `━━ Task ${t.seq + 1} · ${t.status}${exit}${time} · ${node}${files}`);
    if (t.stdout) {
      lines.push(t.stdout.replace(/\s+$/, ''));
    }
    if (t.stderr) {
      lines.push('── stderr', t.stderr.replace(/\s+$/, ''));
    }
    if (!t.stdout && !t.stderr && !['pending', 'running'].includes(t.status)) {
      lines.push('(no output)');
    }
  }
  return lines.join('\n') + '\n';
}

/** task-001.out.txt style names, padded so they sort. */
export function resultFileName(seq: number, total: number, kind: 'out' | 'err'): string {
  const width = Math.max(3, String(total).length);
  return `task-${String(seq + 1).padStart(width, '0')}.${kind}.txt`;
}

function round1(v: number): number {
  return Math.round(v * 10) / 10;
}

/** "Python 3.14.8" -> "3.14.8"; "GNU bash, version 5.2.21(1)" -> "5.2.21"; otherwise as is. */
export function versionNumber(raw: string | null | undefined): string {
  const v = (raw ?? '').trim();
  return /\d+(?:\.\d+)+/.exec(v)?.[0] ?? v;
}

/** "python 3.14.8"; just "sh" when the "version" only names the shell. */
export function runtimeWithVersion(id: string, raw: string | null | undefined): string {
  const v = versionNumber(raw);
  return !v || (!/\d/.test(v) && (v.toLowerCase().includes(id.toLowerCase()) || id.toLowerCase().includes(v.toLowerCase())))
    ? id
    : `${id} ${v}`;
}

// Extension-facing domain models, mirrored from the Go extension-gateway JSON
// contract (`services/extension-gateway/server/types.go`). Pointer fields on the
// Go side are optional here and may be `undefined` when the backend has not
// reported a value — rendered as "Unknown"/"Not reported", never fabricated.

export type NodeStatus =
  | 'online'
  | 'busy'
  | 'degraded'
  | 'offline'
  | 'unreachable'
  | 'draining'
  | 'unknown';

export interface ComputeNode {
  id: string;
  name: string;
  host?: string;
  osType?: string;
  osVersion?: string;
  status: NodeStatus;
  cpuTotal?: number;
  cpuAllocatable?: number;
  cpuReserved?: number;
  memoryTotalGb?: number;
  memoryAllocatableGb?: number;
  memoryReservedGb?: number;
  gpuCount?: number;
  gpuModel?: string;
  gpuMemoryTotalGb?: number;
  /** GPU slice lent to the platform. */
  gpuAllocatableCount?: number;
  gpuMemoryAllocatableGb?: number;
  gpuFraction?: number;
  /** Detected GPU runtime (e.g. torch); absent when none was found. */
  gpuRuntime?: string;
  resourceEnforcement?: 'strict' | 'best_effort';
  /** Disk lent for cached project environments, in GB (0 or missing = off). */
  envCacheGb?: number;
  activeTasks: number;
  lastHealthCheck?: string;
}

export interface NodeList {
  items: ComputeNode[];
  total: number;
}

const KNOWN_STATUSES: ReadonlySet<string> = new Set<NodeStatus>([
  'online',
  'busy',
  'degraded',
  'offline',
  'unreachable',
  'draining',
  'unknown',
]);

/** Normalize an arbitrary backend status string to a known NodeStatus. */
export function normalizeStatus(raw: unknown): NodeStatus {
  const s = typeof raw === 'string' ? raw.toLowerCase() : '';
  return (KNOWN_STATUSES.has(s) ? s : 'unknown') as NodeStatus;
}

/** Human label for a possibly-missing numeric value — honest about gaps. */
export function orUnknown(value: number | undefined, suffix = ''): string {
  if (value === undefined || value === null || Number.isNaN(value)) {
    return 'Unknown';
  }
  return `${value}${suffix}`;
}

/** "2 / 8" style capacity label; either side may be unknown. */
export function capacityLabel(used?: number, total?: number): string {
  return `${orUnknown(used)} / ${orUnknown(total)}`;
}

/** Body for PATCH /nodes/{id}; omitted fields stay unchanged. */
export interface NodeSliceRequest {
  cpuAllocatable?: number;
  memoryAllocatableGb?: number;
  gpuAllocatableCount?: number;
  gpuMemoryAllocatableGb?: number;
  /** Disk lent for cached project environments (0 turns the cache off). */
  envCacheGb?: number;
}

function fmtGb(gb: number): string {
  return `${Number.isInteger(gb) ? gb : gb.toFixed(1)}GB`;
}

/** True when the node reports a GPU with known VRAM. */
export function hasGpu(n: ComputeNode): boolean {
  return (n.gpuCount ?? 0) > 0 && (n.gpuMemoryTotalGb ?? 0) > 0;
}

/**
 * "Slice: 8GB / 24GB VRAM | 4 / 10 Cores", or "Slice: 4 / 10 Cores" for
 * CPU-only nodes. Unknown values render as "Unknown".
 */
export function sliceLabel(n: ComputeNode): string {
  const cores = `${capacityLabel(n.cpuAllocatable, n.cpuTotal)} Cores`;
  if (!hasGpu(n)) {
    return `Slice: ${cores}`;
  }
  return `Slice: ${fmtGb(n.gpuMemoryAllocatableGb ?? 0)} / ${fmtGb(n.gpuMemoryTotalGb as number)} VRAM | ${cores}`;
}

/** Tree label: "Worker 1 (RTX 3090) - Slice: ..." */
export function nodeLabel(n: ComputeNode): string {
  const name = n.name || n.id;
  const model = hasGpu(n) && n.gpuModel ? ` (${n.gpuModel})` : '';
  return `${name}${model} - ${sliceLabel(n)}`;
}

/** VRAM preset in GB, rounded down to 0.5 GB. */
export function gpuPresetGb(totalGb: number, fraction: number): number {
  return Math.floor(totalGb * fraction * 2) / 2;
}

/** "2 of 12 cores" (lent of detected), for the node list. */
export function coresLent(n: Pick<ComputeNode, 'cpuAllocatable' | 'cpuTotal'>): string {
  return `${orUnknown(n.cpuAllocatable)} of ${orUnknown(n.cpuTotal)} cores`;
}

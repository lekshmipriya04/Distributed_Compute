// `distributed.json` project configuration: default generation, structural
// validation, and workspace path-safety checks. Pure and dependency-free so it
// unit-tests without VS Code or a filesystem; the command layer does the actual
// read/write and user confirmation.

import { RUNTIMES, isRuntime, type Runtime } from '../models/execution';
import { validateProjectBlock, type ProjectBlock } from '../models/project';

export type Placement = 'spread' | 'pack' | 'balanced' | 'gpu_first' | 'memory_first';

const PLACEMENTS: ReadonlySet<string> = new Set<Placement>([
  'spread',
  'pack',
  'balanced',
  'gpu_first',
  'memory_first',
]);

export interface DistributedConfig {
  version: number;
  name: string;
  entrypoint: string;
  runtime: Runtime;
  dataset?: string;
  resources?: { cpu?: number; memory?: string; gpu?: number };
  execution?: { timeoutSeconds?: number; maxRetries?: number; placement?: Placement };
  output?: string;
  training?: TrainingConfig;
  /** Run the whole folder ("Run Project on Workers"). */
  project?: ProjectBlock;
}

/** Saved answers of the training wizard; "Run Again" submits them as is. */
export interface TrainingConfig {
  /** storage-service dataset id. */
  dataset: string;
  /** linear_regression | logistic_regression | cnn | custom | spark:<Estimator> */
  modelType: string;
  /** Workspace-relative model.py, for modelType "custom". */
  modelFile?: string;
  engine?: 'python' | 'spark_ray';
  mode?: 'single' | 'multi';
  targetColumn?: string;
  featureColumns?: string[];
  rounds?: number;
  epochs?: number;
  learningRate?: number;
  executionPolicy?: string;
  modelParams?: Record<string, unknown>;
  paramGrid?: Record<string, unknown[]>;
  usesGpu?: boolean;
}

const MODEL_TYPE = /^(linear_regression|logistic_regression|cnn|custom|spark:[A-Za-z][A-Za-z0-9]{1,63})$/;
const POLICIES: ReadonlySet<string> = new Set(['safe_background', 'balanced', 'max_available']);

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

export function validateTraining(value: unknown): string[] {
  const errors: string[] = [];
  if (!isPlainObject(value)) {
    return ['"training" must be an object.'];
  }
  const t = value;
  if (typeof t.dataset !== 'string' || t.dataset.trim() === '') {
    errors.push('"training.dataset" must be a dataset id.');
  }
  if (typeof t.modelType !== 'string' || !MODEL_TYPE.test(t.modelType)) {
    errors.push('"training.modelType" must be a built-in model, "custom", or "spark:<Estimator>".');
  }
  if (t.modelType === 'custom' && (typeof t.modelFile !== 'string' || !isSafeRelativePath(t.modelFile))) {
    errors.push('"training.modelFile" must be a workspace-relative path to model.py when modelType is "custom".');
  }
  if (t.engine !== undefined && t.engine !== 'python' && t.engine !== 'spark_ray') {
    errors.push('"training.engine" must be "python" or "spark_ray".');
  }
  if (typeof t.modelType === 'string' && t.modelType.startsWith('spark:') && t.engine !== 'spark_ray') {
    errors.push('Spark models need "training.engine": "spark_ray".');
  }
  if (t.mode !== undefined && t.mode !== 'single' && t.mode !== 'multi') {
    errors.push('"training.mode" must be "single" or "multi".');
  }
  for (const key of ['rounds', 'epochs'] as const) {
    if (t[key] !== undefined && (typeof t[key] !== 'number' || !Number.isInteger(t[key]) || (t[key] as number) < 1)) {
      errors.push(`"training.${key}" must be a whole number >= 1.`);
    }
  }
  if (t.learningRate !== undefined && (typeof t.learningRate !== 'number' || t.learningRate <= 0)) {
    errors.push('"training.learningRate" must be a number > 0.');
  }
  if (t.executionPolicy !== undefined && (typeof t.executionPolicy !== 'string' || !POLICIES.has(t.executionPolicy))) {
    errors.push(`"training.executionPolicy" must be one of ${[...POLICIES].join(', ')}.`);
  }
  if (t.featureColumns !== undefined && (!Array.isArray(t.featureColumns) || t.featureColumns.some((c) => typeof c !== 'string'))) {
    errors.push('"training.featureColumns" must be a list of column names.');
  }
  if (t.modelParams !== undefined && !isPlainObject(t.modelParams)) {
    errors.push('"training.modelParams" must be an object.');
  }
  if (t.paramGrid !== undefined && (!isPlainObject(t.paramGrid) || Object.values(t.paramGrid).some((v) => !Array.isArray(v)))) {
    errors.push('"training.paramGrid" must map parameter names to lists of values.');
  }
  return errors;
}

export interface ValidationResult {
  ok: boolean;
  errors: string[];
}

export function buildDefault(name: string, runtime: Runtime, timeoutSeconds: number): DistributedConfig {
  return {
    version: 1,
    name,
    entrypoint: 'train.py',
    runtime,
    dataset: './data',
    resources: { cpu: 8, memory: '12GB', gpu: 0 },
    execution: { timeoutSeconds, maxRetries: 2, placement: 'spread' },
    output: './outputs',
  };
}

/**
 * Reject paths that are absolute or escape the project root. The config may only
 * reference files inside the workspace; `..` traversal and absolute/UNC/drive
 * paths are blocked.
 */
export function isSafeRelativePath(p: string): boolean {
  if (typeof p !== 'string' || p.length === 0) {
    return false;
  }
  if (p.startsWith('/') || p.startsWith('\\') || /^[A-Za-z]:[\\/]/.test(p)) {
    return false; // absolute / drive / UNC
  }
  const parts = p.replace(/\\/g, '/').split('/');
  let depth = 0;
  for (const part of parts) {
    if (part === '' || part === '.') {
      continue;
    }
    if (part === '..') {
      depth--;
      if (depth < 0) {
        return false; // escapes the root
      }
    } else {
      depth++;
    }
  }
  return true;
}

export function validateConfig(value: unknown): ValidationResult {
  const errors: string[] = [];
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return { ok: false, errors: ['Configuration must be a JSON object.'] };
  }
  const c = value as Record<string, unknown>;

  if (c.version !== 1) {
    errors.push('"version" must be 1.');
  }
  if (typeof c.name !== 'string' || c.name.trim() === '') {
    errors.push('"name" is required and must be a non-empty string.');
  }
  // A project config needs only its "project" block; a file config needs these.
  const fileRun = c.project === undefined || c.entrypoint !== undefined || c.runtime !== undefined;
  if (fileRun && (typeof c.entrypoint !== 'string' || !isSafeRelativePath(c.entrypoint))) {
    errors.push('"entrypoint" must be a workspace-relative path inside the project.');
  }
  if (fileRun && !isRuntime(c.runtime)) {
    errors.push(`"runtime" must be a runtime id from GET /runtimes (e.g. ${RUNTIMES.join(', ')}).`);
  }
  for (const key of ['dataset', 'output'] as const) {
    const v = c[key];
    if (v !== undefined && (typeof v !== 'string' || !isSafeRelativePath(v))) {
      errors.push(`"${key}" must be a workspace-relative path inside the project.`);
    }
  }
  if (c.resources !== undefined) {
    const r = c.resources as Record<string, unknown>;
    if (r.cpu !== undefined && (typeof r.cpu !== 'number' || r.cpu < 1)) {
      errors.push('"resources.cpu" must be a number >= 1.');
    }
    if (r.gpu !== undefined && (typeof r.gpu !== 'number' || r.gpu < 0)) {
      errors.push('"resources.gpu" must be a number >= 0.');
    }
    if (r.memory !== undefined && typeof r.memory !== 'string') {
      errors.push('"resources.memory" must be a string like "12GB".');
    }
  }
  if (c.execution !== undefined) {
    const e = c.execution as Record<string, unknown>;
    if (e.timeoutSeconds !== undefined && (typeof e.timeoutSeconds !== 'number' || e.timeoutSeconds < 1)) {
      errors.push('"execution.timeoutSeconds" must be a number >= 1.');
    }
    if (e.maxRetries !== undefined && (typeof e.maxRetries !== 'number' || e.maxRetries < 0)) {
      errors.push('"execution.maxRetries" must be a number >= 0.');
    }
    if (e.placement !== undefined && (typeof e.placement !== 'string' || !PLACEMENTS.has(e.placement))) {
      errors.push(`"execution.placement" must be one of ${[...PLACEMENTS].join(', ')}.`);
    }
  }

  if (c.training !== undefined) {
    errors.push(...validateTraining(c.training));
  }
  if (c.project !== undefined) {
    errors.push(...validateProjectBlock(c.project));
  }

  return { ok: errors.length === 0, errors };
}

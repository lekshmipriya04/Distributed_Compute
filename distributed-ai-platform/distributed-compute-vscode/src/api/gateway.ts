// High-level, typed calls against the extension-gateway contract. Each method
// maps 1:1 to a documented route and shallow-validates the response so a
// malformed payload surfaces as a clear 'bad-response' error rather than a
// downstream `undefined`.

import { ApiClient } from './client';
import { ApiError } from '../models/http';
import {
  isRuntime,
  type ClusterStatus,
  type Execution,
  type ExecutionList,
  type ExecutionRequest,
  type Health,
  type RuntimeInfo,
} from '../models/execution';
import { normalizeStatus, type ComputeNode, type NodeList, type NodeSliceRequest } from '../models/node';
import type { Dataset, EngineInfo, SparkModelInfo, TrainRequest, TrainingRun } from '../models/training';
import type {
  BackendNode,
  ExecutionTasks,
  ManualProvisionResult,
  NodeConnectResult,
  NodeDeleteResult,
  NodeProvisionResult,
  NodeRegisterRequest,
} from '../models/onboarding';

function badResponse(what: string): never {
  throw new ApiError(`The gateway returned an unexpected ${what} payload.`, 'bad-response');
}

function asObject(v: unknown, what: string): Record<string, unknown> {
  if (v === null || typeof v !== 'object' || Array.isArray(v)) {
    badResponse(what);
  }
  return v as Record<string, unknown>;
}

function coerceNode(v: unknown): ComputeNode {
  const o = asObject(v, 'node');
  if (typeof o.id !== 'string' || typeof o.name !== 'string') {
    badResponse('node');
  }
  return {
    ...(o as unknown as ComputeNode),
    status: normalizeStatus(o.status),
    activeTasks: typeof o.activeTasks === 'number' ? o.activeTasks : 0,
  };
}

function coerceExecution(v: unknown): Execution {
  const o = asObject(v, 'execution');
  if (typeof o.id !== 'string' || !isRuntime(o.runtime)) {
    badResponse('execution');
  }
  return o as unknown as Execution;
}

function coerceProvision(v: unknown): NodeProvisionResult {
  const o = asObject(v, 'provision');
  if (typeof o.provision_id !== 'string') {
    badResponse('provision');
  }
  return { ...(o as unknown as NodeProvisionResult), runtimes: (o.runtimes as Record<string, string>) ?? {} };
}

function coerceBackendNode(v: unknown): BackendNode {
  const o = asObject(v, 'node');
  if (typeof o.id !== 'string') {
    badResponse('node');
  }
  return { ...(o as unknown as BackendNode), runtimes: (o.runtimes as Record<string, string>) ?? {} };
}

function coerceRun(v: unknown): TrainingRun {
  const o = asObject(v, 'training run');
  if (typeof o.id !== 'string' || typeof o.model_type !== 'string' || typeof o.status !== 'string') {
    badResponse('training run');
  }
  return o as unknown as TrainingRun;
}

export class GatewayApi {
  constructor(private readonly client: ApiClient) {}

  async health(signal?: AbortSignal): Promise<Health> {
    return this.client.request<Health>('/health', { signal, timeoutMs: 8000 });
  }

  /** One-click sign-in; served only by a local master in development. */
  async localToken(signal?: AbortSignal): Promise<{ token: string; expiresAt?: string }> {
    const raw = asObject(
      await this.client.request<unknown>('/auth/local-token', { method: 'POST', signal, timeoutMs: 8000 }),
      'local token',
    );
    if (typeof raw.token !== 'string' || raw.token === '') {
      badResponse('local token');
    }
    return { token: raw.token, expiresAt: typeof raw.expiresAt === 'string' ? raw.expiresAt : undefined };
  }

  async clusterStatus(signal?: AbortSignal): Promise<ClusterStatus> {
    const raw = await this.client.request<unknown>('/cluster/status', { signal });
    return asObject(raw, 'cluster status') as unknown as ClusterStatus;
  }

  async listNodes(signal?: AbortSignal): Promise<NodeList> {
    const raw = await this.client.request<unknown>('/nodes', { signal });
    const o = asObject(raw, 'node list');
    const items = Array.isArray(o.items) ? o.items.map(coerceNode) : [];
    return { items, total: typeof o.total === 'number' ? o.total : items.length };
  }

  async getNode(id: string, signal?: AbortSignal): Promise<ComputeNode> {
    const raw = await this.client.request<unknown>(`/nodes/${encodeURIComponent(id)}`, { signal });
    return coerceNode(raw);
  }

  /** Change the resource slice a node lends (PATCH /nodes/{id}). */
  async updateNodeSlice(id: string, req: NodeSliceRequest, signal?: AbortSignal): Promise<ComputeNode> {
    const raw = await this.client.request<unknown>(`/nodes/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: req,
      signal,
    });
    return coerceNode(raw);
  }

  /** Runtimes the backend offers, each with the nodes where it was detected. */
  async listRuntimes(signal?: AbortSignal): Promise<RuntimeInfo[]> {
    const raw = await this.client.request<unknown>('/runtimes', { signal });
    const o = asObject(raw, 'runtime list');
    if (!Array.isArray(o.items)) {
      badResponse('runtime list');
    }
    return (o.items as unknown[]).map((v) => {
      const r = asObject(v, 'runtime');
      if (!isRuntime(r.id)) {
        badResponse('runtime');
      }
      return {
        ...(r as unknown as RuntimeInfo),
        availableNodeIds: Array.isArray(r.availableNodeIds) ? (r.availableNodeIds as string[]) : [],
      };
    });
  }

  async listExecutions(signal?: AbortSignal): Promise<ExecutionList> {
    const raw = await this.client.request<unknown>('/executions', { signal });
    const o = asObject(raw, 'execution list');
    const items = Array.isArray(o.items) ? o.items.map(coerceExecution) : [];
    return { items, total: typeof o.total === 'number' ? o.total : items.length };
  }

  async getExecution(id: string, signal?: AbortSignal): Promise<Execution> {
    const raw = await this.client.request<unknown>(`/executions/${encodeURIComponent(id)}`, { signal });
    return coerceExecution(raw);
  }

  /**
   * Submit a new execution. `requestId` is client-generated and stable per
   * logical submission so a double-click or retry cannot create duplicates.
   */
  async createExecution(req: ExecutionRequest, requestId: string, signal?: AbortSignal): Promise<Execution> {
    const raw = await this.client.request<unknown>('/executions', {
      method: 'POST',
      body: req,
      requestId,
      signal,
    });
    return coerceExecution(raw);
  }

  async cancelExecution(id: string, signal?: AbortSignal): Promise<Execution> {
    const raw = await this.client.request<unknown>(`/executions/${encodeURIComponent(id)}/cancel`, {
      method: 'POST',
      signal,
    });
    return coerceExecution(raw);
  }

  // --- node onboarding and lifecycle, execution output (backend shapes) ---

  /** Step 1: the worker's host key. No credential is sent. */
  async connectNode(host: string, port: number, username: string, signal?: AbortSignal): Promise<NodeConnectResult> {
    const raw = asObject(
      await this.client.request<unknown>('/nodes/connect', {
        method: 'POST',
        body: { host, port, username },
        signal,
        timeoutMs: 60000,
      }),
      'node connect',
    );
    if (typeof raw.onboarding_token !== 'string' || typeof raw.fingerprint !== 'string') {
      badResponse('node connect');
    }
    return raw as unknown as NodeConnectResult;
  }

  /** Step 2: the password is used once to install the managed key, then dropped. */
  async provisionNode(onboardingToken: string, password: string, signal?: AbortSignal): Promise<NodeProvisionResult> {
    const raw = await this.client.request<unknown>('/nodes/provision', {
      method: 'POST',
      body: { onboarding_token: onboardingToken, password },
      signal,
      timeoutMs: 120000,
    });
    return coerceProvision(raw);
  }

  /** Step 2, without a password: get the public key and an install command. */
  async provisionNodeManually(onboardingToken: string, signal?: AbortSignal): Promise<ManualProvisionResult> {
    const raw = asObject(
      await this.client.request<unknown>('/nodes/provision/manual', {
        method: 'POST',
        body: { onboarding_token: onboardingToken },
        signal,
        timeoutMs: 60000,
      }),
      'manual provision',
    );
    if (typeof raw.provision_id !== 'string') {
      badResponse('manual provision');
    }
    return raw as unknown as ManualProvisionResult;
  }

  /** After a manual install: log in with the key and detect the machine. */
  async verifyProvision(provisionId: string, signal?: AbortSignal): Promise<NodeProvisionResult> {
    const raw = await this.client.request<unknown>(`/nodes/provision/${encodeURIComponent(provisionId)}/verify`, {
      method: 'POST',
      signal,
      timeoutMs: 120000,
    });
    return coerceProvision(raw);
  }

  /** Step 3: save the node with the slice it lends. */
  async registerNode(req: NodeRegisterRequest, signal?: AbortSignal): Promise<BackendNode> {
    return coerceBackendNode(await this.client.request<unknown>('/nodes', { method: 'POST', body: req, signal }));
  }

  /** Remove the node; the master also deletes its key line from the worker. */
  async deleteNode(id: string, signal?: AbortSignal): Promise<NodeDeleteResult> {
    const raw = asObject(
      await this.client.request<unknown>(`/nodes/${encodeURIComponent(id)}`, { method: 'DELETE', signal, timeoutMs: 60000 }),
      'node delete',
    );
    return raw as unknown as NodeDeleteResult;
  }

  async healthCheckNode(id: string, signal?: AbortSignal): Promise<BackendNode> {
    return this.nodeAction(id, 'health-check', signal);
  }

  /** Detect the worker's hardware and languages again. */
  async refreshNode(id: string, signal?: AbortSignal): Promise<BackendNode> {
    return this.nodeAction(id, 'refresh', signal);
  }

  /** Replace the platform's key on the worker with a fresh one. */
  async rotateNodeKey(id: string, signal?: AbortSignal): Promise<BackendNode> {
    return this.nodeAction(id, 'rotate-key', signal);
  }

  /** Drain (no new work) or resume a node. */
  async setNodeStatus(id: string, status: 'online' | 'draining', signal?: AbortSignal): Promise<ComputeNode> {
    const raw = await this.client.request<unknown>(`/nodes/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: { status },
      signal,
    });
    return coerceNode(raw);
  }

  private async nodeAction(id: string, action: string, signal?: AbortSignal): Promise<BackendNode> {
    return coerceBackendNode(
      await this.client.request<unknown>(`/nodes/${encodeURIComponent(id)}/${action}`, {
        method: 'POST',
        signal,
        timeoutMs: 120000,
      }),
    );
  }

  /** Every task of an execution with its output. */
  async executionTasks(id: string, signal?: AbortSignal): Promise<ExecutionTasks> {
    const raw = asObject(
      await this.client.request<unknown>(`/executions/${encodeURIComponent(id)}/tasks`, { signal }),
      'execution tasks',
    );
    if (!Array.isArray(raw.tasks) || raw.batch === null || typeof raw.batch !== 'object') {
      badResponse('execution tasks');
    }
    return raw as unknown as ExecutionTasks;
  }

  /** Run a finished execution again: every input, or only failed/cancelled ones. */
  async retryExecution(id: string, failedOnly: boolean, signal?: AbortSignal): Promise<{ id: string }> {
    const raw = asObject(
      await this.client.request<unknown>(
        `/executions/${encodeURIComponent(id)}/retry${failedOnly ? '?failed_only=true' : ''}`,
        { method: 'POST', signal },
      ),
      'retry',
    );
    if (typeof raw.id !== 'string') {
      badResponse('retry');
    }
    return { id: raw.id };
  }

  // --- training, engines, datasets (backend shapes, passed through) --------

  async listEngines(refresh = false, signal?: AbortSignal): Promise<EngineInfo[]> {
    const raw = asObject(
      await this.client.request<unknown>(`/engines${refresh ? '?refresh=true' : ''}`, { signal, timeoutMs: 30000 }),
      'engine list',
    );
    if (!Array.isArray(raw.items)) {
      badResponse('engine list');
    }
    return raw.items as EngineInfo[];
  }

  /** Every Spark MLlib model on the master. The first call starts Spark. */
  async listSparkModels(signal?: AbortSignal): Promise<SparkModelInfo[]> {
    const raw = asObject(
      await this.client.request<unknown>('/engines/spark-ray/models', { signal, timeoutMs: 120000 }),
      'Spark model list',
    );
    if (!Array.isArray(raw.items)) {
      badResponse('Spark model list');
    }
    return raw.items as SparkModelInfo[];
  }

  async listDatasets(signal?: AbortSignal): Promise<Dataset[]> {
    const raw = asObject(await this.client.request<unknown>('/datasets?limit=200', { signal }), 'dataset list');
    if (!Array.isArray(raw.items)) {
      badResponse('dataset list');
    }
    return raw.items as Dataset[];
  }

  /** True when this user already uploaded the project archive with that sha256. */
  async hasBundle(sha256: string, signal?: AbortSignal): Promise<boolean> {
    try {
      await this.client.request<unknown>(`/bundles/${encodeURIComponent(sha256)}`, { signal });
      return true;
    } catch (err) {
      if (err instanceof ApiError && err.kind === 'not-found') {
        return false;
      }
      throw err;
    }
  }

  /** Upload a project's .tar.gz; its id is its sha256. */
  async uploadBundle(archive: Uint8Array, signal?: AbortSignal): Promise<{ id: string; sizeBytes: number }> {
    const response = await this.client.requestRaw('/bundles', {
      method: 'POST',
      rawBody: new Blob([archive]),
      contentType: 'application/gzip',
      signal,
      timeoutMs: 30 * 60 * 1000,
    });
    const raw = asObject(await response.json(), 'bundle');
    if (typeof raw.id !== 'string') {
      badResponse('bundle');
    }
    return { id: raw.id, sizeBytes: typeof raw.size_bytes === 'number' ? raw.size_bytes : archive.length };
  }

  /** What one project task wrote to its output folders, as a .tar.gz. */
  async downloadTaskOutputs(executionId: string, taskId: string, signal?: AbortSignal): Promise<Uint8Array> {
    const response = await this.client.requestRaw(
      `/executions/${encodeURIComponent(executionId)}/tasks/${encodeURIComponent(taskId)}/outputs`,
      { signal, accept: '*/*', timeoutMs: 30 * 60 * 1000 },
    );
    return new Uint8Array(await response.arrayBuffer());
  }

  /** Upload a local file to storage-service via multipart streaming. */
  async uploadDataset(name: string, filename: string, content: Uint8Array, signal?: AbortSignal): Promise<Dataset> {
    const form = new FormData();
    form.append('name', name);
    form.append('file', new Blob([content]), filename);
    const response = await this.client.requestRaw('/datasets', {
      method: 'POST',
      rawBody: form,
      signal,
      timeoutMs: 30 * 60 * 1000,
    });
    const raw = asObject(await response.json(), 'dataset');
    if (typeof raw.id !== 'string') {
      badResponse('dataset');
    }
    return raw as unknown as Dataset;
  }

  async submitTraining(req: TrainRequest, requestId: string, signal?: AbortSignal): Promise<TrainingRun> {
    // Validation of a Spark model may start Spark on the master first.
    const raw = await this.client.request<unknown>('/training', {
      method: 'POST',
      body: req,
      requestId,
      signal,
      timeoutMs: 120000,
    });
    return coerceRun(raw);
  }

  async listTrainingRuns(signal?: AbortSignal): Promise<TrainingRun[]> {
    const raw = await this.client.request<unknown>('/training', { signal });
    if (!Array.isArray(raw)) {
      badResponse('training run list');
    }
    return raw.map(coerceRun);
  }

  async getTrainingRun(id: string, signal?: AbortSignal): Promise<TrainingRun> {
    return coerceRun(await this.client.request<unknown>(`/training/${encodeURIComponent(id)}`, { signal }));
  }

  async deleteTrainingRun(id: string, signal?: AbortSignal): Promise<void> {
    await this.client.request<unknown>(`/training/${encodeURIComponent(id)}`, { method: 'DELETE', signal });
  }

  /** The trained model: ONNX, params JSON, or a zipped Spark PipelineModel. */
  async downloadModel(id: string, signal?: AbortSignal): Promise<{ bytes: Uint8Array; filename: string }> {
    const response = await this.client.requestRaw(`/training/${encodeURIComponent(id)}/model`, {
      signal,
      accept: '*/*',
      timeoutMs: 30 * 60 * 1000,
    });
    const disposition = response.headers.get('content-disposition') ?? '';
    const match = /filename="([^"]+)"/.exec(disposition);
    return { bytes: new Uint8Array(await response.arrayBuffer()), filename: match?.[1] ?? `model-${id}` };
  }
}

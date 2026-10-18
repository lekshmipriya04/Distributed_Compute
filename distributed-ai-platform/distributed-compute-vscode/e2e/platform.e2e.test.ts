// End-to-end tests of the extension's API layer against a real, running
// platform (docker compose --profile dev up) with the two test SSH workers.
// Three users on three gateway ports:
//   dev-user  -> DC_E2E_URL_DEV   (default :8090, one-click local sign-in)
//   alice     -> DC_E2E_URL_ALICE (default :8091, JWT)
//   bob       -> DC_E2E_URL_BOB   (default :8092, JWT)
// Run with: npm run test:e2e

import { createHmac } from 'node:crypto';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { ApiClient } from '../src/api/client';
import { GatewayApi } from '../src/api/gateway';
import { ApiError } from '../src/models/http';
import { isTerminal, type Execution, type ExecutionStatus } from '../src/models/execution';
import { formatTaskLog } from '../src/models/onboarding';
import { createTarGz, readTarGz, sha256Hex, type TarEntry } from '../src/models/project';

const env = process.env;
const URLS = {
  dev: env.DC_E2E_URL_DEV ?? 'http://127.0.0.1:8090',
  alice: env.DC_E2E_URL_ALICE ?? 'http://127.0.0.1:8091',
  bob: env.DC_E2E_URL_BOB ?? 'http://127.0.0.1:8092',
};
const SECRET = env.DC_E2E_JWT_SECRET ?? 'change_me_in_production';
const WORKER_A = { host: 'ssh-provider', port: 2222, username: 'provider', password: 'providerpass' };
const WORKER_B = { host: 'ssh-provider-2', port: 2222, username: 'provider', password: 'providerpass' };
// Unique per run so reruns do not collide with earlier data.
const RUN = Date.now().toString(36);

function jwt(sub: string, secret = SECRET, ttlSeconds = 3600): string {
  const b64 = (o: unknown) => Buffer.from(JSON.stringify(o)).toString('base64url');
  const now = Math.floor(Date.now() / 1000);
  const body = `${b64({ alg: 'HS256', typ: 'JWT' })}.${b64({ sub, iat: now, exp: now + ttlSeconds })}`;
  return `${body}.${createHmac('sha256', secret).update(body).digest('base64url')}`;
}

function gateway(baseUrl: string, token: string | undefined): GatewayApi {
  return new GatewayApi(new ApiClient({ baseUrl, getToken: () => token }));
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function waitFor<T>(what: string, fn: () => Promise<T>, done: (v: T) => boolean, timeoutMs = 180_000): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  let last: T | undefined;
  while (Date.now() < deadline) {
    last = await fn();
    if (done(last)) {
      return last;
    }
    await sleep(1500);
  }
  throw new Error(`timed out waiting for ${what}: ${JSON.stringify(last)}`);
}

const finished = (gw: GatewayApi, id: string) =>
  waitFor(`execution ${id}`, () => gw.getExecution(id), (e) => isTerminal(e.status));

async function addWorker(gw: GatewayApi, w: typeof WORKER_A, name: string) {
  const c = await gw.connectNode(w.host, w.port, w.username);
  expect(c.fingerprint).toMatch(/^SHA256:/);
  const p = await gw.provisionNode(c.onboarding_token, w.password);
  expect(p.detected_cpu).toBeGreaterThan(0);
  expect(p.runtimes.python).toBeTruthy();
  return gw.registerNode({
    provision_id: p.provision_id,
    name,
    allocated_cpu: Math.max(1, Math.min(2, p.detected_cpu)),
    allocated_memory_gb: 1,
  });
}

async function expectApiError(p: Promise<unknown>, status: number, message?: RegExp) {
  try {
    await p;
  } catch (err) {
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(status);
    if (message) {
      expect((err as ApiError).message).toMatch(message);
    }
    return err as ApiError;
  }
  throw new Error(`expected HTTP ${status}`);
}

let dev: GatewayApi;
let alice: GatewayApi;
let bob: GatewayApi;
let aliceNode = '';
let bobNode = '';

beforeAll(async () => {
  for (const url of Object.values(URLS)) {
    await gateway(url, undefined).health();
  }
  const local = await gateway(URLS.dev, undefined).localToken();
  dev = gateway(URLS.dev, local.token);
  alice = gateway(URLS.alice, jwt(`alice-${RUN}`));
  bob = gateway(URLS.bob, jwt(`bob-${RUN}`));
}, 30_000);

afterAll(async () => {
  for (const [gw, id] of [
    [alice, aliceNode],
    [bob, bobNode],
  ] as const) {
    if (id) {
      await gw.deleteNode(id).catch(() => undefined);
    }
  }
}, 120_000);

describe('sign-in and identity', () => {
  it('one-click local sign-in works on the default port', async () => {
    const cs = await dev.clusterStatus();
    expect(cs.nodesTotal).toBeGreaterThanOrEqual(0);
  });

  it('rejects a missing, forged or expired token', async () => {
    await expectApiError(gateway(URLS.alice, undefined).listNodes(), 401);
    await expectApiError(gateway(URLS.alice, jwt('mallory', 'wrong-secret')).listNodes(), 401);
    await expectApiError(gateway(URLS.alice, jwt('alice', SECRET, -60)).listNodes(), 401);
  });

  it('identity follows the token, not the port', async () => {
    const aliceOnBobsPort = gateway(URLS.bob, jwt(`alice-${RUN}`));
    expect((await aliceOnBobsPort.listNodes()).total).toBe((await alice.listNodes()).total);
  });
});

describe('node onboarding and lifecycle', () => {
  it('reports a refused password and an unreachable host with their reasons', async () => {
    const c = await alice.connectNode(WORKER_A.host, WORKER_A.port, WORKER_A.username);
    const err = await expectApiError(alice.provisionNode(c.onboarding_token, 'definitely-wrong'), 400);
    expect(err.kind).not.toBe('auth');
    const unreachable = await expectApiError(alice.connectNode('10.255.255.1', 22, 'nobody'), 502);
    expect(unreachable.message).not.toMatch(/reported an error/);
  }, 120_000);

  it('alice adds worker A and bob adds worker B', async () => {
    aliceNode = (await addWorker(alice, WORKER_A, `alice-${RUN}`)).id;
    bobNode = (await addWorker(bob, WORKER_B, `bob-${RUN}`)).id;
    expect((await alice.listNodes()).items.map((n) => n.id)).toEqual([aliceNode]);
    expect((await bob.listNodes()).items.map((n) => n.id)).toEqual([bobNode]);
  }, 240_000);

  it('users cannot see or touch each other’s nodes', async () => {
    await expectApiError(bob.getNode(aliceNode), 404);
    await expectApiError(bob.deleteNode(aliceNode), 404);
    await expectApiError(bob.setNodeStatus(aliceNode, 'draining'), 404);
    expect((await dev.listNodes()).items.map((n) => n.id)).not.toContain(aliceNode);
  });

  it('tests, re-detects, re-slices and rotates the key', async () => {
    expect((await alice.healthCheckNode(aliceNode)).health_status).toBe('online');
    expect((await alice.refreshNode(aliceNode)).runtimes.python).toBeTruthy();
    expect((await alice.updateNodeSlice(aliceNode, { cpuAllocatable: 1 })).cpuAllocatable).toBe(1);
    await alice.rotateNodeKey(aliceNode);
    expect((await alice.healthCheckNode(aliceNode)).health_status).toBe('online');
  }, 180_000);

  it('a drained node gets no new work, and takes it again after resume', async () => {
    expect((await alice.setNodeStatus(aliceNode, 'draining')).status).toBe('draining');
    await expectApiError(alice.createExecution({ runtime: 'python', code: 'print(1)' }, `drain-${RUN}`), 400);
    expect((await alice.setNodeStatus(aliceNode, 'online')).status).toBe('online');
  });
});

describe('executions', () => {
  it('lists the runtimes detected on the user’s own nodes', async () => {
    const runtimes = await alice.listRuntimes();
    const py = runtimes.find((r) => r.id === 'python');
    expect(py?.availableNodeIds).toEqual([aliceNode]);
    expect(runtimes.find((r) => r.id === 'bash')?.availableNodeIds).toEqual([aliceNode]);
  });

  it('runs one task per input in parallel, in Python and Bash', async () => {
    const py = await alice.createExecution(
      { runtime: 'python', code: 'n = int(input())\nprint(n * n)', inputs: ['1', '2', '3', '4', '5', '6'] },
      `py-${RUN}`,
    );
    const sh = await alice.createExecution(
      { runtime: 'bash', code: 'read n; echo "bash:$((n + 1))"', inputs: ['10', '20'] },
      `sh-${RUN}`,
    );
    expect((await finished(alice, py.id)).status).toBe('completed');
    expect((await finished(alice, sh.id)).status).toBe('completed');
    const out = await alice.executionTasks(py.id);
    expect(out.tasks.map((t) => t.stdout?.trim())).toEqual(['1', '4', '9', '16', '25', '36']);
    expect((await alice.executionTasks(sh.id)).tasks.map((t) => t.stdout?.trim())).toEqual(['bash:11', 'bash:21']);
    expect(formatTaskLog(out)).toContain('Task 6 · completed');
  }, 240_000);

  it('spreads tasks over two workers and runs them at the same time', async () => {
    // Alice lends herself worker B too (her own key, independent of bob's).
    const second = (await addWorker(alice, WORKER_B, `alice-b-${RUN}`)).id;
    try {
      await alice.updateNodeSlice(aliceNode, { cpuAllocatable: 2 });
      const started = Date.now();
      const e = await alice.createExecution(
        { runtime: 'python', code: 'import time\ntime.sleep(4)\nprint(input())', inputs: ['a', 'b', 'c', 'd'] },
        `par-${RUN}`,
      );
      expect((await finished(alice, e.id)).status).toBe('completed');
      const elapsed = Date.now() - started;
      const tasks = (await alice.executionTasks(e.id)).tasks as Array<{ node_id?: string | null }>;
      expect(new Set(tasks.map((t) => t.node_id)).size).toBe(2);
      // Four 4-second tasks, one after another, would take 16 s or more.
      expect(elapsed).toBeLessThan(14_000);
    } finally {
      await alice.deleteNode(second);
    }
  }, 240_000);

  it('reports partial failure and retries only the failed tasks', async () => {
    const code = 'import sys\nn = int(input())\nif n % 2: sys.exit("odd " + str(n))\nprint("even", n)';
    const e = await alice.createExecution({ runtime: 'python', code, inputs: ['1', '2', '3', '4'] }, `mix-${RUN}`);
    const done = await finished(alice, e.id);
    expect(done.status as ExecutionStatus).toBe('completed_with_errors');
    expect(done.failed).toBe(2);
    const tasks = (await alice.executionTasks(e.id)).tasks;
    expect(tasks.find((t) => t.seq === 0)?.stderr).toContain('odd 1');

    const retry = await alice.retryExecution(e.id, true);
    const again = await finished(alice, retry.id);
    expect(again.total).toBe(2);
    expect(again.failed).toBe(2);
    await expectApiError(bob.retryExecution(e.id, false), 404);
  }, 240_000);

  it('cancels a running execution', async () => {
    const e = await alice.createExecution(
      { runtime: 'python', code: 'import time\ntime.sleep(120)', inputs: ['a', 'b'], timeoutSeconds: 300 },
      `cancel-${RUN}`,
    );
    await waitFor('running', () => alice.getExecution(e.id), (x) => x.status === 'running', 60_000);
    await alice.cancelExecution(e.id);
    const end = await finished(alice, e.id);
    expect(end.status).toBe('cancelled');
  }, 180_000);

  it('enforces the timeout', async () => {
    const e = await alice.createExecution(
      { runtime: 'python', code: 'import time\ntime.sleep(60)', timeoutSeconds: 3 },
      `timeout-${RUN}`,
    );
    const end = await finished(alice, e.id);
    expect(['failed', 'completed_with_errors']).toContain(end.status);
  }, 120_000);

  it('keeps executions private to their owner', async () => {
    const mine: Execution[] = (await alice.listExecutions()).items;
    expect(mine.length).toBeGreaterThan(0);
    const bobs = (await bob.listExecutions()).items.map((x) => x.id);
    for (const e of mine) {
      expect(bobs).not.toContain(e.id);
    }
    await expectApiError(bob.getExecution(mine[0].id), 404);
    await expectApiError(bob.executionTasks(mine[0].id), 404);
  });
});

describe('training', () => {
  let dataset = '';

  it('uploads a dataset the other user cannot see', async () => {
    const rows = ['x1,x2,label'];
    for (let i = 0; i < 200; i++) {
      const a = (i % 17) / 17;
      const b = (i % 11) / 11;
      rows.push(`${a.toFixed(3)},${b.toFixed(3)},${a + b > 1 ? 1 : 0}`);
    }
    const d = await alice.uploadDataset(`e2e-${RUN}`, `e2e-${RUN}.csv`, Buffer.from(rows.join('\n') + '\n'));
    dataset = d.id;
    expect((await alice.listDatasets()).map((x) => x.id)).toContain(dataset);
    expect((await bob.listDatasets()).map((x) => x.id)).not.toContain(dataset);
  }, 120_000);

  it('trains a built-in model across the worker and downloads ONNX', async () => {
    const run = await alice.submitTraining(
      {
        dataset_id: dataset, model_type: 'logistic_regression', engine: 'python', target_column: 'label',
        feature_columns: [], mode: 'multi', learning_rate: 0.5, epochs: 5, rounds: 2,
        execution_policy: 'safe_background', node_ids: [], model_params: {}, param_grid: {},
      },
      `train-${RUN}`,
    );
    const done = await waitFor('training', () => alice.getTrainingRun(run.id), (r) => ['completed', 'failed'].includes(r.status), 300_000);
    expect(done.status, done.message ?? '').toBe('completed');
    expect(done.metrics).toBeTruthy();
    const model = await alice.downloadModel(run.id);
    expect(model.filename).toMatch(/\.onnx$/);
    expect(model.bytes.length).toBeGreaterThan(50);
    await expectApiError(bob.getTrainingRun(run.id), 404);
  }, 360_000);

  it('trains the user’s own model.py on the worker', async () => {
    const code = [
      'def init(config):',
      '    return {"w": 0.0}',
      'def train(params, rows, config):',
      '    w = params["w"] + sum(r[0] for r in rows) / max(1, len(rows))',
      '    return {"w": w}',
    ].join('\n');
    const run = await alice.submitTraining(
      {
        dataset_id: dataset, model_type: 'custom', engine: 'python', target_column: 'label', feature_columns: [],
        mode: 'multi', learning_rate: 0.01, epochs: 1, rounds: 2, execution_policy: 'safe_background',
        node_ids: [], model_code: code, model_params: {}, param_grid: {},
      },
      `custom-${RUN}`,
    );
    const done = await waitFor('custom training', () => alice.getTrainingRun(run.id), (r) => ['completed', 'failed'].includes(r.status), 300_000);
    expect(done.status, done.message ?? '').toBe('completed');
    expect((await alice.downloadModel(run.id)).filename).toMatch(/\.json$/);
    await alice.deleteTrainingRun(run.id);
    await expectApiError(alice.getTrainingRun(run.id), 404);
  }, 360_000);
});

describe('projects (a folder with modules, requirements and outputs)', () => {
  const file = (path: string, text: string): TarEntry => ({ path, data: Buffer.from(text) });
  const PROJECT = [
    file('train.py', [
      'import json, os, sys',
      'from tabulate import tabulate',
      'from lib.scale import scale',
      'lr = float(sys.stdin.read().strip() or "0.1")',
      'rows = [r.split(",") for r in open(os.path.join(os.environ["DC_DATA_DIR"], "points.csv")).read().split()[1:]]',
      'loss = sum((float(y) - scale(float(x), lr)) ** 2 for x, y in rows)',
      'os.makedirs(os.environ["DC_OUTPUT_DIR"], exist_ok=True)',
      'json.dump({"lr": lr, "loss": round(loss, 4), "task": os.environ["DC_TASK_INDEX"]},',
      '          open(os.path.join(os.environ["DC_OUTPUT_DIR"], "metrics.json"), "w"))',
      'open(os.path.join(os.environ["DC_OUTPUT_DIR"], "model.bin"), "wb").write(bytes(range(256)))',
      'print(tabulate([["lr", lr], ["loss", round(loss, 4)]]))',
    ].join('\n') + '\n'),
    file('lib/__init__.py', ''),
    file('lib/scale.py', 'def scale(x, lr):\n    return x * lr * 10\n'),
    file('requirements.txt', 'tabulate==0.9.0\n'),
  ];
  const archive = createTarGz(PROJECT);
  const bundleId = sha256Hex(archive);
  let dataset = '';
  let runId = '';

  it('uploads the folder once, privately', async () => {
    expect(await alice.hasBundle(bundleId)).toBe(false);
    expect((await alice.uploadBundle(archive)).id).toBe(bundleId);
    expect(await alice.hasBundle(bundleId)).toBe(true);
    // Another user, on another port, cannot see or run it.
    expect(await bob.hasBundle(bundleId)).toBe(false);
    await expectApiError(
      bob.createExecution({ runtime: 'project', project: { bundleId, command: 'python3 train.py' } }, `steal-${RUN}`),
      400,
      /Unknown bundle_id/,
    );
    const csv = 'x,y\n1,1\n2,2\n3,3\n';
    dataset = (await alice.uploadDataset(`points-${RUN}`, 'points.csv', Buffer.from(csv))).id;
  }, 120_000);

  it('runs the folder once per input, with its requirements and a dataset, and brings back outputs', async () => {
    const exec = await alice.createExecution(
      {
        runtime: 'project',
        inputs: ['0.1', '0.05', '0.2'],
        project: {
          bundleId,
          command: 'python3 train.py',
          requirements: 'requirements.txt',
          datasets: [{ datasetId: dataset, path: 'data/points.csv' }],
        },
      },
      `project-${RUN}`,
    );
    runId = exec.id;
    expect(exec.runtime).toBe('project');
    expect(exec.project?.command).toBe('python3 train.py');
    const done = await waitFor(`project ${exec.id}`, () => alice.getExecution(exec.id), (e) => isTerminal(e.status), 600_000);
    const tasks = await alice.executionTasks(exec.id);
    expect(done.status, formatTaskLog(tasks)).toBe('completed');
    for (const t of tasks.tasks) {
      expect(t.stdout).toMatch(/lr\s+0\.\d+/);
      expect(t.stderr).toMatch(/\[dc\] environment: built with (pip|uv)/);
      expect(t.output_files).toBe(2);
      const out = readTarGz(await alice.downloadTaskOutputs(exec.id, t.id));
      expect(out.map((e) => e.path).sort()).toEqual(['outputs/metrics.json', 'outputs/model.bin']);
      const metrics = JSON.parse(Buffer.from(out.find((e) => e.path === 'outputs/metrics.json')!.data).toString());
      expect(metrics.lr).toBe(Number(['0.1', '0.05', '0.2'][t.seq]));
      expect(metrics.task).toBe(String(t.seq));
      expect([...out.find((e) => e.path === 'outputs/model.bin')!.data].length).toBe(256);
    }
    // Bob, and the local user on the default port, see nothing of it.
    await expectApiError(bob.executionTasks(exec.id), 404);
    await expectApiError(bob.downloadTaskOutputs(exec.id, tasks.tasks[0].id), 404);
    expect((await dev.listExecutions()).items.map((e) => e.id)).not.toContain(exec.id);
  }, 660_000);

  it('reuses a cached environment when the node lends a cache', async () => {
    expect((await alice.updateNodeSlice(aliceNode, { envCacheGb: 5 })).envCacheGb).toBe(5);
    const run = async (tag: string) => {
      const e = await alice.createExecution(
        { runtime: 'project', nodeIds: [aliceNode], project: { bundleId, command: 'python3 -c "import tabulate; print(tabulate.__version__)"', requirements: 'requirements.txt', outputs: [] } },
        `cache-${tag}-${RUN}`,
      );
      await waitFor(`cache run ${tag}`, () => alice.getExecution(e.id), (x) => isTerminal(x.status), 600_000);
      return (await alice.executionTasks(e.id)).tasks[0];
    };
    const first = await run('1');
    expect(first.status, first.stderr ?? '').toBe('completed');
    expect(first.stderr).toMatch(/cached on this node/);
    const second = await run('2');
    expect(second.stdout?.trim()).toBe('0.9.0');
    expect(second.stderr).toMatch(/reused the cached one/);
  }, 900_000);

  it('reports a broken requirements file as a task failure with pip’s message', async () => {
    const bad = createTarGz([...PROJECT.filter((f) => f.path !== 'requirements.txt'),
      file('requirements.txt', 'this-package-does-not-exist-dc-e2e==1.0\n')]);
    await alice.uploadBundle(bad);
    const e = await alice.createExecution(
      { runtime: 'project', project: { bundleId: sha256Hex(bad), command: 'python3 train.py', requirements: 'requirements.txt' } },
      `badreq-${RUN}`,
    );
    const done = await waitFor('bad requirements', () => alice.getExecution(e.id), (x) => isTerminal(x.status), 600_000);
    const t = (await alice.executionTasks(e.id)).tasks[0];
    // The task failed; the batch itself ran, so it ends "completed with errors".
    expect(done.status).toBe('completed_with_errors');
    expect(done.failed).toBe(1);
    expect(t.stderr).toMatch(/\[dc\] Building the environment failed/);
    expect(t.stderr).toMatch(/this-package-does-not-exist-dc-e2e/);
    expect(t.attempts, 'a broken requirements file is not a node failure').toBe(1);
  }, 660_000);

  it('runs a project again with Retry', async () => {
    const again = await alice.retryExecution(runId, false);
    const done = await waitFor('project retry', () => alice.getExecution(again.id), (x) => isTerminal(x.status), 600_000);
    expect(done.status).toBe('completed');
    expect(done.project?.bundleId).toBe(bundleId);
  }, 660_000);
});

describe('removal', () => {
  it('removes the nodes and their keys from the workers', async () => {
    for (const [gw, id] of [
      [alice, aliceNode],
      [bob, bobNode],
    ] as const) {
      const r = await gw.deleteNode(id);
      expect(r.deleted).toBe(true);
      expect(r.key_removed).toBe(true);
    }
    aliceNode = '';
    bobNode = '';
    expect((await alice.listNodes()).total).toBe(0);
  }, 120_000);
});

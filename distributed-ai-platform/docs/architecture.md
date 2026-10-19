# Architecture

How the platform is built, as of the `development` branch (October 2026). The
request and response shapes live in [API_CONTRACT.md](API_CONTRACT.md). Ports
and protocols are in [connections.md](connections.md).

## Contents

1. [Summary](#1-summary)
2. [Design rules](#2-design-rules)
3. [Components](#3-components)
4. [Worker onboarding (password once, managed key after)](#4-worker-onboarding)
5. [Task Executor (any language, sandboxed)](#5-task-executor)
6. [Resource slices, leases and execution policy](#6-resource-slices-leases-and-execution-policy)
7. [Distributed training](#7-distributed-training)
8. [Data and storage](#8-data-and-storage)
9. [Security](#9-security)
10. [Editor control plane (VS Code)](#10-editor-control-plane)
11. [Deployment, configuration and CI](#11-deployment-configuration-and-ci)
12. [Testing and verification](#12-testing-and-verification)
13. [Honest limits](#13-honest-limits)

---

## 1. Summary

Users lend spare CPU (and optionally a slice of a GPU) from their own machines
to a **master**. A worker needs only a running `sshd` and an existing
`python3`. **Nothing is ever installed on a worker**, and every job runs in a
throwaway sandbox directory that is deleted afterwards.

The master runs four services in Docker Compose:

| Service | Port | Language | Role |
|---|---|---|---|
| `api-gateway` | 8000 | Python/FastAPI | Reverse proxy for `/api/v1/storage/*` and `/api/v1/ssh/*` |
| `storage-service` | 8001 | Python/FastAPI | Datasets: files in the `datasets` volume (or HDFS), metadata in Postgres, owner-scoped |
| `ssh-executor` | 8011 | Python/FastAPI | Workers, Task Executor, training, leases. Talks to workers over SSH |
| `extension-gateway` | 8090 | Go | REST facade for the VS Code extension; forwards to ssh-executor |

Backing systems: PostgreSQL 15 and a Docker volume for dataset files. HDFS
(1 NameNode + 3 DataNodes) is optional, behind the Compose `hdfs` profile. Two
throwaway SSH provider containers exist behind the Compose `dev` profile for
local end-to-end tests.

There is **no Livy, MLflow, Kafka, scheduler, worker registry, resource
manager, worker agent, preprocessing, evaluation, registry or serving
service**. Training is pure Python by default; Apache Spark + Ray is an
optional engine installed on the master only on request (section 7). Worker
nodes never run Spark or Ray.

```
Browser (React, Vite :3000) ──/api──▶ api-gateway :8000 ──▶ storage-service :8001 ──▶ /data + Postgres
                                                     └────▶ ssh-executor   :8011 ──▶ Postgres
VS Code extension ──REST──▶ extension-gateway :8090 ─────────▶ ssh-executor   :8011
                                                                    │
                                    SSH (managed Ed25519 key, pinned host key)
                                                                    ▼
                                              worker nodes: sshd + python3 only
                                              (Linux, macOS; Windows unverified)
```

## 2. Design rules

- **Zero install on workers.** A runtime is offered only where it is detected.
  The GPU memory cap only works where PyTorch is already present. Spark/Ray
  are never staged.
- **No residue.** Each execution gets `dp-exec.<execution_id>.XXXXXX` under the
  worker's temp directory. It holds the code, the input, the runner and any
  build output, and it is removed on success, error, timeout and cancel. A
  stale-sandbox sweep removes leftovers from master crashes.
- **Password once.** The worker password is used for one request to install a
  restricted key the master generated. It is never stored or logged.
- **The slice is a ceiling.** Cores, thread pools and the GPU slice never
  exceed what the owner lent, under every execution policy.
- **Honest reporting.** Unknown values stay unknown. Unverified paths (Windows)
  are labelled as such.

## 3. Components

### 3.1 api-gateway (`services/api-gateway`)
A small FastAPI proxy (`routes.py`, `proxy.py`). It forwards
`/api/v1/storage/{path}` to storage-service and `/api/v1/ssh/{path}` to
ssh-executor unchanged, including `Authorization`. Each downstream service
validates the JWT itself. It also serves `/health` and `/metrics`.

### 3.2 storage-service (`services/storage-service`)
Endpoints: `POST /datasets` (CSV/JSON), `POST /datasets/images` (a ZIP of
`<label>/<image>` folders, converted with Pillow into a flattened grayscale
pixel CSV), `GET /datasets`, `GET /datasets/{id}`,
`GET /datasets/{id}/download`, `DELETE /datasets/{id}`. Bytes go to a store
(`blob_store.py`): `LocalFileStore` writes `raw/<id>.<ext>` under `/data`
(temporary file, fsync, rename, so a failed upload never leaves a partial
file); `WebHdfsStore` writes to HDFS over WebHDFS when
`STORAGE_BACKEND=hdfs`. Each row records its store in `storage_backend`, so
both are read side by side; `hdfs_path` keeps its name but holds the key in
either store. Uploads and downloads are streamed in 1 MiB chunks. Errors: 413
over `MAX_UPLOAD_SIZE_BYTES`, 507 below `MIN_FREE_DISK_BYTES` free, 503 for a
dataset in HDFS while HDFS is off. `migrate_datasets.py` (`make
migrate-datasets`) moves HDFS datasets to local files. Metadata goes to the
`datasets` table. Every read
and delete is scoped to `created_by == jwt.sub`. ssh-executor reads on behalf
of a run owner with an internal token plus `X-On-Behalf-Of` (contract §5).

### 3.3 ssh-executor (`services/ssh-executor`)
The master-side controller. Its modules:

| Module | Responsibility |
|---|---|
| `routes_nodes.py`, `node_service.py`, `key_manager.py`, `key_install.py` | Onboarding (connect → provision → register), manual-key fallback, lifecycle (refresh, health check, rotate key, re-secure, delete) |
| `ssh_transport.py` | paramiko transport with host-key pinning, exec with stdin, SFTP put |
| `capabilities.py`, `os_adapters/` | OS, CPU, memory, GPU, `gpu_runtime` (torch already present), login-shell `PATH` and runtime detection in one round trip |
| `runtimes.py`, `validation.py` | Runtime registry (29 languages + `custom`) and request validation |
| `task_executor.py`, `os_adapters/base.py` | The sandbox: create, upload, run through the runner, cancel, clean up, sweep |
| `routes_batches.py`, `batch_service.py`, `dispatcher.py` | Task batches: persistence, fan-out, retries, cancel |
| `lease_service.py`, `lease_logic.py`, `execution_manager.py`, `lease_recovery.py` | Atomic leases, slot/core/GPU-slice planning, recovery loop |
| `resource_spec.py` | `ResourceSpec` (cores, memory, GPU slice, nice) and policy → nice mapping |
| `routes_train.py`, `training_orchestrator.py`, `ml_templates.py`, `onnx_export.py` | Distributed training, FedAvg, ONNX export |
| `security.py`, `crypto.py` | Refuse placeholder secrets outside development; Fernet encryption of stored keys |

`ssh_client.py` is legacy and no service imports it. Only the unit tests still
load it.

### 3.4 extension-gateway (`services/extension-gateway`, Go)
`GET /health`, `GET /cluster/status`, `GET /nodes`, `GET/PATCH /nodes/{id}`,
`GET /runtimes`, `GET/POST /executions`, `GET /executions/{id}`,
`POST /executions/{id}/cancel`. With `UPSTREAM_URL` set (as in Compose), its
`UpstreamStore` calls ssh-executor with the **caller's own JWT**, so results stay
owner-scoped. It maps snake_case to the extension's camelCase. An execution is
a task batch. Without `UPSTREAM_URL` it serves an empty in-memory store, which
is for local development only.

### 3.5 frontend (`frontend`, React + Vite + Tailwind)
Three pages:
- `/ssh`: add a worker (host, port, username, password; fingerprint
  confirmation; manual-key fallback), list nodes, edit the CPU/memory/GPU
  slice, refresh, rotate key, re-secure legacy nodes, delete.
- `/execute`: the Task Executor. Pick a language from `GET /runtimes`, which
  shows only languages detected on your nodes, or use `custom`. Pick nodes,
  cores per task, timeout and execution policy. Run one task per input, watch
  per-task output, cancel. The batch header shows the policy it ran with.
- `/train`: upload a CSV or image ZIP, then train linear regression, logistic
  regression or a small CNN in `single` (master processes) or `multi` (workers)
  mode. Download the ONNX model or the run data as JSON.

### 3.6 shared library (`shared/`)
JWT auth (`get_current_user`, `create_internal_token`), settings, logging,
request-ID, metrics middleware, exception types, and the async SQLAlchemy
`DatabaseManager` with `create_all` plus a short list of idempotent
`ADD COLUMN IF NOT EXISTS` statements.

## 4. Worker onboarding

1. **Connect** `POST /nodes/connect {host, port, username}`. The master reads
   the host key without authenticating and returns its SHA256 fingerprint and
   a signed onboarding token (host, port, username, host key, owner).
   Targets are checked against `allowed_hosts` and `allowed_cidrs`; loopback and
   link-local addresses are blocked by default (SSRF guard).
2. **Provision** `POST /nodes/provision {onboarding_token, password}`. The
   master logs in with the password over the pinned host key and requires
   `python3`. It generates an Ed25519 pair, appends
   `restrict ssh-ed25519 AAAA… dp-platform:<provision_id>` to the right
   authorized-keys file, reconnects with the key only, and detects
   capabilities. The encrypted private key waits in `pending_provisions` (15
   min). The password exists only in that request's memory.
   - Password login disabled on the worker → 409 `password_auth_disabled`. The
     UI then shows the public key and a one-line install command
     (`/provision/manual`, then `/provision/{id}/verify`).
   - Host key changed → 409 `host_key_mismatch`. Wrong password → 401.
3. **Register** `POST /nodes {provision_id, name, allocated_*}`. The pending
   row becomes an `SSHNode` (`auth_type="managed_key"`, `id == provision_id`).
   The allocation is clamped to what was detected.

Lifecycle: `DELETE` removes our authorized-keys line and then the row (if the
node is unreachable, the row goes and the response says the line remains).
`rotate-key` installs and verifies a new key, then removes the old line.
`resecure` converts legacy password or private-key nodes to a managed key and
wipes the old secret.

On Windows, keys for members of Administrators go to
`C:\ProgramData\ssh\administrators_authorized_keys` with restricted ACLs. This
path is written but **not verified on a real Windows host**.

## 5. Task Executor

### 5.1 Runtimes
`runtimes.py` defines each language's source filename, the tool names to probe,
a version command and the run command. Compiled languages build inside the
sandbox (`go run`, `rustc && ./prog`, `cc && ./prog`, single-file `java`). Tool
and module caches go into the sandbox through `HOME`, `TMPDIR` and
per-runtime variables (`GOCACHE`, `CARGO_HOME`, `npm_config_cache`, …).
`custom` takes a filename and a command template containing `{file}`, so any
language works.

Detection runs one shell script under the node's login-shell `PATH`. It
reports a runtime when **any** of its tool names exists (`node`/`nodejs`,
`lua`/`lua5.4`/`lua5.3`, `cc`/`gcc`/`clang`, …). At run time the command
resolves the same names in the same order on the worker, so a runtime that is
detected also runs. `POST /batches` returns 400 naming the runtime when no
selected online node has it.

### 5.2 One execution, step by step
```
mkdir -p /tmp && mktemp -d /tmp/dp-exec.<execution_id>.XXXXXX
upload  task.<ext>, input.txt (also fed on stdin), dp_runner.py
        (+ dp_gpu_preamble.py for a GPU-sliced Python task)
python3 dp_runner.py <sandbox> <timeout> <env-json> \
   "taskset -c <slot cores> nice -n <policy> /bin/sh -c '<run command>'"
capture stdout / stderr / exit code (output capped at max_output_bytes)
finally: rm -rf <sandbox>
```
The runner (`SANDBOX_RUNNER_SOURCE`, POSIX only, stdlib):
- runs the command with a clean environment rooted in the sandbox, with
  thread pools capped to the slot's cores and `CUDA_VISIBLE_DEVICES` set by the
  GPU slice (empty when there is no enforceable slice);
- starts it in its own session and process group and writes the group id to
  `runner.pgid`;
- enforces the wall-clock timeout (exit 124, `[task timed out]` once in stderr);
- kills the process group afterwards, then, as a best effort, any process
  that left the group but still lives in the sandbox (Linux: cwd in the sandbox
  or `DP_SANDBOX=<sandbox>` in its environment; macOS: cwd via `lsof`).

**Cancel** (`POST /batches/{id}/cancel`) sets `cancelling`. The dispatcher's
watcher calls `TaskExecutor.cancel(execution_id)`, which finds
`dp-exec.<execution_id>.*`, signals the recorded process group (TERM, then
KILL) and removes the sandbox. This works from any SSH connection. The
remaining tasks end `cancelled`.

On Windows, a PowerShell script does the same with `%TEMP%\dp-exec.<id>.<rand>`,
`Start-Process` and `taskkill /T /F`. It is **unverified on real hardware**,
and it has no thread or GPU environment yet.

### 5.3 Projects (a folder instead of a file)
```
client: pack the folder (.tar.gz, sorted, no timestamps) → GET /bundles/<sha256>
        → POST /bundles only when missing → POST /batches {project: {bundle_id, command, …}}
master: bundle stored at project_files:/bundles/<owner hash>/<sha256>.tar.gz
per task, in the usual sandbox:
  upload dc_bundle.tar.gz, datasets, dc_project.py, dp_runner.py, input.txt
  python3 dc_project.py prepare   unpack into project/ (paths checked), place datasets
  python3 dc_project.py env       venv from requirements (uv or venv + pip), in the sandbox
                                  or, with env_cache_gb, cached under ~/.cache/distributed-compute
  dp_runner.py "cd project && <command>"   (same limits, timeout, cancel as any task)
  python3 dc_project.py pack      tar the output folders (also after failure or timeout)
  SFTP get dc_outputs.tar.gz → project_files:/outputs/<task id>.tar.gz
finally: rm -rf <sandbox>
```
`dc_project.py` is standard library only, like the runner. Environment builds
in the cache are serialised per environment with `flock`, so parallel tasks on
one node build it once. A failed unpack or build is reported as a failed task
(exit code set), not as a node failure, so it is not retried elsewhere.
Projects run on Linux and macOS workers; Windows workers are refused at submit
time with a clear message.

### 5.4 Dispatch
`dispatch_batch` selects the owner's online nodes (or the chosen subset) that
have the runtime. It reserves a lease on each one atomically and fans out with
`run_across_nodes`. Each node gets `lease.cpu_allocated // cpu_per_task` slots.
Each slot owns one SSH connection, a distinct core slice and its share of
memory and GPU. A task is retried on another node (at most `min(nodes, 3)`
attempts) only for infrastructure failures. A non-zero exit is a finished
`failed` task. Leases are released in `finally`. The first slot on each node
also sweeps sandboxes older than `stale_sandbox_minutes`.

## 6. Resource slices, leases and execution policy

- **Slice.** Each node lends `allocated_cpu`, `allocated_memory_gb` and
  optionally `allocated_gpu_count` and `allocated_gpu_memory_gb` (per device).
  All are clamped to the detected hardware.
- **Leases** are internal. `reserve` locks the node row (`SELECT … FOR UPDATE`),
  sums the active leases and inserts the new one, so concurrent jobs never
  overcommit a node. A heartbeat extends long jobs. `lease_recovery` runs at
  startup and every `recovery_interval_seconds` (60 s). It expires stale leases
  and fails batches and runs that were orphaned by a crash.
- **Cores.** On Linux each slot is pinned with `taskset -c` when `taskset`
  exists. `OMP/MKL/OPENBLAS/NUMEXPR_NUM_THREADS` are always set to the slot's
  cores on POSIX.
- **GPU slicing.** This is enforced only for the `python` runtime on nodes where
  PyTorch with CUDA or MPS is **already installed** (`gpu_runtime`). The task
  sees devices `0..count-1` and is launched as
  `python3 -I -B dp_gpu_preamble.py task.py`. The preamble calls
  `torch.cuda.set_per_process_memory_fraction` (or the MPS equivalent) with the
  slot's fraction, then runs the task. This is PyTorch's caching-allocator
  limit: best effort, and it binds torch only. In every other case
  `CUDA_VISIBLE_DEVICES=""`.
- **Execution policy** (`safe_background` = nice 19, the default; `balanced` =
  nice 10; `max_available` = nice 0). It applies to batches (stored on the
  batch and shown in the UI) and to training runs (passed to the run; not
  stored on the run row). It changes priority only.
- **Memory** is a scheduling figure. No kernel limit (cgroup or rlimit) is
  applied. `resource_enforcement` is reported as `best_effort`.

## 7. Distributed training

`POST /train` takes a `model_type` from three families and an `engine`.

| Family | `model_type` | Trains where | Engines |
|---|---|---|---|
| Built-in | `linear_regression`, `logistic_regression`, `cnn` | workers (`multi`) or the master's cores (`single`) | `python`, `spark_ray` |
| Custom | `custom` + `model_code` (the user's `model.py`) | workers only (`multi`) | `python`, `spark_ray` |
| Spark MLlib | `spark:<Estimator>`, any of `GET /engines/spark_ray/models` | Spark on the master (or `SPARK_MASTER_URL`) | `spark_ray` |

**Engines** (`ml/engines.py`, `GET /engines`). `python` is always available.
`spark_ray` needs pyspark, ray and Java on the master; it is detected, never
installed by the service. The image adds it only when built with
`WITH_SPARK_RAY=true` (`make enable-spark-ray`, or the VS Code command). A
master run outside Docker with its own pyspark/ray/Java is detected the same
way. Asking for `spark_ray` when it is missing returns 400; nothing silently
falls back. Worker nodes never run Spark or Ray.

**Built-in and custom models** (federated averaging):

1. The master downloads the dataset on behalf of the run owner. `python` parses
   CSV/JSON lines itself. `spark_ray` reads CSV, JSON or **Parquet** with Spark,
   casts and drops incomplete rows, and keeps a **uniform random sample** when
   the data exceeds `max_train_rows` (the Python reader keeps the first rows).
2. **single** (built-in only): shards train in a `ProcessPoolExecutor`
   (`python`) or as **Ray tasks** (`spark_ray`), running the same stdlib code
   the workers run.
3. **multi**: every selected node is leased (`cpu_per_task=1`). Shard sizes
   follow a **compute score**, `gpu_mem_gb × shard_weight_gpu + cores ×
   shard_weight_cpu`, with largest-remainder rounding and interleaved rows. The
   GPU term counts only for jobs that use the GPU: never for the built-in
   trainer, and for custom models when the request sets `uses_gpu`.
4. Each round the master averages the shard parameters weighted by row count
   (`ml/params.average_params`, any nested JSON structure of numbers; values
   identical on every shard, such as layer sizes, are kept). With `spark_ray`
   a **Ray parameter-server actor** holds the global model for the run and
   applies every round's update. Both paths produce identical models.
5. Built-in runs export ONNX; custom runs export `params.json`.

**Custom models** (`ml/custom_model.py`). The user's `model.py` defines
`train(params, rows, config)` and optionally `init(config)` and
`evaluate(params, rows, config)`. A generated runner loads it in the normal
task sandbox and prints one `__DP_RESULT__ {json}` line, so the user's own
prints cannot break parsing. Before data is sent, a `check` task on every node
imports the file, so a missing package fails early naming the node. `init`
runs once on one worker; each round's tasks first evaluate the incoming global
model on their shard, so metrics need no extra round trip. **None of the
user's code runs on the master**, which holds `SSH_CRED_KEY` and every node key.

**Spark MLlib models** (`ml/spark_ml.py`). Estimators are discovered from
`pyspark.ml.classification/regression/clustering` at runtime (18 with Spark
3.5.1; `OneVsRest`, `AFTSurvivalRegression` and `LDA` are excluded because a
features + target table cannot feed them). `model_params` sets estimator
parameters; `param_grid` runs Spark's `TrainValidationSplit` and keeps the
best. Labels are indexed outside the saved pipeline, so the downloaded zipped
`PipelineModel` predicts from the feature columns alone and adds
`predicted_label` with the original class names. Metrics are computed on a
held-out `test_fraction`; details include feature importances, coefficients
and the grid-search candidates.

## 8. Data and storage

- **Dataset files**: the `datasets` volume, mounted at `/data` in
  storage-service (runs as uid 10001). `make backup` archives it with a
  database dump.
- **HDFS (optional)**: dataset bytes over WebHDFS (`namenode:9870`) with
  `make up-hdfs`. Replication is configured in `infrastructure/docker/hadoop`.
- **PostgreSQL**: `datasets` (storage-service); `ssh_nodes`,
  `pending_provisions`, `resource_leases`, `task_batches`, `tasks`,
  `training_runs` (ssh-executor). The schema comes from `create_all` plus
  idempotent `ADD COLUMN IF NOT EXISTS` statements (rows from before 1.0.1
  get `storage_backend = 'hdfs'`).
  Columns that the list does not cover need a fresh database (`make reset` drops
  the volumes) or a manual `ALTER`.
- **Project files** (ssh-executor): the `project_files` volume at
  `/var/lib/distributed-compute`: `bundles/` (per owner, by hash), `datasets/`
  (copies sent to project tasks) and `outputs/` (one archive per task;
  included in `make backup`).
- **Workers** keep nothing: their sandboxes are deleted, and the only
  persistent change is one `dp-platform:<id>` line in authorized keys. The
  exception is opt-in: a node with an environment cache keeps project
  environments in `~/.cache/distributed-compute`, removed with the node.

## 9. Security

- **JWT (HS256)**, shared `JWT_SECRET`. With `ENVIRONMENT=production` every
  request needs a token. Otherwise anonymous calls run as `dev-user`, and the
  extension gateway serves `POST /auth/local-token`, which the VS Code
  extension calls to sign in as `dev-user` without a pasted token.
  `generate_token.py` mints tokens for production mode.
- **Secrets**: with any `ENVIRONMENT` other than `development`, services refuse
  to start if `JWT_SECRET` or `SSH_CRED_KEY` is a placeholder or shorter than
  32 characters (`security.py`).
- **SSH**: host keys are pinned at onboarding, and a mismatch is rejected.
  Managed keys are Ed25519, restricted with `restrict`, encrypted at rest
  (Fernet, key from `SSH_CRED_KEY`), and the comment identifies the node. The
  onboarding password is never persisted or logged.
- **Ownership**: nodes, batches, runs and datasets are scoped to `jwt.sub`.
  The Go gateway forwards the user's token and has no identity of its own.
- **Use HTTPS** in front of the gateway for any non-local deployment, because
  the onboarding password travels in a request body.
- **Sandbox**: see §13. Register a dedicated non-admin account on each worker.

## 10. Editor control plane

`distributed-compute-vscode` is a TypeScript client of the extension-gateway.
It stores the token in SecretStorage and shows nodes, the cluster, jobs and
executions. It can edit a node's slice and submit the active file as an
execution: as Python, with the default runtime, or with a runtime picked from
`GET /runtimes` (only runtimes detected on online nodes are offered). The
runtime is any registry id.

Training: **Submit Training Job** walks through dataset (or uploading a local
file), engine, model (built-in, the user's `model.py`, or any Spark MLlib
model), target column and parameters; **Run Training Again** repeats the
`training` block saved in `distributed.json`; the **Training Runs** view shows
round progress, metrics and what ran, and downloads the model. Choosing Spark +
Ray on a master running on the same machine offers to install it
(`docker compose up -d --build ssh-executor` with `WITH_SPARK_RAY=true`).
The gateway passes training, engine and dataset calls through to ssh-executor
and storage-service unchanged.

Everything else works from the editor too, with no browser: adding a node
(host key check, one-time password or a manual key command, detection, slice),
testing, re-detecting, rotating its key, draining/resuming and removing it;
running a file as many parallel tasks; live per-task output; cancel; retry
(all or only failed tasks); and saving results into the workspace.

## 11. Deployment, configuration and CI

- `make up` (core stack), `make up-dev` (plus two SSH providers, login
  `provider/providerpass`, dev only), `make down` (keeps data), `make reset` (drops volumes), `make up-hdfs`
  (HDFS storage), `make migrate-datasets`, `make backup`.
- `config/.env` provides `ENVIRONMENT`, `JWT_SECRET`, `SSH_CRED_KEY`,
  `DATABASE_URL` and, for storage, `STORAGE_BACKEND`, `MAX_UPLOAD_SIZE_BYTES`,
  `MIN_FREE_DISK_BYTES`, `HDFS_WEBHDFS_URL` and `HDFS_USER`. ssh-executor settings
  (`config.py`) include timeouts, output and code caps, `allowed_runtimes`,
  `allowed_hosts`/`allowed_cidrs`, lease TTL, the recovery interval, the stale
  sandbox age and the shard weights.
- The ssh-executor image is `python:3.11-slim-bookworm` plus `procps`. Built
  with `WITH_SPARK_RAY=true` it also has Java 17, PySpark 3.5.1 and Ray 2.31
  (about 1 GB). Compose tags the two variants separately and gives the
  container 2 GB of shared memory for Ray.
- CI (`.github/workflows/ci.yml` at the repository root) runs `make lint`, the
  Python unit tests, the engine tests with real Java/PySpark/Ray, the frontend
  tests and build, the extension lint/tests/production build, and `go vet` and
  the Go tests. There is no CD: the platform is meant to run on your own machine.

## 12. Testing and verification

`pytest tests -q` covers onboarding, adapters, capabilities, runtimes, batch
API, leases, the dispatcher, weighted shards, GPU slicing and storage
ownership. `tests/unit/services/test_task_executor_local.py` runs the real
POSIX sandbox runner on the development host through a local transport. It
tests cancel by execution id, a setsid double-fork escape, a single timeout
marker, and runtimes found under alternative tool names (`nodejs`, `lua5.4`).
The VS Code extension has vitest tests (`npm test`). The Go gateway has
`go test ./...`.

What has **not** been verified: Windows workers (any part), physical remote
machines (onboarding, sandbox, cancel and training over a real network), and a
full run against the Docker dev providers during this change set.

## 13. Honest limits

- The sandbox isolates **files and processes, not the kernel**. Code runs with
  the SSH user's permissions and can read that user's files and use the
  network. There is no seccomp, no namespaces and no container.
- Process cleanup is best effort. A task that leaves its process group **and**
  changes directory (and on Linux also clears its environment) can outlive the
  sandbox.
- Memory is not capped by the kernel. On macOS, cores are not pinned (no
  `taskset`), so priority is the only CPU control.
- The GPU cap applies only where PyTorch is already installed, only for the
  `python` runtime and only to torch allocations.
- Windows support is written but unverified.
- There is no database migration tool beyond the additive column list.
- A task runs for at most `max_task_timeout_seconds` (1 hour) and ends if the
  master restarts while it runs. Tasks of a project are independent: there is
  no multi-machine launcher (`torchrun`, DeepSpeed) yet.
- Project environments need `python3 -m venv` (or uv) on the worker, and
  network access to the package index from the worker.

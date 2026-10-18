# API & Interface Contract

Single source of truth for the interfaces between backend, frontend, the VS Code
extension gateway, and the internal ssh-executor modules. Code that implements
these: `services/ssh-executor/schemas.py` (request/response shapes),
`services/ssh-executor/models.py` (persistence), `frontend/src/api/ssh.ts`.

All paths below are as seen by the browser through the api-gateway
(`http://<gateway>:8000`). The gateway forwards `/api/v1/ssh/*` to ssh-executor
and `/api/v1/storage/*` to storage-service unchanged. Every endpoint needs
`Authorization: Bearer <jwt>` (anonymous `dev-user` is allowed only when
`ENVIRONMENT != production`). Every resource is scoped to the caller
(`owner_id = jwt.sub`). Errors are HTTP status + JSON body from FastAPI; the UI
shows `detail` (or `message`) as text.

---

## 1. Nodes (workers)

### 1.1 Onboarding — the user only types host, port, username, password

| Step | Request | Response |
|---|---|---|
| 1 Connect | `POST /api/v1/ssh/nodes/connect` `NodeConnectRequest {host, port=22, username}` | `NodeConnectResponse {host, port, username, host_key_type, fingerprint, onboarding_token, already_registered}` |
| 2 Provision | `POST /api/v1/ssh/nodes/provision` `NodeProvisionRequest {onboarding_token, password}` | `NodeProvisionResponse {provision_id, host, port, username, fingerprint, expires_at, os_type, os_info, detected_cpu, detected_memory_gb, detected_gpu, gpu_*, python_version, runtimes, resource_enforcement, execution_backend}` |
| 2b Manual fallback | `POST /api/v1/ssh/nodes/provision/manual` `{onboarding_token}` | `NodeManualProvisionResponse {provision_id, public_key, install_command_posix, install_command_windows, expires_at}` |
| 2c Verify manual | `POST /api/v1/ssh/nodes/provision/{provision_id}/verify` (no body) | `NodeProvisionResponse` |
| 3 Register | `POST /api/v1/ssh/nodes` `NodeRegisterRequest {provision_id, name, allocated_cpu, allocated_memory_gb, allocated_gpu_count=0, allocated_gpu_memory_gb=0}` | `201 NodeResponse` |

Semantics:
- Step 1 sends **no credential**. The server reads the host key only and
  returns its SHA256 fingerprint for the user to confirm, plus a signed token
  (TTL `onboarding_token_ttl_seconds`) binding host, port, username, host key
  and owner.
- Step 2 logs in with the password **over the pinned host key**, requires
  `python3` on the node (400 with a clear message otherwise), generates an
  Ed25519 key pair on the master, appends
  `restrict <pubkey> dp-platform:<provision_id>` to the node's authorized keys
  (correct file for Windows administrators), reconnects with the key only,
  detects capabilities and runtimes, and stores the encrypted private key in a
  `pending_provisions` row. The password is never stored or logged.
  - Wrong password → 401 `detail: "Authentication failed"`.
  - Server does not allow password login → 409 `detail` containing
    `password_auth_disabled`; the UI then offers step 2b.
  - Host key changed since step 1 → 409 `detail` containing `host_key_mismatch`.
- Step 3 turns the pending row into an `SSHNode` (`auth_type="managed_key"`),
  clamping the allocation to the detected values. The `ssh_nodes.id` equals the
  `provision_id`, so the authorized_keys comment `dp-platform:<id>` identifies
  the node.

### 1.2 Lifecycle

| Request | Response | Notes |
|---|---|---|
| `GET /api/v1/ssh/nodes` | `NodeListResponse {items: NodeResponse[], total}` | |
| `GET /api/v1/ssh/nodes/{id}` | `NodeResponse` | |
| `PATCH /api/v1/ssh/nodes/{id}` `NodeUpdateRequest {name?, status?, allocated_cpu?, allocated_memory_gb?, allocated_gpu_count?, allocated_gpu_memory_gb?, env_cache_gb?}` | `NodeResponse` | rename / change allocation (≤ detected, GPU included); `status` `draining` keeps running work but gives the node no new tasks or training, `online` resumes; `env_cache_gb` (0–10000) lends disk for cached project environments, 0 turns the cache off |
| `DELETE /api/v1/ssh/nodes/{id}` | `NodeDeleteResponse {deleted, key_removed, message}` | removes `~/.cache/distributed-compute` (project environments) and our authorized_keys line, then the row |
| `POST /api/v1/ssh/nodes/{id}/health-check` | `NodeResponse` | key login + `echo` |
| `POST /api/v1/ssh/nodes/{id}/refresh` | `NodeResponse` | re-detect capabilities + runtimes |
| `POST /api/v1/ssh/nodes/{id}/rotate-key` | `NodeResponse` | install new key, verify, remove old line |
| `POST /api/v1/ssh/nodes/{id}/resecure` `NodeResecureRequest {password?}` | `NodeResponse` | legacy password/private_key node → managed key; wipes the old secret |
| `GET /api/v1/ssh/nodes/{id}/capacity` | `{node_id, name, status, cpu_total, cpu_reserved, cpu_available, memory_total_gb, memory_reserved_gb, memory_available_gb, ...}` | |

`NodeResponse` = capability fields + `{id, owner_id, name, host, port, username,
auth_type, fingerprint, key_created_at, allocated_cpu, allocated_memory_gb,
allocated_gpu_count, allocated_gpu_memory_gb, allocated_gpu_fraction,
env_cache_gb, status, health_status, last_health_check, created_at,
updated_at}`.
`auth_type != "managed_key"` means a legacy node; the UI shows a "Re-secure"
action. `runtimes` is `{runtime_id: version}`. Capability fields include
`gpu_runtime` (`"torch-cuda" | "torch-mps" | null`): PyTorch with a GPU
backend that is **already** on the worker (detected, never installed).

#### GPU slice (per node)
* `allocated_gpu_count` (int, default 0): devices `0..count-1` are exposed.
* `allocated_gpu_memory_gb` (float, default 0): cap **per device**.
* `allocated_gpu_fraction` (read-only) = `allocated_gpu_memory_gb / gpu_memory_gb`
  clamped to 0..1; 0 when there is no GPU, no slice, or unknown device memory.
* Both are clamped to the detected `gpu_count` / `gpu_memory_gb` on register,
  update, refresh and re-secure. A memory slice with count 0 implies 1 device;
  memory 0 means no GPU.
* Enforcement only exists where PyTorch is already installed
  (`gpu_runtime` set) and only for the `python` runtime: the task gets
  `CUDA_VISIBLE_DEVICES`, `DP_GPU_FRACTION`, `DP_GPU_MEMORY_GB`,
  `PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:128`, and is launched as
  `python3 -I -B dp_gpu_preamble.py task.py`; the preamble calls
  `torch.cuda.set_per_process_memory_fraction(f, device=i)` (or the MPS
  equivalent) if `torch` imports, then runs `task.py` via `runpy` as
  `__main__`. (`-I` ignores `PYTHONPATH`/`sitecustomize`, hence the wrapper.)
  This is PyTorch's caching-allocator limit: it binds torch allocations only,
  and code that calls the setter again can raise it — best effort.
* Every other case (no slice, no torch, non-python runtime) gets
  `CUDA_VISIBLE_DEVICES=""`.
* A slot's fraction is the node fraction × `cpu_per_task / allocated_cpu`, so
  all concurrent slots on a node (leases bound their cores) stay within the
  node's slice. Leases record `gpu_allocated = allocated_gpu_count`.

#### Always applied to POSIX sandbox tasks
`nice -n <policy>` around the whole command (`/bin/sh -c`), `taskset -c` to the
slot's cores where available, and `OMP_NUM_THREADS`, `MKL_NUM_THREADS`,
`OPENBLAS_NUM_THREADS`, `NUMEXPR_NUM_THREADS` = the slot's cores. Windows is
unchanged (no thread/GPU env yet).

#### Execution policy (`execution_policy` on batches and training)
`safe_background` (default; `nice` = `task_nice`, 19) | `balanced` (`nice 10`)
| `max_available` (`nice 0`). Only priority changes; cores, threads and GPU
memory never exceed the allocated slice under any policy.

There are no public lease endpoints. Leases are internal (§4).

---

## 2. Task Executor — any language present on the worker

### 2.1 Runtimes
`GET /api/v1/ssh/runtimes` → `RuntimeListResponse {items: RuntimeInfo[]}`

`RuntimeInfo {id, name, filename, kind, example_code, windows_supported, available_node_ids}`

The registry lives in `services/ssh-executor/runtimes.py`. It must include at
least these ids:

`python`, `bash`, `sh`, `powershell`, `node` (JavaScript), `deno`
(TypeScript/JavaScript), `bun`, `ruby`, `perl`, `php`, `lua`, `r` (Rscript),
`julia`, `go` (`go run`), `java` (single-file source launch, JDK 11+),
`kotlin` (kotlinc -script), `scala`, `c` (cc/gcc/clang), `cpp`
(c++/g++/clang++), `rust` (rustc), `swift`, `dart`, `haskell` (runghc),
`csharp` (dotnet-script if present), `elixir`, `erlang` (escript), `ocaml`,
`tcl`, `awk`, and `custom`.

`custom` lets the user run **any** language: they give the source filename
(e.g. `main.zig`) and a command template containing `{file}` (e.g.
`zig run {file}`). The command runs inside the sandbox with the same
confinement as built-in runtimes.

Nothing is ever installed on a worker. A runtime is offered for a node only if
it was detected there (`NodeResponse.runtimes`); `available_node_ids` lists the
caller's online nodes that have it. Compiled languages compile **inside the
sandbox** (build caches go to the sandbox because `HOME` points there), and the
sandbox is deleted afterwards.

### 2.2 Batches
| Request | Response |
|---|---|
| `POST /api/v1/ssh/batches` `BatchCreateRequest {code, inputs[], node_ids[], runtime, custom_command?, custom_filename?, cpu_per_task=1, timeout_seconds?, execution_policy="safe_background", project?}` | `201 BatchResponse` |
| `GET /api/v1/ssh/batches` | `BatchListResponse {items}` (latest 20) |
| `GET /api/v1/ssh/batches/{id}` | `BatchResponse` |
| `GET /api/v1/ssh/batches/{id}/tasks` | `BatchTasksResponse {batch, tasks[]}` |
| `POST /api/v1/ssh/batches/{id}/cancel` | `BatchResponse` (status `cancelling` → `cancelled`) |
| `POST /api/v1/ssh/batches/{id}/retry[?failed_only=true]` | `201 BatchResponse`: a new batch with the same code (or project) and settings; every input, or only those whose task failed or was cancelled. 409 while the batch is still running; 400 when `failed_only` finds nothing to retry |
| `GET /api/v1/ssh/batches/{id}/tasks/{task_id}/outputs` | `200 application/gzip`, `Content-Disposition: attachment; filename="outputs-<batch8>-task<N>.tar.gz"`: what a project task wrote to its output folders. 404 when the batch is not the caller's or the task brought back no files |

Each input becomes one task. The task's code runs with that input on **stdin**
(and also available as file `input.txt` in the sandbox). Validation (400):
unknown or disallowed runtime; `custom` without `{file}` in `custom_command` or
with a `custom_filename` that fails `^[A-Za-z0-9._-]{1,128}$`; empty code; code
or input limits; `timeout_seconds > max_task_timeout_seconds`; no selected
online node has the runtime (400 naming the runtime).

`TaskResponse` = `{id, batch_id, seq, node_id, status, exit_code, stdout,
stderr, attempts, started_at, finished_at, duration_ms, output_files,
output_bytes}`; `output_files`/`output_bytes` are set for project tasks.
`BatchResponse` carries `project` (the `ProjectSpec` below) for project runs.

### 2.3 Projects (new in 2.0)

A project runs a folder instead of one file. Upload it once, then start a
batch with `project` set; `runtime` becomes `project` and `code` is ignored.

| Request | Response |
|---|---|
| `POST /api/v1/ssh/bundles` (raw body: a `.tar.gz`, `Content-Type: application/gzip`) | `201 BundleResponse {id, size_bytes, files, unpacked_bytes}`; `id` is the archive's sha256. 400 not a gzip tar, unsafe path (absolute, `..`), a link or device, or empty; 413 over `max_bundle_bytes` (200 MB), `max_bundle_unpacked_bytes` (2 GB) or `max_bundle_files` (50,000) |
| `GET /api/v1/ssh/bundles/{sha256}` | `200 BundleResponse` when the caller already uploaded it, else 404. Bundles are stored per owner: another user's hash is always 404 |

`ProjectSpec {bundle_id, command, requirements?, outputs=["outputs"],
datasets=[{dataset_id, path?}]}`:

- `command` (≤ 4096 characters) runs with `/bin/sh` inside the unpacked folder.
- `requirements`: a pip requirements file in the folder. The worker builds a
  virtual environment from it (`uv` when installed, else `python3 -m venv` +
  pip) and puts its `bin/` first on `PATH`. It is built in the sandbox and
  deleted, unless the node has `env_cache_gb > 0`: then it is kept under
  `~/.cache/distributed-compute/envs/<hash of the file, Python version,
  platform>` and reused; least recently used environments are removed to stay
  within the cache size.
- `outputs`: folders (or files) brought back after the command, whatever its
  exit code, including after a timeout; at most `max_project_output_bytes`
  (500 MB) per task, larger files are left out and named in the task's stderr.
- `datasets`: copied into the folder before the command (default path
  `data/<name>.<format>`), read through storage-service as the batch's owner.
- Paths must be relative and stay inside the folder (400 otherwise).

The command sees `DC_PROJECT_DIR`, `DC_OUTPUT_DIR` (first output folder),
`DC_DATA_DIR`, `DC_INPUT_FILE`, `DC_TASK_INDEX` (0-based), `DC_TASK_COUNT`,
and `VIRTUAL_ENV` when an environment was built, plus the usual sandbox
variables. The task's input is also on stdin.

A failed unpack or environment build fails that task with exit code ≠ 0 and
`[dc] Building the environment failed: …` followed by pip's output in
`stderr`; it is not retried on another node. Validation (400): unknown
`bundle_id` for this owner; no selected online **Linux or macOS** node with
`python3` (Windows workers do not run projects yet); input or timeout limits.

Through the extension gateway (port 8090, used by VS Code and `dc`):
`POST /bundles`, `GET /bundles/{id}`, `POST /executions` with
`project {bundleId, command, requirements?, outputs?, datasets?: [{datasetId, path?}]}`
(camelCase; `code` may then be empty), and
`GET /executions/{id}/tasks/{taskId}/outputs`. `PATCH /nodes/{id}` accepts
`envCacheGb`, and nodes report it.

Batch statuses: `pending | running | cancelling | cancelled | completed |
completed_with_errors | failed`. Task statuses: `pending | running |
completed | failed | cancelled`.

---

## 3. Training

| Route | Purpose |
|---|---|
| `GET /api/v1/ssh/engines[?refresh=true]` | `{items: [{id, name, available, missing[], versions{spark,ray,java}, spark_master, ray_address, hint}]}` for `python` and `spark_ray`. Detection only. |
| `GET /api/v1/ssh/engines/spark_ray/models` | Every supported Spark MLlib estimator: `{items: [{id: "spark:<Name>", name, family, needs_target, params: [{name, type, default, has_default, required, doc}]}]}`. Starts Spark; 409 when Spark + Ray is not installed. |
| `GET /api/v1/ssh/models/custom/template` | `{filename: "model.py", code}`: a starter custom model. |
| `POST /api/v1/ssh/train` | Start a run (`TrainRequest`, below). 400 for anything that cannot run, before it is stored. |
| `GET /api/v1/ssh/train`, `GET /api/v1/ssh/train/{id}` | Runs (`TrainRunResponse`), the owner's latest 20. |
| `GET /api/v1/ssh/train/{id}/model` | `model-<id>.onnx` (built-in), `params-<id>.json` (custom) or `spark-model-<id>.zip` (Spark `PipelineModel`). 409 until completed. |
| `GET /api/v1/ssh/train/{id}/data` | The numeric rows the run trained on plus its manifest (JSON). |
| `DELETE /api/v1/ssh/train/{id}` | Remove a run. |

`TrainRequest`:

| Field | Notes |
|---|---|
| `dataset_id` | storage-service dataset |
| `model_type` | `linear_regression` \| `logistic_regression` \| `cnn` \| `custom` \| `spark:<Estimator>` |
| `engine` | `python` (default) \| `spark_ray` (400 with an install hint when missing) |
| `target_column` | required except for Spark clustering models |
| `feature_columns` | empty = every numeric column but the target |
| `mode` | `multi` (workers) \| `single` (master cores, built-in only). Ignored for Spark models. |
| `local_cores`, `learning_rate`, `epochs`, `rounds`, `num_classes`, `node_ids`, `execution_policy` | as before |
| `model_code` | `custom` only: the model.py source (≤ `max_code_bytes`; must parse and define `train`) |
| `model_params` | `custom`: merged into the config passed to the model. `spark:*`: estimator parameters, validated against the estimator. |
| `param_grid` | `spark:*` only: `{param: [values]}`; the best on a validation split is kept |
| `test_fraction` | `spark:*`: held-out share for test metrics (0.05–0.5, default 0.2) |
| `uses_gpu` | `custom`: GPU nodes get shares by GPU memory as well as cores |
| `seed` | sampling and splits |

`TrainRunResponse` adds `model_family` (`builtin|custom|spark`),
`model_format` (`onnx|json|spark-zip`) and `details`: `engine`, `data`
(`loader`, `rows_total`, `rows_used`, `sampled`, `partitions`), `ray`
(parameter-server `version` and per-round `updates`), `custom`
(`has_init`, `has_evaluate`) and `spark` (model, rows, params, feature
importances, coefficients, labels, grid search). Failed custom shards carry
`error` (the worker's error output) in `shards[]`.

Workers receive training work through the same sandboxed executor as batches
(runtime `python`): the built-in stdlib script, or the custom runner plus the
user's `model.py`. Shards are sized by **compute score**
`P = gpu_mem_gb × shard_weight_gpu + cores × shard_weight_cpu` (defaults
5.0 / 1.0), largest-remainder rounding, interleaved rows; the GPU term counts
only for jobs that use the GPU. FedAvg weights by shard size.

---

## 4. Internal interfaces (ssh-executor modules)

| Provider (owner) | Interface | Consumers |
|---|---|---|
| `node_service.py` | `transport_for_node(node: SSHNode) -> SSHTransport` (not connected; key auth for managed nodes, legacy secret otherwise, host key pinned) | dispatcher, training, recovery |
| `capabilities.py` | `detect_capabilities(transport, adapter, timeout=20) -> NodeCapabilities` with fields `os_type, os_version, cpu_total, memory_total_gb, gpu_vendor, gpu_backend, gpu_model, gpu_count, gpu_memory_gb, gpu_runtime: Optional[str], python_version, runtimes: dict[str,str], env_path: Optional[str], execution_backend, resource_enforcement` (no java/spark/ray fields) | node_service |
| `task_executor.py` | `detect_os(transport, timeout=15) -> str`, `select_adapter(os_type) -> OSAdapter` (signatures stable) | node_service |
| `tasks.py` | `TaskExecutor(transport, adapter, ...).run(runtime_id, code, input_data, spec, *, env_path, custom_command=None, custom_filename=None, execution_id=None, extra_files=None, project: ProjectRun = None, task_index=0, task_count=1) -> dict{status, exit_code, stdout, stderr, execution_id, started_at, finished_at, duration_ms, output_files?, output_bytes?}`; `cancel(execution_id)`; `sweep_stale_sandboxes(older_than_minutes)` | dispatcher, training |
| `projects.py` | `BundleStore`, `OutputStore`, `ProjectRun`, `HELPER_SOURCE` (the worker-side `dc_project.py`: `prepare`, `env`, `pack`, standard library only) | routes, dispatcher |
| `runtimes.py` | `list_runtimes() -> list[Runtime]`, `get_runtime(id) -> Runtime \| None` | routes_batches, capabilities, validation |
| `lease_service.py` | `reserve(...)` is atomic (`SELECT … FOR UPDATE` on the node row) | dispatcher, training |
| `lease_recovery.py` | `start_recovery_loop()`, `stop_recovery_loop(task)` | main.py |
| `security.py` | `enforce_secrets(settings)` | main.py |

Authorized-keys line format: `restrict ssh-ed25519 AAAA… dp-platform:<uuid>`.

## 5. Storage service ownership

Datasets are owner-scoped (`created_by == jwt.sub`) for get, list, download and
delete. Internal calls from ssh-executor use `create_internal_token("ssh-executor")`
(claim `internal: true`) **and** header `X-On-Behalf-Of: <owner_id>`. storage-service
honours that header only when the token has `internal: true`, and then scopes to
that owner.

Each dataset carries `storage_backend` (`local`, or `hdfs` for datasets stored
in HDFS, including every dataset from before 2.0.0) and `storage_path` (the
file's key in that store; `hdfs_path` holds the same value and is kept for
older clients). Uploads and downloads are streamed. Storage errors:

| Status | `error_code` | When |
|---|---|---|
| 413 | `FILE_TOO_LARGE` | The upload is over `MAX_UPLOAD_SIZE_BYTES` (default 500 MB) |
| 503 | `STORAGE_UNAVAILABLE` | The dataset is in HDFS and HDFS is not running; the message says to run `make up-hdfs` or `make migrate-datasets` |
| 507 | `INSUFFICIENT_STORAGE` | Less than `MIN_FREE_DISK_BYTES` (default 512 MiB) would be left free |

A training run on a dataset that cannot be read fails with that message. The
extension gateway passes 413, 503 and 507 through with the message, as
`too_large`, `unavailable` and `insufficient_storage`.

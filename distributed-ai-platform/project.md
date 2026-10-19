# Distributed Data Processing & Task Execution Platform

> Borrow spare CPU and GPU from your own machines over plain SSH. Run your
> own projects (a folder with its `requirements.txt`) or code in any language
> on them in throwaway sandboxes, and train models across them. Nothing is
> installed on the machines that lend the compute, unless their owner lends an
> environment cache.

Details: [docs/architecture.md](docs/architecture.md) ·
[docs/connections.md](docs/connections.md) ·
[docs/API_CONTRACT.md](docs/API_CONTRACT.md).

---

## 1. Overview

A **master** (Docker Compose) manages **workers**, which are any machines you
can reach over SSH that have `python3`. There are four user-facing features:

1. **Add a worker with a password, once.** Enter host, port, username and
   password, and confirm the host-key fingerprint. The master generates an
   Ed25519 key, installs it as a `restrict`ed authorized key, verifies it, and
   forgets the password. You then choose how many cores, how much memory and
   optionally how much GPU memory the worker lends.
2. **Task Executor.** Run code in any language the worker already has
   (Python, Bash, Node.js, Go, Rust, Java, C/C++, Ruby, R, Julia, … or a
   `custom` command) as a batch, one task per input, in parallel across
   workers. Each task runs in its own sandbox directory, which is deleted
   afterwards. Tasks can be cancelled.
3. **Projects** (1.0.1). Send a whole folder (modules, configs,
   `requirements.txt`) and a command. Each worker builds the Python
   environment (in the sandbox, or cached when the owner lends disk), runs
   the command once per input, and the files each task writes to `outputs/`
   come back for download. From VS Code, the dashboard or the `dc` CLI.
4. **Training.** Built-in linear regression, logistic regression or a small
   CNN, **your own model** (a `model.py` with `train()`), or with the optional
   Spark + Ray engine **any Spark MLlib model**. Built-in and custom models use
   federated averaging: on the master's cores (`single`) or on workers
   (`multi`), with shards sized by each worker's compute. Spark models train
   with Spark on the master. Pure Python is the default; Spark + Ray is
   installed on the master only when you ask for it, and never on workers.

A VS Code extension, through a small Go gateway, shows the same nodes and
executions, submits the active file, and runs and tracks training jobs.

## 2. Objectives

- **Zero install on workers**: only `sshd` + `python3` are required, and nothing
  is left behind except one authorized-keys line.
- **Respect the owner**: the lent slice is a hard ceiling for cores, threads
  and the GPU slice. Background priority is the default.
- **Safe onboarding**: the password is used once, the host key is pinned, keys
  are managed, rotatable and removable.
- **Honest limits**: unknown stays unknown, and unverified paths are labelled.

## 3. Scope

### Implemented
- Password-only onboarding with managed keys, plus a manual-key fallback when
  the worker refuses password login. Rotate key, re-secure legacy nodes,
  delete (removes the key line).
- Capability detection: OS, CPU, memory, GPU model/count/memory, PyTorch GPU
  backend if already present, and 29 language runtimes plus `custom`.
- Sandboxed Task Executor with a timeout, cancel by execution id, process-group
  cleanup (plus a best-effort sweep of escaped processes) and a stale-sandbox
  sweep.
- Atomic leases, per-slot core pinning (Linux `taskset`), thread caps, GPU
  slicing, execution policies (`safe_background`, `balanced`,
  `max_available`), and a recovery loop.
- Projects: bundles stored once per owner and hash, environment builds with
  uv or venv + pip, opt-in per-node environment cache with LRU eviction,
  datasets copied in, output folders brought back (also after failure or
  timeout), the `dc` command line, and example projects (`examples/`).
- Pure-Python distributed training with compute-weighted shards and ONNX
  export.
- Owner-scoped datasets (local files + Postgres; HDFS optional), CSV/JSON and
  image-ZIP upload, streamed, with `make migrate-datasets` and `make backup`.
- React UI (`/ssh`, `/execute`, `/train`). VS Code extension plus Go gateway.

### Not provided / limits
- The sandbox isolates files and processes, **not the kernel**. There are no
  namespaces, seccomp or network isolation, and no kernel memory cap.
- Windows workers: code paths exist, but they are **not verified on a real
  Windows host**.
- Not yet tested against physical remote machines. Tests use mocks, a local
  transport on the development host, and optional Docker SSH providers.
- GPU memory caps apply only where PyTorch is already installed, and only for
  Python tasks.
- There is no `/login` endpoint. Outside production, anonymous requests run as
  `dev-user` and the VS Code extension signs in automatically. In production,
  mint tokens with `generate_token.py`.
- There is no migration tool. The schema comes from `create_all` plus a list of
  additive `ADD COLUMN IF NOT EXISTS` statements.

## 4. Architecture

```
Browser (React :3000) ──▶ api-gateway :8000 ──▶ storage-service :8001 ──▶ /data volume, Postgres
                                         └────▶ ssh-executor   :8011 ──▶ Postgres
VS Code extension ──▶ extension-gateway :8090 (Go) ──▶ ssh-executor :8011
ssh-executor ══SSH (managed key, pinned host key)══▶ workers: sshd + python3
```

| Service | Port | Responsibility |
|---|---|---|
| api-gateway | 8000 | Proxies `/api/v1/storage/*` and `/api/v1/ssh/*` |
| storage-service | 8001 | Datasets, owner-scoped; local files, or HDFS via WebHDFS |
| ssh-executor | 8011 | Workers, runtimes, batches, training, leases |
| extension-gateway | 8090 | Extension REST API that forwards to ssh-executor with the user's JWT |

Removed for good: Spark, Livy, Ray, MLflow, Kafka, Prometheus/Grafana,
preprocessing/training/evaluation/registry/serving services, worker registry,
resource manager, scheduler and the `worker/` agent.

## 5. Technology stack

| Layer | Technology |
|---|---|
| Services | Python 3.11, FastAPI, SQLAlchemy (async), asyncpg, pydantic v2, structlog |
| SSH | paramiko; Ed25519 keys via `cryptography`; Fernet at rest |
| Training | Pure Python on the master, with numpy for ONNX export; stdlib-only scripts on workers |
| Storage | PostgreSQL 15, files in a Docker volume; HDFS 3 (WebHDFS) optional |
| Frontend | React 18, TypeScript, Vite, Tailwind |
| Editor | VS Code extension (TypeScript, esbuild, vitest), Go (`go.mod`: 1.27) `net/http` gateway |
| Packaging | Docker Compose; GitHub Actions CI file (see §8) |

## 6. How a task runs

```
lease the node → mktemp -d /tmp/dp-exec.<exec_id>.XXXXXX
→ upload code, input.txt, dp_runner.py
→ python3 dp_runner.py: clean env, own process group, taskset + nice, timeout
→ stdout / stderr / exit code → kill the group (and escaped processes) → rm -rf sandbox
```
A runtime is offered only where it was detected. If none of the selected
online nodes has it, `POST /batches` returns 400 naming it. Compiled languages
build inside the sandbox, so build caches are deleted with it.

## 7. Configuration

`config/.env` (from `config/.env.example`): `ENVIRONMENT`, `JWT_SECRET`,
`SSH_CRED_KEY`, `DATABASE_URL`; storage: `STORAGE_BACKEND` (`local` or `hdfs`),
`MAX_UPLOAD_SIZE_BYTES`, `MIN_FREE_DISK_BYTES`, `HDFS_WEBHDFS_URL`, `HDFS_USER`. Outside
`development`, services refuse to start with placeholder or short (< 32 chars)
secrets. The ssh-executor tunables are in `services/ssh-executor/config.py`:
task timeouts, output and code caps, `allowed_runtimes`,
`allowed_hosts`/`allowed_cidrs`, lease TTL, recovery interval, stale sandbox
age, shard weights and `max_train_rows`.

## 8. Running and testing

```bash
# make up creates config/.env from config/.env.example on first use;
# set real secrets there for non-local use.
make up            # or: make up-dev   (adds two throwaway SSH providers)
                   # dashboard: http://localhost:3001
python generate_token.py --sub alice    # token for production mode / REST clients

pytest tests -q                         # Python unit tests
make lint                               # ruff (E9,F) + mypy shared/
cd frontend && npm run build
cd distributed-compute-vscode && npm test
pip install ./clients/cli && dc --help         # command-line client
cd services/extension-gateway && go test ./...
```

CI: `.github/workflows/ci.yml` runs lint, the Python tests, the frontend build
and the Go tests. It sits inside the project folder and must be moved to the
git root's `.github/workflows/` to run. The CD workflows are placeholders.

## 9. Repository layout

```
distributed-ai-platform/
├── services/
│   ├── api-gateway/          # proxy (8000)
│   ├── storage-service/      # datasets (8001)
│   ├── ssh-executor/         # workers, sandbox executor, batches, training (8011)
│   └── extension-gateway/    # Go gateway for VS Code (8090)
├── shared/                   # auth, config, logging, metrics, DB manager
├── clients/cli/              # dc, the command-line client (stdlib only)
├── examples/                 # hello-project, sklearn-sweep, pytorch-train
├── frontend/                 # React UI
├── distributed-compute-vscode/
├── infrastructure/docker/    # postgres init, hadoop config (optional HDFS)
├── tests/unit/services/      # pytest suites
├── tests/load/locustfile.py  # load test for /api/v1/ssh/batches
├── docs/                     # architecture, connections, API contract, plan/status
└── docker-compose.yml, Makefile, generate_token.py
```
